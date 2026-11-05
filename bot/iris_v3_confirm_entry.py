"""
iris v3 — 매수 확정(동시호가 종료 후).

bot/iris_v3_entry.py가 조건부지정가로 접수해둔 주문(iris_v3_pending_entry)의
실제 체결결과를 15:20~15:30 동시호가 종료 후 확인해 positions 테이블에
최종 기록한다. pending 기록이 없으면(오늘 매수 자체가 없었거나 이미
처리됨) 조용히 종료한다.

cron: 35 6 * * 1-5 (KST 15:35 — 동시호가 종료(15:30) 후 여유)
실행: python3 -m bot.iris_v3_confirm_entry [--dry-run]
"""
import sys

from core.db import get_conn, state_set
from core.logger import get_logger
from core.notify import TAG_ENTRY, TAG_ALERT
from core.timeutil import now_kst, today_kst
from strategy.iris_v3_params import IRIS_V3_VERSION

logger = get_logger("iris_v3_confirm_entry", "IRIS_V3")


def _record_entry(date: str, ticker: str, name: str, entry_price: float, shares: int, gap: float | None):
    now = now_kst()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO positions
               (ticker, name, date_open, time_open, entry_price, shares,
                mode, status, strategy_version, strategy, score)
               VALUES (?,?,?,?,?,?,'real','open',?,?,?)""",
            (ticker, name, date, now.strftime("%H%M%S"), entry_price, shares,
             IRIS_V3_VERSION, "iris_v3", gap),
        )


def _tg(msg: str, tag: str = None):
    from core.notify import send_ops, TAG_INFO
    send_ops(msg, tag or TAG_INFO, label="IRIS-v3")


def run(dry_run: bool = False):
    from core.kis_api import is_trading_day
    date = today_kst()

    if not is_trading_day(date):
        logger.info(f"{date} 휴장일 — 종료")
        return

    from core.order_exec_v3 import get_pending_entry, clear_pending_entry, set_halt
    pending = get_pending_entry(date)
    if pending is None:
        logger.info(f"{date} 확정 대기중인 v3 매수 없음 — 종료")
        return

    ticker, name = pending["ticker"], pending["name"]
    odno = pending["odno"]
    intended_qty = pending["intended_qty"]
    ref_price = pending["ref_price"]
    gap = pending.get("gap")  # 2026-08-25: 구버전 pending(gap 없음) 호환용 .get

    from core.order_exec import verify_buy_fill, resolve_entry_price
    # 동시호가는 이미 끝났으니 짧게(합10초) 확인해도 충분 — v2의 FILL_WAIT_SECS(45초)
    # 는 "아직 체결 진행중일 수 있는" 정규장 상황을 위한 예산이라 여기선 과함.
    actual_qty = verify_buy_fill(ticker, intended_qty, odno=odno, wait_secs=(2, 3, 5))

    if dry_run:
        logger.info(f"[dry-run] {ticker} 확정체결={actual_qty}/{intended_qty}주 (DB 미기록)")
        return

    if actual_qty <= 0:
        # 조건부지정가는 동시호가에서 시장가로 자동전환되므로 이 경우는 매우
        # 드물어야 정상(그 종목 자체가 동시호가에서도 전혀 체결 안 될 만큼
        # 유동성이 완전히 고갈됐다는 뜻) — "확실하지 않으면 멈춤" 원칙 그대로.
        clear_pending_entry(date)
        set_halt(date, ticker, "매수체결미확인(동시호가 이후)",
                  f"주문번호={odno} 의도수량={intended_qty}")
        _tg(f"매수 체결 미확인(동시호가 종료 후에도): {name}({ticker}) 주문번호 {odno} — "
            f"오늘 v3 자동매매 중단, 수동 확인 필요", TAG_ALERT)
        return

    entry_price = resolve_entry_price(ticker, ref_price, odno)
    partial_note = f" (목표{intended_qty}주 중 일부만 체결)" if actual_qty < intended_qty else ""
    _record_entry(date, ticker, name, entry_price, actual_qty, gap)
    clear_pending_entry(date)
    _tg(
        f"매수 확정: <b>{name}</b>({ticker})\n"
        f"체결가 {entry_price:,.0f}원 · {actual_qty}주{partial_note}\n"
        f"주문번호 {odno}",
        TAG_ENTRY,
    )


if __name__ == "__main__":
    from core.script_guard import run_guarded
    from core.proc_lock import script_lock, LockHeld
    _dry = "--dry-run" in sys.argv
    try:
        with script_lock("iris_v3_confirm_entry"):
            run_guarded(lambda: run(dry_run=_dry), "iris_v3_confirm_entry", logger, _dry)
    except LockHeld:
        logger.warning("이전 iris_v3_confirm_entry 실행이 아직 안 끝나 이번 실행 스킵(중복실행 방지)")
