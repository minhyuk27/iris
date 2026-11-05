"""
core/script_guard.py 단위테스트 — 예상 못한 예외가 나도 반드시 텔레그램 알림 후
재발생하는지 검증(cron이 조용히 죽어서 아무 알림도 안 가는 걸 막는 최종 안전망).

실행: python -m pytest tests/test_script_guard.py -v
"""
import logging
import unittest
from unittest.mock import patch, MagicMock

from core.script_guard import run_guarded


class TestRunGuarded(unittest.TestCase):
    def setUp(self):
        self.logger = MagicMock(spec=logging.Logger)

    def test_success_no_alert(self):
        with patch("core.notify.send_telegram") as mock_tg:
            run_guarded(lambda: None, "test_script", self.logger, dry_run=False)
        mock_tg.assert_not_called()
        self.logger.critical.assert_not_called()

    def test_unexpected_exception_alerts_and_reraises(self):
        def boom():
            raise ValueError("예상못한버그")

        with patch("core.notify.send_telegram") as mock_tg:
            with self.assertRaises(ValueError):
                run_guarded(boom, "test_script", self.logger, dry_run=False)
        mock_tg.assert_called_once()
        self.assertIn("test_script", mock_tg.call_args[0][0])
        self.logger.critical.assert_called_once()

    def test_dry_run_does_not_send_telegram_but_still_reraises(self):
        def boom():
            raise RuntimeError("dry-run 중 버그")

        with patch("core.notify.send_telegram") as mock_tg:
            with self.assertRaises(RuntimeError):
                run_guarded(boom, "test_script", self.logger, dry_run=True)
        mock_tg.assert_not_called()

    def test_telegram_send_failure_does_not_swallow_original_exception(self):
        def boom():
            raise ValueError("원래버그")

        with patch("core.notify.send_telegram", side_effect=Exception("텔레그램도 실패")):
            with self.assertRaises(ValueError):
                run_guarded(boom, "test_script", self.logger, dry_run=False)


if __name__ == "__main__":
    unittest.main()
