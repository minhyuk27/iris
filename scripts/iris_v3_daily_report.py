"""
iris v3 — 09:10 KST 일간 성과 리포트(리포트봇 전용).

bot/iris_v3_exit.py(09:00)가 어제 매수분을 오늘 아침 청산한 직후 결과를
요약해 보낸다. v2의 잔고기준 리포트(scripts/iris_v2_daily_report.py)와
달리, v3는 매매마다 목표금액이 고정(CAPITAL_PER_TRADE)이라 복리재투자가
없으므로 **거래기록 기준**(v2 paper 리포트와 같은 방식)으로 충분하다 —
잔고조회·공식기록전환·입출금추적 같은 장치는 필요해지면(자본 확대 등)
그때 추가.

cron: 10 0 * * 1-5 (KST 09:10 — 매도 확정 여유 후)
실행: python3 -m scripts.iris_v3_daily_report [--dry-run]
"""
import sys

import pandas as pd

from core.config import TRADE_MODE
from core.db import get_conn
from core.logger import get_logger
from core.timeutil import today_kst

logger = get_logger("iris_v3_daily_report", "IRIS_V3")

# 2026-08-18: v3 최초 실거래 개시와 함께 v2(100만원 고정금 전환)와 동일한
# 2주 테스트 기간을 사용자가 명시적으로 확정(iris DECISIONS 2026-08-18 참조).
# v3는 아직 v2의 start_tracking 같은 공식기록 전환 장치가 없어(잔고기준이
# 아니라 거래기록기준 리포트라 불필요) 이 날짜만으로 테스트기간 여부를 표시.
TEST_PERIOD_END = "20260828"
TEST_PERIOD_END_DISP = "2026-08-28(금)"


def _closed_trades() -> pd.DataFrame:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT date_close, ticker, name, entry_price, exit_price, pnl_pct, pnl_amount
               FROM positions
               WHERE strategy='iris_v3' AND mode=? AND status='closed'
               ORDER BY date_close""",
            (TRADE_MODE,),
        ).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def _compound(pcts: pd.Series) -> float:
    return ((1 + pcts / 100).prod() - 1) * 100


def build_report(today: str, df: pd.DataFrame) -> str:
    mode_label = "실거래" if TRADE_MODE == "real" else "페이퍼"
    period_note = (
        f"⚠️ 테스트 기간 중(~{TEST_PERIOD_END_DISP}, 100만원 고정금)\n"
        if today <= TEST_PERIOD_END else ""
    )
    if df.empty:
        return (f"<b>{today} iris v3 성과 리포트</b> ({mode_label})\n{period_note}"
                 "아직 청산된 거래가 없습니다.")

    lines = [f"<b>{today} iris v3 성과 리포트</b> ({mode_label})"]
    if period_note:
        lines.append(period_note.rstrip("\n"))

    today_df = df[df["date_close"] == today]
    if len(today_df):
        r = today_df.iloc[0]  # v3는 1일1종목이라 최대 1건
        lines.append(
            f"\n<b>오늘</b>: {r['name']}({r['ticker']}) {r['entry_price']:,.0f}원→"
            f"{r['exit_price']:,.0f}원 · {r['pnl_pct']:+.2f}% · {r['pnl_amount']:+,.0f}원"
        )
    else:
        lines.append("\n<b>오늘</b>: 청산거래 없음")

    recent = df.tail(10)
    win_rate_recent = (recent["pnl_pct"] > 0).mean() * 100
    lines.append(
        f"\n<b>최근{len(recent)}건</b>: 승률 {win_rate_recent:.0f}% · "
        f"평균 {recent['pnl_pct'].mean():+.2f}% · 복리환산 {_compound(recent['pnl_pct']):+.2f}%"
    )

    total_win = (df["pnl_pct"] > 0).mean() * 100
    total_amt = df["pnl_amount"].sum()
    lines.append(
        f"\n<b>전체누적</b>({df['date_close'].min()}~{df['date_close'].max()}, {len(df)}건): "
        f"승률 {total_win:.0f}% · 복리환산 {_compound(df['pnl_pct']):+.1f}% · "
        f"실현손익 {total_amt:+,.0f}원"
    )
    return "\n".join(lines)


def run(dry_run: bool = False):
    from core.kis_api import is_trading_day
    today = today_kst()

    if not is_trading_day(today):
        logger.info(f"{today} 휴장일 — 종료(리포트 미발송)")
        return

    df = _closed_trades()
    msg = build_report(today, df)
    logger.info(msg.replace("\n", " | "))
    if dry_run:
        logger.info("[dry-run] 텔레그램 미발송")
        return
    from core.notify import send_report
    send_report(msg)


if __name__ == "__main__":
    from core.script_guard import run_guarded
    from core.proc_lock import script_lock, LockHeld
    _dry = "--dry-run" in sys.argv
    try:
        with script_lock("iris_v3_daily_report"):
            run_guarded(lambda: run(dry_run=_dry), "iris_v3_daily_report", logger, _dry)
    except LockHeld:
        logger.warning("이전 iris_v3_daily_report 실행이 아직 안 끝나 이번 실행 스킵(중복실행 방지)")
