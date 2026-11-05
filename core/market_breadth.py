"""
IRIS v2 — KOSDAQ 시장전체 50일선 위 비율 (2026-08-05 신규) +
KOSDAQ 시장유동성 백분위 (2026-08-11 신규, v2.9).

v2.5의 above_ma50 필터가 폭락장에서 후보풀을 거의 다 걸러내는 게 무진입의 주
원인으로 확인됨(iris DECISIONS 2026-08-04 참조) — 이 비율이 그 선행지표라
diag CLI와 저녁 후보계산 텔레그램 알림 양쪽에서 재사용.
"""
import pandas as pd


def kosdaq_above_ma50_pct(conn, date: str) -> float | None:
    """date 기준 KOSDAQ 종목 중 자기 50일선 위 비율(0~1). 50일선 계산에 필요한
    과거 데이터가 부족하면 None."""
    cutoff = (pd.Timestamp(date) - pd.Timedelta(days=110)).strftime("%Y%m%d")
    daily = pd.read_sql(
        "SELECT ticker, date, close FROM price_daily WHERE date >= ? AND date <= ?",
        conn, params=(cutoff, date),
    )
    uni = pd.read_sql("SELECT ticker, market FROM universe WHERE market='KOSDAQ'", conn)
    if daily.empty or uni.empty:
        return None
    daily = daily.sort_values(["ticker", "date"])
    daily["ma50"] = daily.groupby("ticker")["close"].transform(
        lambda s: s.rolling(50, min_periods=50).mean())
    daily = daily.merge(uni, on="ticker", how="inner")
    day = daily[(daily["date"] == date) & daily["ma50"].notna()]
    if day.empty:
        return None
    return float((day["close"] > day["ma50"]).mean())


def _get_kosdaq_daily_tv(conn, dates: list[str]) -> "pd.Series":
    """주어진 날짜들의 KOSDAQ 전종목 총거래대금(원)을 반환 — 이미
    market_liquidity_daily에 캐시된 날짜는 그대로 재사용하고, 없는 날짜만 새로
    계산해서 캐시에 저장한다(2026-08-17, v2.9.2). 한 번 캐시된 날짜의 값은
    이후 universe가 갱신돼도 다시 계산하지 않음 — kosdaq_liquidity_percentile()이
    매번 "그 시점 최신 universe"로 과거 날짜까지 재계산해 매주 월요일 universe
    갱신 전후로 같은 과거 날짜의 값이 흔들리던 문제의 근본수정(상세 배경은
    kosdaq_liquidity_percentile() docstring 참조)."""
    if not dates:
        return pd.Series(dtype=float)
    dates = sorted(set(dates))
    placeholders = ",".join("?" for _ in dates)
    cached = pd.read_sql(
        f"SELECT date, kosdaq_total_tv FROM market_liquidity_daily WHERE date IN ({placeholders})",
        conn, params=dates,
    ).set_index("date")["kosdaq_total_tv"]

    missing = sorted(set(dates) - set(cached.index))
    if missing:
        placeholders2 = ",".join("?" for _ in missing)
        daily = pd.read_sql(
            f"SELECT ticker, date, close, volume FROM price_daily WHERE date IN ({placeholders2})",
            conn, params=missing,
        )
        uni = pd.read_sql("SELECT ticker FROM universe WHERE market='KOSDAQ'", conn)
        if not daily.empty and not uni.empty:
            daily = daily.merge(uni, on="ticker", how="inner")
            daily["tv"] = daily["close"] * daily["volume"]
            fresh = daily.groupby("date")["tv"].sum()
            if len(fresh):
                conn.executemany(
                    "INSERT OR IGNORE INTO market_liquidity_daily(date, kosdaq_total_tv, computed_at) "
                    "VALUES (?, ?, datetime('now'))",
                    [(d, float(v)) for d, v in fresh.items()],
                )
                cached = fresh if cached.empty else pd.concat([cached, fresh])
    return cached.sort_index()


def kosdaq_liquidity_percentile(conn, date: str, lookback_days: int = 250,
                                 min_periods: int = 60) -> float | None:
    """date 기준 KOSDAQ 전종목 총거래대금(원)이 최근 lookback_days거래일 분포에서
    몇 %ile인지(0.0=최저~1.0=최고). 데이터가 min_periods 미만이면 판단 불가(None) —
    이 경우 호출측은 필터를 발동시키지 않는 것으로 취급해야 함(데이터 부족 =
    무조건 통과, 실수로 매매를 막지 않도록).

    진입 필터 자체가 아니라 "이 값이 계산 가능한가"만 보는 순수 조회 함수 —
    실제 필터 발동 임계값(LIQUIDITY_PERCENTILE_THRESHOLD)은 strategy/iris_v2_params.py.

    iris DECISIONS 2026-08-11 (67차)(68차) — 전일 KOSDAQ 총거래대금이 하위25%ile인
    날은 iris v2 성과가 뚜렷이 나쁨(EV≈0%/승률35.6%, 나머지 3분위는 EV+1.16~2.13%/
    승률49~59%)을 발견, 3중검증(최근구간 제외 재현·split-half·몬테카를로) 통과.

    2026-08-17(v2.9.2) — 종목별 총거래대금 합산은 매번 "그 시점 최신 universe"를
    썼는데, universe가 매주 월요일 갱신되다보니 그 갱신 전후로 "같은 과거 날짜"를
    다시 계산하면 대상종목 구성이 달라져 결과가 흔들리는 문제 발견(실거래 중,
    08-14 데이터 기준 필터발동여부가 금요일저녁 19%ile→월요일저녁 25%ile로 갈려
    필터 발동 자체가 뒤집힘 — 대상거래일이 휴장일 때문에 우연히 겹친 케이스,
    iris DECISIONS 2026-08-17 참조). `_get_kosdaq_daily_tv()`로 날짜별 합계를
    한 번 계산되면 캐시에 고정해 해결 — 계산 로직 자체(250일 롤링·백분위 산출)는
    무변경, 순수 안정성 수정(신호 자체가 달라지는 게 아니라 "같은 신호가 계산
    시점에 따라 흔들리던 것"을 고치는 것).
    """
    cutoff = (pd.Timestamp(date) - pd.Timedelta(days=int(lookback_days * 1.6))).strftime("%Y%m%d")
    all_dates = pd.read_sql(
        "SELECT DISTINCT date FROM price_daily WHERE date >= ? AND date <= ?",
        conn, params=(cutoff, date),
    )["date"].tolist()
    if not all_dates:
        return None
    mkt = _get_kosdaq_daily_tv(conn, all_dates).sort_index().tail(lookback_days)
    if len(mkt) < min_periods or date not in mkt.index:
        return None
    today_tv = mkt.loc[date]
    return float((mkt < today_tv).mean())


def percentile_label(pct: float) -> str:
    """0~1 백분위를 '상위 X%'/'하위 X%'로 변환(2026-08-11 신규 — "%ile"·"평균
    대비" 표현이 비율처럼 오해될 수 있다는 사용자 지적으로, 유동성이 좋은
    쪽이면 상위로 나쁜 쪽이면 하위로 자동 전환해 항상 작은 숫자·직관적인
    쪽으로 표시). pct=0.85(85번째 백분위, 상위 15%에 해당) -> "상위 15%",
    pct=0.15 -> "하위 15%". candidates.py·iris_v2_diag.py 공용."""
    if pct >= 0.5:
        return f"상위 {(1 - pct) * 100:.0f}%"
    return f"하위 {pct * 100:.0f}%"
