"""
iris v1 봇 — 세대별 리밸런싱 판단 로직 단위테스트.
실행: python -m pytest tests/test_iris_v1_tranching.py -v
     또는 python tests/test_iris_v1_tranching.py

실제 프로덕션 DB(iris.db)를 건드리지 않도록 매 테스트마다 임시 SQLite 파일로
core.db.DB_PATH를 패치한다. KIS API 호출(가격조회/주문/잔고)은 전부 모킹.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timedelta


def _dates_back(n, from_date="20260710"):
    """from_date 기준 n일 전부터 매일(주말포함 단순화) 날짜 문자열 리스트, 오름차순."""
    base = datetime.strptime(from_date, "%Y%m%d")
    return [(base - timedelta(days=n - i)).strftime("%Y%m%d") for i in range(n + 1)]


class IrisV1TestBase(unittest.TestCase):
    """임시 DB로 격리된 테스트베이스. 서브클래스가 setUp에서 super() 호출."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp_db = Path(self._tmpdir.name) / "test_iris.db"

        import core.db as dbmod
        self._orig_db_path = dbmod.DB_PATH
        dbmod.DB_PATH = tmp_db

        import bot.iris_v1_bot as bot
        self.bot = bot
        bot.init_db()

        # 신규(미마이그레이션) DB의 candidates 테이블은 core/db.py init_db()의
        # 최초 CREATE문에 marcap_bil/rank_no가 빠져있어(마이그레이션 블록은 기존
        # DB의 strategy컬럼 부재 케이스만 감지 — 신규생성 DB는 대상이 아님) 실제
        # 운영 DB(오래전부터 누적 마이그레이션됨)와 다르다. 테스트 DB에서만 보정.
        import core.db as dbmod
        with dbmod.get_conn() as conn:
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(candidates)").fetchall()]
            for col in ("marcap_bil", "rank_no", "skip_reason"):
                if col not in cols:
                    coltype = "TEXT DEFAULT ''" if col == "skip_reason" else "REAL DEFAULT 0"
                    conn.execute(f"ALTER TABLE candidates ADD COLUMN {col} {coltype}")

        # 3세대 sleeve_cash 시딩(균등분배 가정, 세대당 100만원 — 테스트용 소액)
        from core.timeutil import iso_kst
        with dbmod.get_conn() as conn:
            for gen in range(3):
                conn.execute(
                    "INSERT INTO sleeve_cash(strategy, account, cash, updated_at) VALUES (?, 'KIS', ?, ?)",
                    (f"iris_v1_gen{gen}", 1_000_000.0, iso_kst()),
                )

    def tearDown(self):
        import core.db as dbmod
        dbmod.DB_PATH = self._orig_db_path
        self._tmpdir.cleanup()

    def _seed_price_daily(self, ticker: str, closes: list, dates: list):
        import core.db as dbmod
        with dbmod.get_conn() as conn:
            for d, c in zip(dates, closes):
                conn.execute(
                    "INSERT OR REPLACE INTO price_daily(ticker, date, open, high, low, close, volume) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (ticker, d, c, c, c, c, 1000),
                )

    def _seed_candidates(self, trade_date: str, strategy: str, rows: list):
        """rows: [{'ticker','name','weight','rank_no'}]"""
        import core.db as dbmod
        with dbmod.get_conn() as conn:
            for r in rows:
                conn.execute("""
                    INSERT INTO candidates
                      (trade_date, ticker, name, sector, score, total_amt, val_ratio, avg_val,
                       marcap_bil, rank_no, strategy, strategy_version)
                    VALUES (?,?,?,?,?,0,0,0,0,?,?,'test')
                """, (trade_date, r["ticker"], r["name"], "", r["weight"], r["rank_no"], strategy))


class TestGateOn(IrisV1TestBase):
    """_gate_on(): KODEX200 100일선 상/하회 판단."""

    def test_gate_on_when_above_sma(self):
        dates = _dates_back(105)
        closes = [10000] * 100 + [20000] * 6  # 최근 급등 → 오늘(미포함) 이전 종가는 상승세
        self._seed_price_daily("069500", closes, dates)
        with patch.object(self.bot, "_today", return_value="20260710"):
            self.assertTrue(self.bot._gate_on())

    def test_gate_off_when_below_sma(self):
        dates = _dates_back(105)
        closes = [20000] * 100 + [10000] * 6  # 최근 급락 → 100일선 아래
        self._seed_price_daily("069500", closes, dates)
        with patch.object(self.bot, "_today", return_value="20260710"):
            self.assertFalse(self.bot._gate_on())

    def test_gate_defaults_on_insufficient_data(self):
        """데이터 부족(100일 미만) → 안전기본값 True"""
        dates = _dates_back(10)
        closes = [10000] * 11
        self._seed_price_daily("069500", closes, dates)
        with patch.object(self.bot, "_today", return_value="20260710"):
            self.assertTrue(self.bot._gate_on())

    def test_gate_excludes_today(self):
        """오늘 날짜 종가가 있어도 게이트 계산에 안 쓰임(lookahead 방지) —
        오늘자에 극단값을 넣어도 결과가 안 바뀌어야 함."""
        dates = _dates_back(100)  # ...~20260710 (오늘 포함)
        closes = [10000] * 100 + [999999]  # 마지막(오늘)에 극단값
        self._seed_price_daily("069500", closes, dates)
        with patch.object(self.bot, "_today", return_value="20260710"):
            # 오늘 제외 100일 전부 10000이면 상수선 → 마지막(어제) 종가도 10000 == sma100 → False(> 아님)
            self.assertFalse(self.bot._gate_on())


class TestProcessGeneration(IrisV1TestBase):
    """_process_generation(): 세대별 4가지 분기(정규리밸/무동작/게이트오프/캐치업)."""

    def setUp(self):
        super().setUp()

        # 2026-07-11: _verify_buy_fill()이 실제로 get_balance()의 보유수량을 확인하게
        # 되면서, 정적(항상 빈 holdings) mock으로는 매수 체결이 영원히 확인되지 않아
        # 매 매수마다 폴링타임아웃(10초)을 전부 소진해버린다. buy_market/sell_market/
        # get_balance를 하나의 인메모리 상태로 엮은 stateful mock으로 교체해 "주문하면
        # 잔고에 즉시 반영되는" 정상 체결을 시뮬레이션한다(부분체결/미체결은 개별
        # 테스트에서 이 상태를 직접 조작해 재현).
        self._fake_holdings: dict[str, int] = {}

        def _fake_buy_market(ticker, qty):
            self._fake_holdings[ticker] = self._fake_holdings.get(ticker, 0) + qty
            return {"output": {"ODNO": "TEST123"}}

        def _fake_sell_market(ticker, qty):
            self._fake_holdings.pop(ticker, None)
            return {"output": {"ODNO": "TEST456"}}

        def _fake_get_balance():
            return {
                "cash": 0,
                "holdings": [{"ticker": t, "qty": q} for t, q in self._fake_holdings.items()],
            }

        # 모든 KIS API 호출을 무해한 더미로 모킹 (실제 네트워크/주문 절대 발생 안 함)
        self._patchers = [
            patch("core.kis_api.get_price", return_value={
                "price": 10000, "prev_close": 9900, "open": 10000,
            }),
            patch("core.kis_api.buy_market", side_effect=_fake_buy_market),
            patch("core.kis_api.sell_market", side_effect=_fake_sell_market),
            patch("core.kis_api.get_order_psbl_qty", return_value=999),
            patch("core.kis_api.get_balance", side_effect=_fake_get_balance),
            patch("core.kis_api.get_filled_price", return_value=10000),
            patch.object(self.bot, "_tg"),  # 텔레그램 실전송 방지
            patch.object(self.bot.time, "sleep"),  # 폴링 대기(2/3/5초) 테스트에서 스킵
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_no_action_when_not_due_and_gate_on(self):
        """리밸일 아님 + 게이트 ON → positions 변화 없음"""
        today = "20260710"
        self.bot._process_generation(0, today, gate_on=True)
        self.assertEqual(self.bot._open_positions(0), [])

    def test_regular_rebalance_opens_positions(self):
        """오늘자 iris_v1_gen0 candidates 존재 + 게이트 ON → 신규 진입"""
        today = "20260710"
        self._seed_candidates(today, "iris_v1_gen0", [
            {"ticker": "005930", "name": "삼성전자", "weight": 0.6, "rank_no": 1},
            {"ticker": "000660", "name": "SK하이닉스", "weight": 0.4, "rank_no": 2},
        ])
        self.bot._process_generation(0, today, gate_on=True)
        positions = self.bot._open_positions(0)
        self.assertEqual({p["ticker"] for p in positions}, {"005930", "000660"})
        # 다른 세대(gen1)는 영향 없어야 함
        self.assertEqual(self.bot._open_positions(1), [])

    def test_gate_off_parks_into_bond(self):
        """게이트 OFF → 나스닥100 ETF로 전환 + gateoff_parked 플래그 설정"""
        today = "20260710"
        self.bot._process_generation(0, today, gate_on=False)
        positions = self.bot._open_positions(0)
        self.assertEqual([p["ticker"] for p in positions], ["133690"])
        self.assertTrue(self.bot._is_parked(0))

    def test_gate_off_when_already_parked_is_noop(self):
        """이미 파킹중인데 게이트 계속 OFF → 추가 매매 없음(중복 매수 방지)"""
        today = "20260710"
        self.bot._set_parked(0, True)
        self.bot._open_position(0, "133690", "TIGER 미국나스닥100", 10000, 50)
        self.bot._process_generation(0, today, gate_on=False)
        # 포지션이 정확히 1개(기존 그대로)여야 함 — 추가 매수 안 됨
        self.assertEqual(len(self.bot._open_positions(0)), 1)

    def test_catchup_rebalance_on_gate_recovery(self):
        """파킹중 + 게이트 ON 복귀 → 오늘자 iris_v1_signal로 강제 캐치업, 파킹 해제"""
        today = "20260710"
        self.bot._set_parked(0, True)
        self.bot._open_position(0, "133690", "TIGER 미국나스닥100", 10000, 50)
        self._seed_candidates(today, "iris_v1_signal", [
            {"ticker": "005930", "name": "삼성전자", "weight": 1.0, "rank_no": 1},
        ])
        self.bot._process_generation(0, today, gate_on=True)
        positions = self.bot._open_positions(0)
        self.assertEqual([p["ticker"] for p in positions], ["005930"])
        self.assertFalse(self.bot._is_parked(0))

    def test_catchup_skipped_without_signal_candidates(self):
        """파킹중 + 게이트 ON인데 오늘자 signal 후보가 없으면(동기화 지연 등) 그대로 유지"""
        today = "20260710"
        self.bot._set_parked(0, True)
        self.bot._open_position(0, "133690", "TIGER 미국나스닥100", 10000, 50)
        self.bot._process_generation(0, today, gate_on=True)
        positions = self.bot._open_positions(0)
        self.assertEqual([p["ticker"] for p in positions], ["133690"])
        self.assertTrue(self.bot._is_parked(0))  # 캐치업 실패했으니 파킹 유지


class TestSafetyNets(IrisV1TestBase):
    """2026-07-11 추가: 부분체결 감지, 매수실패 알림, 현금 정합성 체크."""

    def setUp(self):
        super().setUp()
        self._tg_patcher = patch.object(self.bot, "_tg")
        self._tg_mock = self._tg_patcher.start()
        self.addCleanup(self._tg_patcher.stop)
        self._sleep_patcher = patch.object(self.bot.time, "sleep")
        self._sleep_patcher.start()
        self.addCleanup(self._sleep_patcher.stop)

    def test_verify_buy_fill_full(self):
        """잔고에 목표수량 그대로 반영되면 전량체결로 확정, 알림 없음."""
        with patch("core.kis_api.get_balance",
                    return_value={"cash": 0, "holdings": [{"ticker": "005930", "qty": 60}]}):
            actual = self.bot._verify_buy_fill("005930", 60)
        self.assertEqual(actual, 60)
        self._tg_mock.assert_not_called()

    def test_verify_buy_fill_partial(self):
        """잔고 수량이 목표보다 적으면 부분체결로 보고 실제수량만 반환 + 알림."""
        with patch("core.kis_api.get_balance",
                    return_value={"cash": 0, "holdings": [{"ticker": "005930", "qty": 30}]}):
            actual = self.bot._verify_buy_fill("005930", 60)
        self.assertEqual(actual, 30)
        self._tg_mock.assert_called_once()
        self.assertIn("부분체결", self._tg_mock.call_args[0][0])

    def test_verify_buy_fill_none(self):
        """잔고에 아예 없으면(미체결) 0 반환 + 미체결 알림."""
        with patch("core.kis_api.get_balance", return_value={"cash": 0, "holdings": []}):
            actual = self.bot._verify_buy_fill("005930", 60)
        self.assertEqual(actual, 0)
        self._tg_mock.assert_called_once()
        self.assertIn("미체결", self._tg_mock.call_args[0][0])

    def test_buy_safe_failure_sends_telegram(self):
        """매수가 재시도 끝에 전부 실패하면(기존엔 로그만) 텔레그램도 발송해야 함."""
        with patch("core.kis_api.buy_market", side_effect=RuntimeError("주문거부")):
            ok, odno = self.bot._buy_safe("005930", 10)
        self.assertFalse(ok)
        self._tg_mock.assert_called_once()
        self.assertIn("매수 실패", self._tg_mock.call_args[0][0])

    def test_cash_reconcile_ok_when_matching(self):
        """실제 가용현금이 DB sleeve_cash 합계(3세대 × 100만원=300만원)와 거의 같으면 통과."""
        with patch("core.kis_api.get_available_cash", return_value=3_000_000.0):
            self.assertTrue(self.bot._cash_reconcile_ok())
        self._tg_mock.assert_not_called()

    def test_cash_reconcile_ok_when_actual_has_surplus(self):
        """실제 계좌에 iris_v1 몫보다 더 많은 여유자금이 있어도(다른 용도) 정상 — 부족한
        쪽만 위험하다는 설계 그대로."""
        with patch("core.kis_api.get_available_cash", return_value=50_000_000.0):
            self.assertTrue(self.bot._cash_reconcile_ok())
        self._tg_mock.assert_not_called()

    def test_cash_reconcile_fails_on_shortfall(self):
        """실제 가용현금이 DB 합계보다 임계값 이상 부족하면 실패 + CRITICAL 알림."""
        with patch("core.kis_api.get_available_cash", return_value=1_000_000.0):  # 300만원 기대, 100만원뿐
            self.assertFalse(self.bot._cash_reconcile_ok())
        self._tg_mock.assert_called_once()
        self.assertIn("정합성", self._tg_mock.call_args[0][0])

    def test_cash_reconcile_fails_on_api_error(self):
        """실제 잔고 조회 자체가 실패하면 정합성 확인 불가로 안전하게 실패 처리."""
        with patch("core.kis_api.get_available_cash", side_effect=RuntimeError("API 오류")):
            self.assertFalse(self.bot._cash_reconcile_ok())
        self._tg_mock.assert_called_once()


class TestDuplicateRunGuard(IrisV1TestBase):
    """_already_ran_today() / _mark_ran_today(): 당일 중복실행 방지."""

    def test_not_ran_initially(self):
        with patch.object(self.bot, "_today", return_value="20260710"):
            self.assertFalse(self.bot._already_ran_today())

    def test_marks_and_detects(self):
        with patch.object(self.bot, "_today", return_value="20260710"):
            self.bot._mark_ran_today()
            self.assertTrue(self.bot._already_ran_today())

    def test_different_day_not_marked(self):
        with patch.object(self.bot, "_today", return_value="20260710"):
            self.bot._mark_ran_today()
        with patch.object(self.bot, "_today", return_value="20260711"):
            self.assertFalse(self.bot._already_ran_today())


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in [TestGateOn, TestProcessGeneration, TestSafetyNets, TestDuplicateRunGuard]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
