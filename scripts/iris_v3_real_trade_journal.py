"""
iris v3 — 실거래 거래일지 생성 (2026-08-18 신규, TRADE_MODE=real 전용).

v2의 real_trade_journal.py는 candidates 테이블이 Oracle 야간동기화로
전체교체(DELETE ALL+INSERT ALL)되어 매일 밤 지워지는 문제 때문에 어쩔 수
없이 로그 파일을 파싱했다(iris DECISIONS 2026-07-30). v3는 **무진입
사유를 bot_state(`iris_v3_entered_{date}`)에 이미 구조화된 값으로
남겨두므로**(bot/iris_v3_entry.py 참고 — "no_candidates"/"price_fetch_failed"/
"insufficient_capital"/"buy_failed"/티커) bot_state는 Oracle 동기화가
건드리지 않는 테이블이라 로그 파싱이 필요 없다 — DB만으로 재구성 가능.

체결정보는 `positions`(mode='real', strategy='iris_v3', status='closed').

실행: python3 -m scripts.iris_v3_real_trade_journal
출력: reports/v3_real_trade_journal.md
"""
import os

import pandas as pd

from core.db import get_conn

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "reports", "v3_real_trade_journal.md")

_OUTCOME_LABEL = {
    "no_candidates": "갭밴드(15~30%) 통과 후보 없음",
    "price_fetch_failed": "시세조회 실패",
    "insufficient_capital": "자본 부족(사이징=0주)",
    "buy_failed": "매수 주문 실패(halt)",
}


def _entered_states() -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT key, value FROM bot_state WHERE key LIKE 'iris_v3_entered_%'"
        ).fetchall()
    out = {}
    import json
    for r in rows:
        date = r["key"].replace("iris_v3_entered_", "")
        try:
            out[date] = json.loads(r["value"])
        except (json.JSONDecodeError, TypeError):
            out[date] = r["value"]
    return out


def build():
    with get_conn() as conn:
        trades = conn.execute(
            """SELECT date_open, ticker, name, entry_price, exit_price, score AS gap,
                      pnl_pct, pnl_amount, exit_reason
               FROM positions
               WHERE strategy='iris_v3' AND mode='real' AND status='closed'
               ORDER BY date_open"""
        ).fetchall()

    states = _entered_states()
    trades_by_day = {r["date_open"]: r for r in trades}
    all_days = sorted(set(states) | set(trades_by_day))

    lines = []
    lines.append("# iris v3 실거래 거래일지 (실제 KIS 계좌, TRADE_MODE=real)")
    lines.append("")
    lines.append("> **자동 생성 파일 — 직접 수정하지 말 것.** "
                  "`python3 -m scripts.iris_v3_real_trade_journal`로 재생성됨. "
                  "체결정보는 `positions`(`mode='real'`), 무진입 사유는 "
                  "`bot_state`(`iris_v3_entered_YYYYMMDD`, Oracle 야간동기화 영향 없음)에서 "
                  "직접 조회 — v2와 달리 로그 파싱 불필요.")
    lines.append("")

    if trades:
        n = len(trades)
        win_rate = sum(1 for r in trades if r["pnl_pct"] and r["pnl_pct"] > 0) / n * 100
        total_amount = sum(r["pnl_amount"] or 0 for r in trades)
        avg_pct = sum(r["pnl_pct"] or 0 for r in trades) / n
        lines.append(f"- 실거래 체결: **{n}건** / 확인된 실제 판단일 {len(all_days)}일 중")
        lines.append(f"- 거래당 평균수익률: **{avg_pct:+.2f}%**  ·  승률: **{win_rate:.1f}%**")
        lines.append(f"- 누적 실현손익: **{total_amount:+,.0f}원**")
    else:
        lines.append(f"- 아직 종결된 실거래 없음 (확인된 실제 판단일 {len(all_days)}일)")
    lines.append("")
    lines.append("| 날짜 | 종목/결과 | 갭 | 매수가 | 매도가 | 청산사유 | 수익률 | 손익금액 |")
    lines.append("|---|---|---|---|---|---|---|---|")

    for d in all_days:
        d_fmt = pd.to_datetime(d, format="%Y%m%d").strftime("%Y-%m-%d")
        if d in trades_by_day:
            r = trades_by_day[d]
            gap_s = f"{r['gap']*100:+.2f}%" if r["gap"] is not None else "-"
            entry = f"{r['entry_price']:,.0f}"
            exit_p = f"{r['exit_price']:,.0f}" if r["exit_price"] else "-"
            pnl_pct_s = f"{r['pnl_pct']:+.2f}%" if r["pnl_pct"] is not None else "-"
            pnl_amt_s = f"{r['pnl_amount']:+,.0f}원" if r["pnl_amount"] is not None else "-"
            lines.append(f"| {d_fmt} | {r['name']}({r['ticker']}) | {gap_s} | {entry} | "
                         f"{exit_p} | {r['exit_reason'] or '-'} | {pnl_pct_s} | {pnl_amt_s} |")
            continue

        state = states.get(d)
        label = _OUTCOME_LABEL.get(state, f"미체결 상태값: {state}")
        lines.append(f"| {d_fmt} | **무진입** | - | - | - | {label} | - | - |")

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"작성 완료: {OUT_PATH} ({len(trades)}건 체결 / {len(all_days)}일)")


if __name__ == "__main__":
    build()
