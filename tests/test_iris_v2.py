"""
iris v2 봇 — 후보선정·진입(순위대체)·TP/SL/중간평가/강제청산 판단 로직 단위테스트.
실제 프로덕션 DB를 건드리지 않도록 매 테스트마다 임시 SQLite 파일로 core.db의
DB_PATH를 패치한다. KIS API(시세조회) 전부 모킹 — 실제 네트워크 호출 없음.

실행: python -m pytest tests/test_iris_v2.py -v
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))


class IrisV2TestBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp_db = Path(self._tmpdir.name) / "test_iris_v2.db"

        import core.db as dbmod
        self._orig_db_path = dbmod.DB_PATH
        dbmod.DB_PATH = tmp_db
        dbmod.init_db()

        with dbmod.get_conn() as conn:
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(candidates)").fetchall()]
            for col in ("marcap_bil", "rank_no", "skip_reason", "avg_val"):
                if col not in cols:
                    coltype = "TEXT DEFAULT ''" if col == "skip_reason" else "REAL DEFAULT 0"
                    conn.execute(f"ALTER TABLE candidates ADD COLUMN {col} {coltype}")

        import bot.iris_v2_entry as entry
        import bot.iris_v2_monitor as monitor
        import bot.iris_v2_force_close as force_close
        self.entry = entry
        self.monitor = monitor
        self.force_close = force_close

        # TRADE_MODE을 실제 .env 값과 무관하게 "paper"로 고정(2026-07-23 발견: .env가
        # real로 바뀌자 이 값에 의존하던 기존 paper경로 테스트들이 무더기로 real분기를
        # 타면서 깨졌음 — 테스트는 환경변수 상태와 완전히 격리돼야 함). TestStageBRealMode는
        # 각 테스트에서 개별적으로 "real"로 명시 오버라이드하므로 영향 없음.
        patch.object(entry, "TRADE_MODE", "paper").start()
        patch.object(monitor, "TRADE_MODE", "paper").start()
        patch.object(force_close, "TRADE_MODE", "paper").start()

        # v2.5.4: entry.run()이 dry_run=False일 때마다 core.telegram_commands.
        # process_pending_commands()를 호출하는데, 이건 실제 텔레그램 getUpdates API에
        # 네트워크 요청을 보낸다 — .env의 실제 TELEGRAM_TOKEN이 테스트에도 그대로
        # 로드되므로, 패치 없이 두면 테스트마다 운영 봇에 실제 네트워크 요청이 나감.
        # 기본은 무력화(no-op)하고, 이 로직 자체를 검증하는 테스트는 개별적으로
        # requests.get을 모킹해서 별도 클래스(TestTelegramCommands)에서 다룬다.
        patch("core.telegram_commands.process_pending_commands", lambda: None).start()

        # 2026-08-03 발견: 위와 똑같은 문제가 core.notify.send_telegram에도 있었음 —
        # entry.run(dry_run=False)를 쓰는 TestEntryFallback 등 여러 테스트가 개별적으로
        # send_telegram을 모킹하지 않아서, pytest를 돌릴 때마다 "종목A(000001)" 같은
        # 가짜 테스트 데이터가 담긴 진짜 텔레그램 메시지가 실제 운영봇으로 나가고
        # 있었음(사용자가 실제로 수신 확인). 개별 테스트가 mock_tg를 직접 검증하고
        # 싶을 땐 그 테스트 안에서 다시 `patch("core.notify.send_telegram")`로 감싸면
        # 이 전역 패치 위에 정상적으로 덮어씌워짐(unittest.mock 중첩 patch는 안전).
        patch("core.notify.send_telegram", return_value=True).start()
        patch("core.notify.send_telegram_report", return_value=True).start()

        # 2026-08-08 코드감사: entry/monitor/force_close가 real 모드에서 이제
        # core.config.validate()를 호출함(KIS 자격증명 누락시 조기에 명확히
        # 실패시키기 위함, 예전엔 어디서도 호출 안 됐음). 테스트는 .env의 실제
        # 값 유무와 무관하게 동작해야 하므로 기본은 무력화 — validate() 자체를
        # 검증하는 테스트는 개별적으로 다시 patch(위 send_telegram과 동일 관례).
        patch("core.config.validate", return_value=None).start()

        self.addCleanup(patch.stopall)

    def tearDown(self):
        import core.db as dbmod
        dbmod.DB_PATH = self._orig_db_path
        self._tmpdir.cleanup()

    def _seed_candidates(self, date: str, rows: list):
        """rows: [(rank_no, ticker, name, prev_close, vol_ratio), ...]"""
        import core.db as dbmod
        with dbmod.get_conn() as conn:
            for rank_no, ticker, name, prev_close, vol_ratio in rows:
                conn.execute(
                    """INSERT INTO candidates
                       (trade_date, ticker, name, sector, score, total_amt, avg_val,
                        marcap_bil, rank_no, strategy, strategy_version)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (date, ticker, name, "", vol_ratio, 1e10, prev_close, 0.0, rank_no, "iris_v2", "2.0"),
                )

    def _open_positions(self):
        import core.db as dbmod
        with dbmod.get_conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM positions WHERE strategy='iris_v2'"
            ).fetchall()]


class TestIsExcluded(unittest.TestCase):
    def test_brand_etf_needs_sector_not_name(self):
        # "KODEX 200"은 이름에 EXCL_KW가 없어 이름만으로는 못 잡음(2026-07-20 실제로 겪은
        # 버그와 동일 유형) — candidates 스크립트가 sector를 반드시 함께 넘겨야 하는 이유.
        from strategy.iris_v2_params import is_excluded
        self.assertFalse(is_excluded("KODEX 200"))
        self.assertTrue(is_excluded("KODEX 200", sector="ETF(실물복제/수익증권)"))

    def test_etf_by_keyword(self):
        from strategy.iris_v2_params import is_excluded
        self.assertTrue(is_excluded("TIGER 미국나스닥100 ETF"))
        self.assertTrue(is_excluded("무슨무슨 스팩"))

    def test_etf_by_sector(self):
        from strategy.iris_v2_params import is_excluded
        self.assertTrue(is_excluded("KODEX 200", sector="ETF(실물복제/수익증권)"))

    def test_preferred_suffix(self):
        from strategy.iris_v2_params import is_excluded
        self.assertTrue(is_excluded("삼성전자우"))
        self.assertFalse(is_excluded("삼성전자"))

    def test_normal_stock_not_excluded(self):
        from strategy.iris_v2_params import is_excluded
        self.assertFalse(is_excluded("현대모비스", sector="운송장비·부품"))

    def test_nan_or_none_input_does_not_crash(self):
        # 2026-07-22 발견: universe에 없는 티커를 candidates.py가 left-join하면
        # name/sector가 NaN(float)으로 들어올 수 있음 — `name or ""`는 NaN을 그대로
        # 통과시켜(NaN은 truthy) TypeError로 저녁 후보선정 배치 전체가 죽었음.
        from strategy.iris_v2_params import is_excluded
        self.assertFalse(is_excluded(float("nan"), float("nan")))
        self.assertFalse(is_excluded(None, None))


class TestEntryFallback(IrisV2TestBase):
    """순위→체크→대체(fallback) 로직 — 2026-07-21 감사에서 백테스트와 230/230 동일함을
    확인한 방식. 여기선 그 로직 자체가 올바르게 구현됐는지 검증."""

    def setUp(self):
        super().setUp()
        patch.object(self.entry, "_sleep_until_entry", lambda: 0.0).start()
        self.addCleanup(patch.stopall)

    def test_rank1_qualifies_picks_rank1(self):
        date = "20260722"
        self._seed_candidates(date, [
            (1, "000001", "종목A", 10000, 15.0),
            (2, "000002", "종목B", 20000, 10.0),
        ])
        # 종목A: gap = (9850-10000)/10000 = -1.5% (밴드 -2%~-1% 안)
        with patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 9850, "price": 9850}):
            self.entry.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["ticker"], "000001")

    def test_rank1_fails_falls_back_to_rank2(self):
        date = "20260722"
        self._seed_candidates(date, [
            (1, "000001", "종목A", 10000, 15.0),
            (2, "000002", "종목B", 20000, 10.0),
        ])

        def fake_get_price(ticker):
            if ticker == "000001":
                return {"open": 10000, "price": 10000}  # gap 0% — 밴드 밖
            return {"open": 19700, "price": 19700}  # gap -1.5% — 밴드 안

        with patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", side_effect=fake_get_price):
            self.entry.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["ticker"], "000002")

    def test_gap_uses_current_price_not_daily_open(self):
        """v2.5.2 회귀테스트: 갭은 09:30 시점 현재가(price) 기준이어야 함 — 당일 09:00
        시가(open)를 쓰면 09:00~09:30 변동만큼 오판(2026-07-24 발견 버그)."""
        date = "20260722"
        self._seed_candidates(date, [(1, "000001", "종목A", 10000, 15.0)])
        # 09:00 시가는 갭 0%(밴드 밖)지만 09:30 현재가는 -1.5%(밴드 안) → 진입해야 함
        with patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 10000, "price": 9850}):
            self.entry.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["entry_price"], 9850)  # 진입가도 현재가 기준

    def test_severe_delay_aborts_entry(self):
        """v2.5.3 회귀테스트: 정밀대기 종료 후 목표시각보다 5분(DELAY_ABORT_SECS) 넘게
        지연됐으면 그날 진입을 스킵해야 함 — 2026-07-24 금요일 원인불명 83초 지연
        사례의 재발방지 안전장치(83초는 경보만, 이 테스트는 그보다 훨씬 큰 확실한
        스킵 케이스로 검증)."""
        date = "20260722"
        self._seed_candidates(date, [(1, "000001", "종목A", 10000, 15.0)])
        with patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch.object(self.entry, "_sleep_until_entry", lambda: 400.0), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 9850, "price": 9850}) as mock_price:
            self.entry.run(dry_run=False)
        self.assertEqual(len(self._open_positions()), 0)
        mock_price.assert_not_called()  # 시세조회 자체를 안 해야 함(스킵이 조회 전에 일어남)

    def test_moderate_delay_still_enters(self):
        """v2.5.3: 30~300초 지연은 경보만 남기고 매매는 정상 진행돼야 함."""
        date = "20260722"
        self._seed_candidates(date, [(1, "000001", "종목A", 10000, 15.0)])
        with patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch.object(self.entry, "_sleep_until_entry", lambda: 45.0), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 9850, "price": 9850}):
            self.entry.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["ticker"], "000001")

    def test_no_candidate_qualifies_no_entry(self):
        date = "20260722"
        self._seed_candidates(date, [(1, "000001", "종목A", 10000, 15.0)])
        with patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 10000, "price": 10000}):
            self.entry.run(dry_run=False)
        self.assertEqual(len(self._open_positions()), 0)

    def test_idempotent_second_run_skips(self):
        date = "20260722"
        self._seed_candidates(date, [(1, "000001", "종목A", 10000, 15.0)])
        with patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 9850, "price": 9850}) as mock_price:
            self.entry.run(dry_run=False)
            self.entry.run(dry_run=False)  # 재실행(cron 중복 등 상정)
        self.assertEqual(len(self._open_positions()), 1)  # 이중진입 없음

    def test_holiday_no_action(self):
        date = "20260725"  # 예: 토요일이라 가정
        self._seed_candidates(date, [(1, "000001", "종목A", 10000, 15.0)])
        with patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=False), \
             patch("core.kis_api.get_price") as mock_price:
            self.entry.run(dry_run=False)
        mock_price.assert_not_called()
        self.assertEqual(len(self._open_positions()), 0)

    def test_all_price_fail_sends_critical_alert(self):
        # 후보 전원이 "밴드 밖"이 아니라 "시세조회 자체가 실패"면 API 장애로 보고
        # CRITICAL 텔레그램을 보내야 함(정상적인 무진입과 조용히 섞이면 안 됨)
        date = "20260722"
        self._seed_candidates(date, [(1, "000001", "종목A", 10000, 15.0)])
        with patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", side_effect=Exception("네트워크 오류")), \
             patch("core.notify.send_telegram") as mock_tg:
            self.entry.run(dry_run=False)
        self.assertEqual(len(self._open_positions()), 0)
        mock_tg.assert_called_once()
        self.assertIn("장애", mock_tg.call_args[0][0])

    def test_legit_no_entry_sends_plain_info_not_alert(self):
        # 정상 조회했지만 갭이 밴드 밖이라 무진입인 경우: v2.9부터 정보성 알림은
        # 오되(2026-08-11, "왜 09:30에 아무 알림도 없었지" 혼란 방지), CRITICAL/ALERT
        # 태그(🚨)는 아니어야 함 — halt·API장애 알림과 구분돼야 하므로.
        date = "20260722"
        self._seed_candidates(date, [(1, "000001", "종목A", 10000, 15.0)])
        with patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 10000, "price": 10000}), \
             patch("core.notify.send_telegram") as mock_tg:
            self.entry.run(dry_run=False)
        self.assertEqual(len(self._open_positions()), 0)
        mock_tg.assert_called_once()
        prefix = mock_tg.call_args.kwargs.get("prefix", "")
        self.assertNotIn("🚨", prefix)
        self.assertIn("무진입", mock_tg.call_args[0][0])


class TestMonitorExitLogic(IrisV2TestBase):
    def _seed_open_position(self, date: str, entry_price: float = 10000, shares: int = 100):
        import core.db as dbmod
        with dbmod.get_conn() as conn:
            conn.execute(
                """INSERT INTO positions
                   (ticker, name, date_open, time_open, entry_price, shares, score,
                    target_pct, stop_pct, mode, status, strategy_version, strategy)
                   VALUES ('000001','종목A',?,?,?,?,0,15,-4,'paper','open','2.0','iris_v2')""",
                (date, "093000", entry_price, shares),
            )

    def test_take_profit_triggers(self):
        date = "20260722"
        self._seed_open_position(date, entry_price=10000)
        with patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("bot.iris_v2_monitor.now_kst", return_value=datetime(2026, 7, 22, 10, 5, tzinfo=KST)), \
             patch("core.kis_api.get_price", return_value={"open": 11600, "price": 11600}):
            self.monitor.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "closed")
        self.assertEqual(positions[0]["exit_reason"], "익절")

    def test_stop_loss_triggers(self):
        date = "20260722"
        self._seed_open_position(date, entry_price=10000)
        with patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("bot.iris_v2_monitor.now_kst", return_value=datetime(2026, 7, 22, 10, 5, tzinfo=KST)), \
             patch("core.kis_api.get_price", return_value={"open": 9550, "price": 9550}):
            self.monitor.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "closed")
        self.assertEqual(positions[0]["exit_reason"], "손절")

    def test_mid_eval_exits_when_underwater(self):
        date = "20260722"
        self._seed_open_position(date, entry_price=10000)
        with patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("bot.iris_v2_monitor.now_kst", return_value=datetime(2026, 7, 22, 11, 30, tzinfo=KST)), \
             patch("core.kis_api.get_price", return_value={"open": 9900, "price": 9900}):
            self.monitor.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "closed")
        self.assertEqual(positions[0]["exit_reason"], "중간청산")

    def test_mid_eval_holds_when_recovered(self):
        # v2.5 MID_THRESHOLD 기본값(+2%)과 무관하게 이 테스트는 "0% 기준 회복"만 격리검증
        date = "20260722"
        self._seed_open_position(date, entry_price=10000)
        with patch.object(self.monitor, "MID_THRESHOLD", 0.0), \
             patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("bot.iris_v2_monitor.now_kst", return_value=datetime(2026, 7, 22, 11, 30, tzinfo=KST)), \
             patch("core.kis_api.get_price", return_value={"open": 10100, "price": 10100}):
            self.monitor.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "open")  # 회복했으니 계속 보유

    def test_mid_eval_only_once(self):
        """11:30에 크론이 실수로 두 번 불려도 중복 청산판단(플래그) 없이 안전해야 함."""
        date = "20260722"
        self._seed_open_position(date, entry_price=10000)
        with patch.object(self.monitor, "MID_THRESHOLD", 0.0), \
             patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("bot.iris_v2_monitor.now_kst", return_value=datetime(2026, 7, 22, 11, 30, tzinfo=KST)), \
             patch("core.kis_api.get_price", return_value={"open": 10100, "price": 10100}):
            self.monitor.run(dry_run=False)
            self.monitor.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "open")

    def test_mid_eval_catches_up_after_missed_tick(self):
        """v2.5.2 회귀테스트: 11:30 정각 틱을 놓쳐도(시세조회 실패·cron 지연 등)
        이후 최초 성공 틱에서 중간평가가 수행돼야 함 — `hhmm == MID_TIME`이던 시절엔
        그날 중간평가가 통째로 사라졌음(2026-07-24 발견 버그)."""
        date = "20260722"
        self._seed_open_position(date, entry_price=10000)
        with patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("bot.iris_v2_monitor.now_kst", return_value=datetime(2026, 7, 22, 11, 47, tzinfo=KST)), \
             patch("core.kis_api.get_price", return_value={"open": 9900, "price": 9900}):
            self.monitor.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "closed")
        self.assertEqual(positions[0]["exit_reason"], "중간청산")

    def test_no_action_between_checkpoints(self):
        date = "20260722"
        self._seed_open_position(date, entry_price=10000)
        with patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("bot.iris_v2_monitor.now_kst", return_value=datetime(2026, 7, 22, 10, 45, tzinfo=KST)), \
             patch("core.kis_api.get_price", return_value={"open": 10050, "price": 10050}):
            self.monitor.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "open")

    def test_no_open_position_fast_noop(self):
        date = "20260722"
        with patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price") as mock_price:
            self.monitor.run(dry_run=False)
        mock_price.assert_not_called()

    def test_price_fail_alerts_only_after_3_consecutive_minutes(self):
        # 크론이 매분 새 프로세스로 도는 구조라 "3연속 실패"는 3번의 별도 run() 호출로
        # 재현. 1~2분째는 조용히 재시도만, 3분째 도달 시 최초 1회만 CRITICAL 알림.
        date = "20260722"
        self._seed_open_position(date, entry_price=10000)
        with patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("bot.iris_v2_monitor.now_kst", return_value=datetime(2026, 7, 22, 10, 5, tzinfo=KST)), \
             patch("bot.iris_v2_monitor.time.sleep", return_value=None), \
             patch("core.kis_api.get_price", side_effect=Exception("네트워크 오류")), \
             patch("core.notify.send_telegram") as mock_tg:
            self.monitor.run(dry_run=False)
            self.monitor.run(dry_run=False)
            mock_tg.assert_not_called()
            self.monitor.run(dry_run=False)
            mock_tg.assert_called_once()
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "open")  # 시세불명이라 임의청산 안 함

    def test_price_fail_streak_resets_after_recovery(self):
        date = "20260722"
        self._seed_open_position(date, entry_price=10000)
        import core.db as dbmod
        with patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("bot.iris_v2_monitor.now_kst", return_value=datetime(2026, 7, 22, 10, 5, tzinfo=KST)), \
             patch("bot.iris_v2_monitor.time.sleep", return_value=None), \
             patch("core.kis_api.get_price", side_effect=Exception("네트워크 오류")):
            self.monitor.run(dry_run=False)
        self.assertEqual(dbmod.state_get(self.monitor._pricefail_key(date)), 1)
        with patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("bot.iris_v2_monitor.now_kst", return_value=datetime(2026, 7, 22, 10, 6, tzinfo=KST)), \
             patch("core.kis_api.get_price", return_value={"open": 10050, "price": 10050}):
            self.monitor.run(dry_run=False)
        self.assertEqual(dbmod.state_get(self.monitor._pricefail_key(date)), 0)

    def test_partial_tp_triggers_and_keeps_position_open(self):
        # v2.5: +8% 도달(TP 15%/SL -4.5% 어느쪽도 아직 아님)이면 보유 50%만 선매도,
        # 포지션 자체는 계속 open 상태 유지
        date = "20260722"
        self._seed_open_position(date, entry_price=10000, shares=100)
        with patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("bot.iris_v2_monitor.now_kst", return_value=datetime(2026, 7, 22, 10, 5, tzinfo=KST)), \
             patch("core.kis_api.get_price", return_value={"open": 10900, "price": 10900}):  # +9%
            self.monitor.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "open")
        self.assertEqual(positions[0]["shares"], 50)
        self.assertEqual(positions[0]["partial_shares"], 50)
        self.assertEqual(positions[0]["partial_exit_price"], 10900)

    def test_partial_tp_does_not_retrigger(self):
        date = "20260722"
        self._seed_open_position(date, entry_price=10000, shares=100)
        with patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("bot.iris_v2_monitor.now_kst", return_value=datetime(2026, 7, 22, 10, 5, tzinfo=KST)), \
             patch("core.kis_api.get_price", return_value={"open": 10900, "price": 10900}):
            self.monitor.run(dry_run=False)
            self.monitor.run(dry_run=False)  # 다음 분에도 여전히 +9% -- 재발동되면 안 됨
        positions = self._open_positions()
        self.assertEqual(positions[0]["shares"], 50)  # 100->50에서 더 줄지 않음
        self.assertEqual(positions[0]["partial_shares"], 50)

    def test_final_close_blends_partial_and_remaining(self):
        # 1차 부분익절(+9%, 50주) 후 나머지가 익절(+16%)로 마감되면 손익은 두 체결가의
        # 블렌드여야 함: (50*10900 + 50*11600)/(100*10000) - 1
        date = "20260722"
        self._seed_open_position(date, entry_price=10000, shares=100)
        with patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("bot.iris_v2_monitor.now_kst", return_value=datetime(2026, 7, 22, 10, 5, tzinfo=KST)), \
             patch("core.kis_api.get_price", return_value={"open": 10900, "price": 10900}):
            self.monitor.run(dry_run=False)  # 1차 부분익절
        with patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("bot.iris_v2_monitor.now_kst", return_value=datetime(2026, 7, 22, 10, 20, tzinfo=KST)), \
             patch("core.kis_api.get_price", return_value={"open": 11600, "price": 11600}):
            self.monitor.run(dry_run=False)  # 잔여 50주 익절
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "closed")
        self.assertEqual(positions[0]["exit_reason"], "익절")
        expected_pct = ((50 * 10900 + 50 * 11600) / (100 * 10000) - 1) * 100
        self.assertAlmostEqual(positions[0]["pnl_pct"], expected_pct, places=1)


class TestForceClose(IrisV2TestBase):
    def _seed_open_position(self, date: str, entry_price: float = 10000, shares: int = 100):
        import core.db as dbmod
        with dbmod.get_conn() as conn:
            conn.execute(
                """INSERT INTO positions
                   (ticker, name, date_open, time_open, entry_price, shares, score,
                    target_pct, stop_pct, mode, status, strategy_version, strategy)
                   VALUES ('000001','종목A',?,?,?,?,0,15,-4,'paper','open','2.0','iris_v2')""",
                (date, "093000", entry_price, shares),
            )

    def test_force_closes_open_position(self):
        date = "20260722"
        self._seed_open_position(date, entry_price=10000)
        with patch("bot.iris_v2_force_close.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 10050, "price": 10050}):
            self.force_close.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "closed")
        self.assertEqual(positions[0]["exit_reason"], "강제청산")

    def test_force_close_blends_partial_and_remaining(self):
        # v2.5: 13:00까지 1차 부분익절(+9%, 50주)만 발동된 채 살아남은 잔여 50주는
        # 강제청산 시점 가격(10200)으로 청산 -- 손익은 두 체결가 블렌드
        date = "20260722"
        self._seed_open_position(date, entry_price=10000, shares=100)
        import core.db as dbmod
        with dbmod.get_conn() as conn:
            conn.execute(
                "UPDATE positions SET shares=50, partial_exit_price=10900, partial_shares=50, "
                "partial_time='100500' WHERE ticker='000001' AND date_open=?", (date,),
            )
        with patch("bot.iris_v2_force_close.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 10200, "price": 10200}):
            self.force_close.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "closed")
        self.assertEqual(positions[0]["exit_reason"], "강제청산")
        expected_pct = ((50 * 10900 + 50 * 10200) / (100 * 10000) - 1) * 100
        self.assertAlmostEqual(positions[0]["pnl_pct"], expected_pct, places=1)

    def test_no_open_position_noop(self):
        date = "20260722"
        with patch("bot.iris_v2_force_close.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price") as mock_price:
            self.force_close.run(dry_run=False)
        mock_price.assert_not_called()


class TestStageBRealMode(IrisV2TestBase):
    """TRADE_MODE=real 경로 — 실제 KIS 주문 함수는 전부 모킹, core.order_exec의
    체결확인·halt 로직이 올바르게 동작하는지 검증. 자금이 걸린 부분이라 특히
    "실패/미확인시 절대 이중주문하지 않고 멈춘다"는 안전장치를 중점 검증한다."""

    def _seed_candidates(self, date: str, rows: list):
        import core.db as dbmod
        with dbmod.get_conn() as conn:
            for rank_no, ticker, name, prev_close, vol_ratio in rows:
                conn.execute(
                    """INSERT INTO candidates
                       (trade_date, ticker, name, sector, score, total_amt, avg_val,
                        marcap_bil, rank_no, strategy, strategy_version)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (date, ticker, name, "", vol_ratio, 1e10, prev_close, 0.0, rank_no, "iris_v2", "2.5"),
                )

    def _seed_open_position(self, date: str, entry_price: float = 10000, shares: int = 100):
        import core.db as dbmod
        with dbmod.get_conn() as conn:
            conn.execute(
                """INSERT INTO positions
                   (ticker, name, date_open, time_open, entry_price, shares, score,
                    target_pct, stop_pct, mode, status, strategy_version, strategy)
                   VALUES ('000001','종목A',?,?,?,?,0,15,-4.5,'real','open','2.5','iris_v2')""",
                (date, "093000", entry_price, shares),
            )

    # ── 2026-08-08 코드감사: 미해결 halt는 날짜를 넘어 유효해야 함 ──────────
    def test_entry_blocked_by_unresolved_halt_from_previous_day(self):
        """halt 키가 날짜별이라 예전엔 entry.py가 어제 halt를 전혀 확인하지
        않았음(코드감사에서 발견) — find_unresolved_halt()로 막혀야 함."""
        import core.order_exec as oe
        date = "20260722"
        self._seed_candidates(date, [(1, "000001", "종목A", 10000, 15.0)])
        oe.set_halt("20260721", "999999", "매도체결미확인", "전날 미해결")
        with patch.object(self.entry, "TRADE_MODE", "real"), \
             patch.object(self.entry, "_sleep_until_entry", lambda: 0.0), \
             patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price") as mock_price, \
             patch("core.order_exec.buy_safe") as mock_buy, \
             patch("core.notify.send_telegram") as mock_tg:
            self.entry.run(dry_run=False)
        mock_price.assert_not_called()  # 후보 시세조회까지 가지도 않고 차단돼야 함
        mock_buy.assert_not_called()
        self.assertEqual(len(self._open_positions()), 0)
        mock_tg.assert_called_once()
        self.assertIn("20260721", mock_tg.call_args[0][0])

    def test_entry_proceeds_normally_when_no_unresolved_halt(self):
        date = "20260722"
        self._seed_candidates(date, [(1, "000001", "종목A", 10000, 15.0)])
        with patch.object(self.entry, "TRADE_MODE", "real"), \
             patch.object(self.entry, "SPLIT_BUY_ENABLED", False), \
             patch.object(self.entry, "_sleep_until_entry", lambda: 0.0), \
             patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 9850, "price": 9850}), \
             patch("core.order_exec.compute_buy_qty", return_value=100), \
             patch("core.order_exec.buy_safe", return_value=(True, "ODNO123")), \
             patch("core.order_exec.verify_buy_fill", return_value=100), \
             patch("core.order_exec.resolve_entry_price", return_value=9860):
            self.entry.run(dry_run=False)
        self.assertEqual(len(self._open_positions()), 1)

    # ── 2026-08-08 코드감사: KIS 자격증명 조기검증(config.validate()) ──────
    def test_real_mode_calls_config_validate(self):
        date = "20260722"
        self._seed_candidates(date, [(1, "000001", "종목A", 10000, 15.0)])
        with patch.object(self.entry, "TRADE_MODE", "real"), \
             patch.object(self.entry, "_sleep_until_entry", lambda: 0.0), \
             patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price") as mock_price, \
             patch("core.config.validate") as mock_validate:
            mock_validate.side_effect = RuntimeError(".env 필수값 누락: KIS_APP_KEY")
            with self.assertRaises(RuntimeError):
                self.entry.run(dry_run=False)
        mock_validate.assert_called_once()
        mock_price.assert_not_called()  # validate 실패시 그 뒤로 아무것도 진행 안 됨

    def test_paper_mode_does_not_call_config_validate(self):
        date = "20260722"
        self._seed_candidates(date, [(1, "000001", "종목A", 10000, 15.0)])
        with patch.object(self.entry, "_sleep_until_entry", lambda: 0.0), \
             patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 9850, "price": 9850}), \
             patch("core.config.validate") as mock_validate:
            self.entry.run(dry_run=False)
        mock_validate.assert_not_called()

    # ── entry.py 실거래 매수 ────────────────────────────────────
    def test_real_buy_success_records_position(self):
        date = "20260722"
        self._seed_candidates(date, [(1, "000001", "종목A", 10000, 15.0)])
        with patch.object(self.entry, "TRADE_MODE", "real"), \
             patch.object(self.entry, "SPLIT_BUY_ENABLED", False), \
             patch.object(self.entry, "_sleep_until_entry", lambda: 0.0), \
             patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 9850, "price": 9850}), \
             patch("core.order_exec.compute_buy_qty", return_value=100), \
             patch("core.order_exec.buy_safe", return_value=(True, "ODNO123")), \
             patch("core.order_exec.verify_buy_fill", return_value=100), \
             patch("core.order_exec.resolve_entry_price", return_value=9860):
            self.entry.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["mode"], "real")
        self.assertEqual(positions[0]["shares"], 100)
        self.assertEqual(positions[0]["entry_price"], 9860)

    def test_real_buy_passes_base_price_for_upper_limit_protection(self):
        # 2026-06-11 K3.1 사고(상한가기준 현금검증) 재발방지 — compute_buy_qty가
        # 전일종가(prev_close)를 base_price로 반드시 받아야 함
        date = "20260722"
        self._seed_candidates(date, [(1, "000001", "종목A", 10000, 15.0)])  # prev_close=10000
        with patch.object(self.entry, "TRADE_MODE", "real"), \
             patch.object(self.entry, "_sleep_until_entry", lambda: 0.0), \
             patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 9850, "price": 9850}), \
             patch("core.order_exec.compute_buy_qty", return_value=100) as mock_qty, \
             patch("core.order_exec.buy_safe", return_value=(True, "ODNO123")), \
             patch("core.order_exec.verify_buy_fill", return_value=100), \
             patch("core.order_exec.resolve_entry_price", return_value=9860):
            self.entry.run(dry_run=False)
        mock_qty.assert_called_once()
        self.assertEqual(mock_qty.call_args.kwargs.get("base_price"), 10000)

    def test_real_buy_default_mode_is_full_balance(self):
        """v2.5.4: 포지션 사이징 모드를 아무도 설정한 적 없으면(bot_state에 값 없음)
        기본값은 잔고전액(target_notional=inf) — 지금 계좌규모(300만원)에서 기존
        동작과 완전히 동일해야 함."""
        date = "20260722"
        self._seed_candidates(date, [(1, "000001", "종목A", 10000, 15.0)])
        with patch.object(self.entry, "TRADE_MODE", "real"), \
             patch.object(self.entry, "_sleep_until_entry", lambda: 0.0), \
             patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 9850, "price": 9850}), \
             patch("core.order_exec.compute_buy_qty", return_value=100) as mock_qty, \
             patch("core.order_exec.buy_safe", return_value=(True, "ODNO123")), \
             patch("core.order_exec.verify_buy_fill", return_value=100), \
             patch("core.order_exec.resolve_entry_price", return_value=9860):
            self.entry.run(dry_run=False)
        target_notional = mock_qty.call_args.args[2]
        self.assertEqual(target_notional, float("inf"))

    def test_real_buy_fixed_mode_caps_target_notional(self):
        """v2.5.4: /mode fixed로 설정해두면 compute_buy_qty에 그 고정금액이 그대로
        전달돼야 함(잔고와 무관하게 그 값을 상한으로 사용)."""
        from core.telegram_commands import MODE_KEY, FIXED_AMOUNT_KEY, MODE_FIXED
        from core.db import state_set
        date = "20260722"
        self._seed_candidates(date, [(1, "000001", "종목A", 10000, 15.0)])
        state_set(MODE_KEY, MODE_FIXED)
        state_set(FIXED_AMOUNT_KEY, 3_000_000)
        with patch.object(self.entry, "TRADE_MODE", "real"), \
             patch.object(self.entry, "_sleep_until_entry", lambda: 0.0), \
             patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 9850, "price": 9850}), \
             patch("core.order_exec.compute_buy_qty", return_value=100) as mock_qty, \
             patch("core.order_exec.buy_safe", return_value=(True, "ODNO123")), \
             patch("core.order_exec.verify_buy_fill", return_value=100), \
             patch("core.order_exec.resolve_entry_price", return_value=9860):
            self.entry.run(dry_run=False)
        target_notional = mock_qty.call_args.args[2]
        self.assertEqual(target_notional, 3_000_000)

    def _seed_candidate_with_mom5(self, date, ticker, name, prev_close, vol_ratio, mom5):
        import core.db as dbmod
        with dbmod.get_conn() as conn:
            conn.execute(
                """INSERT INTO candidates
                   (trade_date, ticker, name, sector, score, total_amt, avg_val,
                    marcap_bil, rank_no, strategy, strategy_version, val_ratio)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (date, ticker, name, "", vol_ratio, 1e10, prev_close, 0.0, 1, "iris_v2", "2.7", mom5),
            )

    def test_real_buy_high_mom5_default_disabled_is_noop(self):
        """mom5 사이징캡은 설계·구현 완료됐으나 **배포 보류 중**(MOM5_CAP_ENABLED=False,
        2026-08-07 — 정본 230건 데이터셋 재검증에서 비용이 최초추정보다 커서 사용자가
        추가 리서치 전까지 보류 결정, iris DECISIONS 2026-08-07 (5차) 참조). 기본값
        상태에선 mom5가 임계값 이상이어도 size_factor=1.0(완전 무변화)이어야 함."""
        from strategy.iris_v2_params import MOM5_CAP_THRESHOLD
        date = "20260722"
        self._seed_candidate_with_mom5(date, "000001", "종목A", 10000, 15.0, MOM5_CAP_THRESHOLD)
        with patch.object(self.entry, "TRADE_MODE", "real"), \
             patch.object(self.entry, "_sleep_until_entry", lambda: 0.0), \
             patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 9850, "price": 9850}), \
             patch("core.order_exec.compute_buy_qty", return_value=100) as mock_qty, \
             patch("core.order_exec.buy_safe", return_value=(True, "ODNO123")), \
             patch("core.order_exec.verify_buy_fill", return_value=100), \
             patch("core.order_exec.resolve_entry_price", return_value=9860):
            self.entry.run(dry_run=False)
        target_notional = mock_qty.call_args.args[2]
        self.assertEqual(target_notional, float("inf"))
        self.assertEqual(mock_qty.call_args.kwargs.get("size_factor"), 1.0)

    def test_real_buy_high_mom5_passes_size_factor_when_enabled(self):
        """메커니즘 자체 검증(활성화 시): MOM5_CAP_ENABLED=True로 강제 패치하면
        mom5>=MOM5_CAP_THRESHOLD일 때 compute_buy_qty에 size_factor=
        MOM5_CAP_SIZE_FACTOR가 전달돼야 함. target_notional 자체를 줄이면
        잔고전액모드(inf)에서 무효화되는 버그가 있어(구현 중 발견) size_factor로
        별도 전달하는 방식으로 확정 — 이 테스트는 그 회귀를 방지."""
        import strategy.iris_v2_params as params
        from strategy.iris_v2_params import MOM5_CAP_THRESHOLD, MOM5_CAP_SIZE_FACTOR
        date = "20260722"
        self._seed_candidate_with_mom5(date, "000001", "종목A", 10000, 15.0, MOM5_CAP_THRESHOLD)
        with patch.object(self.entry, "TRADE_MODE", "real"), \
             patch.object(self.entry, "MOM5_CAP_ENABLED", True), \
             patch.object(self.entry, "_sleep_until_entry", lambda: 0.0), \
             patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 9850, "price": 9850}), \
             patch("core.order_exec.compute_buy_qty", return_value=100) as mock_qty, \
             patch("core.order_exec.buy_safe", return_value=(True, "ODNO123")), \
             patch("core.order_exec.verify_buy_fill", return_value=100), \
             patch("core.order_exec.resolve_entry_price", return_value=9860):
            self.entry.run(dry_run=False)
        target_notional = mock_qty.call_args.args[2]
        self.assertEqual(target_notional, float("inf"))  # 잔고전액모드는 그대로 유지
        self.assertEqual(mock_qty.call_args.kwargs.get("size_factor"), MOM5_CAP_SIZE_FACTOR)

    def test_real_buy_high_mom5_passes_size_factor_fixed_mode_when_enabled(self):
        """활성화 시 고정금액 모드에서도 mom5 캡(size_factor)이 함께 전달돼야 함."""
        from core.telegram_commands import MODE_KEY, FIXED_AMOUNT_KEY, MODE_FIXED
        from core.db import state_set
        from strategy.iris_v2_params import MOM5_CAP_THRESHOLD, MOM5_CAP_SIZE_FACTOR
        date = "20260722"
        self._seed_candidate_with_mom5(date, "000001", "종목A", 10000, 15.0, MOM5_CAP_THRESHOLD)
        state_set(MODE_KEY, MODE_FIXED)
        state_set(FIXED_AMOUNT_KEY, 3_000_000)
        with patch.object(self.entry, "TRADE_MODE", "real"), \
             patch.object(self.entry, "MOM5_CAP_ENABLED", True), \
             patch.object(self.entry, "_sleep_until_entry", lambda: 0.0), \
             patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 9850, "price": 9850}), \
             patch("core.order_exec.compute_buy_qty", return_value=100) as mock_qty, \
             patch("core.order_exec.buy_safe", return_value=(True, "ODNO123")), \
             patch("core.order_exec.verify_buy_fill", return_value=100), \
             patch("core.order_exec.resolve_entry_price", return_value=9860):
            self.entry.run(dry_run=False)
        target_notional = mock_qty.call_args.args[2]
        self.assertEqual(target_notional, 3_000_000)
        self.assertEqual(mock_qty.call_args.kwargs.get("size_factor"), MOM5_CAP_SIZE_FACTOR)

    def test_real_buy_low_mom5_uses_default_size_factor_when_enabled(self):
        """활성화 상태에서도 mom5가 임계값 미만이면 size_factor=1.0(기존 동작과 동일)."""
        from strategy.iris_v2_params import MOM5_CAP_THRESHOLD
        date = "20260722"
        self._seed_candidate_with_mom5(date, "000001", "종목A", 10000, 15.0, MOM5_CAP_THRESHOLD - 0.01)
        with patch.object(self.entry, "TRADE_MODE", "real"), \
             patch.object(self.entry, "MOM5_CAP_ENABLED", True), \
             patch.object(self.entry, "_sleep_until_entry", lambda: 0.0), \
             patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 9850, "price": 9850}), \
             patch("core.order_exec.compute_buy_qty", return_value=100) as mock_qty, \
             patch("core.order_exec.buy_safe", return_value=(True, "ODNO123")), \
             patch("core.order_exec.verify_buy_fill", return_value=100), \
             patch("core.order_exec.resolve_entry_price", return_value=9860):
            self.entry.run(dry_run=False)
        target_notional = mock_qty.call_args.args[2]
        self.assertEqual(target_notional, float("inf"))
        self.assertEqual(mock_qty.call_args.kwargs.get("size_factor"), 1.0)

    def test_paper_buy_high_mom5_shrinks_shares_when_enabled(self):
        """활성화 시 paper 모드(잔고전액 기본)에서도 mom5 캡이 CAPITAL_PER_TRADE에
        적용돼 매수수량이 줄어야 함."""
        from strategy.iris_v2_params import MOM5_CAP_THRESHOLD, MOM5_CAP_SIZE_FACTOR, CAPITAL_PER_TRADE
        date = "20260722"
        self._seed_candidate_with_mom5(date, "000001", "종목A", 10000, 15.0, MOM5_CAP_THRESHOLD)
        with patch.object(self.entry, "TRADE_MODE", "paper"), \
             patch.object(self.entry, "MOM5_CAP_ENABLED", True), \
             patch.object(self.entry, "_sleep_until_entry", lambda: 0.0), \
             patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 9850, "price": 9850}):
            self.entry.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(len(positions), 1)
        expected_shares = int((CAPITAL_PER_TRADE * MOM5_CAP_SIZE_FACTOR) // positions[0]["entry_price"])
        self.assertEqual(positions[0]["shares"], expected_shares)

    def test_real_buy_order_failure_no_position_and_halts(self):
        date = "20260722"
        self._seed_candidates(date, [(1, "000001", "종목A", 10000, 15.0)])
        with patch.object(self.entry, "TRADE_MODE", "real"), \
             patch.object(self.entry, "_sleep_until_entry", lambda: 0.0), \
             patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 9850, "price": 9850}), \
             patch("core.order_exec.compute_buy_qty", return_value=100), \
             patch("core.order_exec.buy_safe", return_value=(False, "")), \
             patch("core.notify.send_telegram") as mock_tg:
            self.entry.run(dry_run=False)
        self.assertEqual(len(self._open_positions()), 0)  # 포지션 기록 안 됨
        mock_tg.assert_called_once()
        self.assertIn("실패", mock_tg.call_args[0][0])
        import core.order_exec as oe
        self.assertIsNotNone(oe.is_halted(date))  # 자동매매 중단됨

    def test_real_buy_unfilled_no_position_and_halts(self):
        date = "20260722"
        self._seed_candidates(date, [(1, "000001", "종목A", 10000, 15.0)])
        with patch.object(self.entry, "TRADE_MODE", "real"), \
             patch.object(self.entry, "_sleep_until_entry", lambda: 0.0), \
             patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 9850, "price": 9850}), \
             patch("core.order_exec.compute_buy_qty", return_value=100), \
             patch("core.order_exec.buy_safe", return_value=(True, "ODNO123")), \
             patch("core.order_exec.verify_buy_fill", return_value=0):
            self.entry.run(dry_run=False)
        self.assertEqual(len(self._open_positions()), 0)
        import core.order_exec as oe
        self.assertIsNotNone(oe.is_halted(date))

    def test_real_buy_second_run_does_not_retry_after_failure(self):
        # 매수실패로 halt+entered_key 세팅된 뒤 재실행돼도 다시 주문 시도하면 안 됨
        date = "20260722"
        self._seed_candidates(date, [(1, "000001", "종목A", 10000, 15.0)])
        with patch.object(self.entry, "TRADE_MODE", "real"), \
             patch.object(self.entry, "SPLIT_BUY_ENABLED", False), \
             patch.object(self.entry, "_sleep_until_entry", lambda: 0.0), \
             patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 9850, "price": 9850}), \
             patch("core.order_exec.compute_buy_qty", return_value=100), \
             patch("core.order_exec.buy_safe", return_value=(False, "")) as mock_buy:
            self.entry.run(dry_run=False)
            self.entry.run(dry_run=False)
        self.assertEqual(mock_buy.call_count, 1)  # 두번째 실행은 idempotency로 재시도 안 함

    # ── monitor.py 실거래 부분익절/청산 ─────────────────────────
    def test_real_partial_tp_success(self):
        date = "20260722"
        self._seed_open_position(date, entry_price=10000, shares=100)
        with patch.object(self.monitor, "TRADE_MODE", "real"), \
             patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("bot.iris_v2_monitor.now_kst", return_value=datetime(2026, 7, 22, 10, 5, tzinfo=KST)), \
             patch("core.kis_api.get_price", return_value={"open": 10900, "price": 10900}), \
             patch("core.order_exec.execute_partial_sell", return_value=(50, 50, 10880, "ODNO456")):
            self.monitor.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "open")
        self.assertEqual(positions[0]["shares"], 50)
        self.assertEqual(positions[0]["partial_shares"], 50)
        self.assertEqual(positions[0]["partial_exit_price"], 10880)

    def test_real_partial_tp_unconfirmed_halts_and_keeps_position_untouched(self):
        date = "20260722"
        self._seed_open_position(date, entry_price=10000, shares=100)
        with patch.object(self.monitor, "TRADE_MODE", "real"), \
             patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("bot.iris_v2_monitor.now_kst", return_value=datetime(2026, 7, 22, 10, 5, tzinfo=KST)), \
             patch("core.kis_api.get_price", return_value={"open": 10900, "price": 10900}), \
             patch("core.order_exec.execute_partial_sell", return_value=None), \
             patch("core.notify.send_telegram") as mock_tg:
            self.monitor.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "open")
        self.assertEqual(positions[0]["shares"], 100)  # 변경 없음(미확인이라 손 안 댐)
        self.assertIsNone(positions[0]["partial_shares"])
        mock_tg.assert_called_once()

    def test_real_full_close_success(self):
        date = "20260722"
        self._seed_open_position(date, entry_price=10000, shares=100)
        with patch.object(self.monitor, "TRADE_MODE", "real"), \
             patch.object(self.monitor, "SPLIT_SELL_ENABLED", False), \
             patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("bot.iris_v2_monitor.now_kst", return_value=datetime(2026, 7, 22, 10, 5, tzinfo=KST)), \
             patch("core.kis_api.get_price", return_value={"open": 9500, "price": 9500}), \
             patch("core.order_exec.execute_full_close", return_value=(9480, "ODNO789")):
            self.monitor.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "closed")
        self.assertEqual(positions[0]["exit_reason"], "손절")
        self.assertEqual(positions[0]["exit_price"], 9480)

    def test_real_full_close_unconfirmed_halts_and_position_stays_open(self):
        date = "20260722"
        self._seed_open_position(date, entry_price=10000, shares=100)
        with patch.object(self.monitor, "TRADE_MODE", "real"), \
             patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("bot.iris_v2_monitor.now_kst", return_value=datetime(2026, 7, 22, 10, 5, tzinfo=KST)), \
             patch("core.kis_api.get_price", return_value={"open": 9500, "price": 9500}), \
             patch("core.order_exec.execute_full_close", return_value=None), \
             patch("core.notify.send_telegram") as mock_tg:
            self.monitor.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "open")  # 미확인이면 절대 청산기록 안 함
        mock_tg.assert_called_once()

    def test_halt_blocks_monitor_entirely(self):
        date = "20260722"
        self._seed_open_position(date, entry_price=10000, shares=100)
        import core.order_exec as oe
        oe.set_halt(date, "000001", "테스트halt", "상세")
        with patch.object(self.monitor, "TRADE_MODE", "real"), \
             patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price") as mock_price:
            self.monitor.run(dry_run=False)
        mock_price.assert_not_called()  # halt 상태면 시세조회조차 안 함

    # ── force_close.py 실거래 강제청산 ──────────────────────────
    def test_real_force_close_success(self):
        date = "20260722"
        self._seed_open_position(date, entry_price=10000, shares=100)
        with patch.object(self.force_close, "TRADE_MODE", "real"), \
             patch.object(self.force_close, "SPLIT_SELL_ENABLED", False), \
             patch("bot.iris_v2_force_close.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.order_exec.execute_full_close", return_value=(10200, "ODNO999")):
            self.force_close.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "closed")
        self.assertEqual(positions[0]["exit_reason"], "강제청산")
        self.assertEqual(positions[0]["exit_price"], 10200)

    def test_real_force_close_unconfirmed_halts(self):
        date = "20260722"
        self._seed_open_position(date, entry_price=10000, shares=100)
        with patch.object(self.force_close, "TRADE_MODE", "real"), \
             patch("bot.iris_v2_force_close.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.order_exec.execute_full_close", return_value=None), \
             patch("core.notify.send_telegram") as mock_tg:
            self.force_close.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "open")
        mock_tg.assert_called_once()

    def test_real_force_close_stale_pending_tranche2_alerts_but_still_closes(self):
        """2026-08-08 코드감사 신규 — 트랑쉬2 pending이 13:00까지 안 풀린(이론상
        불가능해야 하는) 비정상 상태를 방어적으로 감지해 알림을 보내되, 기록된
        수량 기준 강제청산 자체는 그대로 진행해야 함."""
        date = "20260722"
        self._seed_open_position(date, entry_price=10000, shares=100)
        from core.order_exec import register_pending_tranche2
        register_pending_tranche2(date, "000001", 50)
        with patch.object(self.force_close, "TRADE_MODE", "real"), \
             patch.object(self.force_close, "SPLIT_SELL_ENABLED", False), \
             patch("bot.iris_v2_force_close.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.order_exec.execute_full_close", return_value=(10200, "ODNO999")), \
             patch("core.notify.send_telegram") as mock_tg:
            self.force_close.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "closed")  # 강제청산 자체는 진행됨
        messages = [c.args[0] for c in mock_tg.call_args_list]
        self.assertTrue(any("비정상" in m for m in messages))  # 별도 경고 알림도 감

    def test_halt_blocks_force_close(self):
        date = "20260722"
        self._seed_open_position(date, entry_price=10000, shares=100)
        import core.order_exec as oe
        oe.set_halt(date, "000001", "테스트halt", "상세")
        with patch.object(self.force_close, "TRADE_MODE", "real"), \
             patch("bot.iris_v2_force_close.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.order_exec.execute_full_close") as mock_close:
            self.force_close.run(dry_run=False)
        mock_close.assert_not_called()
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "open")

    # ── 분할매도(2026-08-08 신규) — force_close.py 통합 검증 ────────────────
    # force_close는 "다음 틱"이 없는 그날 마지막 스크립트라 pending_split_sell을
    # 이 자리에서 끝까지 반복 재확인해야 함(_handle_pending_exit와 동일 원칙).
    def test_real_force_close_uses_split_close_when_enabled(self):
        date = "20260722"
        self._seed_open_position(date, entry_price=10000, shares=100)
        with patch.object(self.force_close, "TRADE_MODE", "real"), \
             patch.object(self.force_close, "SPLIT_SELL_ENABLED", True), \
             patch.object(self.force_close, "SPLIT_SELL_TRANCHES", 2), \
             patch("bot.iris_v2_force_close.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.order_exec.execute_split_close", return_value=(10150.0, "S2")) as mock_split, \
             patch("core.order_exec.execute_full_close") as mock_full:
            self.force_close.run(dry_run=False)
        mock_split.assert_called_once_with("000001", 100, 10000, "강제청산", date, 2)
        mock_full.assert_not_called()
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "closed")
        self.assertEqual(positions[0]["exit_price"], 10150.0)

    def test_real_force_close_split_pending_loops_until_done(self):
        # execute_split_close가 처음엔 "pending"을 반환했다가, 이 자리에서 반복
        # 호출하는 retry_pending_split_sell이 결국 "done"을 반환하는 시나리오 —
        # 이중매도(새 sell_market 호출) 없이 재확인만으로 끝나야 함.
        date = "20260722"
        self._seed_open_position(date, entry_price=10000, shares=100)
        with patch.object(self.force_close, "TRADE_MODE", "real"), \
             patch.object(self.force_close, "SPLIT_SELL_ENABLED", True), \
             patch("bot.iris_v2_force_close.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.order_exec.execute_split_close", return_value="pending"), \
             patch("core.order_exec.retry_pending_split_sell", side_effect=[
                 {"status": "pending", "ticker": "000001", "reason": "강제청산"},
                 {"status": "done", "ticker": "000001", "reason": "강제청산",
                  "exit_price": 10100.0, "odno": "S3"},
             ]) as mock_retry, \
             patch("core.kis_api.sell_market") as mock_sell_again:
            self.force_close.run(dry_run=False)
        mock_sell_again.assert_not_called()
        self.assertEqual(mock_retry.call_count, 2)
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "closed")
        self.assertEqual(positions[0]["exit_price"], 10100.0)

    def test_real_force_close_split_halted_sends_alert_position_stays_open(self):
        date = "20260722"
        self._seed_open_position(date, entry_price=10000, shares=100)
        with patch.object(self.force_close, "TRADE_MODE", "real"), \
             patch.object(self.force_close, "SPLIT_SELL_ENABLED", True), \
             patch("bot.iris_v2_force_close.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.order_exec.execute_split_close", return_value="pending"), \
             patch("core.order_exec.retry_pending_split_sell",
                   return_value={"status": "halted", "ticker": "000001", "reason": "강제청산"}), \
             patch("core.notify.send_telegram") as mock_tg:
            self.force_close.run(dry_run=False)
        mock_tg.assert_called_once()
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "open")

    def test_force_close_resumes_pending_split_sell_from_monitor(self):
        # monitor.py 마지막 틱이 남긴 pending_split_sell을 force_close가 이어받아
        # 처리 — 새 매도주문(sell_market) 없이 재확인만으로 끝나야 함.
        date = "20260722"
        self._seed_open_position(date, entry_price=10000, shares=100)
        import core.order_exec as oe
        with patch("core.kis_api.sell_market", return_value={"output": {"ODNO": "S1"}}), \
             patch("core.kis_api.get_balance",
                   return_value={"holdings": [{"ticker": "000001", "qty": 50}]}), \
             patch("core.kis_api.get_filled_price", return_value=9600.0), \
             patch("core.kis_api.get_order_fill_qty", return_value=None):
            result = oe.execute_split_close("000001", 100, 10000, "강제청산", date, 2)
        self.assertEqual(result, "pending")

        with patch.object(self.force_close, "TRADE_MODE", "real"), \
             patch.object(self.force_close, "SPLIT_SELL_ENABLED", True), \
             patch("bot.iris_v2_force_close.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.sell_market") as mock_sell_again, \
             patch("core.kis_api.get_balance",
                   return_value={"holdings": [{"ticker": "000001", "qty": 0}]}), \
             patch("core.kis_api.get_filled_price", return_value=9550.0), \
             patch("core.kis_api.get_order_fill_qty", return_value=None):
            self.force_close.run(dry_run=False)
        mock_sell_again.assert_not_called()
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "closed")
        blended = (50 * 9600.0 + 50 * 9550.0) / 100
        self.assertAlmostEqual(positions[0]["exit_price"], blended)
        self.assertIsNone(oe.get_pending_split_sell(date))

    def test_disabled_force_close_uses_execute_full_close(self):
        date = "20260722"
        self._seed_open_position(date, entry_price=10000, shares=100)
        with patch.object(self.force_close, "TRADE_MODE", "real"), \
             patch.object(self.force_close, "SPLIT_SELL_ENABLED", False), \
             patch("bot.iris_v2_force_close.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.order_exec.execute_split_close") as mock_split, \
             patch("core.order_exec.execute_full_close", return_value=(10200, "F1")) as mock_full:
            self.force_close.run(dry_run=False)
        mock_split.assert_not_called()
        mock_full.assert_called_once()

    # ── pending exit(2026-08-07 신규) — monitor/force_close 통합 검증 ──────────
    # 2026-08-07 halt 사고 재발방지책: 매도체결 미확인이어도 즉시 halt하지 않고
    # pending 등록 후 다음 틱에서 재확인. 여기선 order_exec 레벨(TestPendingExit,
    # test_order_exec.py)이 아니라 monitor.py/force_close.py가 그 pending을
    # "우선 처리하고, 새 매도주문은 절대 다시 안 내는지"를 통합레벨로 검증한다.

    def _seed_pending_exit(self, date, kind="full", **overrides):
        """order_exec.execute_full_close/partial_sell을 실제로 호출하면 FILL_WAIT_SECS
        만큼(최대 45초) 진짜로 sleep한다(core.order_exec.time은 test_order_exec.py의
        OrderExecTestBase에서만 mock됨, 이 파일에선 아님) — 여기선 그 폴링루프 자체가
        아니라 monitor.py/force_close.py가 "이미 등록된 pending"을 다루는지만 보고
        싶으므로, 상태를 직접 심어 그 45초를 건너뛴다(2026-08-07, 처음엔 실제
        execute_full_close를 호출해 테스트 3개가 총 3분 넘게 걸렸던 걸 발견해 수정)."""
        import core.order_exec as oe
        data = {"kind": kind, "ticker": "000001", "odno": "9", "reason": "손절",
                "shares_before": 100, "entry_price": 10000, "retry_count": 0}
        if kind == "partial":
            data.update(total_shares=100, intended_qty=50)
        data.update(overrides)
        oe._set_pending_exit(date, data)

    def test_monitor_resolves_pending_exit_without_resubmitting_order(self):
        date = "20260722"
        self._seed_open_position(date, entry_price=10000, shares=100)
        import core.order_exec as oe
        self._seed_pending_exit(date)  # 이전 틱에서 매도는 냈지만 미확인이었던 상황 재현

        with patch.object(self.monitor, "TRADE_MODE", "real"), \
             patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price") as mock_price, \
             patch("core.kis_api.sell_market") as mock_sell_again, \
             patch("core.kis_api.get_balance", return_value={"holdings": []}), \
             patch("core.kis_api.get_order_fill_qty", return_value=None), \
             patch("core.kis_api.get_filled_price", return_value=9600.0):
            self.monitor.run(dry_run=False)
        mock_sell_again.assert_not_called()  # pending 재확인만 했지 새 매도주문은 절대 안 냄
        mock_price.assert_not_called()  # pending 해소가 최우선이라 정상 SL/TP 재평가 자체를 안 함
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "closed")
        self.assertEqual(positions[0]["exit_price"], 9600.0)
        self.assertEqual(positions[0]["exit_reason"], "손절")
        self.assertIsNone(oe.get_pending_exit(date))
        self.assertIsNone(oe.is_halted(date))

    def test_monitor_pending_exit_still_unconfirmed_does_not_halt_yet(self):
        # "여전히 미확인" 경로는 poll_remaining_shares가 FILL_WAIT_SECS(최대 45초)를
        # 전부 소진한 뒤에야 반환되는 실제 코드라 — 이 통합테스트에선 그 폴링 자체가
        # 아니라 monitor.py가 "pending" 응답을 받았을 때 새 주문을 안 내고 halt도
        # 안 거는지만 보고 싶으므로 core.order_exec.retry_pending_exit을 직접
        # mock(실제 폴링루프는 test_order_exec.py::TestPendingExit에서 이미 검증됨).
        date = "20260722"
        self._seed_open_position(date, entry_price=10000, shares=100)
        import core.order_exec as oe
        with patch.object(self.monitor, "TRADE_MODE", "real"), \
             patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.sell_market") as mock_sell_again, \
             patch("core.order_exec.retry_pending_exit", return_value={
                 "status": "pending", "kind": "full", "ticker": "000001",
                 "reason": "손절", "retry_count": 2}):
            self.monitor.run(dry_run=False)  # 여전히 미확인 -> pending 유지, halt 아직 아님
        mock_sell_again.assert_not_called()
        self.assertIsNone(oe.is_halted(date))
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "open")  # 아직 확정 안 됐으니 open 그대로

    def test_force_close_resumes_pending_exit_from_monitor(self):
        date = "20260722"
        self._seed_open_position(date, entry_price=10000, shares=100)
        import core.order_exec as oe
        self._seed_pending_exit(date)

        with patch.object(self.force_close, "TRADE_MODE", "real"), \
             patch("bot.iris_v2_force_close.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.sell_market") as mock_sell_again, \
             patch("core.kis_api.get_balance", return_value={"holdings": []}), \
             patch("core.kis_api.get_order_fill_qty", return_value=None), \
             patch("core.kis_api.get_filled_price", return_value=9400.0):
            self.force_close.run(dry_run=False)
        mock_sell_again.assert_not_called()  # 강제청산이 새 매도주문을 또 내면 안 됨(이중매도)
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "closed")
        self.assertEqual(positions[0]["exit_price"], 9400.0)
        self.assertEqual(positions[0]["exit_reason"], "손절")
        self.assertIsNone(oe.get_pending_exit(date))


class TestSplitBuyEntry(IrisV2TestBase):
    """entry.py의 분할매수(2026-08-08 신규, SPLIT_BUY_ENABLED) — 트랑쉬1(지정가)
    확정 즉시 그 수량만으로 포지션 open 기록 + 트랑쉬2는 pending 등록만."""

    def _seed_candidates(self, date: str, rows: list):
        import core.db as dbmod
        with dbmod.get_conn() as conn:
            for rank_no, ticker, name, prev_close, vol_ratio in rows:
                conn.execute(
                    """INSERT INTO candidates
                       (trade_date, ticker, name, sector, score, total_amt, avg_val,
                        marcap_bil, rank_no, strategy, strategy_version)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (date, ticker, name, "", vol_ratio, 1e10, prev_close, 0.0, rank_no, "iris_v2", "2.5"),
                )

    def _open_positions(self):
        import core.db as dbmod
        with dbmod.get_conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM positions WHERE strategy='iris_v2' AND status='open'").fetchall()]

    def test_tranche1_confirmed_records_position_and_registers_tranche2(self):
        date = "20260722"
        self._seed_candidates(date, [(1, "000001", "종목A", 10000, 15.0)])
        with patch.object(self.entry, "TRADE_MODE", "real"), \
             patch.object(self.entry, "SPLIT_BUY_ENABLED", True), \
             patch.object(self.entry, "_sleep_until_entry", lambda: 0.0), \
             patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 9850, "price": 9850}), \
             patch("core.order_exec.compute_buy_qty", return_value=100), \
             patch("core.order_exec.buy_safe_limit", return_value=(True, "T1", "06010")) as mock_limit, \
             patch("core.order_exec.verify_buy_fill", return_value=50), \
             patch("core.order_exec.resolve_entry_price", return_value=9860):
            self.entry.run(dry_run=False)
        mock_limit.assert_called_once()
        self.assertEqual(mock_limit.call_args.args[1], 50)  # 트랑쉬1 목표(50:50 기본비율)
        positions = self._open_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["shares"], 50)
        self.assertEqual(positions[0]["entry_price"], 9860)
        from core.order_exec import get_pending_tranche2
        pending = get_pending_tranche2(date)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["ticker"], "000001")
        self.assertEqual(pending["qty"], 50)

    def test_tranche1_order_failure_halts_no_position(self):
        date = "20260722"
        self._seed_candidates(date, [(1, "000001", "종목A", 10000, 15.0)])
        with patch.object(self.entry, "TRADE_MODE", "real"), \
             patch.object(self.entry, "SPLIT_BUY_ENABLED", True), \
             patch.object(self.entry, "_sleep_until_entry", lambda: 0.0), \
             patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 9850, "price": 9850}), \
             patch("core.order_exec.compute_buy_qty", return_value=100), \
             patch("core.order_exec.buy_safe_limit", return_value=(False, "", "")):
            self.entry.run(dry_run=False)
        self.assertEqual(len(self._open_positions()), 0)
        import core.order_exec as oe
        self.assertIsNotNone(oe.is_halted(date))
        self.assertIsNone(oe.get_pending_tranche2(date))

    def test_tranche1_unfilled_halts_no_position(self):
        date = "20260722"
        self._seed_candidates(date, [(1, "000001", "종목A", 10000, 15.0)])
        with patch.object(self.entry, "TRADE_MODE", "real"), \
             patch.object(self.entry, "SPLIT_BUY_ENABLED", True), \
             patch.object(self.entry, "_sleep_until_entry", lambda: 0.0), \
             patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 9850, "price": 9850}), \
             patch("core.order_exec.compute_buy_qty", return_value=100), \
             patch("core.order_exec.buy_safe_limit", return_value=(True, "T1", "06010")), \
             patch("core.order_exec.verify_buy_fill", return_value=0):
            self.entry.run(dry_run=False)
        self.assertEqual(len(self._open_positions()), 0)
        import core.order_exec as oe
        self.assertIsNotNone(oe.is_halted(date))

    def test_tranche1_partial_fill_does_not_register_tranche2(self):
        # 단순화 설계 결정(2026-08-08): 트랑쉬1이 목표만큼 다 안 찼으면 트랑쉬2는
        # 등록 안 함 — 언더필 그대로 허용(매수는 부족한 쪽이 항상 안전).
        date = "20260722"
        self._seed_candidates(date, [(1, "000001", "종목A", 10000, 15.0)])
        with patch.object(self.entry, "TRADE_MODE", "real"), \
             patch.object(self.entry, "SPLIT_BUY_ENABLED", True), \
             patch.object(self.entry, "_sleep_until_entry", lambda: 0.0), \
             patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 9850, "price": 9850}), \
             patch("core.order_exec.compute_buy_qty", return_value=100), \
             patch("core.order_exec.buy_safe_limit", return_value=(True, "T1", "06010")), \
             patch("core.order_exec.verify_buy_fill", return_value=30), \
             patch("core.order_exec.resolve_entry_price", return_value=9860):
            self.entry.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(positions[0]["shares"], 30)
        from core.order_exec import get_pending_tranche2
        self.assertIsNone(get_pending_tranche2(date))

    def test_tranche1_market_fallback_success_records_position_no_halt(self):
        """2026-08-08 최종검증에서 발견·수정: 지정가가 안 채워져도 buy_tranche1_
        with_fallback()이 시장가전환까지 시도해 결국 체결되면 halt 없이 정상
        기록돼야 함(예전엔 이 폴백이 없어서 지정가 미체결시 그냥 halt하고 그날
        진입을 통째로 놓쳤음 — entry.py는 이제 이 함수 하나만 호출)."""
        date = "20260722"
        self._seed_candidates(date, [(1, "000001", "종목A", 10000, 15.0)])
        with patch.object(self.entry, "TRADE_MODE", "real"), \
             patch.object(self.entry, "SPLIT_BUY_ENABLED", True), \
             patch.object(self.entry, "_sleep_until_entry", lambda: 0.0), \
             patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 9850, "price": 9850}), \
             patch("core.order_exec.compute_buy_qty", return_value=100), \
             patch("core.order_exec.buy_tranche1_with_fallback", return_value=(50, "T1")) as mock_fb, \
             patch("core.order_exec.resolve_entry_price", return_value=9900):
            self.entry.run(dry_run=False)
        mock_fb.assert_called_once_with("000001", 50, 9850)
        positions = self._open_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["shares"], 50)
        import core.order_exec as oe
        self.assertIsNone(oe.is_halted(date))

    def test_tranche1_zero_fill_even_after_fallback_halts(self):
        date = "20260722"
        self._seed_candidates(date, [(1, "000001", "종목A", 10000, 15.0)])
        with patch.object(self.entry, "TRADE_MODE", "real"), \
             patch.object(self.entry, "SPLIT_BUY_ENABLED", True), \
             patch.object(self.entry, "_sleep_until_entry", lambda: 0.0), \
             patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 9850, "price": 9850}), \
             patch("core.order_exec.compute_buy_qty", return_value=100), \
             patch("core.order_exec.buy_tranche1_with_fallback", return_value=(0, "T1")):
            self.entry.run(dry_run=False)
        self.assertEqual(len(self._open_positions()), 0)
        import core.order_exec as oe
        self.assertIsNotNone(oe.is_halted(date))

    def test_disabled_uses_normal_single_shot_market_flow(self):
        date = "20260722"
        self._seed_candidates(date, [(1, "000001", "종목A", 10000, 15.0)])
        with patch.object(self.entry, "TRADE_MODE", "real"), \
             patch.object(self.entry, "SPLIT_BUY_ENABLED", False), \
             patch.object(self.entry, "_sleep_until_entry", lambda: 0.0), \
             patch("bot.iris_v2_entry.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"open": 9850, "price": 9850}), \
             patch("core.order_exec.compute_buy_qty", return_value=100), \
             patch("core.order_exec.buy_safe_limit") as mock_limit, \
             patch("core.order_exec.buy_safe", return_value=(True, "ODNO123")), \
             patch("core.order_exec.verify_buy_fill", return_value=100), \
             patch("core.order_exec.resolve_entry_price", return_value=9860):
            self.entry.run(dry_run=False)
        mock_limit.assert_not_called()
        positions = self._open_positions()
        self.assertEqual(positions[0]["shares"], 100)
        from core.order_exec import get_pending_tranche2
        self.assertIsNone(get_pending_tranche2(date))


class TestSplitBuyMonitor(IrisV2TestBase):
    """monitor.py의 트랑쉬2 처리(2026-08-08 신규) — 09:31 첫틱에서 halt·pending_exit·
    pending_split_sell 다음으로 처리, 완료시 shares/entry_price를 가중평균으로 갱신."""

    def _seed_open_position(self, date: str, entry_price: float = 10000, shares: int = 50):
        import core.db as dbmod
        with dbmod.get_conn() as conn:
            conn.execute(
                """INSERT INTO positions
                   (ticker, name, date_open, time_open, entry_price, shares, score,
                    target_pct, stop_pct, mode, status, strategy_version, strategy)
                   VALUES ('000001','종목A',?,?,?,?,0,15,-4.5,'real','open','2.5','iris_v2')""",
                (date, "093000", entry_price, shares),
            )

    def _open_positions(self):
        import core.db as dbmod
        with dbmod.get_conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM positions WHERE strategy='iris_v2' AND status='open'").fetchall()]

    def test_tranche2_confirmed_updates_shares_and_blended_price(self):
        date = "20260722"
        self._seed_open_position(date, entry_price=10000, shares=50)
        from core.order_exec import register_pending_tranche2, get_pending_tranche2
        register_pending_tranche2(date, "000001", 50)
        with patch.object(self.monitor, "TRADE_MODE", "real"), \
             patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.order_exec.buy_safe", return_value=(True, "T2")), \
             patch("core.order_exec.verify_buy_fill", return_value=50), \
             patch("core.order_exec.resolve_entry_price", return_value=10200):
            self.monitor.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(positions[0]["shares"], 100)
        expected_avg = (50 * 10000 + 50 * 10200) / 100
        self.assertAlmostEqual(positions[0]["entry_price"], expected_avg, places=2)
        self.assertIsNone(get_pending_tranche2(date))

    def test_tranche2_pending_skips_sltp_evaluation_this_tick(self):
        date = "20260722"
        self._seed_open_position(date, entry_price=10000, shares=50)
        from core.order_exec import register_pending_tranche2
        register_pending_tranche2(date, "000001", 50)
        with patch.object(self.monitor, "TRADE_MODE", "real"), \
             patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.order_exec.buy_safe", return_value=(True, "T2")), \
             patch("core.order_exec.verify_buy_fill", return_value=0), \
             patch("core.kis_api.get_price") as mock_price:
            self.monitor.run(dry_run=False)
        mock_price.assert_not_called()  # 이번 틱엔 SL/TP 평가 자체가 안 일어남
        positions = self._open_positions()
        self.assertEqual(positions[0]["shares"], 50)

    def test_tranche2_confirmed_but_position_missing_alerts_instead_of_silent_log(self):
        """2026-08-08 코드감사 신규 — 트랑쉬2가 done으로 확정됐는데 positions에
        해당 open 포지션이 없는(이론상 불가능해야 하는) 비정상 상태면, 예전엔
        로그만 남기고 조용히 넘어갔음(실제 계좌엔 주식이 남는데 아무도 모름) —
        이제 반드시 텔레그램으로도 알려야 함."""
        date = "20260722"
        # 의도적으로 open 포지션을 안 심음 — "포지션을 못 찾음" 분기를 재현.
        from core.order_exec import register_pending_tranche2
        register_pending_tranche2(date, "000001", 50)
        with patch.object(self.monitor, "TRADE_MODE", "real"), \
             patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.order_exec.retry_pending_entry_tranche",
                   return_value={"status": "done", "ticker": "000001", "qty": 50, "odno": "T2"}), \
             patch("core.notify.send_telegram") as mock_tg:
            self.monitor.run(dry_run=False)
        mock_tg.assert_called_once()
        self.assertIn("비정상", mock_tg.call_args[0][0])

    def test_tranche2_gives_up_after_retries_resumes_with_tranche1_only_no_halt(self):
        date = "20260722"
        self._seed_open_position(date, entry_price=10000, shares=50)
        from core.order_exec import register_pending_tranche2, get_pending_tranche2
        import core.order_exec as oe
        register_pending_tranche2(date, "000001", 50)
        with patch.object(self.monitor, "TRADE_MODE", "real"), \
             patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.order_exec.buy_safe", return_value=(True, "T2")), \
             patch("core.order_exec.verify_buy_fill", return_value=0), \
             patch("core.notify.send_telegram") as mock_tg:
            for _ in range(oe.PENDING_RETRY_MAX):
                self.monitor.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(positions[0]["shares"], 50)  # 트랑쉬1만으로 확정
        self.assertIsNone(get_pending_tranche2(date))
        self.assertIsNone(oe.is_halted(date))  # halt 아님(매수 언더필=안전 원칙)
        mock_tg.assert_called()


class TestSplitSellMonitor(IrisV2TestBase):
    """monitor.py의 분할매도(2026-08-08 신규, SPLIT_SELL_ENABLED) — SL/TP/중간청산
    전량청산시 execute_split_close를 타는지, pending_split_sell이 halt·pending_exit
    다음으로 처리되는지 검증. 강제청산·부분익절(+8%)은 이 경로를 안 탐(설계 결정)."""

    def _seed_open_position(self, date: str, entry_price: float = 10000, shares: int = 100):
        import core.db as dbmod
        with dbmod.get_conn() as conn:
            conn.execute(
                """INSERT INTO positions
                   (ticker, name, date_open, time_open, entry_price, shares, score,
                    target_pct, stop_pct, mode, status, strategy_version, strategy)
                   VALUES ('000001','종목A',?,?,?,?,0,15,-4.5,'real','open','2.5','iris_v2')""",
                (date, "093000", entry_price, shares),
            )

    def _open_positions(self):
        import core.db as dbmod
        with dbmod.get_conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM positions WHERE strategy='iris_v2'").fetchall()]

    def test_sl_triggers_execute_split_close_instead_of_full_close(self):
        date = "20260722"
        self._seed_open_position(date, entry_price=10000, shares=100)
        with patch.object(self.monitor, "TRADE_MODE", "real"), \
             patch.object(self.monitor, "SPLIT_SELL_ENABLED", True), \
             patch.object(self.monitor, "SPLIT_SELL_TRANCHES", 2), \
             patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("bot.iris_v2_monitor.now_kst", return_value=datetime(2026, 7, 22, 10, 5, tzinfo=KST)), \
             patch("core.kis_api.get_price", return_value={"open": 9500, "price": 9500}), \
             patch("core.order_exec.execute_split_close", return_value=(9480.0, "S2")) as mock_split, \
             patch("core.order_exec.execute_full_close") as mock_full:
            self.monitor.run(dry_run=False)
        mock_split.assert_called_once_with("000001", 100, 10000, "손절", date, 2)
        mock_full.assert_not_called()
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "closed")
        self.assertEqual(positions[0]["exit_price"], 9480.0)

    def test_split_sell_pending_registers_and_position_stays_open(self):
        date = "20260722"
        self._seed_open_position(date, entry_price=10000, shares=100)
        with patch.object(self.monitor, "TRADE_MODE", "real"), \
             patch.object(self.monitor, "SPLIT_SELL_ENABLED", True), \
             patch.object(self.monitor, "SPLIT_SELL_TRANCHES", 2), \
             patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("bot.iris_v2_monitor.now_kst", return_value=datetime(2026, 7, 22, 10, 5, tzinfo=KST)), \
             patch("core.kis_api.get_price", return_value={"open": 9500, "price": 9500}), \
             patch("core.order_exec.execute_split_close", return_value="pending"):
            self.monitor.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "open")  # 아직 안 닫힘
        self.assertEqual(positions[0]["shares"], 100)  # 잔량 그대로(트랑쉬 내부에서만 진행중)

    def test_pending_split_sell_resolves_on_next_tick(self):
        date = "20260722"
        self._seed_open_position(date, entry_price=10000, shares=100)
        with patch("core.kis_api.sell_market", return_value={"output": {"ODNO": "S1"}}), \
             patch("core.kis_api.get_balance",
                   return_value={"holdings": [{"ticker": "000001", "qty": 50}]}), \
             patch("core.kis_api.get_filled_price", return_value=9480.0), \
             patch("core.kis_api.get_order_fill_qty", return_value=None):
            import core.order_exec as oe
            result = oe.execute_split_close("000001", 100, 10000, "손절", date, 2)
        self.assertEqual(result, "pending")

        with patch.object(self.monitor, "TRADE_MODE", "real"), \
             patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_balance",
                   return_value={"holdings": [{"ticker": "000001", "qty": 0}]}), \
             patch("core.kis_api.get_filled_price", return_value=9450.0), \
             patch("core.kis_api.get_order_fill_qty", return_value=None):
            self.monitor.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "closed")
        blended = (50 * 9480.0 + 50 * 9450.0) / 100
        self.assertAlmostEqual(positions[0]["exit_price"], blended)

    def test_disabled_uses_normal_execute_full_close(self):
        date = "20260722"
        self._seed_open_position(date, entry_price=10000, shares=100)
        with patch.object(self.monitor, "TRADE_MODE", "real"), \
             patch.object(self.monitor, "SPLIT_SELL_ENABLED", False), \
             patch("bot.iris_v2_monitor.today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("bot.iris_v2_monitor.now_kst", return_value=datetime(2026, 7, 22, 10, 5, tzinfo=KST)), \
             patch("core.kis_api.get_price", return_value={"open": 9500, "price": 9500}), \
             patch("core.order_exec.execute_split_close") as mock_split, \
             patch("core.order_exec.execute_full_close", return_value=(9480.0, "F1")) as mock_full:
            self.monitor.run(dry_run=False)
        mock_split.assert_not_called()
        mock_full.assert_called_once()


class TestCandidateSelection(IrisV2TestBase):
    """v2.3 필터(VOL_RATIO_MIN·STOCK_MA_WINDOW/REQUIRE_ABOVE_STOCK_MA·MOM5_MIN) 검증.
    price_daily를 70거래일치 시딩해 종목별 이평선 위/아래·vol_ratio·모멘텀 크기를 통제한다.
    각 테스트는 검증대상이 아닌 다른 필터를 명시적으로 무력화해 하나씩만 격리 검증한다."""

    def setUp(self):
        super().setUp()
        import scripts.iris_v2_candidates as candidates
        self.candidates = candidates
        # next_trading_date()는 내부적으로 core.kis_api.get_trading_days(실네트워크)를 시도함 —
        # 테스트에서는 그냥 다음날 날짜 문자열만 있으면 되므로 고정값으로 패치
        patch.object(candidates, "next_trading_date", return_value="20260610").start()
        # v2.5.7: build_candidates()가 이제 데이터신선도 체크(is_trading_day 호출)를
        # 함 — 여기 테스트들은 seed한 과거날짜가 "오늘"과 다른 게 당연하므로(신선도
        # 체크 대상이 아님), is_trading_day를 False로 고정해 그 체크 자체를 우회
        # (안 그러면 실제 KIS API 호출 + 실제 텔레그램 CRITICAL 알림이 나갈 뻔했음 —
        # process_pending_commands와 같은 함정, 여기서도 재확인).
        patch("core.kis_api.is_trading_day", return_value=False).start()
        self.addCleanup(patch.stopall)

    def _seed_ticker(self, ticker: str, name: str, market: str, latest_close: float,
                      baseline_close: float = 10000, latest_volume: int = 100_000,
                      baseline_volume: int = 1_000, n_days: int = 70, sector: str = ""):
        """baseline_close/volume으로 n_days-1일 채우고 마지막 날만 latest_*로 설정 —
        latest_close가 baseline보다 높으면 50일선 위 + mom5 양수, 낮으면 반대가 되도록 통제
        (마지막 5거래일 전까지도 baseline이라 mom5 = latest_close/baseline_close - 1)."""
        import core.db as dbmod
        from datetime import datetime, timedelta
        base_date = datetime(2026, 4, 1)
        dates = [(base_date + timedelta(days=i)).strftime("%Y%m%d") for i in range(n_days)]
        with dbmod.get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO universe(ticker, name, market, sector, updated) VALUES (?,?,?,?,?)",
                (ticker, name, market, sector, "20260721"),
            )
            for i, d in enumerate(dates):
                is_latest = i == len(dates) - 1
                close = latest_close if is_latest else baseline_close
                volume = latest_volume if is_latest else baseline_volume
                conn.execute(
                    "INSERT OR REPLACE INTO price_daily(ticker,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)",
                    (ticker, d, close, close, close, close, volume),
                )
        return dates[-1]  # latest_date

    def test_stock_below_own_ma_excluded(self):
        # MA필터만 격리(mom5·vol_ratio는 무력화) — 8000<baseline10000이므로 50일선 아래
        with patch.object(self.candidates, "REQUIRE_ABOVE_STOCK_MA", True), \
             patch.object(self.candidates, "MOM5_MIN", -1.0), \
             patch.object(self.candidates, "VOL_RATIO_MIN", 0.0):
            self._seed_ticker("000001", "하락종목", "KOSDAQ", latest_close=8000,
                               latest_volume=2_000_000, baseline_volume=1_000)  # tv=160억
            target_date, cand = self.candidates.build_candidates()
        self.assertNotIn("000001", cand["ticker"].tolist())

    def test_stock_below_own_ma_included_when_filter_off(self):
        with patch.object(self.candidates, "REQUIRE_ABOVE_STOCK_MA", False), \
             patch.object(self.candidates, "MOM5_MIN", -1.0), \
             patch.object(self.candidates, "VOL_RATIO_MIN", 0.0):
            self._seed_ticker("000001", "하락종목", "KOSDAQ", latest_close=8000,
                               latest_volume=2_000_000, baseline_volume=1_000)
            target_date, cand = self.candidates.build_candidates()
        self.assertIn("000001", cand["ticker"].tolist())

    def test_stock_above_own_ma_included(self):
        # baseline 10000 유지하다 마지막날만 12000으로 급등 -> 50일선 위 + mom5 20%(기본필터 통과)
        self._seed_ticker("000002", "상승종목", "KOSDAQ", latest_close=12000,
                           latest_volume=1_000_000, baseline_volume=1_000)  # tv=120억, vr~20
        target_date, cand = self.candidates.build_candidates()
        self.assertIn("000002", cand["ticker"].tolist())

    def test_low_vol_ratio_excluded_when_floor_set(self):
        with patch.object(self.candidates, "VOL_RATIO_MIN", 5.0), \
             patch.object(self.candidates, "REQUIRE_ABOVE_STOCK_MA", False), \
             patch.object(self.candidates, "MOM5_MIN", -1.0):
            self._seed_ticker("000003", "저vol_ratio종목", "KOSDAQ", latest_close=11000,
                               latest_volume=950_000, baseline_volume=850_000)  # tv~104.5억, vr~1.1
            target_date, cand = self.candidates.build_candidates()
        self.assertNotIn("000003", cand["ticker"].tolist())

    def test_low_vol_ratio_included_when_floor_off(self):
        with patch.object(self.candidates, "VOL_RATIO_MIN", 0.0), \
             patch.object(self.candidates, "REQUIRE_ABOVE_STOCK_MA", False), \
             patch.object(self.candidates, "MOM5_MIN", -1.0):
            self._seed_ticker("000003", "저vol_ratio종목", "KOSDAQ", latest_close=11000,
                               latest_volume=950_000, baseline_volume=850_000)
            target_date, cand = self.candidates.build_candidates()
        self.assertIn("000003", cand["ticker"].tolist())

    def test_low_mom5_excluded(self):
        # MA·vol_ratio는 통과하도록(살짝 상승 5%) 하되 MOM5_MIN(10%)에는 못미침
        with patch.object(self.candidates, "REQUIRE_ABOVE_STOCK_MA", False):
            self._seed_ticker("000007", "약한상승종목", "KOSDAQ", latest_close=10500,
                               latest_volume=1_000_000, baseline_volume=1_000)  # mom5=5%, tv=105억
            target_date, cand = self.candidates.build_candidates()
        self.assertNotIn("000007", cand["ticker"].tolist())

    def test_high_mom5_included(self):
        # mom5=20%(>=10%)면 다른 조건(TV·vol_ratio·MA) 통과 시 포함
        self._seed_ticker("000008", "강한상승종목", "KOSDAQ", latest_close=12000,
                           latest_volume=1_000_000, baseline_volume=1_000)  # mom5=20%, tv=120억
        target_date, cand = self.candidates.build_candidates()
        self.assertIn("000008", cand["ticker"].tolist())

    def test_low_trade_value_excluded(self):
        # 이평선 위 + vol_ratio·mom5 충분해도 거래대금 자체가 50억 미만이면 제외
        # (10500*300000=31.5억 < TV_MIN 50억)
        self._seed_ticker("000004", "소형주", "KOSDAQ", latest_close=10500,
                           latest_volume=300_000, baseline_volume=1_000)
        target_date, cand = self.candidates.build_candidates()
        self.assertNotIn("000004", cand["ticker"].tolist())

    def test_etf_excluded_even_if_otherwise_qualifies(self):
        # TV·vol_ratio·이평선·모멘텀 전부 통과하도록 세팅해도 sector가 ETF면 제외돼야 함
        self._seed_ticker("069500", "KODEX 200", "KOSDAQ", latest_close=12000,
                           latest_volume=1_000_000, baseline_volume=1_000, sector="ETF(실물복제/수익증권)")
        target_date, cand = self.candidates.build_candidates()
        self.assertNotIn("069500", cand["ticker"].tolist())

    def test_rank_no_assigned_by_vol_ratio_desc(self):
        # 둘 다 TV·이평선·모멘텀 통과하되, A(vr~8.8)가 B(vr~6.0)보다 순위가 높아야 함
        self._seed_ticker("000005", "종목A", "KOSDAQ", latest_close=12000,
                           latest_volume=1_500_000, baseline_volume=100_000)  # tv=180억, vr~8.8, mom5=20%
        self._seed_ticker("000006", "종목B", "KOSDAQ", latest_close=13000,
                           latest_volume=820_000, baseline_volume=100_000)  # tv~106.6억, vr~6.0, mom5=30%
        target_date, cand = self.candidates.build_candidates()
        cand = cand.set_index("ticker")
        self.assertIn("000005", cand.index)
        self.assertIn("000006", cand.index)
        self.assertLess(cand.loc["000005", "rank_no"], cand.loc["000006", "rank_no"])

    def test_kospi_excluded_when_kosdaq_only_filter_on(self):
        # v2.4: 다른 필터는 전부 통과해도 market=KOSPI면 REQUIRE_KOSDAQ_ONLY로 제외돼야 함
        self._seed_ticker("000009", "코스피종목", "KOSPI", latest_close=12000,
                           latest_volume=1_000_000, baseline_volume=1_000)  # mom5=20%, tv=120억
        target_date, cand = self.candidates.build_candidates()
        self.assertNotIn("000009", cand["ticker"].tolist())

    def test_kospi_included_when_kosdaq_only_filter_off(self):
        with patch.object(self.candidates, "REQUIRE_KOSDAQ_ONLY", False):
            self._seed_ticker("000009", "코스피종목", "KOSPI", latest_close=12000,
                               latest_volume=1_000_000, baseline_volume=1_000)
            target_date, cand = self.candidates.build_candidates()
        self.assertIn("000009", cand["ticker"].tolist())

    def _seed_price_only_no_universe(self, ticker: str, latest_close: float,
                                      baseline_close: float = 10000, latest_volume: int = 1_000_000,
                                      baseline_volume: int = 1_000, n_days: int = 70):
        """universe에는 없고 price_daily에만 있는 티커 재현(신규상장 타이밍 어긋남 등)."""
        import core.db as dbmod
        from datetime import datetime, timedelta
        base_date = datetime(2026, 4, 1)
        dates = [(base_date + timedelta(days=i)).strftime("%Y%m%d") for i in range(n_days)]
        with dbmod.get_conn() as conn:
            for i, d in enumerate(dates):
                is_latest = i == len(dates) - 1
                close = latest_close if is_latest else baseline_close
                volume = latest_volume if is_latest else baseline_volume
                conn.execute(
                    "INSERT OR REPLACE INTO price_daily(ticker,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)",
                    (ticker, d, close, close, close, close, volume),
                )

    def test_ticker_missing_from_universe_excluded_without_crash(self):
        # 2026-07-22 발견: universe에 없는 티커가 섞여 있으면 name/sector가 NaN으로
        # merge돼 is_excluded()가 TypeError로 저녁 배치 전체를 죽였음(수정 전). 지금은
        # 크래시 없이 해당 티커만 조용히 배제되고, 정상 티커는 그대로 후보에 남아야 함.
        self._seed_price_only_no_universe("000099", latest_close=12000,
                                           latest_volume=1_000_000, baseline_volume=1_000)
        self._seed_ticker("000002", "정상종목", "KOSDAQ", latest_close=12000,
                           latest_volume=1_000_000, baseline_volume=1_000)
        target_date, cand = self.candidates.build_candidates()  # 예외 없이 완료돼야 함
        self.assertNotIn("000099", cand["ticker"].tolist())
        self.assertIn("000002", cand["ticker"].tolist())


class TestMarketBreadth(IrisV2TestBase):
    """core/market_breadth.py — 2026-08-05 신규. v2.5 후보풀이 왜 이 크기인지
    맥락을 주는 KOSDAQ 50일선 위 비율 계산(diag CLI·저녁 후보알림 공용)."""

    def _seed(self, ticker: str, name: str, latest_close: float, baseline_close: float = 10000,
               n_days: int = 60):
        import core.db as dbmod
        from datetime import datetime, timedelta
        base_date = datetime(2026, 4, 1)
        dates = [(base_date + timedelta(days=i)).strftime("%Y%m%d") for i in range(n_days)]
        with dbmod.get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO universe(ticker, name, market, sector, updated) VALUES (?,?,?,?,?)",
                (ticker, name, "KOSDAQ", "", "20260721"),
            )
            for i, d in enumerate(dates):
                close = latest_close if i == len(dates) - 1 else baseline_close
                conn.execute(
                    "INSERT OR REPLACE INTO price_daily(ticker,date,open,high,low,close,volume) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (ticker, d, close, close, close, close, 1000),
                )
        return dates[-1]

    def test_computes_correct_fraction(self):
        from core.market_breadth import kosdaq_above_ma50_pct
        from core.db import get_conn
        d = self._seed("000001", "위종목", latest_close=12000, baseline_close=10000)
        self._seed("000002", "아래종목", latest_close=8000, baseline_close=10000)
        with get_conn() as conn:
            frac = kosdaq_above_ma50_pct(conn, d)
        self.assertAlmostEqual(frac, 0.5, places=2)

    def test_kospi_excluded(self):
        from core.market_breadth import kosdaq_above_ma50_pct
        from core.db import get_conn
        d = self._seed("000001", "코스닥종목", latest_close=12000, baseline_close=10000)
        with get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO universe(ticker,name,market,sector,updated) VALUES (?,?,?,?,?)",
                ("000003", "코스피종목", "KOSPI", "", "20260721"))
            conn.execute(
                "INSERT OR REPLACE INTO price_daily(ticker,date,open,high,low,close,volume) "
                "VALUES ('000003',?,8000,8000,8000,8000,1000)", (d,))
        with get_conn() as conn:
            frac = kosdaq_above_ma50_pct(conn, d)
        self.assertAlmostEqual(frac, 1.0, places=2)  # 코스피종목(마이너스 방향)은 제외돼야 함

    def test_no_data_returns_none(self):
        from core.market_breadth import kosdaq_above_ma50_pct
        from core.db import get_conn
        with get_conn() as conn:
            frac = kosdaq_above_ma50_pct(conn, "20260101")
        self.assertIsNone(frac)

    def test_notify_prepends_breadth_line(self):
        """scripts.iris_v2_candidates.notify()가 텔레그램 메시지 맨 앞에 시장여건
        한 줄을 붙이는지 확인(2026-08-05 신규, 빈 후보/일반 후보 양쪽 경로 다)."""
        import pandas as pd
        import scripts.iris_v2_candidates as candidates
        self._seed("000001", "위종목", latest_close=12000, baseline_close=10000)
        cand = pd.DataFrame([{"rank_no": 1, "ticker": "000002", "name": "종목A", "vol_ratio": 3.0}])
        with patch("core.notify.send_telegram") as mock_tg:
            candidates.notify("20260610", cand)
        msg = mock_tg.call_args[0][0]
        self.assertIn("시장여건", msg)
        self.assertIn("KOSDAQ 50일선 위", msg)

        with patch("core.notify.send_telegram") as mock_tg:
            candidates.notify("20260610", pd.DataFrame())
        msg = mock_tg.call_args[0][0]
        self.assertIn("시장여건", msg)  # 후보 0건이어도 붙어야 함


class TestLiquidityFilter(IrisV2TestBase):
    """core.market_breadth.kosdaq_liquidity_percentile() + build_candidates() 연동
    (v2.9 신규, 2026-08-11). iris DECISIONS (67차)(68차) — 전일 KOSDAQ 총거래대금이
    하위25%ile인 날은 무진입."""

    def _seed_market_days(self, ticker: str, n_days: int, close: float, volume: int,
                            last_day_volume: int | None = None, start=None):
        """단일 티커로 n_days치 KOSDAQ 시세를 채운다. last_day_volume이 주어지면
        마지막 날만 그 거래량으로(시장 전체 유동성을 인위적으로 낮추는 용도)."""
        import core.db as dbmod
        from datetime import datetime, timedelta
        base_date = start or datetime(2026, 1, 1)
        dates = [(base_date + timedelta(days=i)).strftime("%Y%m%d") for i in range(n_days)]
        with dbmod.get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO universe(ticker, name, market, sector, updated) VALUES (?,?,?,?,?)",
                (ticker, f"더미{ticker}", "KOSDAQ", "", "20260721"),
            )
            for i, d in enumerate(dates):
                vol = volume
                if last_day_volume is not None and i == len(dates) - 1:
                    vol = last_day_volume
                conn.execute(
                    "INSERT OR REPLACE INTO price_daily(ticker,date,open,high,low,close,volume) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (ticker, d, close, close, close, close, vol),
                )
        return dates[-1]

    def _seed_ticker(self, ticker: str, name: str, latest_close: float,
                      baseline_close: float = 10000, latest_volume: int = 1_000_000,
                      baseline_volume: int = 1_000, n_days: int = 70):
        """iris_v2_candidates의 다른 필터(TV/vol_ratio/mom5/MA)를 전부 통과하는
        후보를 하나 만든다(TestCandidateSelection의 test_high_mom5_included와 동일
        규모: tv~120억, vr~1000, mom5~20%)."""
        import core.db as dbmod
        from datetime import datetime, timedelta
        base_date = datetime(2026, 1, 1)
        dates = [(base_date + timedelta(days=i)).strftime("%Y%m%d") for i in range(n_days)]
        with dbmod.get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO universe(ticker, name, market, sector, updated) VALUES (?,?,?,?,?)",
                (ticker, name, "KOSDAQ", "", "20260721"),
            )
            for i, d in enumerate(dates):
                is_latest = i == len(dates) - 1
                close = latest_close if is_latest else baseline_close
                volume = latest_volume if is_latest else baseline_volume
                conn.execute(
                    "INSERT OR REPLACE INTO price_daily(ticker,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)",
                    (ticker, d, close, close, close, close, volume),
                )
        return dates[-1]

    def test_percentile_none_when_no_data(self):
        from core.market_breadth import kosdaq_liquidity_percentile
        from core.db import get_conn
        with get_conn() as conn:
            pct = kosdaq_liquidity_percentile(conn, "20260101")
        self.assertIsNone(pct)

    def test_percentile_none_when_insufficient_periods(self):
        from core.market_breadth import kosdaq_liquidity_percentile
        from core.db import get_conn
        d = self._seed_market_days("900001", n_days=30, close=10000, volume=1000)  # min_periods=60 미달
        with get_conn() as conn:
            pct = kosdaq_liquidity_percentile(conn, d, lookback_days=250, min_periods=60)
        self.assertIsNone(pct)

    def test_percentile_low_when_last_day_lowest(self):
        from core.market_breadth import kosdaq_liquidity_percentile
        from core.db import get_conn
        # 69일은 대량거래(close*volume 큼), 마지막날만 거래량이 확 줄어든 시장상황
        d = self._seed_market_days("900002", n_days=70, close=100000, volume=10_000_000,
                                     last_day_volume=100_000)
        with get_conn() as conn:
            pct = kosdaq_liquidity_percentile(conn, d, lookback_days=250, min_periods=60)
        self.assertLess(pct, 0.05)  # 마지막날이 사실상 최저치라 백분위 0에 가까움

    def test_percentile_high_when_last_day_highest(self):
        from core.market_breadth import kosdaq_liquidity_percentile
        from core.db import get_conn
        d = self._seed_market_days("900003", n_days=70, close=10000, volume=1_000,
                                     last_day_volume=100_000_000)
        with get_conn() as conn:
            pct = kosdaq_liquidity_percentile(conn, d, lookback_days=250, min_periods=60)
        self.assertGreater(pct, 0.95)

    def test_percentile_frozen_after_universe_update(self):
        """2026-08-17 실거래 중 발견한 버그의 재발방지 테스트 — 한 번 계산된
        과거 날짜의 유동성 백분위는, 그 이후 universe가 갱신돼(종목 추가/제외)도
        다시 계산되면 안 됨(캐시된 값을 그대로 재사용해야 함). 실제 사고: 같은
        08-14 데이터가 금요일저녁 계산 땐 하위19%ile, 월요일저녁(유니버스 갱신
        직후) 재계산 땐 하위25%ile로 갈려 필터 발동여부 자체가 뒤집혔음."""
        from core.market_breadth import kosdaq_liquidity_percentile
        from core.db import get_conn
        d = self._seed_market_days("900005", n_days=70, close=10000, volume=1_000,
                                     last_day_volume=100_000_000)
        with get_conn() as conn:
            pct_before = kosdaq_liquidity_percentile(conn, d, lookback_days=250, min_periods=60)

        # universe 갱신 시뮬레이션 — 거래대금이 훨씬 큰 신규종목이 KOSDAQ에 추가됨
        # (실제로는 매주 월요일 collect_universe가 종목추가/제외·재분류를 함)
        import core.db as dbmod
        with dbmod.get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO universe(ticker, name, market, sector, updated) VALUES (?,?,?,?,?)",
                ("900099", "신규대형종목", "KOSDAQ", "", d),
            )
            conn.execute(
                "INSERT OR REPLACE INTO price_daily(ticker,date,open,high,low,close,volume) "
                "VALUES (?,?,?,?,?,?,?)",
                ("900099", d, 100000, 100000, 100000, 100000, 50_000_000),  # 압도적으로 큰 거래대금
            )

        with get_conn() as conn:
            pct_after = kosdaq_liquidity_percentile(conn, d, lookback_days=250, min_periods=60)

        self.assertEqual(pct_before, pct_after,
                          "universe 갱신 후에도 이미 계산된 과거 날짜의 백분위는 그대로여야 함")

    def test_build_candidates_skips_when_liquidity_low(self):
        """시장 전체 유동성이 바닥인 날은, 개별 종목이 다른 필터를 전부 통과해도
        후보 자체가 비어야 함(무진입)."""
        import scripts.iris_v2_candidates as candidates
        with patch.object(candidates, "next_trading_date", return_value="20260610"), \
             patch("core.kis_api.is_trading_day", return_value=False):
            # 더미 대형 유동성 티커 — 마지막날만 거래량 급감시켜 시장전체 유동성을 바닥으로
            self._seed_market_days("900004", n_days=70, close=100000, volume=10_000_000,
                                     last_day_volume=100_000)
            # 이 종목 자체는 다른 필터를 전부 통과하도록(하지만 유동성필터에 막혀야 함)
            self._seed_ticker("000008", "강한상승종목", latest_close=12000,
                               latest_volume=1_000_000, baseline_volume=1_000)
            target_date, cand = candidates.build_candidates()
        self.assertTrue(cand.empty)
        from core.db import state_get
        reason = state_get(f"iris_v2_liquidity_skip_{target_date}")
        self.assertIsNotNone(reason)
        self.assertIn("매매하지 않습니다", reason)  # v2.9: 사용자용 문구로 정리(2026-08-11)

    def test_build_candidates_not_blocked_when_filter_disabled(self):
        """같은 저유동성 상황이라도 LIQUIDITY_FILTER_ENABLED=False면 막지 않아야 함."""
        import scripts.iris_v2_candidates as candidates
        with patch.object(candidates, "next_trading_date", return_value="20260610"), \
             patch("core.kis_api.is_trading_day", return_value=False), \
             patch.object(candidates, "LIQUIDITY_FILTER_ENABLED", False):
            self._seed_market_days("900004", n_days=70, close=100000, volume=10_000_000,
                                     last_day_volume=100_000)
            self._seed_ticker("000008", "강한상승종목", latest_close=12000,
                               latest_volume=1_000_000, baseline_volume=1_000)
            target_date, cand = candidates.build_candidates()
        self.assertIn("000008", cand["ticker"].tolist())

    def test_build_candidates_not_blocked_on_normal_liquidity(self):
        """개별종목 하나만 있는 평범한 상황(기존 TestCandidateSelection과 동일 패턴)—
        그 종목 자신의 마지막날 거래량 급등이 시장전체 집계에서도 최고치가 되므로
        유동성필터가 오작동으로 막으면 안 됨(회귀 방지)."""
        import scripts.iris_v2_candidates as candidates
        with patch.object(candidates, "next_trading_date", return_value="20260610"), \
             patch("core.kis_api.is_trading_day", return_value=False):
            self._seed_ticker("000008", "강한상승종목", latest_close=12000,
                               latest_volume=1_000_000, baseline_volume=1_000)
            target_date, cand = candidates.build_candidates()
        self.assertIn("000008", cand["ticker"].tolist())

    def test_notify_shows_liquidity_reason_when_state_set(self):
        import pandas as pd
        import scripts.iris_v2_candidates as candidates
        from core.db import state_set
        state_set("iris_v2_liquidity_skip_20260610", "전일(20260609) KOSDAQ 총거래대금이 하위3.0%ile — 유동성필터로 무진입")
        with patch("core.notify.send_telegram") as mock_tg:
            candidates.notify("20260610", pd.DataFrame())
        msg = mock_tg.call_args[0][0]
        self.assertIn("유동성필터", msg)

    def test_notify_prepends_liquidity_line(self):
        """notify()가 후보 있음/없음 양쪽 다 유동성 백분위 한 줄을 붙이는지 확인
        (2026-08-11 신규, _breadth_line()과 동일 패턴 — 사용자 요청으로 추가)."""
        import pandas as pd
        import scripts.iris_v2_candidates as candidates
        d = self._seed_market_days("900005", n_days=70, close=10000, volume=1_000_000)
        cand = pd.DataFrame([{"rank_no": 1, "ticker": "000002", "name": "종목A", "vol_ratio": 3.0}])
        with patch("core.notify.send_telegram") as mock_tg:
            candidates.notify(d, cand)
        msg = mock_tg.call_args[0][0]
        self.assertIn("유동성:", msg)
        self.assertTrue("상위" in msg or "하위" in msg)  # v2.9.1: "%ile" 대신 상위/하위 X% 표기


class TestDataFreshness(unittest.TestCase):
    """scripts/iris_v2_candidates.check_data_freshness — v2.5.7 신규."""

    def test_fresh_data_on_trading_day_is_ok(self):
        import scripts.iris_v2_candidates as candidates
        with patch.object(candidates, "today_kst", return_value="20260724"), \
             patch("core.kis_api.is_trading_day", return_value=True):
            self.assertTrue(candidates.check_data_freshness("20260724"))

    def test_stale_data_on_trading_day_is_not_ok(self):
        import scripts.iris_v2_candidates as candidates
        with patch.object(candidates, "today_kst", return_value="20260724"), \
             patch("core.kis_api.is_trading_day", return_value=True):
            self.assertFalse(candidates.check_data_freshness("20260722"))

    def test_stale_data_on_holiday_is_ok(self):
        """오늘이 휴장일이면 어제 데이터 그대로가 정상 — 이상 아님."""
        import scripts.iris_v2_candidates as candidates
        with patch.object(candidates, "today_kst", return_value="20260725"), \
             patch("core.kis_api.is_trading_day", return_value=False):
            self.assertTrue(candidates.check_data_freshness("20260724"))

    def test_build_candidates_alerts_on_stale_data(self):
        """실제 build_candidates() 호출 경로에서도 신선도 이상시 CRITICAL 텔레그램이
        나가야 함(동기화 실패를 조용히 넘기지 않도록)."""
        import scripts.iris_v2_candidates as candidates
        import tempfile
        from pathlib import Path
        tmpdir = tempfile.TemporaryDirectory()
        import core.db as dbmod
        orig_db_path = dbmod.DB_PATH
        dbmod.DB_PATH = Path(tmpdir.name) / "test.db"
        dbmod.init_db()
        try:
            with dbmod.get_conn() as conn:
                # 오래된 날짜(20260722)로 최소 1행 시딩 -- "동기화가 며칠째 안 됨" 재현
                conn.execute(
                    "INSERT INTO price_daily(ticker,date,open,high,low,close,volume) "
                    "VALUES ('000001','20260722',1000,1000,1000,1000,100)"
                )
                conn.execute(
                    "INSERT INTO universe(ticker,name,market,sector,updated) "
                    "VALUES ('000001','종목A','KOSDAQ','',20260722)"
                )
            with patch.object(candidates, "next_trading_date", return_value="20260610"), \
                 patch.object(candidates, "today_kst", return_value="20260724"), \
                 patch("core.kis_api.is_trading_day", return_value=True), \
                 patch("core.notify.send_telegram") as mock_tg:
                candidates.build_candidates()
            mock_tg.assert_called_once()
            self.assertIn("데이터 신선도", mock_tg.call_args.args[0])
        finally:
            dbmod.DB_PATH = orig_db_path
            tmpdir.cleanup()

    def test_build_candidates_dry_run_suppresses_freshness_alert(self):
        """2026-08-12 발견: --dry-run 점검 호출도 신선도 이상시 진짜 텔레그램을
        보내고 있었음(저녁 동기화 전 낮시간 점검마다 오탐 CRITICAL 발생) — dry_run=True면
        로그는 남기되 텔레그램은 나가면 안 된다."""
        import scripts.iris_v2_candidates as candidates
        import tempfile
        from pathlib import Path
        tmpdir = tempfile.TemporaryDirectory()
        import core.db as dbmod
        orig_db_path = dbmod.DB_PATH
        dbmod.DB_PATH = Path(tmpdir.name) / "test.db"
        dbmod.init_db()
        try:
            with dbmod.get_conn() as conn:
                conn.execute(
                    "INSERT INTO price_daily(ticker,date,open,high,low,close,volume) "
                    "VALUES ('000001','20260722',1000,1000,1000,1000,100)"
                )
                conn.execute(
                    "INSERT INTO universe(ticker,name,market,sector,updated) "
                    "VALUES ('000001','종목A','KOSDAQ','',20260722)"
                )
            with patch.object(candidates, "next_trading_date", return_value="20260610"), \
                 patch.object(candidates, "today_kst", return_value="20260724"), \
                 patch("core.kis_api.is_trading_day", return_value=True), \
                 patch("core.notify.send_telegram") as mock_tg:
                candidates.build_candidates(dry_run=True)
            mock_tg.assert_not_called()
        finally:
            dbmod.DB_PATH = orig_db_path
            tmpdir.cleanup()


class TestProcLock(unittest.TestCase):
    """core/proc_lock.py — v2.5.7 신규, cron 틱 중복실행 방지."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        import core.proc_lock as proc_lock
        self.proc_lock = proc_lock
        self._orig_lock_dir = proc_lock.LOCK_DIR
        proc_lock.LOCK_DIR = Path(self._tmpdir.name)

    def tearDown(self):
        self.proc_lock.LOCK_DIR = self._orig_lock_dir
        self._tmpdir.cleanup()

    def test_second_acquisition_raises_lock_held(self):
        with self.proc_lock.script_lock("iris_v2_monitor"):
            with self.assertRaises(self.proc_lock.LockHeld):
                with self.proc_lock.script_lock("iris_v2_monitor"):
                    pass  # 도달하면 안 됨

    def test_lock_released_after_context_exits(self):
        with self.proc_lock.script_lock("iris_v2_monitor"):
            pass
        with self.proc_lock.script_lock("iris_v2_monitor"):
            pass  # 예외 없이 재획득 가능해야 정상

    def test_different_names_do_not_conflict(self):
        with self.proc_lock.script_lock("iris_v2_monitor"):
            with self.proc_lock.script_lock("iris_v2_entry"):
                pass  # 다른 스크립트끼리는 서로 안 막아야 함


class TestDailyReport(IrisV2TestBase):
    """scripts/iris_v2_daily_report.py — 성과 리포트 집계 로직 검증(리포트봇 전용)."""

    def setUp(self):
        super().setUp()
        import scripts.iris_v2_daily_report as report
        self.report = report
        patch.object(report, "TRADE_MODE", "paper").start()
        self.addCleanup(patch.stopall)

    def _seed_closed(self, date_close: str, pnl_pct: float, pnl_amount: float,
                      ticker: str = "000001", exit_reason: str = "익절"):
        import core.db as dbmod
        with dbmod.get_conn() as conn:
            conn.execute(
                """INSERT INTO positions
                   (ticker, name, date_open, time_open, date_close, time_close, entry_price,
                    exit_price, shares, pnl_pct, pnl_amount, exit_reason, mode, status,
                    strategy_version, strategy)
                   VALUES (?,?,?,?,?,?,10000,11000,100,?,?,?,'paper','closed','2.5','iris_v2')""",
                (ticker, "종목A", date_close, "093000", date_close, "130000",
                 pnl_pct, pnl_amount, exit_reason),
            )

    def test_no_trades_yet(self):
        df = self.report._closed_trades()
        msg = self.report.build_report("20260722", df)
        self.assertIn("아직 청산된 거래가 없습니다", msg)

    def test_report_sections_present_and_correct(self):
        self._seed_closed("20260715", 5.0, 50000)
        self._seed_closed("20260716", -4.0, -40000)
        self._seed_closed("20260717", 10.0, 100000)
        self._seed_closed("20260720", 2.0, 20000)
        self._seed_closed("20260721", -1.0, -10000)
        self._seed_closed("20260722", 3.0, 30000)  # "오늘"
        df = self.report._closed_trades()
        msg = self.report.build_report("20260722", df)
        self.assertIn("오늘", msg)
        self.assertIn("+3.00%", msg)  # 오늘 수익률
        self.assertIn("최근 5거래일", msg)
        self.assertIn("이번달(202607)", msg)
        self.assertIn("전체누적", msg)
        self.assertIn("6건", msg)  # 전체 거래건수

    def test_today_with_no_trades_shows_none(self):
        self._seed_closed("20260721", 2.0, 20000)
        df = self.report._closed_trades()
        msg = self.report.build_report("20260722", df)
        self.assertIn("오늘</b>: 거래 없음", msg)

    def test_compound_matches_manual_calc(self):
        import pandas as pd
        pcts = pd.Series([10.0, -5.0, 2.0])
        expected = ((1.10 * 0.95 * 1.02) - 1) * 100
        self.assertAlmostEqual(self.report._compound(pcts), expected, places=6)


class TestDailyReportBalanceMode(IrisV2TestBase):
    """scripts/iris_v2_daily_report.py TRADE_MODE=real 경로 — v2.5.5 신규,
    잔고기준 리포트(거래손익률 복리시뮬레이션이 아니라 실제 KIS 잔고 조회)."""

    def setUp(self):
        super().setUp()
        import scripts.iris_v2_daily_report as report
        self.report = report
        patch.object(report, "TRADE_MODE", "real").start()
        self.addCleanup(patch.stopall)

    def test_no_principal_recorded_shows_reminder(self):
        with patch("core.kis_api.get_balance", return_value={"cash": 3_100_000, "final_cash": 3_100_000, "holdings": []}), \
             patch("core.order_exec.is_halted", return_value=None):
            msg = self.report.build_report("20260724", self.report._closed_trades())
        self.assertIn("현재 잔고", msg)
        self.assertIn("3,100,000원", msg)
        self.assertIn("총입금액 미기록", msg)

    def test_uses_final_cash_not_cash_for_display(self):
        """v2.5.8: cash(예수금총액)는 T+2 미반영이라 당일 실현손익이 안 보임 —
        리포트는 final_cash(가수도정산금액)를 써야 함. 2026-07-28 실거래에서
        cash 기준으로는 -1,300원 손실이 잔고에 반영 안 되는 문제 발견."""
        with patch("core.kis_api.get_balance",
                   return_value={"cash": 3_100_000, "final_cash": 3_098_700, "holdings": []}), \
             patch("core.order_exec.is_halted", return_value=None):
            msg = self.report.build_report("20260724", self.report._closed_trades())
        self.assertIn("3,098,700원", msg)
        self.assertNotIn("3,100,000원", msg)

    def test_total_capital_shown_but_no_cumulative_pct_before_tracking_starts(self):
        """v2.5.6: 총입금액은 항상 보여주되, 공식기록(start_tracking) 전에는
        '누적수익률(공식)'을 보여주면 안 됨 — 테스트기간 손익이 공식성과처럼
        비치지 않게 구분하는 게 이 기능의 핵심."""
        from core.db import add_capital_event
        add_capital_event("20260724", 3_000_000, "1차입금")
        with patch("core.kis_api.get_balance", return_value={"cash": 3_150_000, "final_cash": 3_150_000, "holdings": []}), \
             patch("core.order_exec.is_halted", return_value=None):
            msg = self.report.build_report("20260724", self.report._closed_trades())
        self.assertIn("총입금액", msg)
        self.assertIn("3,000,000원", msg)
        self.assertIn("테스트 기간", msg)
        self.assertNotIn("누적수익률(공식", msg)

    def test_cumulative_pct_after_tracking_starts_uses_anchor_not_total_capital(self):
        """공식기록 시작 후에는 anchor(시작시점 잔고) 기준으로 계산 — 총입금액과
        다른 값이어도(예: 테스트기간 손실로 총입금액보다 잔고가 낮은 채로 시작)
        anchor 기준이 맞아야 함."""
        from core.db import add_capital_event, start_live_tracking
        add_capital_event("20260720", 3_000_000, "1차입금")
        start_live_tracking("20260723", 2_900_000)  # 테스트기간에 소폭 손실난 채로 공식시작
        with patch("core.kis_api.get_balance", return_value={"cash": 3_015_000, "final_cash": 3_015_000, "holdings": []}), \
             patch("core.order_exec.is_halted", return_value=None):
            msg = self.report.build_report("20260724", self.report._closed_trades())
        # (3,015,000-2,900,000)/2,900,000*100 = +3.97% (총입금액 3,000,000 기준이면 +0.5%로 달라짐)
        self.assertIn("누적수익률(공식", msg)
        self.assertIn("+3.97%", msg)
        self.assertNotIn("테스트 기간", msg)

    def test_snapshot_marked_unreliable_when_halted(self):
        from core.db import get_balance_snapshot
        with patch("core.kis_api.get_balance", return_value={"cash": 3_000_000, "final_cash": 3_000_000, "holdings": []}), \
             patch("core.order_exec.is_halted", return_value={"action": "테스트halt"}):
            self.report.build_report("20260724", self.report._closed_trades())
        snap = get_balance_snapshot("20260724")
        self.assertEqual(snap["reliable"], 0)

    def test_snapshot_marked_unreliable_when_open_position_remains(self):
        from core.db import get_balance_snapshot, get_conn
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO positions
                   (ticker, name, date_open, time_open, entry_price, shares, mode, status,
                    strategy_version, strategy)
                   VALUES ('000001','종목A','20260724','093000',10000,100,'real','open','2.5','iris_v2')"""
            )
        with patch("core.kis_api.get_balance", return_value={"cash": 3_000_000, "final_cash": 3_000_000, "holdings": []}), \
             patch("core.order_exec.is_halted", return_value=None):
            self.report.build_report("20260724", self.report._closed_trades())
        snap = get_balance_snapshot("20260724")
        self.assertEqual(snap["reliable"], 0)

    def test_dry_run_does_not_write_snapshot(self):
        from core.db import get_balance_snapshot
        with patch("core.kis_api.get_balance", return_value={"cash": 3_000_000, "final_cash": 3_000_000, "holdings": []}), \
             patch("core.order_exec.is_halted", return_value=None):
            self.report.build_report("20260724", self.report._closed_trades(), dry_run=True)
        self.assertIsNone(get_balance_snapshot("20260724"))

    def test_balance_query_failure_falls_back_to_trade_summary(self):
        with patch("core.kis_api.get_balance", side_effect=Exception("네트워크 오류")):
            msg = self.report.build_report("20260724", self.report._closed_trades())
        self.assertIn("잔고조회 실패", msg)

    def test_period_return_excludes_mid_period_deposit(self):
        """구간 중 입금이 있었으면 그 금액만큼 제외하고 수익률을 계산해야 함."""
        from core.db import record_balance_snapshot, add_capital_event
        record_balance_snapshot("20260720", 3_000_000, reliable=True)
        record_balance_snapshot("20260721", 3_050_000, reliable=True)
        add_capital_event("20260722", 1_000_000, "추가입금")  # 구간 중 입금
        record_balance_snapshot("20260722", 4_060_000, reliable=True)
        record_balance_snapshot("20260723", 4_080_000, reliable=True)
        with patch("core.kis_api.get_balance", return_value={"cash": 4_100_000, "final_cash": 4_100_000, "holdings": []}), \
             patch("core.order_exec.is_halted", return_value=None):
            msg = self.report.build_report("20260724", self.report._closed_trades())
        # (4,100,000 - 3,000,000(구간시작) - 1,000,000(구간중입금)) / 3,000,000 = +3.33%
        self.assertIn("+3.33%", msg)
        self.assertIn("입출금", msg)

    def test_balance_inconsistency_detected_when_deposit_not_recorded(self):
        """v2.5.7: 어제잔고+오늘실현손익+기록된입출금과 오늘 실제잔고가 안 맞으면
        (=입금을 깜빡 기록 안 한 상황을 재현) 경고가 떠야 함."""
        from core.db import record_balance_snapshot
        record_balance_snapshot("20260723", 3_000_000, reliable=True)
        # 오늘 실현손익 0, 기록된 입출금 0인데 실제 잔고는 3,100,000(=100,000원 미기록 입금)
        with patch("core.kis_api.get_balance", return_value={"cash": 3_100_000, "final_cash": 3_100_000, "holdings": []}), \
             patch("core.order_exec.is_halted", return_value=None):
            msg = self.report.build_report("20260724", self.report._closed_trades())
        self.assertIn("잔고 불일치", msg)
        self.assertIn("+100,000", msg)

    def test_no_false_positive_when_balance_matches_expectation(self):
        from core.db import record_balance_snapshot
        record_balance_snapshot("20260723", 3_000_000, reliable=True)
        with patch("core.kis_api.get_balance", return_value={"cash": 3_000_500, "final_cash": 3_000_500, "holdings": []}), \
             patch("core.order_exec.is_halted", return_value=None):
            msg = self.report.build_report("20260724", self.report._closed_trades())
        # 500원 차이는 오차범위 내(1만원 미만) -- 경고 없어야 함
        self.assertNotIn("잔고 불일치", msg)


class TestTelegramCommands(unittest.TestCase):
    """core/telegram_commands.py — v2.5.4 신규. 텔레그램 getUpdates는 항상
    requests.get을 모킹해 실제 네트워크 호출 없이 검증(운영봇 실제 API 절대 안 건드림).
    **IrisV2TestBase를 상속하지 않음** — 그 기반클래스는 entry.py 테스트들을 보호하려고
    process_pending_commands() 자체를 no-op으로 전역 패치하는데, 여기선 정확히 그 함수의
    진짜 동작을 검증해야 하므로 상속받으면 테스트 대상이 사라져버림(최초 구현시 실제로
    이 문제로 4개 테스트가 전부 실패했었음 — 별도 클래스로 분리해 해결)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp_db = Path(self._tmpdir.name) / "test_telegram.db"
        import core.db as dbmod
        self._orig_db_path = dbmod.DB_PATH
        dbmod.DB_PATH = tmp_db
        dbmod.init_db()

        import core.telegram_commands as tgcmd
        self.tgcmd = tgcmd
        patch.object(tgcmd, "TELEGRAM_TOKEN", "test-token").start()
        patch.object(tgcmd, "TELEGRAM_CHAT_ID", "12345").start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        import core.db as dbmod
        dbmod.DB_PATH = self._orig_db_path
        self._tmpdir.cleanup()

    def _mock_updates(self, updates: list):
        return patch(
            "core.telegram_commands.requests.get",
            return_value=type("R", (), {
                "raise_for_status": lambda self: None,
                "json": lambda self: {"ok": True, "result": updates},
            })(),
        )

    def test_mode_query_returns_current_mode(self):
        mode, amount = self.tgcmd.get_position_size_mode()
        self.assertEqual(mode, self.tgcmd.MODE_FULL)  # 기본값

    def test_full_command_sets_full_balance_mode(self):
        from core.db import state_set
        state_set(self.tgcmd.MODE_KEY, self.tgcmd.MODE_FIXED)  # 뭔가 다른 값에서 시작
        updates = [{"update_id": 1, "message": {"chat": {"id": 12345}, "text": "/mode full"}}]
        with self._mock_updates(updates), patch("core.notify.send_telegram") as mock_tg:
            self.tgcmd.process_pending_commands()
        mode, _ = self.tgcmd.get_position_size_mode()
        self.assertEqual(mode, self.tgcmd.MODE_FULL)
        mock_tg.assert_called_once()

    def test_fixed_command_sets_amount(self):
        updates = [{"update_id": 1, "message": {"chat": {"id": 12345}, "text": "/mode fixed 3000000"}}]
        with self._mock_updates(updates), patch("core.notify.send_telegram"):
            self.tgcmd.process_pending_commands()
        mode, amount = self.tgcmd.get_position_size_mode()
        self.assertEqual(mode, self.tgcmd.MODE_FIXED)
        self.assertEqual(amount, 3_000_000)

    def test_fixed_command_rejects_out_of_bounds_amount(self):
        updates = [{"update_id": 1, "message": {"chat": {"id": 12345}, "text": "/mode fixed 100"}}]
        with self._mock_updates(updates), patch("core.notify.send_telegram") as mock_tg:
            self.tgcmd.process_pending_commands()
        mode, _ = self.tgcmd.get_position_size_mode()
        self.assertEqual(mode, self.tgcmd.MODE_FULL)  # 거부돼 기본값 그대로
        reply_text = mock_tg.call_args.args[0]
        self.assertIn("범위여야", reply_text)

    def test_unauthorized_chat_ignored(self):
        """.env TELEGRAM_CHAT_ID(12345)와 다른 채팅에서 온 명령은 무시해야 함(보안)."""
        updates = [{"update_id": 1, "message": {"chat": {"id": 99999}, "text": "/mode fixed 3000000"}}]
        with self._mock_updates(updates), patch("core.notify.send_telegram") as mock_tg:
            self.tgcmd.process_pending_commands()
        mode, _ = self.tgcmd.get_position_size_mode()
        self.assertEqual(mode, self.tgcmd.MODE_FULL)  # 변경 안 됨
        mock_tg.assert_not_called()

    def test_offset_advances_past_processed_updates(self):
        """같은 업데이트를 다음 폴링에서 재처리하지 않도록 offset이 전진해야 함."""
        from core.db import state_get
        updates = [{"update_id": 42, "message": {"chat": {"id": 12345}, "text": "/mode full"}}]
        with self._mock_updates(updates), patch("core.notify.send_telegram"):
            self.tgcmd.process_pending_commands()
        self.assertEqual(state_get(self.tgcmd.OFFSET_KEY), 43)

    def test_network_failure_does_not_raise(self):
        """텔레그램 API 자체가 실패해도 예외를 밖으로 던지면 안 됨(매매판단 방해 금지)."""
        with patch("core.telegram_commands.requests.get", side_effect=Exception("네트워크 오류")):
            self.tgcmd.process_pending_commands()  # 예외 없이 조용히 리턴돼야 함

    def test_unrecognized_text_ignored_silently(self):
        updates = [{"update_id": 1, "message": {"chat": {"id": 12345}, "text": "안녕하세요"}}]
        with self._mock_updates(updates), patch("core.notify.send_telegram") as mock_tg:
            self.tgcmd.process_pending_commands()
        mock_tg.assert_not_called()


if __name__ == "__main__":
    unittest.main()
