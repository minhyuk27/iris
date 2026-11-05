"""
core/order_exec_v3.py 테스트 — tests/test_order_exec.py(OrderExecTestBase)와
동일한 관례(임시 sqlite + patch("core.kis_api.X")).
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core.order_exec_v3 as oev3


class OrderExecV3TestBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp_db = Path(self._tmpdir.name) / "test_order_exec_v3.db"
        import core.db as dbmod
        self._orig_db_path = dbmod.DB_PATH
        dbmod.DB_PATH = tmp_db
        dbmod.init_db()
        patch.object(oev3, "time").start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        import core.db as dbmod
        dbmod.DB_PATH = self._orig_db_path
        self._tmpdir.cleanup()


class TestBuyConditionalLimitSafe(OrderExecV3TestBase):
    def test_success_rounds_price_and_returns_odno(self):
        with patch("core.kis_api.buy_conditional_limit",
                   return_value={"output": {"ODNO": "1", "KRX_FWDG_ORD_ORGNO": "99999"}}) as mock_call:
            ok, odno, orgno = oev3.buy_conditional_limit_safe("000001", 10, 10123)
        self.assertTrue(ok)
        self.assertEqual(odno, "1")
        self.assertEqual(orgno, "99999")
        # round_to_tick(10123, "down") — 10123은 5000~20000 구간(호가단위10)이라 10120
        called_price = mock_call.call_args[0][2]
        self.assertEqual(called_price, 10120)

    def test_zero_qty_is_noop(self):
        ok, odno, orgno = oev3.buy_conditional_limit_safe("000001", 0, 10000)
        self.assertFalse(ok)
        self.assertEqual(odno, "")

    def test_all_retries_fail(self):
        with patch("core.kis_api.buy_conditional_limit", side_effect=Exception("거부")):
            ok, odno, orgno = oev3.buy_conditional_limit_safe("000001", 10, 10000)
        self.assertFalse(ok)


class TestHaltV3(OrderExecV3TestBase):
    def test_set_and_check_halt(self):
        self.assertIsNone(oev3.is_halted("20260819"))
        oev3.set_halt("20260819", "000001", "테스트", "상세")
        halt = oev3.is_halted("20260819")
        self.assertEqual(halt["ticker"], "000001")

    def test_v3_halt_independent_from_v2_halt(self):
        # 2026-08-18: v2/v3 halt가 서로 다른 날짜같은 key를 써도 충돌 안 함을
        # 명시적으로 검증(구현계획의 핵심 설계결정).
        import core.order_exec as oe2
        oe2.set_halt("20260819", "000002", "v2사유", "v2상세")
        self.assertIsNone(oev3.is_halted("20260819"))  # v2 halt가 v3엔 안 보임

        oev3.set_halt("20260819", "000003", "v3사유", "v3상세")
        v2_halt = oe2.is_halted("20260819")
        self.assertEqual(v2_halt["ticker"], "000002")  # v3 halt가 v2를 안 건드림

    def test_find_unresolved_halt_scans_all_dates(self):
        self.assertIsNone(oev3.find_unresolved_halt())
        oev3.set_halt("20260810", "000001", "사유", "상세")
        found = oev3.find_unresolved_halt()
        self.assertEqual(found["date"], "20260810")


class TestPendingEntry(OrderExecV3TestBase):
    def test_set_get_clear(self):
        self.assertIsNone(oev3.get_pending_entry("20260819"))
        oev3.set_pending_entry("20260819", {"ticker": "000001", "odno": "1"})
        pending = oev3.get_pending_entry("20260819")
        self.assertEqual(pending["ticker"], "000001")
        oev3.clear_pending_entry("20260819")
        self.assertIsNone(oev3.get_pending_entry("20260819"))


class TestExecuteExit(OrderExecV3TestBase):
    def test_immediate_fill_no_market_conversion_needed(self):
        with patch("core.kis_api.sell_limit", return_value={"output": {"ODNO": "1", "KRX_FWDG_ORD_ORGNO": "99999"}}), \
             patch("core.kis_api.get_balance", return_value={"holdings": []}), \
             patch("core.kis_api.get_order_fill_qty", return_value=None), \
             patch("core.kis_api.get_filled_price", return_value=9500.0):
            result = oev3.execute_exit("000001", 100, entry_price=10000, ref_price=9500, date="20260819")
        self.assertEqual(result, (9500.0, "1"))
        self.assertIsNone(oev3.get_pending_exit("20260819"))

    def test_pending_registered_before_poll_completes(self):
        def _poll_checks_pending(*a, **k):
            self.assertIsNotNone(oev3.get_pending_exit("20260819"))
            return 0
        with patch("core.kis_api.sell_limit", return_value={"output": {"ODNO": "2", "KRX_FWDG_ORD_ORGNO": "99999"}}), \
             patch("core.order_exec.poll_remaining_shares", side_effect=_poll_checks_pending), \
             patch("core.order_exec.resolve_exit_price", return_value=9999.0):
            result = oev3.execute_exit("000001", 100, entry_price=10000, ref_price=9500, date="20260819")
        self.assertEqual(result, (9999.0, "2"))

    def test_unfilled_after_timeout_converts_to_market(self):
        # poll_remaining_shares 첫 호출(지정가 대기)은 미체결(remaining=100),
        # cancel_or_convert_to_market → converted, 두번째 poll은 체결완료(0).
        call_count = {"n": 0}

        def _poll_side_effect(*a, **k):
            call_count["n"] += 1
            return 100 if call_count["n"] == 1 else 0

        with patch("core.kis_api.sell_limit", return_value={"output": {"ODNO": "3", "KRX_FWDG_ORD_ORGNO": "99999"}}), \
             patch("core.order_exec.poll_remaining_shares", side_effect=_poll_side_effect), \
             patch("core.order_exec.cancel_or_convert_to_market",
                   return_value={"status": "converted", "psbl_qty": 100}) as mock_conv, \
             patch("core.order_exec.resolve_exit_price", return_value=9400.0):
            result = oev3.execute_exit("000001", 100, entry_price=10000, ref_price=9500, date="20260819")
        mock_conv.assert_called_once()
        self.assertEqual(result, (9400.0, "3"))
        self.assertIsNone(oev3.get_pending_exit("20260819"))

    def test_order_submission_failure_halts(self):
        with patch("core.kis_api.sell_limit", side_effect=Exception("거부")):
            result = oev3.execute_exit("000001", 100, entry_price=10000, ref_price=9500, date="20260819")
        self.assertIsNone(result)
        self.assertIsNotNone(oev3.is_halted("20260819"))


class TestRetryPendingExit(OrderExecV3TestBase):
    def test_no_pending_returns_none(self):
        self.assertIsNone(oev3.retry_pending_exit("20260819"))

    def test_resolves_to_done_on_retry(self):
        with patch("core.kis_api.sell_limit", return_value={"output": {"ODNO": "4", "KRX_FWDG_ORD_ORGNO": "99999"}}), \
             patch("core.order_exec.poll_remaining_shares", return_value=50), \
             patch("core.order_exec.cancel_or_convert_to_market", return_value=None), \
             patch("core.order_exec.resolve_exit_price", return_value=9000.0):
            result = oev3.execute_exit("000001", 100, entry_price=10000, ref_price=9500, date="20260819")
        self.assertEqual(result, "pending")

        with patch("core.order_exec.poll_remaining_shares", return_value=0), \
             patch("core.order_exec.cancel_or_convert_to_market", return_value={"status": "already_done"}), \
             patch("core.order_exec.resolve_exit_price", return_value=9000.0):
            follow_up = oev3.retry_pending_exit("20260819")
        self.assertEqual(follow_up["status"], "done")
        self.assertIsNone(oev3.get_pending_exit("20260819"))

    def test_pending_survives_across_midnight_to_next_days_lookup(self):
        """2026-08-18 코드감사에서 발견: pending이 자정을 넘기면 다음날
        exit.py가 today_kst()로 조회할 때 못 찾아 이중매도 위험이 있었다
        (v2는 force_close.py가 그날 안에 끝까지 재시도해 이 시나리오 자체가
        없음, v3는 1회성 cron이라 날짜가 넘어갈 수 있음). 어제 날짜로 등록된
        pending이 오늘 날짜 조회에서도 그대로 잡혀야 한다."""
        with patch("core.kis_api.sell_limit", return_value={"output": {"ODNO": "5", "KRX_FWDG_ORD_ORGNO": "99999"}}), \
             patch("core.order_exec.poll_remaining_shares", return_value=50), \
             patch("core.order_exec.cancel_or_convert_to_market", return_value=None), \
             patch("core.order_exec.resolve_exit_price", return_value=9000.0):
            result = oev3.execute_exit("000001", 100, entry_price=10000, ref_price=9500, date="20260819")
        self.assertEqual(result, "pending")
        self.assertIsNotNone(oev3.get_pending_exit("20260820"))  # 다음날 날짜로도 조회됨

        with patch("core.order_exec.poll_remaining_shares", return_value=0), \
             patch("core.order_exec.cancel_or_convert_to_market", return_value={"status": "already_done"}), \
             patch("core.order_exec.resolve_exit_price", return_value=9000.0):
            follow_up = oev3.retry_pending_exit("20260820")  # 다음날 exit.py가 재시도
        self.assertEqual(follow_up["status"], "done")


if __name__ == "__main__":
    unittest.main()
