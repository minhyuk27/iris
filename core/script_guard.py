"""
IRIS v2 — 스크립트 최종 안전망 (2026-07-23 신규).

각 봇/배치 스크립트는 예상 가능한 실패(시세조회 실패, 주문 실패 등)를 이미
개별적으로 처리하지만, 그 외 완전히 예상 못한 예외(DB 잠금, 코드 버그 등)가 나면
지금까지는 cron이 그냥 조용히 죽고 아무 알림도 안 갔다 — 로그를 직접 열어보기 전엔
그날 스크립트가 아예 안 돈 것처럼 보임. run_guarded()가 이 마지막 구멍을 막는다:
예외를 CRITICAL로 로그(전체 traceback 포함)하고 반드시 텔레그램(운영봇)으로 알린 뒤
그대로 재발생시킨다(cron 로그에도 정상적으로 실패가 기록되도록, 조용히 삼키지 않음).
"""
from typing import Callable


def run_guarded(fn: Callable, script_name: str, logger, dry_run: bool):
    try:
        fn()
    except Exception as e:
        logger.critical(f"{script_name} 예상치 못한 오류로 중단: {e}", exc_info=True)
        if not dry_run:
            try:
                from core.notify import send_ops, TAG_ALERT
                send_ops(
                    f"<b>{script_name}</b> 스크립트가 예상치 못한 오류로 중단됨\n"
                    f"{type(e).__name__}: {e}\n로그(logs/{script_name}.log) 확인 필요",
                    TAG_ALERT,
                )
            except Exception:
                pass
        raise
