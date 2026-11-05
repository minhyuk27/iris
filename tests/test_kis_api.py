"""
core/kis_api.py 단위테스트 — 2026-08-07 신규(④지정가 주문): get_cancelable_order()의
정정취소가능주문조회 응답 파싱/필터링 로직만 검증(그 외 buy_market 등 단순 body조립
함수는 기존 관례대로 core/order_exec.py 쪽에서 모킹 테스트, 여기선 실제 로직이 있는
것만 직접 테스트). HTTP 호출은 _get을 모킹해 네트워크 없이 검증.

실행: python -m pytest tests/test_kis_api.py -v
"""
import unittest
from unittest.mock import patch

import core.kis_api as kis


class TestGetCancelableOrder(unittest.TestCase):
    def test_finds_matching_odno(self):
        response = {"output": [
            {"odno": "111", "psbl_qty": "5", "ord_gno_brno": "06010",
             "ord_dvsn_cd": "00", "pdno": "000001"},
            {"odno": "999", "psbl_qty": "10", "ord_gno_brno": "06020",
             "ord_dvsn_cd": "00", "pdno": "000002"},
        ]}
        with patch("core.kis_api._get", return_value=response):
            info = kis.get_cancelable_order("999")
        self.assertEqual(info, {
            "psbl_qty": 10, "krx_fwdg_ord_orgno": "06020",
            "ord_dvsn": "00", "pdno": "000002",
        })

    def test_no_matching_odno_returns_none(self):
        response = {"output": [
            {"odno": "111", "psbl_qty": "5", "ord_gno_brno": "06010",
             "ord_dvsn_cd": "00", "pdno": "000001"},
        ]}
        with patch("core.kis_api._get", return_value=response):
            info = kis.get_cancelable_order("999")
        self.assertIsNone(info)

    def test_empty_output_returns_none(self):
        with patch("core.kis_api._get", return_value={"output": []}):
            info = kis.get_cancelable_order("999")
        self.assertIsNone(info)

    def test_missing_psbl_qty_defaults_to_zero(self):
        response = {"output": [
            {"odno": "999", "ord_gno_brno": "06020", "ord_dvsn_cd": "00", "pdno": "000002"},
        ]}
        with patch("core.kis_api._get", return_value=response):
            info = kis.get_cancelable_order("999")
        self.assertEqual(info["psbl_qty"], 0)


if __name__ == "__main__":
    unittest.main()
