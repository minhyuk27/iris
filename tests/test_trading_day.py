"""
거래일 체크 테스트
실행: python -m pytest tests/test_trading_day.py -v
     또는 python tests/test_trading_day.py
"""
import sys
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))

# state_get/state_set은 kis_api 함수 내부에서 'from core.db import ...' 형태로 로컬 import됨
# → 패치 대상: 'core.db.state_get', 'core.db.state_set'
STATE_GET = "core.db.state_get"
STATE_SET = "core.db.state_set"


def _make_api_response(trading_dates: list, all_dates: list) -> dict:
    """chk-holiday API 응답 모킹 헬퍼"""
    output = [
        {"bass_dt": d, "bzdy_yn": "Y" if d in trading_dates else "N"}
        for d in all_dates
    ]
    return {"output": output, "ctx_area_nk": ""}


# 5월 날짜 목록 (테스트 공용)
MAY_DATES = [f"202605{d:02d}" for d in range(1, 31)]
MAY_TRADING = [
    "20260504", "20260506", "20260507", "20260508",
    "20260511", "20260512", "20260513", "20260514", "20260515",
    "20260518", "20260519", "20260520", "20260521", "20260522",
    "20260526", "20260527", "20260528", "20260529",
]
MAY_RESPONSE = _make_api_response(MAY_TRADING, MAY_DATES)


class TestIsTradingDay(unittest.TestCase):
    """is_trading_day() 단위 테스트"""

    @patch(STATE_GET, return_value=None)
    @patch(STATE_SET)
    @patch("core.kis_api._get", return_value=MAY_RESPONSE)
    def test_normal_tuesday(self, _api, _set, _get):
        """화요일 일반 거래일 → True"""
        from core.kis_api import is_trading_day
        self.assertTrue(is_trading_day("20260526"))

    @patch(STATE_GET, return_value=None)
    @patch(STATE_SET)
    @patch("core.kis_api._get", return_value=MAY_RESPONSE)
    def test_holiday_buddha(self, _api, _set, _get):
        """2026-05-25 부처님오신날 → False"""
        from core.kis_api import is_trading_day
        self.assertFalse(is_trading_day("20260525"))

    @patch(STATE_GET, return_value=None)
    @patch(STATE_SET)
    @patch("core.kis_api._get", return_value=MAY_RESPONSE)
    def test_holiday_childrens_day(self, _api, _set, _get):
        """2026-05-05 어린이날 → False"""
        from core.kis_api import is_trading_day
        self.assertFalse(is_trading_day("20260505"))

    @patch(STATE_GET, return_value=None)
    @patch(STATE_SET)
    @patch("core.kis_api._get", return_value=MAY_RESPONSE)
    def test_saturday_not_trading(self, _api, _set, _get):
        """토요일 → False"""
        from core.kis_api import is_trading_day
        self.assertFalse(is_trading_day("20260523"))

    @patch(STATE_GET, return_value=MAY_TRADING)   # 캐시 히트
    def test_cache_hit_trading(self, _get):
        """캐시 히트 시 API 미호출 — 거래일 True"""
        from core.kis_api import is_trading_day
        with patch("core.kis_api._get") as mock_api:
            result = is_trading_day("20260526")
            mock_api.assert_not_called()
        self.assertTrue(result)

    @patch(STATE_GET, return_value=MAY_TRADING)   # 캐시에 5/25 없음
    def test_cache_hit_holiday(self, _get):
        """캐시에 없는 날짜(공휴일) → False"""
        from core.kis_api import is_trading_day
        self.assertFalse(is_trading_day("20260525"))

    @patch(STATE_GET, return_value=None)
    @patch(STATE_SET)
    @patch("core.kis_api._get", side_effect=Exception("네트워크 오류"))
    def test_api_failure_fallback_weekday(self, _api, _set, _get):
        """API 오류 → 폴백: 평일(화요일)이면 True"""
        from core.kis_api import is_trading_day
        self.assertTrue(is_trading_day("20260526"))   # 화요일

    @patch(STATE_GET, return_value=None)
    @patch(STATE_SET)
    @patch("core.kis_api._get", side_effect=Exception("네트워크 오류"))
    def test_api_failure_fallback_weekend(self, _api, _set, _get):
        """API 오류 → 폴백: 토요일이면 False"""
        from core.kis_api import is_trading_day
        self.assertFalse(is_trading_day("20260523"))  # 토요일

    def test_timeout_does_not_block_on_executor_shutdown(self):
        """2026-08-18(08-10(17차) 발견1 수정): with문의 기본 shutdown(wait=True)
        때문에 30초 타임아웃이 실제로는 최대~100초까지 블로킹하던 문제 —
        shutdown(wait=False)로 호출부가 즉시 반환하는지 검증."""
        import concurrent.futures as cf
        from core.kis_api import is_trading_day

        mock_executor = MagicMock()
        mock_future = MagicMock()
        mock_future.result.side_effect = cf.TimeoutError()
        mock_executor.submit.return_value = mock_future

        with patch("concurrent.futures.ThreadPoolExecutor", return_value=mock_executor):
            result = is_trading_day("20260526")  # 화요일(평일 폴백 True)

        self.assertTrue(result)
        mock_executor.shutdown.assert_called_once_with(wait=False)


class TestGetTradingDays(unittest.TestCase):
    """get_trading_days() 캐싱 동작 테스트"""

    @patch(STATE_GET, return_value=None)
    @patch(STATE_SET)
    @patch("core.kis_api._get", return_value=MAY_RESPONSE)
    def test_returns_only_trading_days(self, _api, _set, _get):
        """거래일만 반환 (공휴일/주말 제외)"""
        from core.kis_api import get_trading_days
        days = get_trading_days("202605")
        self.assertIn("20260526", days)
        self.assertNotIn("20260525", days)   # 부처님오신날
        self.assertNotIn("20260505", days)   # 어린이날
        self.assertNotIn("20260523", days)   # 토요일

    @patch(STATE_GET, return_value=None)
    @patch(STATE_SET)
    @patch("core.kis_api._get", return_value=MAY_RESPONSE)
    def test_caches_after_api_call(self, _api, mock_set, _get):
        """API 호출 후 state_set으로 캐싱"""
        from core.kis_api import get_trading_days
        get_trading_days("202605")
        mock_set.assert_called_once()
        cache_key = mock_set.call_args[0][0]
        self.assertEqual(cache_key, "trading_days_202605")

    @patch(STATE_GET, return_value=MAY_TRADING)
    def test_no_api_call_when_cached(self, _get):
        """캐시 있으면 API 미호출"""
        from core.kis_api import get_trading_days
        with patch("core.kis_api._get") as mock_api:
            days = get_trading_days("202605")
            mock_api.assert_not_called()
        self.assertEqual(days, MAY_TRADING)


class TestActualHolidayDates(unittest.TestCase):
    """2026년 5월 실제 공휴일 / 거래일 검증 (캐시 주입)"""

    @patch(STATE_GET, return_value=MAY_TRADING)
    def test_20260525_is_holiday(self, _):
        """오늘(부처님오신날) → 거래일 아님"""
        from core.kis_api import is_trading_day
        self.assertFalse(is_trading_day("20260525"))

    @patch(STATE_GET, return_value=MAY_TRADING)
    def test_20260526_is_trading(self, _):
        """내일(화요일) → 거래일"""
        from core.kis_api import is_trading_day
        self.assertTrue(is_trading_day("20260526"))

    @patch(STATE_GET, return_value=MAY_TRADING)
    def test_20260522_is_trading(self, _):
        """지난 금요일 → 거래일"""
        from core.kis_api import is_trading_day
        self.assertTrue(is_trading_day("20260522"))

    @patch(STATE_GET, return_value=MAY_TRADING)
    def test_20260504_is_trading(self, _):
        """대체공휴일 다음날 월요일 → 거래일"""
        from core.kis_api import is_trading_day
        self.assertTrue(is_trading_day("20260504"))

    @patch(STATE_GET, return_value=MAY_TRADING)
    def test_20260505_is_holiday(self, _):
        """어린이날 → 휴장"""
        from core.kis_api import is_trading_day
        self.assertFalse(is_trading_day("20260505"))


class TestHTTPSessionReuse(unittest.TestCase):
    """core/kis_api.py의 requests.Session 재사용 설정 — 2026-08-07 신규(체결지연
    대응 리서치: 매 호출마다 새 커넥션 맺던 걸 커넥션풀 재사용으로 변경).
    실제 네트워크 동작(왕복시간 단축)까진 단위테스트로 확인 못 하니, 설정
    자체(Session·어댑터·풀사이즈)만 검증한다."""

    def test_session_configured_with_pooled_adapter(self):
        import requests
        from core import kis_api
        self.assertIsInstance(kis_api._session, requests.Session)
        adapter = kis_api._session.get_adapter("https://example.com")
        self.assertIsInstance(adapter, requests.adapters.HTTPAdapter)

    def test_pool_maxsize_covers_parallel_candidate_scan_workers(self):
        # bot/iris_v2_entry.py가 ThreadPoolExecutor(max_workers=10)로 후보를
        # 병렬조회 — 풀사이즈가 이보다 작으면 풀 자체가 새 병목이 됨
        from core import kis_api
        adapter = kis_api._session.get_adapter("https://example.com")
        self.assertGreaterEqual(adapter._pool_maxsize, 10)

    def test_get_and_post_use_shared_session(self):
        from core import kis_api
        with patch.object(kis_api, "_session") as mock_session, \
             patch.object(kis_api, "_rate_limit"), \
             patch.object(kis_api, "_headers", return_value={}):
            mock_session.get.return_value = MagicMock(
                raise_for_status=lambda: None, json=lambda: {"rt_cd": "0"})
            kis_api._get("/test", {}, "tr1")
            mock_session.get.assert_called_once()

            mock_session.post.return_value = MagicMock(
                raise_for_status=lambda: None, json=lambda: {"rt_cd": "0"})
            kis_api._post("/test", {}, "tr1")
            mock_session.post.assert_called_once()


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in [TestIsTradingDay, TestGetTradingDays,
                TestBotHolidayGuard, TestActualHolidayDates, TestHTTPSessionReuse]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
