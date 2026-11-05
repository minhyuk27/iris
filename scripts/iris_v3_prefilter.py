"""
iris v3 — 저녁 사전필터 배치.

v3의 갭(오늘시가/전일종가-1)은 오늘 장이 열려야 알 수 있는 값이라 v2처럼
"저녁에 후보를 확정"할 수 없다 — 대신 갭과 무관하게 전일 데이터만으로
계산 가능한 tv_ma5(5일평균거래대금) 필터만 저녁에 미리 걸어, 다음날
장시작 직후(scripts.iris_v3_candidates) 실시간 시가를 조회할 대상을
좁혀둔다(KOSDAQ 전종목 대신 수백 종목만 조회하면 되게 — v2 entry.py가
전체 유니버스가 아니라 저녁에 이미 좁혀둔 후보만 실시간 조회하는 것과
같은 이유).

cron: 15 10 * * 1-5 (서버는 UTC로 동작 — v2 candidates 직후, KST 19:15)
실행: python3 -m scripts.iris_v3_prefilter [--dry-run]
"""
import sys

import pandas as pd

from core.db import get_conn, state_set
from core.logger import get_logger
from core.timeutil import next_trading_date, today_kst
from strategy.iris_v3_params import IRIS_V3_VERSION, TV_MA5_LOOKBACK, TV_MA5_MIN, is_excluded

logger = get_logger("iris_v3_prefilter", "IRIS_V3")

BUFFER_DAYS = 15  # TV_MA5_LOOKBACK(5거래일) 롤링에 필요한 여유(휴장일 감안)


def build_prefilter(dry_run: bool = False) -> tuple[str, pd.DataFrame]:
    """오늘 저녁 기준 다음 거래일에 실시간 갭을 확인할 KOSDAQ 종목 목록을
    계산해 (target_date, df) 반환. df 컬럼: ticker, name, prev_close, tv_ma5."""
    with get_conn() as conn:
        latest_row = conn.execute("SELECT MAX(date) d FROM price_daily").fetchone()
        latest_date = latest_row["d"]
        cutoff = (pd.Timestamp(latest_date) - pd.Timedelta(days=BUFFER_DAYS)).strftime("%Y%m%d")
        daily = pd.read_sql(
            "SELECT ticker, date, close, volume FROM price_daily WHERE date >= ? AND date <= ?",
            conn, params=(cutoff, latest_date),
        )
        uni = pd.read_sql("SELECT ticker, name, market, sector FROM universe WHERE market='KOSDAQ'", conn)

    target_date = next_trading_date(latest_date)

    daily = daily.sort_values(["ticker", "date"])
    daily["tv"] = daily["close"] * daily["volume"]
    daily["tv_ma5"] = daily.groupby("ticker")["tv"].transform(
        lambda s: s.rolling(TV_MA5_LOOKBACK, min_periods=TV_MA5_LOOKBACK).mean()
    )

    latest = daily[daily["date"] == latest_date].copy()
    cand = latest.merge(uni, on="ticker", how="inner")  # KOSDAQ만(universe를 이미 KOSDAQ으로 필터)
    if cand.empty:
        # DataFrame.apply(axis=1)를 0행 프레임에 쓰면 컬럼 구조 자체가 사라지는
        # pandas 특성이 있어(2026-08-18 테스트로 발견) — 빈 경우 필터 단계를
        # 건너뛰고 곧장 빈 결과로 반환.
        return target_date, pd.DataFrame(columns=["ticker", "name", "prev_close", "tv_ma5"])
    cand = cand[~cand.apply(lambda r: is_excluded(r["name"], r.get("sector", "")), axis=1)]
    cand = cand[cand["tv_ma5"].notna() & (cand["tv_ma5"] >= TV_MA5_MIN)]
    cand = cand.rename(columns={"close": "prev_close"})
    cand = cand[["ticker", "name", "prev_close", "tv_ma5"]].reset_index(drop=True)
    return target_date, cand


def save_prefilter(target_date: str, cand: pd.DataFrame, dry_run: bool = False):
    """별도 테이블 없이 bot_state에 JSON 배열로 저장(iris_v3_candidates.py가
    다음날 아침 읽어 실시간 시가를 조회할 대상 목록으로 사용)."""
    if dry_run:
        logger.info(f"[dry-run] {target_date} 사전필터 통과 {len(cand)}건 (DB 미기록)")
        return
    payload = [
        {"ticker": r["ticker"], "name": r["name"], "prev_close": float(r["prev_close"]),
         "tv_ma5": float(r["tv_ma5"])}
        for _, r in cand.iterrows()
    ]
    state_set(f"iris_v3_prefilter_{target_date}", {"version": IRIS_V3_VERSION, "rows": payload})


def notify(target_date: str, cand: pd.DataFrame):
    from core.notify import send_ops, TAG_CANDIDATE
    if cand.empty:
        send_ops(f"<b>{target_date} 사전필터 통과 종목 0건</b> — 내일 갭 체크 대상 없음",
                  TAG_CANDIDATE, label="IRIS-v3")
        return
    send_ops(
        f"<b>{target_date} 사전필터(tv_ma5≥125억) 통과 {len(cand)}건</b> — "
        f"내일 장시작 직후 실시간 갭 확인 예정",
        TAG_CANDIDATE, label="IRIS-v3",
    )


def main():
    dry_run = "--dry-run" in sys.argv
    target_date, cand = build_prefilter(dry_run=dry_run)
    logger.info(f"target_date={target_date} 사전필터통과={len(cand)}건")
    save_prefilter(target_date, cand, dry_run=dry_run)
    if not dry_run:
        notify(target_date, cand)
    else:
        logger.info("[dry-run] 텔레그램 미발송")


if __name__ == "__main__":
    from core.script_guard import run_guarded
    from core.proc_lock import script_lock, LockHeld
    try:
        with script_lock("iris_v3_prefilter"):
            run_guarded(main, "iris_v3_prefilter", logger, "--dry-run" in sys.argv)
    except LockHeld:
        logger.warning("이전 iris_v3_prefilter 실행이 아직 안 끝나 이번 실행 스킵(중복실행 방지)")
