"""
IRIS - 공통 로거
콘솔 + DB(trade_log) 동시 기록. 파일 기록은 각 스크립트를 실행하는 crontab 라인의
`>> logs/{name}.log 2>&1` 리다이렉트에 맡긴다(2026-08-03 발견: 예전엔 여기서도
FileHandler로 같은 경로에 직접 썼는데, crontab 리다이렉트가 StreamHandler의 stdout을
같은 파일에 또 append해 모든 줄이 항상 두 번씩 찍히고 있었음 — 게다가 pytest가 모듈을
import만 해도 이 로거가 만들어져 프로덕션 로그 파일에 테스트용 가짜 데이터까지 섞여
들어갔음. FileHandler를 없애면 두 문제 다 해결되고, cron 리다이렉트는 logger를 거치지
않는 순수 traceback/print까지 잡아주므로 안전망으로 그대로 둔다).

⚠️ Odroid 서버 자체는 UTC로 동작(Oracle은 KST) — logging.Formatter가 기본으로 쓰는
time.localtime()을 그대로 두면 로그 파일/콘솔 타임스탬프가 실제 KST보다 9시간 느리게
찍힌다(2026-07-22 발견: 실거래 전환 앞두고 로그로 시각을 추적할 때 심각한 혼란 위험).
아래 KST_CONVERTER로 강제 변환.
"""
import logging
import sys
import time
from datetime import datetime

from core.timeutil import KST


def _kst_converter(timestamp):
    """logging.Formatter.converter — asctime을 KST 기준으로 계산.
    time.gmtime()은 서버 자신의 타임존 설정과 무관하게 항상 UTC로 해석하므로,
    여기에 KST 오프셋(+9시간)만 더해 넘기면 서버가 UTC든 KST든 항상 올바른 결과를 낸다
    (time.localtime()을 쓰면 서버 타임존 설정에 이중으로 의존하게 돼 더 위험함)."""
    return time.gmtime(timestamp + KST.utcoffset(None).total_seconds())


def get_logger(name: str, category: str = "SYSTEM") -> logging.Logger:
    """
    name     : 로거 이름 (예: "bot", "prefilter")
    category : DB 기록용 카테고리 (BOT / PREFILTER / COLLECT / SYSTEM)
    """
    logger = logging.getLogger(f"iris.{name}")
    if logger.handlers:  # 이미 설정된 경우 재사용
        return logger

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fmt.converter = _kst_converter

    # 콘솔 (cron이 이 stdout을 logs/{name}.log로 리다이렉트 — 파일 기록은 여기 위임)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # DB 핸들러
    db_handler = _DBHandler(category)
    db_handler.setFormatter(fmt)
    logger.addHandler(db_handler)

    logger.propagate = False
    return logger


class _DBHandler(logging.Handler):
    """로그를 trade_log 테이블에 기록"""

    def __init__(self, category: str):
        super().__init__()
        self.category = category

    def emit(self, record: logging.LogRecord):
        # DB import는 emit 시점에 — 순환 import 방지
        try:
            from core.db import get_conn
            ts  = datetime.fromtimestamp(record.created, KST).isoformat()
            msg = self.format(record)
            with get_conn() as conn:
                conn.execute(
                    "INSERT INTO trade_log(ts, level, category, message) VALUES(?,?,?,?)",
                    (ts, record.levelname, self.category, record.getMessage())
                )
        except Exception:
            pass  # DB 쓰기 실패해도 로깅 자체는 계속
