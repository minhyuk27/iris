"""
iris v1 주간 요약 리포트.

매 거래일 실행되지만, "오늘이 이번 주(ISO week)의 마지막 거래일"일 때만 실제로
텔레그램을 보낸다 — 요일을 "금요일"로 하드코딩하지 않는 이유: 공휴일로 주
마지막 거래일이 목요일 등으로 당겨지는 주(예: 2026-W29, 7/17 제헌절 휴장)에도
정확히 그 주의 실제 마지막 거래일에 발송되도록 하기 위함(iris_v1_prefilter.py의
_is_week_last_trading_day()와 동일한 판정 로직).

리밸런싱 봇(bot/iris_v1_bot.py, 09:05 KST)과는 완전히 분리된 별도 프로세스 —
장마감(15:30 KST) 후 그날 최종 상태를 조회해 보고하므로 봇 실행 이후 아무 때나
실행해도 결과가 같다.

cron: 35 6 * * 1-5 (UTC) = 15:35 KST 매 거래일
실행: python3 -m scripts.iris_v1_weekly_report
"""
from datetime import datetime, timezone, timedelta

from core.db import get_conn, init_db, state_get, state_set
from core.timeutil import today_kst, next_trading_date

KST = timezone(timedelta(hours=9))
STRATEGY_PREFIX = "iris_v1"
N_GEN = 3


def _iso_week(date_str: str) -> str:
    iso = datetime.strptime(date_str, "%Y%m%d").isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _is_week_last_trading_day(date_str: str) -> bool:
    nxt = next_trading_date(date_str)
    return _iso_week(date_str) != _iso_week(nxt)


def run():
    init_db()
    today = today_kst()

    try:
        from core.kis_api import is_trading_day
        if not is_trading_day(today):
            return
    except Exception:
        pass

    if not _is_week_last_trading_day(today):
        return  # 이번 주 마지막 거래일이 아니면 조용히 종료(정상)

    from core.kis_api import get_price
    from core.notify import send_telegram

    lines = [f"iris v1 주간 요약 ({today})"]
    total_value = 0.0

    with get_conn() as conn:
        for gen in range(N_GEN):
            strategy = f"{STRATEGY_PREFIX}_gen{gen}"
            cash_row = conn.execute(
                "SELECT cash FROM sleeve_cash WHERE strategy=? AND account='KIS'", (strategy,)
            ).fetchone()
            cash = float(cash_row["cash"]) if cash_row else 0.0
            positions = conn.execute("""
                SELECT ticker, name, entry_price, shares FROM positions
                WHERE status='open' AND strategy=? AND mode='real'
            """, (strategy,)).fetchall()

            pos_lines = []
            pos_value = 0.0
            for p in positions:
                try:
                    cur = get_price(p["ticker"])["price"]
                except Exception:
                    cur = p["entry_price"]
                val = cur * p["shares"]
                pos_value += val
                pnl_pct = (cur - p["entry_price"]) / p["entry_price"] * 100 if p["entry_price"] else 0
                pos_lines.append(f"  {p['name']}({p['ticker']}) {p['shares']}주 {pnl_pct:+.1f}%")

            gen_total = cash + pos_value
            total_value += gen_total
            lines.append(f"\n[gen{gen}] 현금 {cash:,.0f}원 + 평가 {pos_value:,.0f}원 = {gen_total:,.0f}원")
            lines.extend(pos_lines or ["  (보유없음)"])

    lines.append(f"\n합계: {total_value:,.0f}원")

    prev_total = state_get("iris_v1_last_weekly_total")
    if prev_total:
        prev_total = float(prev_total)
        if prev_total > 0:
            chg = (total_value - prev_total) / prev_total * 100
            lines.append(f"전주 대비: {chg:+.2f}% ({prev_total:,.0f}원 → {total_value:,.0f}원)")
    state_set("iris_v1_last_weekly_total", total_value)

    send_telegram("\n".join(lines), prefix="[iris_v1 \U0001F4C8] ")


if __name__ == "__main__":
    run()
