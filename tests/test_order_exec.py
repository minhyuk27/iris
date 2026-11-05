"""
core/order_exec.py 단위테스트 — 실제 KIS 주문 실행의 재시도·체결확인·halt 로직.
KIS API(core.kis_api) 함수는 전부 모킹, 실제 네트워크 호출 없음.

실행: python -m pytest tests/test_order_exec.py -v
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core.order_exec as oe


class OrderExecTestBase(unittest.TestCase):
    """halt 상태는 bot_state(DB)에 저장되므로 격리용 임시 DB 필요."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp_db = Path(self._tmpdir.name) / "test_order_exec.db"
        import core.db as dbmod
        self._orig_db_path = dbmod.DB_PATH
        dbmod.DB_PATH = tmp_db
        dbmod.init_db()
        patch.object(oe, "time").start()  # sleep 전부 무력화(테스트 속도)
        self.addCleanup(patch.stopall)

    def tearDown(self):
        import core.db as dbmod
        dbmod.DB_PATH = self._orig_db_path
        self._tmpdir.cleanup()


class TestBuySafe(OrderExecTestBase):
    def test_success_first_try(self):
        with patch("core.kis_api.buy_market", return_value={"output": {"ODNO": "111"}}):
            ok, odno = oe.buy_safe("000001", 10)
        self.assertTrue(ok)
        self.assertEqual(odno, "111")

    def test_retries_then_succeeds(self):
        calls = {"n": 0}
        def flaky(ticker, qty):
            calls["n"] += 1
            if calls["n"] < 3:
                raise Exception("일시적 오류")
            return {"output": {"ODNO": "222"}}
        with patch("core.kis_api.buy_market", side_effect=flaky):
            ok, odno = oe.buy_safe("000001", 10)
        self.assertTrue(ok)
        self.assertEqual(calls["n"], 3)

    def test_all_retries_fail(self):
        with patch("core.kis_api.buy_market", side_effect=Exception("영구 오류")):
            ok, odno = oe.buy_safe("000001", 10)
        self.assertFalse(ok)
        self.assertEqual(odno, "")

    def test_zero_qty_returns_false_without_calling_api(self):
        with patch("core.kis_api.buy_market") as mock_buy:
            ok, odno = oe.buy_safe("000001", 0)
        self.assertFalse(ok)
        mock_buy.assert_not_called()


class TestSellSafe(OrderExecTestBase):
    def test_success(self):
        with patch("core.kis_api.sell_market", return_value={"output": {"ODNO": "333"}}):
            ok, odno = oe.sell_safe("000001", 10, reason="손절")
        self.assertTrue(ok)
        self.assertEqual(odno, "333")

    def test_zero_qty_is_noop_success(self):
        with patch("core.kis_api.sell_market") as mock_sell:
            ok, odno = oe.sell_safe("000001", 0)
        self.assertTrue(ok)
        mock_sell.assert_not_called()

    def test_all_retries_fail(self):
        with patch("core.kis_api.sell_market", side_effect=Exception("거부됨")):
            ok, odno = oe.sell_safe("000001", 10)
        self.assertFalse(ok)


class TestRoundToTick(unittest.TestCase):
    """2026-08-07 신규(④지정가 주문) — 2023-01-25 시행 코스피·코스닥 공통
    호가단위표(iris DECISIONS 2026-08-07 (10차)) 기준 반올림 검증."""

    def test_tick_size_boundaries(self):
        cases = [
            (1_999, 1), (2_000, 5), (4_999, 5), (5_000, 10),
            (19_999, 10), (20_000, 50), (49_999, 50), (50_000, 100),
            (199_999, 100), (200_000, 500), (499_999, 500), (500_000, 1000),
            (1_000_000, 1000),
        ]
        for price, expected_tick in cases:
            with self.subTest(price=price):
                self.assertEqual(oe._tick_size(price), expected_tick)

    def test_round_down_for_buy_ceiling(self):
        # 매수 상한가 캡은 의도한 값보다 비싸지지 않게 내림
        self.assertEqual(oe.round_to_tick(10_234, direction="down"), 10_230)  # tick=10
        self.assertEqual(oe.round_to_tick(1_005, direction="down"), 1_005)    # tick=1, 변화없음
        self.assertEqual(oe.round_to_tick(23_456, direction="down"), 23_450)  # tick=50

    def test_round_up_for_sell_floor(self):
        # 매도 하한가 캡은 의도한 값보다 싸지지 않게 올림
        self.assertEqual(oe.round_to_tick(10_231, direction="up"), 10_240)   # tick=10
        self.assertEqual(oe.round_to_tick(10_230, direction="up"), 10_230)   # 이미 정확히 tick 배수
        self.assertEqual(oe.round_to_tick(23_401, direction="up"), 23_450)   # tick=50

    def test_round_nearest_default(self):
        self.assertEqual(oe.round_to_tick(10_234), 10_230)
        self.assertEqual(oe.round_to_tick(10_236), 10_240)


class TestBuySafeLimit(OrderExecTestBase):
    def test_success_rounds_price_down(self):
        with patch("core.kis_api.buy_limit", return_value={
                "output": {"ODNO": "444", "KRX_FWDG_ORD_ORGNO": "06010"}}) as mock_buy:
            ok, odno, orgno = oe.buy_safe_limit("000001", 10, 10_234)
        self.assertTrue(ok)
        self.assertEqual(odno, "444")
        self.assertEqual(orgno, "06010")
        mock_buy.assert_called_once_with("000001", 10, 10_230)  # tick=10 기준 내림

    def test_zero_qty_returns_false_without_calling_api(self):
        with patch("core.kis_api.buy_limit") as mock_buy:
            ok, odno, orgno = oe.buy_safe_limit("000001", 0, 10_000)
        self.assertFalse(ok)
        mock_buy.assert_not_called()

    def test_all_retries_fail(self):
        with patch("core.kis_api.buy_limit", side_effect=Exception("거부됨")):
            ok, odno, orgno = oe.buy_safe_limit("000001", 10, 10_000)
        self.assertFalse(ok)
        self.assertEqual(odno, "")
        self.assertEqual(orgno, "")


class TestSellSafeLimit(OrderExecTestBase):
    def test_success_rounds_price_up(self):
        with patch("core.kis_api.sell_limit", return_value={
                "output": {"ODNO": "555", "KRX_FWDG_ORD_ORGNO": "06010"}}) as mock_sell:
            ok, odno, orgno = oe.sell_safe_limit("000001", 10, 10_231, reason="손절")
        self.assertTrue(ok)
        self.assertEqual(odno, "555")
        mock_sell.assert_called_once_with("000001", 10, 10_240)  # tick=10 기준 올림

    def test_zero_qty_is_noop_success(self):
        with patch("core.kis_api.sell_limit") as mock_sell:
            ok, odno, orgno = oe.sell_safe_limit("000001", 0, 10_000)
        self.assertTrue(ok)
        mock_sell.assert_not_called()

    def test_all_retries_fail(self):
        with patch("core.kis_api.sell_limit", side_effect=Exception("거부됨")):
            ok, odno, orgno = oe.sell_safe_limit("000001", 10, 10_000)
        self.assertFalse(ok)


class TestCancelOrConvertToMarket(OrderExecTestBase):
    def test_converts_remaining_qty_to_market(self):
        with patch("core.kis_api.get_cancelable_order",
                    return_value={"psbl_qty": 7, "krx_fwdg_ord_orgno": "06010",
                                  "ord_dvsn": "00", "pdno": "000001"}), \
             patch("core.kis_api.revise_to_market", return_value={"output": {}}) as mock_revise:
            result = oe.cancel_or_convert_to_market("999")
        self.assertEqual(result, {"status": "converted", "psbl_qty": 7})
        mock_revise.assert_called_once_with("999", "06010", 7)

    def test_prefers_passed_in_orgno_over_queried_one(self):
        with patch("core.kis_api.get_cancelable_order",
                    return_value={"psbl_qty": 3, "krx_fwdg_ord_orgno": "06010",
                                  "ord_dvsn": "00", "pdno": "000001"}), \
             patch("core.kis_api.revise_to_market", return_value={"output": {}}) as mock_revise:
            oe.cancel_or_convert_to_market("999", krx_fwdg_ord_orgno="09999")
        mock_revise.assert_called_once_with("999", "09999", 3)

    def test_already_fully_filled_returns_already_done(self):
        with patch("core.kis_api.get_cancelable_order", return_value=None), \
             patch("core.kis_api.revise_to_market") as mock_revise:
            result = oe.cancel_or_convert_to_market("999")
        self.assertEqual(result, {"status": "already_done"})
        mock_revise.assert_not_called()

    def test_zero_psbl_qty_returns_already_done(self):
        with patch("core.kis_api.get_cancelable_order",
                    return_value={"psbl_qty": 0, "krx_fwdg_ord_orgno": "06010",
                                  "ord_dvsn": "00", "pdno": "000001"}), \
             patch("core.kis_api.revise_to_market") as mock_revise:
            result = oe.cancel_or_convert_to_market("999")
        self.assertEqual(result, {"status": "already_done"})
        mock_revise.assert_not_called()

    def test_query_failure_returns_none(self):
        with patch("core.kis_api.get_cancelable_order", side_effect=Exception("조회실패")), \
             patch("core.kis_api.revise_to_market") as mock_revise:
            result = oe.cancel_or_convert_to_market("999")
        self.assertIsNone(result)
        mock_revise.assert_not_called()

    def test_revise_failure_returns_none(self):
        with patch("core.kis_api.get_cancelable_order",
                    return_value={"psbl_qty": 5, "krx_fwdg_ord_orgno": "06010",
                                  "ord_dvsn": "00", "pdno": "000001"}), \
             patch("core.kis_api.revise_to_market", side_effect=Exception("정정실패")):
            result = oe.cancel_or_convert_to_market("999")
        self.assertIsNone(result)


class TestBuyTranche1WithFallback(OrderExecTestBase):
    """core.order_exec.buy_tranche1_with_fallback — 2026-08-08(최종검증에서
    발견·수정): 트랑쉬1 지정가가 안 채워지면 예전엔 그냥 halt했는데(9차 지정가
    효과분석이 가정했던 "미체결시 시장가전환"과 실제 구현이 달랐던 결함), 이제
    cancel_or_convert_to_market()로 자동 폴백하는지 검증. 직접 의존하는
    buy_safe_limit/verify_buy_fill/cancel_or_convert_to_market은 각자 자체
    테스트클래스가 있으므로 여기선 오케스트레이션 로직만 모킹으로 검증."""

    def test_fills_immediately_on_limit_no_fallback_needed(self):
        with patch("core.order_exec.buy_safe_limit", return_value=(True, "T1", "06010")), \
             patch("core.order_exec.verify_buy_fill", return_value=50) as mock_verify, \
             patch("core.order_exec.cancel_or_convert_to_market") as mock_conv:
            actual, odno = oe.buy_tranche1_with_fallback("000001", 50, 10000)
        self.assertEqual(actual, 50)
        self.assertEqual(odno, "T1")
        mock_conv.assert_not_called()
        mock_verify.assert_called_once()

    def test_order_submission_failure_returns_empty_odno(self):
        with patch("core.order_exec.buy_safe_limit", return_value=(False, "", "")):
            actual, odno = oe.buy_tranche1_with_fallback("000001", 50, 10000)
        self.assertEqual(actual, 0)
        self.assertEqual(odno, "")

    def test_unfilled_limit_converts_to_market_and_fills(self):
        with patch("core.order_exec.buy_safe_limit", return_value=(True, "T1", "06010")), \
             patch("core.order_exec.verify_buy_fill", side_effect=[0, 50]) as mock_verify, \
             patch("core.order_exec.cancel_or_convert_to_market",
                   return_value={"status": "converted", "psbl_qty": 50}) as mock_conv:
            actual, odno = oe.buy_tranche1_with_fallback("000001", 50, 10000)
        self.assertEqual(actual, 50)
        self.assertEqual(odno, "T1")
        mock_conv.assert_called_once_with("T1", "06010")
        self.assertEqual(mock_verify.call_count, 2)

    def test_conversion_still_unfilled_returns_partial(self):
        with patch("core.order_exec.buy_safe_limit", return_value=(True, "T1", "06010")), \
             patch("core.order_exec.verify_buy_fill", side_effect=[10, 10]), \
             patch("core.order_exec.cancel_or_convert_to_market",
                   return_value={"status": "converted", "psbl_qty": 40}):
            actual, odno = oe.buy_tranche1_with_fallback("000001", 50, 10000)
        self.assertEqual(actual, 10)
        self.assertEqual(odno, "T1")

    def test_conversion_already_done_rechecks_actual_holdings(self):
        with patch("core.order_exec.buy_safe_limit", return_value=(True, "T1", "06010")), \
             patch("core.order_exec.verify_buy_fill", side_effect=[20, 50]), \
             patch("core.order_exec.cancel_or_convert_to_market",
                   return_value={"status": "already_done"}):
            actual, odno = oe.buy_tranche1_with_fallback("000001", 50, 10000)
        self.assertEqual(actual, 50)

    def test_conversion_query_failure_keeps_initial_partial_qty(self):
        # cancel_or_convert_to_market이 None(조회/정정 자체 실패)이면 추가 시도
        # 없이 지금까지 확인된 수량만 반환 — "미확인 상태에서는 추가 주문을
        # 절대 내지 않는다" 원칙 그대로.
        with patch("core.order_exec.buy_safe_limit", return_value=(True, "T1", "06010")), \
             patch("core.order_exec.verify_buy_fill", return_value=15) as mock_verify, \
             patch("core.order_exec.cancel_or_convert_to_market", return_value=None):
            actual, odno = oe.buy_tranche1_with_fallback("000001", 50, 10000)
        self.assertEqual(actual, 15)
        self.assertEqual(odno, "T1")
        mock_verify.assert_called_once()  # 폴백 실패 후 재확인 안 함

    def test_zero_fill_even_after_conversion(self):
        with patch("core.order_exec.buy_safe_limit", return_value=(True, "T1", "06010")), \
             patch("core.order_exec.verify_buy_fill", return_value=0), \
             patch("core.order_exec.cancel_or_convert_to_market",
                   return_value={"status": "converted", "psbl_qty": 50}):
            actual, odno = oe.buy_tranche1_with_fallback("000001", 50, 10000)
        self.assertEqual(actual, 0)
        self.assertEqual(odno, "T1")  # odno는 있음(주문은 냈으나 끝내 미체결) — 호출부가 halt 처리


class TestVerifyBuyFill(OrderExecTestBase):
    def test_full_fill_confirmed(self):
        with patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 100}]}):
            actual = oe.verify_buy_fill("000001", 100)
        self.assertEqual(actual, 100)

    def test_partial_fill_after_retries_returns_actual(self):
        with patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 40}]}):
            actual = oe.verify_buy_fill("000001", 100)
        self.assertEqual(actual, 40)  # 기대치 미달이어도 실제 수량 그대로 반환(자동재시도 안 함)

    def test_zero_fill_returns_zero(self):
        with patch("core.kis_api.get_balance", return_value={"holdings": []}):
            actual = oe.verify_buy_fill("000001", 100)
        self.assertEqual(actual, 0)

    def test_balance_query_failure_treated_as_unconfirmed(self):
        with patch("core.kis_api.get_balance", side_effect=Exception("조회실패")):
            actual = oe.verify_buy_fill("000001", 100)
        self.assertEqual(actual, 0)

    def test_order_status_fastpath_confirms_before_balance(self):
        """2026-08-07 신규: odno를 넘기면 주문체결원장으로 잔고확인보다 먼저 확정될
        수 있어야 함(2026-08-07 halt 사고 재발방지 — 실제로 잔고반영이 원장보다
        늦었던 사례)."""
        with patch("core.kis_api.get_order_fill_qty", return_value=100), \
             patch("core.kis_api.get_balance", return_value={"holdings": []}) as mock_bal:
            actual = oe.verify_buy_fill("000001", 100, odno="ODNO1")
        self.assertEqual(actual, 100)
        mock_bal.assert_not_called()  # fast-path가 첫 폴링에서 즉시 확정해 잔고조회 자체가 불필요해짐

    def test_order_status_ambiguous_falls_back_to_balance(self):
        """주문체결원장 조회가 실패/모호(None)해도 기존 잔고폴링 안전망은 그대로 동작."""
        with patch("core.kis_api.get_order_fill_qty", return_value=None), \
             patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 100}]}):
            actual = oe.verify_buy_fill("000001", 100, odno="ODNO1")
        self.assertEqual(actual, 100)

    def test_no_odno_skips_fastpath_without_error(self):
        """odno 안 넘기면(기존 호출부 그대로 호환) 주문체결원장 조회 자체를 시도하지
        않고 기존 잔고폴링만으로 동작해야 함."""
        with patch("core.kis_api.get_order_fill_qty") as mock_status, \
             patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 100}]}):
            actual = oe.verify_buy_fill("000001", 100)
        self.assertEqual(actual, 100)
        mock_status.assert_not_called()


class TestPollRemainingShares(OrderExecTestBase):
    def test_confirms_expected_remaining(self):
        with patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 50}]}):
            remaining = oe.poll_remaining_shares("000001", expect_at_most=50)
        self.assertEqual(remaining, 50)

    def test_full_sell_confirms_ticker_gone(self):
        with patch("core.kis_api.get_balance", return_value={"holdings": []}):
            remaining = oe.poll_remaining_shares("000001", expect_at_most=0)
        self.assertEqual(remaining, 0)

    def test_never_drops_below_threshold_returns_none(self):
        with patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 100}]}):
            remaining = oe.poll_remaining_shares("000001", expect_at_most=0)
        self.assertIsNone(remaining)

    def test_order_status_fastpath_confirms_before_balance(self):
        """2026-08-07 신규: shares_before+odno 넘기면 주문체결원장(체결수량)으로
        '매도전수량-체결수량=잔여'를 잔고확인보다 먼저 계산해 확정할 수 있어야 함."""
        with patch("core.kis_api.get_order_fill_qty", return_value=100), \
             patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 100}]}) as mock_bal:
            remaining = oe.poll_remaining_shares("000001", expect_at_most=0, odno="ODNO1", shares_before=100)
        self.assertEqual(remaining, 0)
        mock_bal.assert_not_called()  # fast-path가 첫 폴링에서 즉시 확정해 잔고조회 자체가 불필요해짐

    def test_order_status_ambiguous_falls_back_to_balance(self):
        with patch("core.kis_api.get_order_fill_qty", return_value=None), \
             patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 0}]}):
            remaining = oe.poll_remaining_shares("000001", expect_at_most=0, odno="ODNO1", shares_before=100)
        self.assertEqual(remaining, 0)

    def test_no_shares_before_skips_fastpath(self):
        """odno는 있어도 shares_before(매도전수량)를 안 넘기면 남은수량을 역산할
        기준이 없으므로 fast-path 자체를 시도하지 않아야 함."""
        with patch("core.kis_api.get_order_fill_qty") as mock_status, \
             patch("core.kis_api.get_balance", return_value={"holdings": []}):
            remaining = oe.poll_remaining_shares("000001", expect_at_most=0, odno="ODNO1")
        self.assertEqual(remaining, 0)
        mock_status.assert_not_called()


class TestResolvePrices(OrderExecTestBase):
    def test_resolve_exit_price_uses_filled_price_first(self):
        with patch("core.kis_api.get_filled_price", return_value=10500.0):
            price = oe.resolve_exit_price("000001", "ODNO1", entry_price=10000)
        self.assertEqual(price, 10500.0)

    def test_resolve_exit_price_falls_back_to_current_quote(self):
        with patch("core.kis_api.get_filled_price", return_value=None), \
             patch("core.order_exec._get_price_safe", return_value={"price": 10300}):
            price = oe.resolve_exit_price("000001", "ODNO1", entry_price=10000)
        self.assertEqual(price, 10300)

    def test_resolve_exit_price_last_resort_entry_price(self):
        with patch("core.kis_api.get_filled_price", side_effect=Exception("실패")), \
             patch("core.order_exec._get_price_safe", return_value=None):
            price = oe.resolve_exit_price("000001", "ODNO1", entry_price=9999)
        self.assertEqual(price, 9999)

    def test_resolve_entry_price_uses_fresh_quote(self):
        with patch("core.order_exec._get_price_safe", return_value={"price": 10050}):
            price = oe.resolve_entry_price("000001", ref_price=10000)
        self.assertEqual(price, 10050)

    def test_resolve_entry_price_falls_back_to_ref_when_quote_fails(self):
        with patch("core.order_exec._get_price_safe", return_value=None):
            price = oe.resolve_entry_price("000001", ref_price=10000)
        self.assertEqual(price, 10000)

    def test_resolve_entry_price_uses_filled_price_first_when_odno_given(self):
        """2026-08-07 신규: odno 있으면 매도쪽과 동일하게 실제 체결가(get_filled_price)를
        최우선으로 써야 함 — 예전엔 매수만 재조회 현재가로 근사해 기록가가 부정확했음."""
        with patch("core.kis_api.get_filled_price", return_value=10500.0), \
             patch("core.order_exec._get_price_safe") as mock_quote:
            price = oe.resolve_entry_price("000001", ref_price=10000, odno="ODNO1")
        self.assertEqual(price, 10500.0)
        mock_quote.assert_not_called()  # 체결가로 확정됐으니 현재가 재조회 자체를 안 해야 함

    def test_resolve_entry_price_falls_back_to_quote_when_filled_price_unavailable(self):
        with patch("core.kis_api.get_filled_price", return_value=None), \
             patch("core.order_exec._get_price_safe", return_value={"price": 10050}):
            price = oe.resolve_entry_price("000001", ref_price=10000, odno="ODNO1")
        self.assertEqual(price, 10050)

    def test_resolve_entry_price_no_odno_behaves_as_before(self):
        """odno 안 넘기면(기존 호출부 호환) get_filled_price 자체를 시도하지 않고
        기존처럼 바로 현재가 재조회로 감."""
        with patch("core.kis_api.get_filled_price") as mock_filled, \
             patch("core.order_exec._get_price_safe", return_value={"price": 10050}):
            price = oe.resolve_entry_price("000001", ref_price=10000)
        self.assertEqual(price, 10050)
        mock_filled.assert_not_called()


class TestOrderStatusQty(OrderExecTestBase):
    """core.kis_api.get_order_fill_qty 및 order_exec._order_status_qty 래퍼 —
    2026-08-07 신규(체결지연 문제 대응). 실제 KIS 응답 필드명(tot_ccld_qty)은
    문서로 재검증 못 했으므로 파싱실패/필드없음 시나리오까지 방어적으로 검증."""

    def test_parses_tot_ccld_qty(self):
        with patch("core.kis_api._get", return_value={"output1": [{"tot_ccld_qty": "150"}]}):
            from core.kis_api import get_order_fill_qty
            qty = get_order_fill_qty("ODNO1", "000001")
        self.assertEqual(qty, 150)

    def test_missing_field_returns_none(self):
        with patch("core.kis_api._get", return_value={"output1": [{"other_field": "1"}]}):
            from core.kis_api import get_order_fill_qty
            qty = get_order_fill_qty("ODNO1", "000001")
        self.assertIsNone(qty)

    def test_api_failure_returns_none(self):
        with patch("core.kis_api._get", side_effect=Exception("네트워크 오류")):
            from core.kis_api import get_order_fill_qty
            qty = get_order_fill_qty("ODNO1", "000001")
        self.assertIsNone(qty)

    def test_order_exec_wrapper_no_odno_returns_none_without_api_call(self):
        with patch("core.kis_api.get_order_fill_qty") as mock_status:
            result = oe._order_status_qty("", "000001")
        self.assertIsNone(result)
        mock_status.assert_not_called()

    def test_order_exec_wrapper_swallows_exceptions(self):
        with patch("core.kis_api.get_order_fill_qty", side_effect=Exception("실패")):
            result = oe._order_status_qty("ODNO1", "000001")
        self.assertIsNone(result)


class TestComputeBuyQty(OrderExecTestBase):
    def test_caps_by_available_cash(self):
        with patch("core.kis_api.get_available_cash", return_value=5_000_000), \
             patch("core.kis_api.get_order_psbl_qty", return_value=10_000):
            qty = oe.compute_buy_qty("000001", ref_price=10000, target_notional=10_000_000)
        self.assertEqual(qty, 500)  # 500만원 / 10000원 = 500주(1000만원 목표보다 적은 가용현금이 제약)

    def test_size_factor_applies_after_cash_cap(self):
        # v2.8 mom5 사이징캡: 잔고전액모드(target_notional=inf)에서도 size_factor가
        # 실제 가용현금 캡 이후에 곱해져 반영돼야 함(target_notional 자체를 줄이는
        # 방식은 inf*factor=inf라 무효화되는 버그가 있었음, 구현 중 발견)
        with patch("core.kis_api.get_available_cash", return_value=10_000_000), \
             patch("core.kis_api.get_order_psbl_qty", return_value=10_000):
            qty = oe.compute_buy_qty("000001", ref_price=10000, target_notional=float("inf"),
                                      size_factor=0.5)
        self.assertEqual(qty, 500)  # 1000만원 x0.5 / 10000원 = 500주

    def test_size_factor_default_is_noop(self):
        with patch("core.kis_api.get_available_cash", return_value=5_000_000), \
             patch("core.kis_api.get_order_psbl_qty", return_value=10_000):
            qty = oe.compute_buy_qty("000001", ref_price=10000, target_notional=10_000_000)
        self.assertEqual(qty, 500)

    def test_caps_by_kis_psbl_qty(self):
        with patch("core.kis_api.get_available_cash", return_value=10_000_000), \
             patch("core.kis_api.get_order_psbl_qty", return_value=300):
            qty = oe.compute_buy_qty("000001", ref_price=10000, target_notional=10_000_000)
        self.assertEqual(qty, 300)  # 예산기준 1000주보다 KIS 상한(300)이 더 낮음

    def test_psbl_qty_failure_without_base_price_falls_back_to_naive_calc(self):
        # base_price(기준가)를 안 넘기면 상한가 보정을 할 수 없으니 예산기준 그대로 사용
        # (매수거부 위험 있는 경로 — 호출부는 가능하면 항상 base_price를 넘겨야 함)
        with patch("core.kis_api.get_available_cash", return_value=10_000_000), \
             patch("core.kis_api.get_order_psbl_qty", side_effect=Exception("실패")):
            qty = oe.compute_buy_qty("000001", ref_price=10000, target_notional=10_000_000)
        self.assertEqual(qty, 1000)

    def test_psbl_qty_failure_with_base_price_caps_by_upper_limit_estimate(self):
        # 2026-06-11 K3.1 사고 재현: get_order_psbl_qty 조회 실패시에도 base_price(기준가)가
        # 있으면 상한가(기준가x1.30) 직접추정으로 캡을 씌워야 함 -- 이게 없으면 옛날처럼
        # 현재가 기준 수량이 그대로 나가 매수거부(APBK0952)당할 수 있었음
        with patch("core.kis_api.get_available_cash", return_value=10_000_000), \
             patch("core.kis_api.get_order_psbl_qty", side_effect=Exception("실패")):
            qty = oe.compute_buy_qty("000001", ref_price=10000, target_notional=10_000_000,
                                      base_price=10000)
        # 예산기준 1000주(현재가10000원)였지만, 상한가 추정(10000x1.30=13000원)
        # 기준으로 다시 계산하면 1000만원/13000원=769주가 안전한 상한
        self.assertEqual(qty, 769)

    def test_psbl_qty_success_takes_precedence_over_base_price_estimate(self):
        # get_order_psbl_qty가 정상 응답하면 그게 우선(더 정확), base_price 추정은 안 씀
        with patch("core.kis_api.get_available_cash", return_value=10_000_000), \
             patch("core.kis_api.get_order_psbl_qty", return_value=850):
            qty = oe.compute_buy_qty("000001", ref_price=10000, target_notional=10_000_000,
                                      base_price=10000)
        self.assertEqual(qty, 850)

    def test_cash_and_balance_both_fail_returns_zero(self):
        with patch("core.kis_api.get_available_cash", side_effect=Exception("실패")), \
             patch("core.kis_api.get_balance", side_effect=Exception("역시 실패")):
            qty = oe.compute_buy_qty("000001", ref_price=10000, target_notional=10_000_000)
        self.assertEqual(qty, 0)

    def test_available_cash_500_falls_back_to_balance_with_safety_margin(self):
        # 2026-06-10 K3.1 사고 재현: get_available_cash 500에러 -> get_balance()["cash"]
        # (예수금 총액, T+2 미반영이라 더 큼)로 폴백하되 이번엔 3% 안전마진을 곱해야 함
        with patch("core.kis_api.get_available_cash", side_effect=Exception("500 Server Error")), \
             patch("core.kis_api.get_balance", return_value={"cash": 10_000_000, "holdings": []}), \
             patch("core.kis_api.get_order_psbl_qty", return_value=10_000):
            qty = oe.compute_buy_qty("000001", ref_price=10000, target_notional=10_000_000)
        # 1000만원 목표였지만 실제 쓸 수 있는 예산은 970만원(1000만원 예수금 x0.97 안전마진)
        self.assertEqual(qty, 970)

    def test_available_cash_retries_before_falling_back(self):
        calls = {"n": 0}
        def flaky():
            calls["n"] += 1
            if calls["n"] < 2:
                raise Exception("일시적 500")
            return 8_000_000
        with patch("core.kis_api.get_available_cash", side_effect=flaky), \
             patch("core.kis_api.get_order_psbl_qty", return_value=10_000):
            qty = oe.compute_buy_qty("000001", ref_price=10000, target_notional=10_000_000)
        self.assertEqual(qty, 800)  # 재시도 2번째에 성공(800만원) — get_balance 폴백까지 안 감
        self.assertEqual(calls["n"], 2)


class TestExecuteFullClose(OrderExecTestBase):
    def test_success(self):
        with patch("core.kis_api.sell_market", return_value={"output": {"ODNO": "1"}}), \
             patch("core.kis_api.get_balance", return_value={"holdings": []}), \
             patch("core.kis_api.get_order_fill_qty", return_value=None), \
             patch("core.kis_api.get_filled_price", return_value=9500.0):
            result = oe.execute_full_close("000001", 100, entry_price=10000, reason="손절", date="20260722")
        self.assertEqual(result, (9500.0, "1"))
        self.assertIsNone(oe.is_halted("20260722"))

    def test_order_failure_halts(self):
        with patch("core.kis_api.sell_market", side_effect=Exception("거부")):
            result = oe.execute_full_close("000001", 100, entry_price=10000, reason="손절", date="20260722")
        self.assertIsNone(result)
        self.assertIsNotNone(oe.is_halted("20260722"))

    def test_partial_remaining_unconfirmed_goes_pending_not_immediate_halt(self):
        # 2026-08-07: 전량매도 시도했는데 일부만 팔린 채 잔고에 남으면(미확인) 이제
        # 즉시 halt가 아니라 pending 등록 — 다음 틱에서 재확인(halt는 재시도한도
        # 초과시에만, 아래 TestPendingExit에서 검증).
        with patch("core.kis_api.sell_market", return_value={"output": {"ODNO": "2"}}), \
             patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 30}]}), \
             patch("core.kis_api.get_order_fill_qty", return_value=None):
            result = oe.execute_full_close("000001", 100, entry_price=10000, reason="익절", date="20260722")
        self.assertEqual(result, "pending")
        self.assertIsNone(oe.is_halted("20260722"))
        self.assertIsNotNone(oe.get_pending_exit("20260722"))

    def test_pending_exit_recorded_before_poll_completes(self):
        # 2026-08-18(08-10(17차) 발견2 수정): 주문접수~poll_remaining_shares
        # 완료 사이(최대45초)에 프로세스가 죽으면 다음 틱이 pending 기록을
        # 못 찾던 문제 — poll 호출 시점에 이미 pending이 기록돼 있어야 함.
        def _poll_checks_pending_already_set(*a, **k):
            self.assertIsNotNone(oe.get_pending_exit("20260722"),
                                  "poll_remaining_shares 호출 시점에 pending이 이미 기록돼 있어야 함")
            return 0
        with patch("core.kis_api.sell_market", return_value={"output": {"ODNO": "5"}}), \
             patch("core.order_exec.poll_remaining_shares", side_effect=_poll_checks_pending_already_set), \
             patch("core.order_exec.resolve_exit_price", return_value=9999.0):
            result = oe.execute_full_close("000001", 100, entry_price=10000, reason="손절", date="20260722")
        self.assertEqual(result, (9999.0, "5"))
        self.assertIsNone(oe.get_pending_exit("20260722"))  # 정상 체결확인되면 pending 해제돼야 함


class TestExecutePartialSell(OrderExecTestBase):
    def test_success(self):
        with patch("core.kis_api.sell_market", return_value={"output": {"ODNO": "3"}}), \
             patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 50}]}), \
             patch("core.kis_api.get_order_fill_qty", return_value=None), \
             patch("core.kis_api.get_filled_price", return_value=10880.0):
            result = oe.execute_partial_sell("000001", intended_qty=50, total_shares=100,
                                              entry_price=10000, date="20260722")
        self.assertEqual(result, (50, 50, 10880.0, "3"))

    def test_unconfirmed_goes_pending_not_immediate_halt(self):
        # 2026-08-07: 즉시 halt 아니고 pending(다음 틱 재확인 예정)
        with patch("core.kis_api.sell_market", return_value={"output": {"ODNO": "4"}}), \
             patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 100}]}), \
             patch("core.kis_api.get_order_fill_qty", return_value=None):
            result = oe.execute_partial_sell("000001", intended_qty=50, total_shares=100,
                                              entry_price=10000, date="20260722")
        self.assertEqual(result, "pending")
        self.assertIsNone(oe.is_halted("20260722"))
        pending = oe.get_pending_exit("20260722")
        self.assertEqual(pending["kind"], "partial")

    def test_pending_exit_recorded_before_poll_completes(self):
        # 2026-08-18(08-10(17차) 발견2 수정), full_close와 동일 원칙.
        def _poll_checks_pending_already_set(*a, **k):
            self.assertIsNotNone(oe.get_pending_exit("20260722"))
            return 50
        with patch("core.kis_api.sell_market", return_value={"output": {"ODNO": "6"}}), \
             patch("core.order_exec.poll_remaining_shares", side_effect=_poll_checks_pending_already_set), \
             patch("core.order_exec.resolve_exit_price", return_value=10880.0):
            result = oe.execute_partial_sell("000001", intended_qty=50, total_shares=100,
                                              entry_price=10000, date="20260722")
        self.assertEqual(result, (50, 50, 10880.0, "6"))
        self.assertIsNone(oe.get_pending_exit("20260722"))


class TestPendingExit(OrderExecTestBase):
    """core.order_exec.retry_pending_exit — 2026-08-07 신규(체결지연 halt 오탐
    재발방지). execute_full_close/execute_partial_sell이 "pending"을 반환한 뒤
    다음 monitor 틱에서 이걸 호출해 이어서 확인하는 흐름 전체를 검증.

    이 클래스의 모든 테스트가 poll_remaining_shares의 fast-path(주문체결원장
    조회)를 거치므로, get_order_fill_qty를 클래스 레벨로 기본 모킹해둔다(개별
    테스트에서 안 잊어버리게) — 2026-08-07 처음 이 파일 작성할 때 일부 테스트에서
    이걸 빠뜨려 실제 KIS API로 네트워크 요청이 나갈 뻔한 걸 뒤늦게 발견한 적 있음
    (pytest가 실제 텔레그램으로 오발신했던 사고와 같은 부류의 실수, 2026-08-03
    항목 참조) — 필요하면 개별 테스트 안에서 다시 patch해서 덮어쓰면 됨."""

    def setUp(self):
        super().setUp()
        patch("core.kis_api.get_order_fill_qty", return_value=None).start()
        self.addCleanup(patch.stopall)

    def test_no_pending_returns_none(self):
        self.assertIsNone(oe.retry_pending_exit("20260722"))

    def test_full_close_resolves_to_done_on_retry(self):
        with patch("core.kis_api.sell_market", return_value={"output": {"ODNO": "2"}}), \
             patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 30}]}):
            result = oe.execute_full_close("000001", 100, entry_price=10000, reason="익절", date="20260722")
        self.assertEqual(result, "pending")

        with patch("core.kis_api.get_balance", return_value={"holdings": []}), \
             patch("core.kis_api.get_filled_price", return_value=10800.0):
            follow_up = oe.retry_pending_exit("20260722")
        self.assertEqual(follow_up["status"], "done")
        self.assertEqual(follow_up["kind"], "full")
        self.assertEqual(follow_up["exit_price"], 10800.0)
        self.assertIsNone(oe.get_pending_exit("20260722"))  # 확정됐으니 pending 해제돼야 함
        self.assertIsNone(oe.is_halted("20260722"))  # halt 안 걸림

    def test_partial_sell_resolves_to_done_on_retry(self):
        with patch("core.kis_api.sell_market", return_value={"output": {"ODNO": "4"}}), \
             patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 100}]}):
            result = oe.execute_partial_sell("000001", intended_qty=50, total_shares=100,
                                              entry_price=10000, date="20260722")
        self.assertEqual(result, "pending")

        with patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 50}]}), \
             patch("core.kis_api.get_filled_price", return_value=10900.0):
            follow_up = oe.retry_pending_exit("20260722")
        self.assertEqual(follow_up["status"], "done")
        self.assertEqual(follow_up["kind"], "partial")
        self.assertEqual(follow_up["actual_qty"], 50)
        self.assertEqual(follow_up["remaining"], 50)
        self.assertEqual(follow_up["exit_price"], 10900.0)
        self.assertIsNone(oe.get_pending_exit("20260722"))

    def test_still_unconfirmed_stays_pending_no_halt(self):
        with patch("core.kis_api.sell_market", return_value={"output": {"ODNO": "2"}}), \
             patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 30}]}):
            oe.execute_full_close("000001", 100, entry_price=10000, reason="익절", date="20260722")

        with patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 30}]}):
            follow_up = oe.retry_pending_exit("20260722")
        self.assertEqual(follow_up["status"], "pending")
        self.assertEqual(follow_up["retry_count"], 1)
        self.assertIsNone(oe.is_halted("20260722"))
        self.assertIsNotNone(oe.get_pending_exit("20260722"))  # 계속 남아있어야 다음 틱도 재시도

    def test_exceeding_retry_max_finally_halts(self):
        """2026-08-07: 무한정 봐주진 않음 — PENDING_RETRY_MAX(5)번째도 안 되면 그때 halt."""
        with patch("core.kis_api.sell_market", return_value={"output": {"ODNO": "2"}}), \
             patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 30}]}):
            oe.execute_full_close("000001", 100, entry_price=10000, reason="익절", date="20260722")

        with patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 30}]}):
            for _ in range(oe.PENDING_RETRY_MAX - 1):
                follow_up = oe.retry_pending_exit("20260722")
                self.assertEqual(follow_up["status"], "pending")
                self.assertIsNone(oe.is_halted("20260722"))
            follow_up = oe.retry_pending_exit("20260722")
        self.assertEqual(follow_up["status"], "timeout")
        self.assertIsNotNone(oe.is_halted("20260722"))
        self.assertIsNone(oe.get_pending_exit("20260722"))  # halt로 넘어갔으니 pending은 정리돼야 함

    def test_new_sell_not_resubmitted_while_pending(self):
        """execute_full_close를 pending 상태에서 다시 호출해도(다음 틱에서 monitor가
        SL/TP를 재평가해 또 청산판단을 내릴 수 있음) 새 매도주문을 또 내면 안 됨 —
        이건 retry_pending_exit()를 통해서만 재확인해야 하고, execute_full_close
        자체는 pending을 모른 채 재호출되면 안 되므로 호출부(monitor.py)가 그
        순서를 지키는지는 별도 검증(여기선 order_exec 레벨의 sell_safe 미호출만 확인)."""
        with patch("core.kis_api.sell_market", return_value={"output": {"ODNO": "2"}}) as mock_sell, \
             patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 30}]}):
            oe.execute_full_close("000001", 100, entry_price=10000, reason="익절", date="20260722")
        self.assertEqual(mock_sell.call_count, 1)
        # retry_pending_exit는 sell_market을 전혀 호출하지 않아야 함(주문 재제출 없음)
        with patch("core.kis_api.sell_market") as mock_sell2, \
             patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 30}]}):
            oe.retry_pending_exit("20260722")
        mock_sell2.assert_not_called()


class TestTrancheSizes(unittest.TestCase):
    """2026-08-08 신규(분할매수/매도) — oe._tranche_sizes 순수함수 검증."""

    def test_even_split(self):
        self.assertEqual(oe._tranche_sizes(10, 2), [5, 5])

    def test_remainder_goes_to_last_tranche(self):
        self.assertEqual(oe._tranche_sizes(10, 3), [3, 3, 4])

    def test_fewer_shares_than_tranches_collapses(self):
        self.assertEqual(oe._tranche_sizes(1, 3), [1])

    def test_zero_shares_returns_empty(self):
        self.assertEqual(oe._tranche_sizes(0, 2), [])

    def test_single_tranche(self):
        self.assertEqual(oe._tranche_sizes(10, 1), [10])


class TestPendingEntryTranche(OrderExecTestBase):
    """core.order_exec.retry_pending_entry_tranche — 2026-08-08 신규(분할매수
    트랑쉬2). pending_exit과 같은 이유로 get_order_fill_qty를 기본 모킹해둠
    (fast-path 경유시 실제 KIS 호출 방지)."""

    def setUp(self):
        super().setUp()
        patch("core.kis_api.get_order_fill_qty", return_value=None).start()
        self.addCleanup(patch.stopall)

    def test_no_pending_returns_none(self):
        self.assertIsNone(oe.retry_pending_entry_tranche("20260722"))

    def test_register_and_get(self):
        oe.register_pending_tranche2("20260722", "000001", 50)
        pending = oe.get_pending_tranche2("20260722")
        self.assertEqual(pending["ticker"], "000001")
        self.assertEqual(pending["qty"], 50)
        self.assertEqual(pending["retry_count"], 0)

    def test_zero_qty_does_not_register(self):
        oe.register_pending_tranche2("20260722", "000001", 0)
        self.assertIsNone(oe.get_pending_tranche2("20260722"))

    def test_confirms_immediately(self):
        oe.register_pending_tranche2("20260722", "000001", 50)
        with patch("core.kis_api.buy_market", return_value={"output": {"ODNO": "T2"}}), \
             patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 50}]}):
            result = oe.retry_pending_entry_tranche("20260722")
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["qty"], 50)
        self.assertEqual(result["odno"], "T2")
        self.assertIsNone(oe.get_pending_tranche2("20260722"))

    def test_order_submission_failure_gives_up_immediately_no_halt(self):
        oe.register_pending_tranche2("20260722", "000001", 50)
        with patch("core.kis_api.buy_market", side_effect=Exception("거부")):
            result = oe.retry_pending_entry_tranche("20260722")
        self.assertEqual(result["status"], "gave_up")
        self.assertEqual(result["qty"], 0)
        self.assertIsNone(oe.get_pending_tranche2("20260722"))
        self.assertIsNone(oe.is_halted("20260722"))  # 매수 언더필=안전 원칙, halt 아님

    def test_unconfirmed_stays_pending_no_halt(self):
        oe.register_pending_tranche2("20260722", "000001", 50)
        with patch("core.kis_api.buy_market", return_value={"output": {"ODNO": "T2"}}), \
             patch("core.kis_api.get_balance", return_value={"holdings": []}):
            result = oe.retry_pending_entry_tranche("20260722")
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["retry_count"], 1)
        self.assertIsNone(oe.is_halted("20260722"))
        self.assertIsNotNone(oe.get_pending_tranche2("20260722"))

    def test_does_not_resubmit_order_on_resume(self):
        oe.register_pending_tranche2("20260722", "000001", 50)
        with patch("core.kis_api.buy_market", return_value={"output": {"ODNO": "T2"}}) as mock_buy, \
             patch("core.kis_api.get_balance", return_value={"holdings": []}):
            oe.retry_pending_entry_tranche("20260722")
        self.assertEqual(mock_buy.call_count, 1)
        with patch("core.kis_api.buy_market") as mock_buy2, \
             patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 50}]}):
            result = oe.retry_pending_entry_tranche("20260722")
        mock_buy2.assert_not_called()
        self.assertEqual(result["status"], "done")

    def test_exceeding_retry_max_gives_up_with_partial_no_halt(self):
        oe.register_pending_tranche2("20260722", "000001", 50)
        with patch("core.kis_api.buy_market", return_value={"output": {"ODNO": "T2"}}), \
             patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 20}]}):
            for _ in range(oe.PENDING_RETRY_MAX - 1):
                result = oe.retry_pending_entry_tranche("20260722")
                self.assertEqual(result["status"], "pending")
            result = oe.retry_pending_entry_tranche("20260722")
        self.assertEqual(result["status"], "gave_up")
        self.assertEqual(result["qty"], 20)
        self.assertIsNone(oe.is_halted("20260722"))
        self.assertIsNone(oe.get_pending_tranche2("20260722"))


class TestExecuteSplitClose(OrderExecTestBase):
    """core.order_exec.execute_split_close/_resume_split_sell/retry_pending_split_sell
    — 2026-08-08 신규(분할매도)."""

    def setUp(self):
        super().setUp()
        patch("core.kis_api.get_order_fill_qty", return_value=None).start()
        self.addCleanup(patch.stopall)

    def test_two_tranches_confirm_immediately_returns_blended_price(self):
        with patch("core.kis_api.sell_market", return_value={"output": {"ODNO": "S1"}}), \
             patch("core.kis_api.get_balance", side_effect=[
                 {"holdings": [{"ticker": "000001", "qty": 50}]},
                 {"holdings": [{"ticker": "000001", "qty": 0}]},
             ]), \
             patch("core.kis_api.get_filled_price", return_value=10500.0):
            result = oe.execute_split_close("000001", 100, entry_price=10000, reason="손절",
                                             date="20260722", n_tranches=2)
        self.assertIsInstance(result, tuple)
        price, odno = result
        self.assertEqual(price, 10500.0)
        self.assertIsNone(oe.get_pending_split_sell("20260722"))

    def test_first_tranche_order_failure_halts(self):
        with patch("core.kis_api.sell_market", side_effect=Exception("거부")):
            result = oe.execute_split_close("000001", 100, entry_price=10000, reason="손절",
                                             date="20260722", n_tranches=2)
        self.assertIsNone(result)
        self.assertIsNotNone(oe.is_halted("20260722"))

    def test_second_tranche_unconfirmed_returns_pending_and_saves_state(self):
        with patch("core.kis_api.sell_market", return_value={"output": {"ODNO": "S1"}}), \
             patch("core.kis_api.get_balance",
                   return_value={"holdings": [{"ticker": "000001", "qty": 50}]}), \
             patch("core.kis_api.get_filled_price", return_value=10500.0):
            result = oe.execute_split_close("000001", 100, entry_price=10000, reason="손절",
                                             date="20260722", n_tranches=2)
        self.assertEqual(result, "pending")
        pending = oe.get_pending_split_sell("20260722")
        self.assertIsNotNone(pending)
        self.assertEqual(pending["idx"], 1)  # 트랑쉬1 완료, 트랑쉬2 대기중
        self.assertEqual(len(pending["completed"]), 1)
        self.assertIsNone(oe.is_halted("20260722"))

    def test_resume_completes_second_tranche_with_correct_blend(self):
        with patch("core.kis_api.sell_market", return_value={"output": {"ODNO": "S1"}}), \
             patch("core.kis_api.get_balance",
                   return_value={"holdings": [{"ticker": "000001", "qty": 50}]}), \
             patch("core.kis_api.get_filled_price", return_value=10500.0):
            result = oe.execute_split_close("000001", 100, entry_price=10000, reason="손절",
                                             date="20260722", n_tranches=2)
        self.assertEqual(result, "pending")
        with patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 0}]}), \
             patch("core.kis_api.get_filled_price", return_value=10450.0):
            follow_up = oe.retry_pending_split_sell("20260722")
        self.assertEqual(follow_up["status"], "done")
        blended = (50 * 10500.0 + 50 * 10450.0) / 100
        self.assertAlmostEqual(follow_up["exit_price"], blended)
        self.assertIsNone(oe.get_pending_split_sell("20260722"))

    def test_resume_does_not_resubmit_confirmed_or_inflight_tranches(self):
        # 트랑쉬1이 그 자리에서 바로 확정되면 같은 execute_split_close 호출 안에서
        # 루프가 곧바로 트랑쉬2까지 이어서 제출한다(설계상 정상 — "같은 틱 안에서
        # 빠르게 연속제출"). 그래서 이 시점 sell_market 호출횟수는 2(트랑쉬1+2)가
        # 맞음 — 진짜 검증포인트는 "트랑쉬2가 미확인인 채로 남았을 때 재시도가
        # 그 주문을 또 내지 않고 기존 odno만 재확인하는지"임(이중매도 방지).
        with patch("core.kis_api.sell_market", return_value={"output": {"ODNO": "S1"}}) as mock_sell, \
             patch("core.kis_api.get_balance",
                   return_value={"holdings": [{"ticker": "000001", "qty": 50}]}), \
             patch("core.kis_api.get_filled_price", return_value=10500.0):
            result = oe.execute_split_close("000001", 100, entry_price=10000, reason="손절",
                                             date="20260722", n_tranches=2)
        self.assertEqual(result, "pending")
        self.assertEqual(mock_sell.call_count, 2)  # 트랑쉬1(확정)+트랑쉬2(미확인이지만 제출은 됨)
        with patch("core.kis_api.sell_market") as mock_sell2, \
             patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 0}]}), \
             patch("core.kis_api.get_filled_price", return_value=10450.0):
            follow_up = oe.retry_pending_split_sell("20260722")
        mock_sell2.assert_not_called()  # 재확인만, 새 매도주문 없음
        self.assertEqual(follow_up["status"], "done")

    def test_exceeding_retry_max_on_tranche_halts(self):
        # 트랑쉬1(잔여기대치50) 확인용으로도, 이후 트랑쉬2(잔여기대치0)가 계속
        # 미확인 상태로 남는 용도로도 잔고 50이 그대로 고정된 상황 — poll_remaining_
        # shares가 SPLIT_SELL_FILL_WAIT_SECS(3개)를 매번 다 순회하며 여러 번
        # get_balance를 부르므로 side_effect 리스트 대신 고정 return_value로 단순화.
        # 재시도 루프도 반드시 이 with 블록 안에서 실행해야 함 — 밖에서 하면 실제
        # KIS API로 나갈 뻔한다(2026-08-03 pytest 오발신 사고와 같은 부류의 실수,
        # 처음 작성 때 실제로 이 버그를 냈다가 여기서 잡음).
        with patch("core.kis_api.sell_market", return_value={"output": {"ODNO": "S1"}}), \
             patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 50}]}), \
             patch("core.kis_api.get_filled_price", return_value=10500.0):
            result = oe.execute_split_close("000001", 100, entry_price=10000, reason="손절",
                                             date="20260722", n_tranches=2)
            self.assertEqual(result, "pending")
            for _ in range(oe.PENDING_RETRY_MAX - 2):
                follow_up = oe.retry_pending_split_sell("20260722")
                self.assertEqual(follow_up["status"], "pending")
            follow_up = oe.retry_pending_split_sell("20260722")
        self.assertEqual(follow_up["status"], "halted")
        self.assertIsNotNone(oe.is_halted("20260722"))
        self.assertIsNone(oe.get_pending_split_sell("20260722"))

    def test_no_pending_returns_none(self):
        self.assertIsNone(oe.retry_pending_split_sell("20260722"))

    def test_single_share_collapses_to_one_tranche(self):
        with patch("core.kis_api.sell_market", return_value={"output": {"ODNO": "S1"}}), \
             patch("core.kis_api.get_balance", return_value={"holdings": [{"ticker": "000001", "qty": 0}]}), \
             patch("core.kis_api.get_filled_price", return_value=9500.0):
            result = oe.execute_split_close("000001", 1, entry_price=10000, reason="손절",
                                             date="20260722", n_tranches=3)
        price, odno = result
        self.assertEqual(price, 9500.0)


class TestHaltFlag(OrderExecTestBase):
    def test_not_halted_by_default(self):
        self.assertIsNone(oe.is_halted("20260722"))

    def test_set_and_read_halt(self):
        oe.set_halt("20260722", "000001", "테스트", "상세정보")
        halt = oe.is_halted("20260722")
        self.assertEqual(halt["ticker"], "000001")
        self.assertEqual(halt["action"], "테스트")

    def test_halt_scoped_per_date(self):
        oe.set_halt("20260722", "000001", "테스트", "상세")
        self.assertIsNone(oe.is_halted("20260723"))  # 다른 날짜엔 영향 없음


class TestFindUnresolvedHalt(OrderExecTestBase):
    """2026-08-08 코드감사 신규 — halt가 날짜별 키라 is_halted(오늘)만으론 어제
    이전의 미해결 halt를 못 보는 허점을 막는 함수."""

    def test_no_halt_returns_none(self):
        self.assertIsNone(oe.find_unresolved_halt())

    def test_finds_halt_from_a_previous_day(self):
        oe.set_halt("20260807", "459550", "매도체결미확인", "상세")
        found = oe.find_unresolved_halt()
        self.assertIsNotNone(found)
        self.assertEqual(found["date"], "20260807")
        self.assertEqual(found["ticker"], "459550")

    def test_cleared_halt_is_not_found(self):
        # iris_v2_halt.py --clear는 halt_key를 DELETE함 — state_set(None)이 아님.
        oe.set_halt("20260807", "459550", "매도체결미확인", "상세")
        from core.db import get_conn
        with get_conn() as conn:
            conn.execute("DELETE FROM bot_state WHERE key=?", (oe.halt_key("20260807"),))
        self.assertIsNone(oe.find_unresolved_halt())

    def test_multiple_unresolved_halts_returns_most_recent(self):
        oe.set_halt("20260805", "000001", "사유A", "상세")
        oe.set_halt("20260807", "000002", "사유B", "상세")
        found = oe.find_unresolved_halt()
        self.assertEqual(found["date"], "20260807")
        self.assertEqual(found["ticker"], "000002")

    def test_todays_halt_is_also_found(self):
        oe.set_halt("20260808", "000003", "사유C", "상세")
        found = oe.find_unresolved_halt()
        self.assertEqual(found["date"], "20260808")


if __name__ == "__main__":
    unittest.main()
