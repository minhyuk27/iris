"""
iris v3 — 장시작 직후 후보확정 배치.

전날 저녁 scripts.iris_v3_prefilter가 tv_ma5≥125억으로 좁혀둔 목록의
실시간 시가를 병렬조회해 갭(오늘시가/전일종가-1)을 계산, [GAP_MIN,GAP_MAX)
밴드를 통과하는 종목을 갭률 내림차순으로 candidates 테이블에 기록한다
(1위가 bot/iris_v3_entry.py가 매수할 종목 — "1일1종목", iris DECISIONS
2026-08-18(8차)).

**gap은 반드시 오늘 09:00 시가(get_price()의 "open" 필드)로 계산할 것** —
v2가 한때 "09:30 현재가"를 쓰다 백테스트 정의(09:30 분봉가)와 맞춰야
했던 것과 반대로, v3의 신호 자체가 "오늘 시가" 그 자체라 시가가 곧
기준값이다(price 필드를 쓰면 신호 정의 자체가 달라짐).

cron: 05 0 * * 1-5 (KST 09:05 — 09:00 시가 확정 후 충분한 여유)
실행: python3 -m scripts.iris_v3_candidates [--dry-run]
"""
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from core.db import get_conn, state_get
from core.logger import get_logger
from core.timeutil import today_kst
from strategy.iris_v3_params import IRIS_V3_VERSION, GAP_MIN, GAP_MAX

logger = get_logger("iris_v3_candidates", "IRIS_V3")
ORDER_RETRY = 3


def _get_price_safe(ticker: str) -> dict | None:
    from core.kis_api import get_price
    for i in range(ORDER_RETRY):
        try:
            return get_price(ticker)
        except Exception as e:
            if i < ORDER_RETRY - 1:
                time.sleep(1)
            else:
                logger.warning(f"시세조회 최종 실패 {ticker}: {e}")
    return None


def _prefilter_list(date: str) -> list[dict]:
    payload = state_get(f"iris_v3_prefilter_{date}")
    if not payload:
        return []
    return payload.get("rows", [])


def build_candidates(dry_run: bool = False) -> tuple[str, pd.DataFrame]:
    """오늘 사전필터 목록의 실시간 시가로 갭을 계산해 (date, df) 반환.
    df 컬럼: ticker, name, prev_close, tv_ma5, gap, rank_no."""
    date = today_kst()
    prefilter = _prefilter_list(date)
    if not prefilter:
        logger.warning(f"{date} 사전필터 목록 없음(전날 저녁 배치 미실행 또는 0건)")
        return date, pd.DataFrame()

    with ThreadPoolExecutor(max_workers=10) as ex:
        prices = list(ex.map(lambda c: _get_price_safe(c["ticker"]), prefilter))

    rows = []
    price_fail_count = 0
    for cand, price in zip(prefilter, prices):
        if price is None:
            price_fail_count += 1
            continue
        prev_close = cand["prev_close"]
        if not prev_close:
            continue
        # v2.5.2 버그의 반대 교훈: v3는 "오늘 시가" 자체가 신호이므로 반드시
        # price["open"](stck_oprc)을 써야 함 — price["price"](최신가)를 쓰면
        # 조회 시점에 따라 값이 흔들려 신호 정의 자체가 바뀐다.
        gap = (price["open"] - prev_close) / prev_close
        if GAP_MIN <= gap < GAP_MAX:
            rows.append({"ticker": cand["ticker"], "name": cand["name"],
                         "prev_close": prev_close, "tv_ma5": cand["tv_ma5"], "gap": gap})

    if price_fail_count:
        logger.warning(f"{date} 시세조회 실패 {price_fail_count}/{len(prefilter)}건")

    cand_df = pd.DataFrame(rows)
    if cand_df.empty:
        return date, cand_df
    cand_df = cand_df.sort_values("gap", ascending=False).reset_index(drop=True)
    cand_df["rank_no"] = range(1, len(cand_df) + 1)
    return date, cand_df


def save_candidates(date: str, cand: pd.DataFrame, dry_run: bool = False):
    if dry_run:
        logger.info(f"[dry-run] {date} 후보 {len(cand)}건 (DB 미기록)")
        return
    with get_conn() as conn:
        conn.execute("DELETE FROM candidates WHERE trade_date=? AND strategy='iris_v3'", (date,))
        for _, r in cand.iterrows():
            # 컬럼 재사용(v2와 동일 관례, 스키마 변경 없음): score=gap,
            # avg_val=전일종가(entry.py 참고용, v3는 갭을 이미 여기서 확정해
            # entry.py가 재계산 안 함), total_amt=tv_ma5.
            conn.execute(
                """INSERT INTO candidates
                   (trade_date, ticker, name, sector, score, total_amt, avg_val,
                    marcap_bil, rank_no, strategy, strategy_version, val_ratio)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (date, r["ticker"], r["name"], "", float(r["gap"]), float(r["tv_ma5"]),
                 float(r["prev_close"]), 0.0, int(r["rank_no"]), "iris_v3", IRIS_V3_VERSION, 0.0),
            )


def notify(date: str, cand: pd.DataFrame):
    from core.notify import send_ops, esc, TAG_CANDIDATE
    if cand.empty:
        send_ops(f"<b>{date} 갭밴드({GAP_MIN*100:.0f}~{GAP_MAX*100:.0f}%) 통과 후보 없음</b> — 오늘 무진입",
                  TAG_CANDIDATE, label="IRIS-v3")
        return
    winner = cand.iloc[0]
    lines = [f"<b>{date} 후보 {len(cand)}건</b> — 1위(매수예정): "
             f"{esc(winner['name'])}({winner['ticker']}) 갭 {winner['gap']*100:.2f}%"]
    for _, r in cand.head(5).iterrows():
        lines.append(f"{int(r['rank_no'])}. {esc(r['name'])}({r['ticker']}) 갭 {r['gap']*100:.2f}%")
    if len(cand) > 5:
        lines.append(f"… 외 {len(cand) - 5}건")
    send_ops("\n".join(lines), TAG_CANDIDATE, label="IRIS-v3")


def main():
    dry_run = "--dry-run" in sys.argv
    date, cand = build_candidates(dry_run=dry_run)
    logger.info(f"date={date} candidates={len(cand)}")
    for _, r in cand.iterrows():
        logger.info(f"  #{int(r['rank_no'])} {r['ticker']} {r['name']} gap={r['gap']:.4f}")
    save_candidates(date, cand, dry_run=dry_run)
    if not dry_run:
        notify(date, cand)
    else:
        logger.info("[dry-run] 텔레그램 미발송")


if __name__ == "__main__":
    from core.script_guard import run_guarded
    from core.proc_lock import script_lock, LockHeld
    try:
        with script_lock("iris_v3_candidates"):
            run_guarded(main, "iris_v3_candidates", logger, "--dry-run" in sys.argv)
    except LockHeld:
        logger.warning("이전 iris_v3_candidates 실행이 아직 안 끝나 이번 실행 스킵(중복실행 방지)")
