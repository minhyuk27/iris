"""
iris v3 — 익일 09:00 매도(지정가+3분타임아웃+시장가전환).

전날 매수해둔 v3 포지션을 익일 시가 부근에서 지정가로 매도 시도, 3분
(strategy.iris_v3_params.SELL_TIMEOUT_SECS) 지나도 안 채워지면 잔량을
시장가로 전환한다(core.order_exec_v3.execute_exit). v2의 매분 모니터링과
달리, 이 스크립트 1회 실행 안에서 전체 흐름을 동기적으로 끝낸다(최대
약 3~4분 소요) — v3는 청산사유가 "익일 시가" 하나뿐이라 반복 감시가
필요 없다.

이 스크립트가 도중에 죽었다가 재실행되는 경우(수동 재실행 등)를 대비해,
실행 시작 시 먼저 iris_v3_pending_exit이 남아있는지 확인하고 있으면
새 매도주문을 내지 않고 이어서 확인한다(이중매도 방지).

cron: 00 0 * * 1-5 (KST 09:00)
실행: python3 -m bot.iris_v3_exit [--dry-run]
"""
import sys

from core.config import TRADE_MODE
from core.db import get_conn
from core.logger import get_logger
from core.notify import TAG_ALERT
from core.timeutil import now_kst, today_kst

logger = get_logger("iris_v3_exit", "IRIS_V3")


def _open_position(date: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT id, ticker, name, entry_price, shares
               FROM positions WHERE status='open' AND strategy='iris_v3' AND date_open<?""",
            (date,),
        ).fetchone()
    return dict(row) if row else None


def _close_position(pos_id: int, exit_price: float, entry_price: float, shares: int):
    now = now_kst()
    pnl_pct = (exit_price - entry_price) / entry_price * 100 if entry_price else 0.0
    pnl_amount = (exit_price - entry_price) * shares
    with get_conn() as conn:
        conn.execute(
            """UPDATE positions SET status='closed', exit_price=?, date_close=?, time_close=?,
               pnl_pct=?, pnl_amount=?, exit_reason='익일시가청산' WHERE id=?""",
            (exit_price, today_kst(), now.strftime("%H%M%S"), pnl_pct, pnl_amount, pos_id),
        )
    return pnl_pct, pnl_amount


def _get_price_safe(ticker: str) -> dict | None:
    from core.kis_api import get_price
    try:
        return get_price(ticker)
    except Exception as e:
        logger.warning(f"시세조회 실패 {ticker}: {e}")
        return None


def _tg(msg: str, tag: str = None):
    from core.notify import send_ops, TAG_INFO
    send_ops(msg, tag or TAG_INFO, label="IRIS-v3")


def run(dry_run: bool = False):
    from core.kis_api import is_trading_day
    date = today_kst()

    if not is_trading_day(date):
        logger.info(f"{date} 휴장일 — 종료")
        return

    if TRADE_MODE == "real":
        from core.config import validate
        validate()

    from core.order_exec_v3 import find_unresolved_halt, is_halted
    unresolved = find_unresolved_halt()
    if unresolved:
        logger.critical(f"{unresolved['date']} v3 halt 미해결 — 매도판단을 건너뜀")
        if not dry_run:
            _tg(f"{unresolved['date']} v3 halt가 아직 해제 안 됨 — 오늘({date}) 매도판단을 "
                f"건너뜀. KIS 앱에서 실제 잔고 확인 후 수동 해제 필요", TAG_ALERT)
        return

    real_mode = TRADE_MODE == "real"

    if real_mode and not dry_run:
        # 이전 실행이 도중에 죽어 pending_exit이 남아있으면 새 주문 없이 이어서 확인.
        from core.order_exec_v3 import get_pending_exit, retry_pending_exit
        pending = get_pending_exit(date)
        if pending is not None:
            logger.info(f"{date} 이전 실행의 pending_exit 발견 — 이어서 확인: {pending['ticker']}")
            result = retry_pending_exit(date)
            if result and result["status"] == "done":
                pos = _open_position_by_ticker(date, pending["ticker"])
                if pos:
                    pnl_pct, pnl_amount = _close_position(
                        pos["id"], result["exit_price"], pos["entry_price"], pos["shares"])
                    from core.notify import exit_tag
                    _tg(f"매도 확정(재실행 이어서 확인): <b>{pos['name']}</b>({pos['ticker']})\n"
                        f"청산가 {result['exit_price']:,.0f}원 · 손익 {pnl_pct:+.2f}%",
                        exit_tag(pnl_pct))
            return

    pos = _open_position(date)
    if pos is None:
        logger.info(f"{date} 매도할 v3 open 포지션 없음 — 종료")
        return

    price = _get_price_safe(pos["ticker"])
    if price is None or price["price"] <= 0:
        logger.error(f"{date} {pos['ticker']} 시세조회 실패 — 매도 불가")
        if not dry_run:
            from core.order_exec_v3 import set_halt
            set_halt(date, pos["ticker"], "시세조회실패(매도)", "")
            _tg(f"{pos['name']}({pos['ticker']}) 시세조회 실패 — 매도 불가, 수동 확인 필요", TAG_ALERT)
        return
    ref_price = price["price"]

    if dry_run:
        logger.info(f"[dry-run] {pos['ticker']} 매도판단(현재가 {ref_price}) — 주문 미제출")
        return

    if real_mode:
        from core.order_exec_v3 import execute_exit
        result = execute_exit(pos["ticker"], pos["shares"], pos["entry_price"], ref_price, date)
        if result is None:
            _tg(f"매도 주문 실패: {pos['name']}({pos['ticker']}) — 오늘 v3 자동매매 중단, "
                f"수동 확인 필요", TAG_ALERT)
            return
        if result == "pending":
            _tg(f"매도 체결 미확인(지정가+시장가전환 모두 시도): {pos['name']}({pos['ticker']}) — "
                f"수동 재실행으로 이어서 확인 필요", TAG_ALERT)
            return
        exit_price, odno = result
        pnl_pct, pnl_amount = _close_position(pos["id"], exit_price, pos["entry_price"], pos["shares"])
        from core.notify import exit_tag
        _tg(
            f"매도 확정: <b>{pos['name']}</b>({pos['ticker']})\n"
            f"청산가 {exit_price:,.0f}원 · 손익 {pnl_pct:+.2f}%({pnl_amount:+,.0f}원)\n"
            f"주문번호 {odno}",
            exit_tag(pnl_pct),
        )
    else:
        pnl_pct, pnl_amount = _close_position(pos["id"], ref_price, pos["entry_price"], pos["shares"])
        from core.notify import exit_tag
        _tg(
            f"매도(페이퍼): <b>{pos['name']}</b>({pos['ticker']})\n"
            f"청산가 {ref_price:,.0f}원 · 손익 {pnl_pct:+.2f}%({pnl_amount:+,.0f}원)",
            exit_tag(pnl_pct),
        )


def _open_position_by_ticker(date: str, ticker: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT id, ticker, name, entry_price, shares
               FROM positions WHERE status='open' AND strategy='iris_v3' AND ticker=? AND date_open<?""",
            (ticker, date),
        ).fetchone()
    return dict(row) if row else None


if __name__ == "__main__":
    from core.script_guard import run_guarded
    from core.proc_lock import script_lock, LockHeld
    _dry = "--dry-run" in sys.argv
    try:
        with script_lock("iris_v3_exit"):
            run_guarded(lambda: run(dry_run=_dry), "iris_v3_exit", logger, _dry)
    except LockHeld:
        logger.warning("이전 iris_v3_exit 실행이 아직 안 끝나 이번 실행 스킵(중복실행 방지)")
