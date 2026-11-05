"""
IRIS - 시간 유틸 (타임존 독립)
────────────────────────────────────────────────────────────
모든 거래/수집 로직은 KST(Asia/Seoul) 기준으로 동작한다.
서버 로컬 타임존(Oracle=KST, Odroid=UTC)에 의존하지 않도록
이 모듈의 헬퍼만 사용한다.

  now_kst()                    → tz-aware datetime (KST)
  now_kst_naive()              → naive datetime, KST 벽시계 (KIS naive 타임스탬프 비교용)
  today_kst()                  → "YYYYMMDD"
  hms_kst()                    → "HHMMSS"
  ts_kst()                     → "YYYY-MM-DD HH:MM:SS"
  iso_kst()                    → ISO8601 문자열 (KST)
  next_trading_date(base=None) → 다음 KRX 거래일 "YYYYMMDD" (공휴일 포함 정확)
  latest_price_date()          → price_daily의 최신 날짜 (공휴일 폴백용)
"""
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    """tz-aware 현재 시각 (KST)."""
    return datetime.now(KST)


def now_kst_naive() -> datetime:
    """naive 현재 KST 벽시계. KIS가 주는 naive KST 문자열과 비교할 때 사용."""
    return datetime.now(KST).replace(tzinfo=None)


def today_kst() -> str:
    return datetime.now(KST).strftime("%Y%m%d")


def hms_kst() -> str:
    return datetime.now(KST).strftime("%H%M%S")


def ts_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def iso_kst() -> str:
    return datetime.now(KST).isoformat()


def next_trading_date(base: str | None = None) -> str:
    """
    base(YYYYMMDD, 기본=오늘 KST)의 다음 KRX 거래일.

    처리 순서:
      1) KIS API get_trading_days()로 해당 월+익월 거래일 목록 조회 (공휴일 포함)
      2) API 실패 시 주말만 스킵하는 폴백 사용
    6월3일(현충일), 추석, 설 등 공휴일을 정확히 건너뛴다.
    """
    if base is None:
        base = today_kst()

    base_dt = datetime.strptime(base, "%Y%m%d")
    candidate = base_dt + timedelta(days=1)

    try:
        from core.kis_api import get_trading_days
        # 후보 달과 다음 달 거래일 목록 확보 (월경계 처리)
        yyyymm_cur  = candidate.strftime("%Y%m")
        yyyymm_next = (candidate.replace(day=1) + timedelta(days=32)).strftime("%Y%m")
        trading = set(get_trading_days(yyyymm_cur)) | set(get_trading_days(yyyymm_next))

        # candidate부터 최대 30일 내에서 거래일 찾기
        for _ in range(30):
            ds = candidate.strftime("%Y%m%d")
            if ds in trading:
                return ds
            candidate += timedelta(days=1)
        # 30일 내 거래일 없으면 폴백
    except Exception:
        pass

    # 폴백: 주말만 스킵
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate.strftime("%Y%m%d")


def latest_price_date() -> str | None:
    """
    price_daily 테이블의 최신 날짜 반환.
    공휴일/장 없는 날엔 당일 데이터가 없으므로 전일 날짜를 기준으로
    후보 선정 로직이 폴백할 때 사용.
    """
    try:
        from core.db import get_conn
        with get_conn() as conn:
            row = conn.execute("SELECT MAX(date) d FROM price_daily").fetchone()
            return row["d"] if row else None
    except Exception:
        return None
