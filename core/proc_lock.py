"""
iris v2 — 스크립트 중복실행 방지 (2026-07-24 신규, v2.5.7).

monitor.py는 매분 cron이 새 프로세스로 기동한다. 청산 처리 중(매도 체결확인 폴링은
최대 45초까지 걸릴 수 있음, FILL_WAIT_SECS)에 다음 분의 cron이 그대로 겹쳐
기동되면, 두 프로세스가 같은 open 포지션을 동시에 보고 둘 다 매도를 시도할 위험이
있다(이중매도) — 지금까지는 이 경합을 막는 장치가 전혀 없었다.

파일락(flock, non-blocking)으로 이전 실행이 안 끝났으면 그냥 조용히 스킵한다 —
"확실하지 않으면 멈춘다"와 같은 원칙: 스킵해도 다음 분에 다시 시도하면 되므로
겹쳐서 도는 것보다 항상 안전하다.
"""
import fcntl
from contextlib import contextmanager
from pathlib import Path

from core.config import BASE_DIR

LOCK_DIR = BASE_DIR / "locks"


class LockHeld(Exception):
    """이전 실행이 아직 락을 쥐고 있음 — 에러가 아니라 정상적인 스킵 신호."""


@contextmanager
def script_lock(name: str):
    """with script_lock("iris_v2_monitor"): ...  획득 실패시 LockHeld 예외."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = LOCK_DIR / f"{name}.lock"
    f = open(lock_path, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise LockHeld(name)
    try:
        yield
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()
