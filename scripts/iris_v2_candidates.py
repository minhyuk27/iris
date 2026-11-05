"""
iris v2 — 저녁 후보선정 배치 (Stage A, 로그기반 시뮬레이션).
전일 종가까지의 로컬 price_daily로 vol_ratio(D-1 shift, 룩어헤드 없음)·거래대금을 계산해
다음 거래일 후보(순위 1~N_CANDIDATES)를 candidates 테이블에 기록한다.

09:30 당일엔 이 목록을 순위대로 실시간 갭만 확인하면 되므로(iris_v2_entry.py),
무거운 계산은 전부 이 저녁 배치가 미리 끝내둔다 — 체결지연 최소화가 목적
(iris DECISIONS 2026-07-21 감사 참조).

cron: 10 19 * * 1-5 (Oracle→Odroid 19:00 동기화 직후)
실행: python3 -m scripts.iris_v2_candidates [--dry-run]
"""
import sys

import pandas as pd

from core.db import get_conn, state_get, state_set
from core.logger import get_logger
from core.timeutil import next_trading_date, today_kst, iso_kst
from strategy.iris_v2_params import (
    IRIS_V2_VERSION, TV_MIN, VOL_ROLL_WINDOW, VOL_ROLL_MIN_PERIODS,
    VOL_RATIO_MIN, STOCK_MA_WINDOW, REQUIRE_ABOVE_STOCK_MA,
    MOM5_WINDOW, MOM5_MIN, REQUIRE_KOSDAQ_ONLY, GAP_MIN, GAP_MAX, N_CANDIDATES, is_excluded,
    LIQUIDITY_FILTER_ENABLED, LIQUIDITY_LOOKBACK_DAYS, LIQUIDITY_MIN_PERIODS,
    LIQUIDITY_PERCENTILE_THRESHOLD,
)

logger = get_logger("iris_v2_candidates", "IRIS_V2")

BUFFER_DAYS = 70  # STOCK_MA_WINDOW(50거래일) 롤링에 필요한 여유(휴장일 감안 넉넉히, v2.1에서 60→70 상향)


def check_data_freshness(latest_date: str) -> bool:
    """오늘이 거래일인데 price_daily 최신 데이터가 오늘 것이 아니면 Oracle→Odroid
    동기화(19:00) 또는 그 이전 수집 파이프라인이 실패했을 가능성 — True면 정상
    (신선하거나, 오늘이 휴장일이라 어제 데이터 그대로가 정상인 경우 포함).
    2026-07-24 신규(v2.5.7): 이전엔 이 체크가 아예 없어서, 동기화가 실패해도
    candidates.py가 오래된 데이터로 조용히 계산하고 그 사실을 아무도 모를 수 있었음
    (결과적으로 다음날 후보가 과거 날짜로 잘못 기록돼 entry.py가 "후보없음"으로
    조용히 넘어가긴 하지만, "정상적으로 신호 없음"과 구분이 안 됐음)."""
    from core.kis_api import is_trading_day
    today = today_kst()
    if not is_trading_day(today):
        return True
    return latest_date == today


def build_candidates(dry_run: bool = False) -> tuple[str, pd.DataFrame]:
    """오늘 저녁 기준 다음 거래일의 후보 목록을 계산해 (target_date, df) 반환.

    dry_run=True(수동 점검용 호출)일 땐 신선도 이상 로그는 그대로 남기되 실제
    텔레그램 발송은 하지 않는다 — 2026-08-11 발견: 저녁 동기화(19:00) 전
    낮시간에 --dry-run으로 점검 삼아 돌리면 "오늘 데이터가 아직 없음"이 완전히
    정상인데도 매번 진짜 CRITICAL 알림이 나가 운영 알림에 잡음을 만들고 있었음
    (save_candidates/notify()는 이미 dry_run으로 걸러지는데 이 경로만 빠져있었음).
    상세: iris DECISIONS 2026-08-12."""
    with get_conn() as conn:
        latest_row = conn.execute("SELECT MAX(date) d FROM price_daily").fetchone()
        latest_date = latest_row["d"]
        if not check_data_freshness(latest_date):
            logger.critical(
                f"price_daily 최신일자({latest_date})가 오늘({today_kst()})이 아님 — "
                f"동기화/수집 파이프라인 확인 필요, 아래 후보는 오래된 데이터 기준일 수 있음"
            )
            if dry_run:
                logger.info("[dry-run] 데이터 신선도 텔레그램 미발송")
            else:
                from core.notify import send_ops, TAG_ALERT
                send_ops(
                    f"데이터 신선도 이상: price_daily 최신일자={latest_date}, 오늘={today_kst()} "
                    f"— Oracle 동기화(19:00) 또는 그 이전 수집 파이프라인 확인 필요",
                    TAG_ALERT,
                )
        cutoff = (pd.Timestamp(latest_date) - pd.Timedelta(days=BUFFER_DAYS * 1.6)).strftime("%Y%m%d")
        daily = pd.read_sql(
            "SELECT ticker, date, close, volume FROM price_daily WHERE date >= ? AND date <= ?",
            conn, params=(cutoff, latest_date),
        )
        uni = pd.read_sql("SELECT ticker, name, market, sector FROM universe", conn)

    target_date = next_trading_date(latest_date)

    # v2.9: 전일(latest_date) KOSDAQ 총거래대금이 최근 LIQUIDITY_LOOKBACK_DAYS
    # 분포에서 하위 LIQUIDITY_PERCENTILE_THRESHOLD 미만이면 그날 후보 자체를
    # 생성하지 않음(무진입) — 09:30 진입판단 시점에 이미 알 수 있는 값만 쓰므로
    # 룩어헤드 없음. 데이터 부족시엔 kosdaq_liquidity_percentile()이 None을
    # 반환해 필터가 자동으로 비활성(무조건 통과)됨. 상세: iris DECISIONS
    # 2026-08-11 (67차)(68차).
    if LIQUIDITY_FILTER_ENABLED:
        from core.market_breadth import kosdaq_liquidity_percentile, percentile_label
        with get_conn() as conn2:
            liq_pct = kosdaq_liquidity_percentile(
                conn2, latest_date, LIQUIDITY_LOOKBACK_DAYS, LIQUIDITY_MIN_PERIODS)
        if liq_pct is not None and liq_pct < LIQUIDITY_PERCENTILE_THRESHOLD:
            logger.warning(
                f"{target_date} 유동성필터 발동 — 전일({latest_date}) KOSDAQ 총거래대금이 "
                f"최근 {LIQUIDITY_LOOKBACK_DAYS}거래일 분포의 하위{liq_pct*100:.1f}%ile"
                f"(임계값 {LIQUIDITY_PERCENTILE_THRESHOLD*100:.0f}%ile 미만) — 오늘 후보 생성 안 함"
            )
            # 텔레그램용 사용자 친화 문구(로그 문구와 별도) — notify()가 이 값을 읽어 표시.
            # "%ile"·"평균 대비"는 비율처럼 오해될 수 있어(사용자 지적, 2026-08-11)
            # percentile_label()로 "하위 X%" 형태 통일.
            tg_reason = (f"전일 코스닥 거래대금이 최근 1년 기준 {percentile_label(liq_pct)}"
                         f"에 해당해 오늘은 매매하지 않습니다.")
            state_set(f"iris_v2_liquidity_skip_{target_date}", tg_reason)
            return target_date, pd.DataFrame()

    daily = daily.sort_values(["ticker", "date"])
    g = daily.groupby("ticker")
    daily["vol_roll"] = g["volume"].transform(
        lambda s: s.rolling(VOL_ROLL_WINDOW, min_periods=VOL_ROLL_MIN_PERIODS).mean()
    )
    # v2.1: 종목 자체 이동평균선(REQUIRE_ABOVE_STOCK_MA) — latest_date(=D-1)까지의 데이터만
    # 사용하므로 룩어헤드 없음. latest_date 자신도 윈도우에 포함(그 시점까지 알 수 있는 값).
    daily["stock_ma"] = g["close"].transform(
        lambda s: s.rolling(STOCK_MA_WINDOW, min_periods=STOCK_MA_WINDOW).mean()
    )
    # v2.3: 진입전 MOM5_WINDOW거래일 모멘텀 — latest_date(=D-1)까지의 종가만 사용, 룩어헤드 없음
    daily["mom5"] = g["close"].pct_change(MOM5_WINDOW)

    latest = daily[daily["date"] == latest_date].copy()
    latest["vol_ratio"] = latest["volume"] / daily.loc[latest.index, "vol_roll"]
    latest["tv"] = latest["close"] * latest["volume"]
    latest["above_stock_ma"] = latest["close"] > latest["stock_ma"]

    cand = latest.merge(uni, on="ticker", how="left")
    unmatched = cand[cand["name"].isna()]
    if len(unmatched):
        # universe에 없는 티커(신규상장 타이밍 어긋남 등) — 이름/섹터를 알 수 없으니
        # 안전하게 배제. is_excluded()에 NaN을 그대로 넘기면 과거엔 TypeError로 저녁
        # 배치 전체가 죽었음(2026-07-22 발견·수정) — 지금은 크래시하지 않지만, 그렇다고
        # "제외 키워드에 안 걸리니 통과"시키는 것도 위험해 여기서 명시적으로 걸러낸다.
        logger.warning(f"universe 미매칭 티커 {len(unmatched)}건 제외: {list(unmatched['ticker'])}")
    cand = cand[cand["name"].notna()]
    cand = cand[~cand.apply(lambda r: is_excluded(r["name"], r.get("sector", "")), axis=1)]
    cand = cand[cand["tv"] >= TV_MIN]
    cand = cand[cand["vol_ratio"].notna()]
    cand = cand[cand["vol_ratio"] >= VOL_RATIO_MIN]
    if REQUIRE_ABOVE_STOCK_MA:
        cand = cand[cand["above_stock_ma"] == True]
    if REQUIRE_KOSDAQ_ONLY:
        cand = cand[cand["market"] == "KOSDAQ"]
    cand = cand[cand["mom5"].notna() & (cand["mom5"] >= MOM5_MIN)]
    cand = cand.sort_values("vol_ratio", ascending=False).head(N_CANDIDATES).reset_index(drop=True)
    cand["rank_no"] = range(1, len(cand) + 1)
    cand["prev_close"] = cand["close"]  # 전일종가(=조회기준일 종가) — entry 스크립트의 갭계산 기준
    return target_date, cand


def save_candidates(target_date: str, cand: pd.DataFrame, dry_run: bool = False):
    if dry_run:
        logger.info(f"[dry-run] {target_date} 후보 {len(cand)}건 (DB 미기록)")
        return
    # 컬럼 재사용(스키마 변경 없이): score=vol_ratio, total_amt=전일거래대금,
    # avg_val=전일종가(entry 스크립트가 이 값으로 실시간 갭 계산),
    # val_ratio=진입전5일모멘텀(mom5, v2.8 신규 — entry.py의 mom5 사이징캡용.
    # 이 컬럼 원래 미사용이었음(항상 기본값 0), iris DECISIONS 2026-08-07 (3차) 참조)
    with get_conn() as conn:
        conn.execute("DELETE FROM candidates WHERE trade_date=? AND strategy='iris_v2'", (target_date,))
        for _, r in cand.iterrows():
            conn.execute(
                """INSERT INTO candidates
                   (trade_date, ticker, name, sector, score, total_amt, avg_val,
                    marcap_bil, rank_no, strategy, strategy_version, val_ratio)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (target_date, r["ticker"], r["name"], r.get("sector", ""), float(r["vol_ratio"]),
                 float(r["tv"]), float(r["prev_close"]), 0.0, int(r["rank_no"]),
                 "iris_v2", IRIS_V2_VERSION, float(r["mom5"])),
            )


def _breadth_line() -> str:
    """KOSDAQ 50일선 위 비율 한 줄 — 후보풀이 왜 이 크기인지 맥락 제공(2026-08-05
    신규, iris DECISIONS 2026-08-04 진단 참조). 계산 실패해도 알림 자체는 막지 않음."""
    try:
        from core.market_breadth import kosdaq_above_ma50_pct
        with get_conn() as conn:
            latest = conn.execute("SELECT MAX(date) d FROM price_daily").fetchone()["d"]
            frac = kosdaq_above_ma50_pct(conn, latest) if latest else None
        if frac is None:
            return ""
        return f"시장여건: KOSDAQ 50일선 위 {frac*100:.1f}%({latest})"
    except Exception:
        return ""


def _liquidity_line() -> str:
    """KOSDAQ 시장유동성 백분위 한 줄 — 유동성필터(v2.9) 발동 여부와 무관하게
    후보 있음/없음 알림 양쪽에 항상 붙여 매일 흐름을 볼 수 있게 함(2026-08-11
    신규, _breadth_line()과 동일 패턴 — 사용자 요청). 계산 실패·데이터부족시
    빈 문자열(알림 자체는 막지 않음)."""
    try:
        from core.market_breadth import kosdaq_liquidity_percentile, percentile_label
        with get_conn() as conn:
            latest = conn.execute("SELECT MAX(date) d FROM price_daily").fetchone()["d"]
            pct = kosdaq_liquidity_percentile(
                conn, latest, LIQUIDITY_LOOKBACK_DAYS, LIQUIDITY_MIN_PERIODS) if latest else None
        if pct is None:
            return ""
        flag = " ⚠️필터임계값 미만" if pct < LIQUIDITY_PERCENTILE_THRESHOLD else ""
        return f"유동성: {percentile_label(pct)}({latest}){flag}"
    except Exception:
        return ""


def notify(target_date: str, cand: pd.DataFrame):
    from core.notify import send_ops, esc, TAG_CANDIDATE
    lines = []
    breadth = _breadth_line()
    if breadth:
        lines.append(breadth)
    liquidity = _liquidity_line()
    if liquidity:
        lines.append(liquidity)
    if cand.empty:
        # v2.9: 유동성필터 발동으로 인한 무후보는 build_candidates()가 state에
        # 남겨둔 사유가 있음 — 있으면 그걸 우선 표시(기존 "필터 통과 0건"
        # 문구는 여기 해당 안 되므로 헷갈리지 않게 구분).
        liq_reason = state_get(f"iris_v2_liquidity_skip_{target_date}")
        if liq_reason:
            lines.append(f"<b>{target_date} 후보 없음 (유동성 부족)</b>")
            lines.append(liq_reason)
        else:
            lines.append(f"<b>{target_date} 후보 없음</b>")
            lines.append("필터(거래대금·거래량비율·50일선·모멘텀·코스닥) 통과 종목 0건 — 내일 무진입 예정")
        send_ops("\n".join(lines), TAG_CANDIDATE)
        return
    # v2.5.2: N_CANDIDATES가 50으로 늘어(v2.5.1) 전체 나열은 텔레그램 스팸 — 상위 10개만
    NOTIFY_TOP = 10
    lines.append(f"<b>{target_date} 후보 {len(cand)}건</b> (09:30 갭 {GAP_MIN*100:.1f}~{GAP_MAX*100:.1f}% 확인 예정)")
    for _, r in cand.head(NOTIFY_TOP).iterrows():
        lines.append(f"{int(r['rank_no'])}. {esc(r['name'])}({r['ticker']}) 거래량비율 {r['vol_ratio']:.2f}")
    if len(cand) > NOTIFY_TOP:
        lines.append(f"… 외 {len(cand) - NOTIFY_TOP}건 (전체는 candidates 테이블 참조)")
    send_ops("\n".join(lines), TAG_CANDIDATE)


def main():
    dry_run = "--dry-run" in sys.argv
    target_date, cand = build_candidates(dry_run=dry_run)
    logger.info(f"target_date={target_date} candidates={len(cand)}")
    for _, r in cand.iterrows():
        logger.info(f"  #{int(r['rank_no'])} {r['ticker']} {r['name']} vol_ratio={r['vol_ratio']:.2f} tv={r['tv']:.0f}")
    save_candidates(target_date, cand, dry_run=dry_run)
    if not dry_run:
        notify(target_date, cand)
    else:
        logger.info("[dry-run] 텔레그램 미발송")


if __name__ == "__main__":
    from core.script_guard import run_guarded
    from core.proc_lock import script_lock, LockHeld
    try:
        with script_lock("iris_v2_candidates"):
            run_guarded(main, "iris_v2_candidates", logger, "--dry-run" in sys.argv)
    except LockHeld:
        logger.warning("이전 iris_v2_candidates 실행이 아직 안 끝나 이번 실행 스킵(중복실행 방지)")
