"""
iris v3 봇/스크립트 — 사전필터·후보확정·매수·매수확정·매도 단위테스트.
tests/test_iris_v2.py의 IrisV2TestBase 관례를 그대로 따른다(임시 sqlite +
KIS API/텔레그램 전부 모킹).

실행: python -m pytest tests/test_iris_v3.py -v
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))


def _ensure_candidates_columns(dbmod):
    """tests/test_iris_v2.py의 IrisV2TestBase와 동일한 workaround — 신선한
    init_db() 스키마엔 이 컬럼들이 빠져있어 마이그레이션으로 보강해야 함."""
    with dbmod.get_conn() as conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(candidates)").fetchall()]
        for col in ("marcap_bil", "rank_no", "skip_reason", "avg_val"):
            if col not in cols:
                coltype = "TEXT DEFAULT ''" if col == "skip_reason" else "REAL DEFAULT 0"
                conn.execute(f"ALTER TABLE candidates ADD COLUMN {col} {coltype}")


class IrisV3TestBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp_db = Path(self._tmpdir.name) / "test_iris_v3.db"

        import core.db as dbmod
        self._orig_db_path = dbmod.DB_PATH
        dbmod.DB_PATH = tmp_db
        dbmod.init_db()
        _ensure_candidates_columns(dbmod)

        import bot.iris_v3_entry as entry
        import bot.iris_v3_confirm_entry as confirm_entry
        import bot.iris_v3_exit as exitbot
        self.entry = entry
        self.confirm_entry = confirm_entry
        self.exitbot = exitbot

        patch.object(entry, "TRADE_MODE", "paper").start()
        patch.object(exitbot, "TRADE_MODE", "paper").start()

        patch("core.notify.send_telegram", return_value=True).start()
        patch("core.notify.send_telegram_report", return_value=True).start()
        patch("core.config.validate", return_value=None).start()

        self.addCleanup(patch.stopall)

    def tearDown(self):
        import core.db as dbmod
        dbmod.DB_PATH = self._orig_db_path
        self._tmpdir.cleanup()

    def _seed_candidate_winner(self, date: str, ticker="000001", name="종목A",
                                prev_close=10000.0, tv_ma5=15_000_000_000.0, gap=0.20):
        import core.db as dbmod
        with dbmod.get_conn() as conn:
            conn.execute(
                """INSERT INTO candidates
                   (trade_date, ticker, name, sector, score, total_amt, avg_val,
                    marcap_bil, rank_no, strategy, strategy_version, val_ratio)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (date, ticker, name, "", gap, tv_ma5, prev_close, 0.0, 1, "iris_v3", "3.0.0", 0.0),
            )

    def _seed_open_position(self, date_open: str, ticker="000001", name="종목A",
                             entry_price=10000.0, shares=100):
        import core.db as dbmod
        with dbmod.get_conn() as conn:
            conn.execute(
                """INSERT INTO positions
                   (ticker, name, date_open, time_open, entry_price, shares,
                    mode, status, strategy_version, strategy)
                   VALUES (?,?,?,?,?,?,'real','open','3.0.0','iris_v3')""",
                (ticker, name, date_open, "150000", entry_price, shares),
            )

    def _open_positions(self):
        import core.db as dbmod
        with dbmod.get_conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM positions WHERE strategy='iris_v3'").fetchall()]


class TestPrefilter(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp_db = Path(self._tmpdir.name) / "test_prefilter.db"
        import core.db as dbmod
        self._orig_db_path = dbmod.DB_PATH
        dbmod.DB_PATH = tmp_db
        dbmod.init_db()
        self.addCleanup(self._restore)

    def _restore(self):
        import core.db as dbmod
        dbmod.DB_PATH = self._orig_db_path
        self._tmpdir.cleanup()

    def _seed_price_daily_and_universe(self, ticker, closes_volumes, market="KOSDAQ"):
        import core.db as dbmod
        with dbmod.get_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO universe (ticker, name, market, sector, updated) VALUES (?,?,?,?,?)",
                (ticker, f"종목_{ticker}", market, "전기전자", "20260814"))
            for date, close, volume in closes_volumes:
                conn.execute(
                    "INSERT INTO price_daily (ticker, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
                    (ticker, date, close, close, close, close, volume),
                )

    def test_high_liquidity_ticker_passes_low_does_not(self):
        import scripts.iris_v3_prefilter as prefilter
        dates = ["20260810", "20260811", "20260812", "20260813", "20260814"]
        # tv_ma5 = close*volume 평균. 고유동성: 20000*2,000,000=400억 >= 125억
        high_liq = [(d, 20000, 2_000_000) for d in dates]
        # 저유동성: 5000*100,000=5억 < 125억
        low_liq = [(d, 5000, 100_000) for d in dates]
        self._seed_price_daily_and_universe("000001", high_liq)
        self._seed_price_daily_and_universe("000002", low_liq)

        with patch("scripts.iris_v3_prefilter.next_trading_date", return_value="20260817"):
            target_date, cand = prefilter.build_prefilter(dry_run=True)

        self.assertEqual(target_date, "20260817")
        tickers = set(cand["ticker"])
        self.assertIn("000001", tickers)
        self.assertNotIn("000002", tickers)

    def test_kospi_ticker_excluded(self):
        import scripts.iris_v3_prefilter as prefilter
        dates = ["20260810", "20260811", "20260812", "20260813", "20260814"]
        self._seed_price_daily_and_universe("005930", [(d, 70000, 10_000_000) for d in dates], market="KOSPI")
        with patch("scripts.iris_v3_prefilter.next_trading_date", return_value="20260817"):
            _, cand = prefilter.build_prefilter(dry_run=True)
        self.assertNotIn("005930", set(cand["ticker"]))


class TestCandidates(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp_db = Path(self._tmpdir.name) / "test_candidates.db"
        import core.db as dbmod
        self._orig_db_path = dbmod.DB_PATH
        dbmod.DB_PATH = tmp_db
        dbmod.init_db()
        self.addCleanup(self._restore)

    def _restore(self):
        import core.db as dbmod
        dbmod.DB_PATH = self._orig_db_path
        self._tmpdir.cleanup()

    def test_uses_open_field_not_price_field_for_gap(self):
        # 2026-08-18: v3의 신호는 "오늘 시가"이므로 get_price()["open"]을 써야 함
        # (["price"]를 쓰면 v2.5.2 버그의 반대방향 재발) — open만 갭밴드 안에 들게,
        # price는 밴드 밖에 들게 설계해 어느 필드를 쓰는지 검증.
        import scripts.iris_v3_candidates as candidates
        from core.db import state_set
        state_set("iris_v3_prefilter_20260818", {"version": "3.0.0", "rows": [
            {"ticker": "000001", "name": "종목A", "prev_close": 10000.0, "tv_ma5": 15_000_000_000.0},
        ]})
        with patch.object(candidates, "today_kst", return_value="20260818"), \
             patch("core.kis_api.get_price", return_value={"open": 12000, "price": 10500}):
            date, cand = candidates.build_candidates(dry_run=True)
        self.assertEqual(len(cand), 1)  # open 기준 gap=0.20 (밴드 안)
        self.assertAlmostEqual(cand.iloc[0]["gap"], 0.20, places=4)

    def test_out_of_band_gap_excluded(self):
        import scripts.iris_v3_candidates as candidates
        from core.db import state_set
        state_set("iris_v3_prefilter_20260818", {"version": "3.0.0", "rows": [
            {"ticker": "000001", "name": "너무작음", "prev_close": 10000.0, "tv_ma5": 15_000_000_000.0},
            {"ticker": "000002", "name": "딱맞음", "prev_close": 10000.0, "tv_ma5": 15_000_000_000.0},
        ]})

        def fake_price(ticker):
            return {"open": 10500, "price": 10500} if ticker == "000001" else {"open": 12000, "price": 12000}

        with patch.object(candidates, "today_kst", return_value="20260818"), \
             patch("core.kis_api.get_price", side_effect=fake_price):
            date, cand = candidates.build_candidates(dry_run=True)
        tickers = set(cand["ticker"])
        self.assertNotIn("000001", tickers)  # gap=0.05, 밴드 밖
        self.assertIn("000002", tickers)     # gap=0.20, 밴드 안

    def test_multiple_candidates_ranked_by_gap_desc(self):
        import scripts.iris_v3_candidates as candidates
        from core.db import state_set
        state_set("iris_v3_prefilter_20260818", {"version": "3.0.0", "rows": [
            {"ticker": "000001", "name": "A", "prev_close": 10000.0, "tv_ma5": 15_000_000_000.0},
            {"ticker": "000002", "name": "B", "prev_close": 10000.0, "tv_ma5": 15_000_000_000.0},
        ]})

        def fake_price(ticker):
            return {"open": 12000, "price": 12000} if ticker == "000001" else {"open": 12500, "price": 12500}

        with patch.object(candidates, "today_kst", return_value="20260818"), \
             patch("core.kis_api.get_price", side_effect=fake_price):
            date, cand = candidates.build_candidates(dry_run=True)
        self.assertEqual(cand.iloc[0]["ticker"], "000002")  # gap 0.25 > 0.20, 1위
        self.assertEqual(int(cand.iloc[0]["rank_no"]), 1)

    def test_empty_prefilter_returns_empty(self):
        import scripts.iris_v3_candidates as candidates
        with patch.object(candidates, "today_kst", return_value="20260818"):
            date, cand = candidates.build_candidates(dry_run=True)
        self.assertTrue(cand.empty)


class TestEntryPaper(IrisV3TestBase):
    def test_paper_mode_records_position_directly(self):
        date = "20260818"
        self._seed_candidate_winner(date)
        with patch.object(self.entry, "today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"price": 12000}):
            self.entry.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["mode"], "paper")
        self.assertEqual(positions[0]["status"], "open")

    def test_no_candidate_no_position(self):
        date = "20260818"
        with patch.object(self.entry, "today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True):
            self.entry.run(dry_run=False)
        self.assertEqual(len(self._open_positions()), 0)

    def test_paper_mode_records_gap_as_score(self):
        """2026-08-25 발견: positions.score(=갭)가 안 채워져 실거래 일지에 갭이
        항상 0%로 찍히던 버그 회귀테스트 — 손익/거래 자체엔 영향 없던 순수
        기록 버그였음."""
        date = "20260818"
        self._seed_candidate_winner(date, gap=0.2228)
        with patch.object(self.entry, "today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"price": 12000}):
            self.entry.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(len(positions), 1)
        self.assertAlmostEqual(positions[0]["score"], 0.2228, places=4)

    def test_holiday_skips(self):
        with patch.object(self.entry, "today_kst", return_value="20260815"), \
             patch("core.kis_api.is_trading_day", return_value=False), \
             patch("core.kis_api.get_price") as mock_price:
            self.entry.run(dry_run=False)
        mock_price.assert_not_called()

    def test_stale_open_position_from_prior_day_blocks_new_entry(self):
        """오버나잇 매도가 pending으로 아직 안 끝나 어제자 포지션이 열려있는
        상태에서, 오늘 새 후보가 있어도 재매수하면 안 된다(2026-08-19 실거래
        개시 전 코드감사에서 발견된 이중매수 위험 — v2는 당일청산 구조라
        이 시나리오 자체가 없었음)."""
        date = "20260818"
        self._seed_open_position(date_open="20260817", ticker="999999")
        self._seed_candidate_winner(date, ticker="000001")
        with patch.object(self.entry, "today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"price": 12000}):
            self.entry.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["ticker"], "999999")


class TestEntryReal(IrisV3TestBase):
    def test_real_mode_submits_conditional_limit_and_registers_pending(self):
        date = "20260818"
        self._seed_candidate_winner(date)
        with patch.object(self.entry, "TRADE_MODE", "real"), \
             patch.object(self.entry, "today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"price": 12000}), \
             patch("core.order_exec.compute_buy_qty", return_value=100), \
             patch("core.kis_api.buy_conditional_limit",
                   return_value={"output": {"ODNO": "1", "KRX_FWDG_ORD_ORGNO": "99999"}}):
            self.entry.run(dry_run=False)
        # real 모드는 confirm_entry.py가 확정하기 전까지 positions에 안 남고 pending만 남음
        self.assertEqual(len(self._open_positions()), 0)
        from core.order_exec_v3 import get_pending_entry
        pending = get_pending_entry(date)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["ticker"], "000001")
        self.assertEqual(pending["intended_qty"], 100)

    def test_pending_entry_includes_gap(self):
        """2026-08-25 회귀테스트 — pending_entry에 gap이 안 담겨 confirm_entry가
        positions.score를 채울 수 없던 버그."""
        date = "20260818"
        self._seed_candidate_winner(date, gap=0.2228)
        with patch.object(self.entry, "TRADE_MODE", "real"), \
             patch.object(self.entry, "today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"price": 12000}), \
             patch("core.order_exec.compute_buy_qty", return_value=100), \
             patch("core.kis_api.buy_conditional_limit",
                   return_value={"output": {"ODNO": "1", "KRX_FWDG_ORD_ORGNO": "99999"}}):
            self.entry.run(dry_run=False)
        from core.order_exec_v3 import get_pending_entry
        pending = get_pending_entry(date)
        self.assertAlmostEqual(pending["gap"], 0.2228, places=4)

    def test_halted_v3_blocks_entry(self):
        date = "20260818"
        self._seed_candidate_winner(date)
        from core.order_exec_v3 import set_halt
        set_halt("20260817", "999999", "이전halt", "상세")
        with patch.object(self.entry, "TRADE_MODE", "real"), \
             patch.object(self.entry, "today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price") as mock_price:
            self.entry.run(dry_run=False)
        mock_price.assert_not_called()


class TestConfirmEntry(IrisV3TestBase):
    def test_full_fill_records_position(self):
        date = "20260818"
        from core.order_exec_v3 import set_pending_entry
        set_pending_entry(date, {"ticker": "000001", "name": "종목A", "odno": "1",
                                  "orgno": "99999", "intended_qty": 100, "ref_price": 12000})
        with patch.object(self.confirm_entry, "today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 100}]}), \
             patch("core.kis_api.get_order_fill_qty", return_value=None), \
             patch("core.kis_api.get_filled_price", return_value=11950.0):
            self.confirm_entry.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["shares"], 100)
        self.assertEqual(positions[0]["entry_price"], 11950.0)
        from core.order_exec_v3 import get_pending_entry
        self.assertIsNone(get_pending_entry(date))

    def test_full_fill_records_gap_as_score(self):
        """2026-08-25 회귀테스트 — 실거래 확정시 positions.score(=갭)가
        pending_entry의 gap 값으로 정확히 채워지는지."""
        date = "20260818"
        from core.order_exec_v3 import set_pending_entry
        set_pending_entry(date, {"ticker": "000001", "name": "종목A", "odno": "1",
                                  "orgno": "99999", "intended_qty": 100, "ref_price": 12000,
                                  "gap": 0.2228})
        with patch.object(self.confirm_entry, "today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 100}]}), \
             patch("core.kis_api.get_order_fill_qty", return_value=None), \
             patch("core.kis_api.get_filled_price", return_value=11950.0):
            self.confirm_entry.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(len(positions), 1)
        self.assertAlmostEqual(positions[0]["score"], 0.2228, places=4)

    def test_full_fill_missing_gap_in_old_pending_records_null_not_crash(self):
        """구버전(2026-08-25 이전) pending_entry엔 gap 키 자체가 없었음 —
        하위호환으로 크래시 없이 score=NULL로 기록되는지 확인."""
        date = "20260818"
        from core.order_exec_v3 import set_pending_entry
        set_pending_entry(date, {"ticker": "000001", "name": "종목A", "odno": "1",
                                  "orgno": "99999", "intended_qty": 100, "ref_price": 12000})
        with patch.object(self.confirm_entry, "today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 100}]}), \
             patch("core.kis_api.get_order_fill_qty", return_value=None), \
             patch("core.kis_api.get_filled_price", return_value=11950.0):
            self.confirm_entry.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(len(positions), 1)
        self.assertIsNone(positions[0]["score"])

    def test_no_pending_is_noop(self):
        with patch.object(self.confirm_entry, "today_kst", return_value="20260818"), \
             patch("core.kis_api.is_trading_day", return_value=True):
            self.confirm_entry.run(dry_run=False)
        self.assertEqual(len(self._open_positions()), 0)

    def test_zero_fill_after_auction_halts(self):
        date = "20260818"
        from core.order_exec_v3 import set_pending_entry
        set_pending_entry(date, {"ticker": "000001", "name": "종목A", "odno": "1",
                                  "orgno": "99999", "intended_qty": 100, "ref_price": 12000})
        with patch.object(self.confirm_entry, "today_kst", return_value=date), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_balance", return_value={"holdings": []}), \
             patch("core.kis_api.get_order_fill_qty", return_value=None):
            self.confirm_entry.run(dry_run=False)
        self.assertEqual(len(self._open_positions()), 0)
        from core.order_exec_v3 import is_halted
        self.assertIsNotNone(is_halted(date))


class TestExitPaper(IrisV3TestBase):
    def test_paper_mode_closes_at_current_price(self):
        self._seed_open_position(date_open="20260818", entry_price=10000.0, shares=100)
        with patch.object(self.exitbot, "today_kst", return_value="20260819"), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"price": 11000}):
            self.exitbot.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "closed")
        self.assertAlmostEqual(positions[0]["pnl_pct"], 10.0, places=2)

    def test_no_open_position_is_noop(self):
        with patch.object(self.exitbot, "today_kst", return_value="20260819"), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price") as mock_price:
            self.exitbot.run(dry_run=False)
        mock_price.assert_not_called()


class TestExitReal(IrisV3TestBase):
    def test_resumes_pending_exit_left_over_from_prior_days_run_without_reselling(self):
        """2026-08-18 코드감사에서 발견한 시나리오: 어제 exit.py 실행이 pending
        으로 끝났을 때, 오늘 다시 실행되는 exit.py가 그 pending을 이어받아
        확인해야지 완전히 새로운 매도주문을 또 내면 안 된다(이중매도 위험)."""
        from core.order_exec_v3 import _set_pending_exit
        self._seed_open_position(date_open="20260817", ticker="000001",
                                  entry_price=10000.0, shares=100)
        _set_pending_exit("20260818", {  # 어제자 exit.py가 남긴 pending
            "ticker": "000001", "odno": "77", "orgno": "1",
            "shares_before": 100, "entry_price": 10000.0,
            "stage": "limit_submitted", "retry_count": 0,
        })
        with patch.object(self.exitbot, "TRADE_MODE", "real"), \
             patch.object(self.exitbot, "today_kst", return_value="20260819"), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.sell_limit") as mock_sell, \
             patch("core.order_exec.poll_remaining_shares", return_value=0), \
             patch("core.order_exec.cancel_or_convert_to_market", return_value={"status": "already_done"}), \
             patch("core.order_exec.resolve_exit_price", return_value=10400.0):
            self.exitbot.run(dry_run=False)
        mock_sell.assert_not_called()  # 새 매도주문을 내지 않고 기존 pending만 재확인
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "closed")
        self.assertEqual(positions[0]["exit_price"], 10400.0)

    def test_real_mode_immediate_fill_closes_position(self):
        self._seed_open_position(date_open="20260818", entry_price=10000.0, shares=100)
        with patch.object(self.exitbot, "TRADE_MODE", "real"), \
             patch.object(self.exitbot, "today_kst", return_value="20260819"), \
             patch("core.kis_api.is_trading_day", return_value=True), \
             patch("core.kis_api.get_price", return_value={"price": 10500}), \
             patch("core.kis_api.sell_limit", return_value={"output": {"ODNO": "9", "KRX_FWDG_ORD_ORGNO": "1"}}), \
             patch("core.kis_api.get_balance", return_value={"holdings": []}), \
             patch("core.kis_api.get_order_fill_qty", return_value=None), \
             patch("core.kis_api.get_filled_price", return_value=10480.0):
            self.exitbot.run(dry_run=False)
        positions = self._open_positions()
        self.assertEqual(positions[0]["status"], "closed")
        self.assertEqual(positions[0]["exit_price"], 10480.0)


if __name__ == "__main__":
    unittest.main()
