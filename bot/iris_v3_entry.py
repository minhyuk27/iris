"""
iris v3 — 장마감 직전 매수(조건부지정가).

전날 저녁~오늘 아침 파이프라인(scripts.iris_v3_prefilter → candidates)이
확정해둔 오늘의 1위 종목을 조건부지정가(core.kis_api.buy_conditional_limit,
ORD_DVSN=02)로 매수 주문한다. 미체결 잔량은 거래소가 15:20~15:30
동시호가에서 자동으로 시장가 전환하므로, **이 스크립트는 최종 체결결과를
확정하지 않는다** — 주문 접수 사실만 pending_entry로 기록해두고,
bot/iris_v3_confirm_entry.py(동시호가 종료 후)가 실제 잔고를 확인해
positions 테이블에 최종 기록한다.

v2와 달리 매분 모니터링 루프가 없다(TP/SL/중간평가 없음, 청산은 익일
시가 하나뿐 — iris v3 다각도 전문가 분석.md 6번 참조).

TRADE_MODE(.env):
  paper(기본) — 시세조회만, 실제 주문 없음, 즉시 positions에 'paper' 기록.
  real        — 실제 조건부지정가 주문 제출, positions 기록은 confirm_entry.py로 이연.

cron: 00 6 * * 1-5 (KST 15:00)
실행: python3 -m bot.iris_v3_entry [--dry-run]
"""
import sys
import time

from core.config import TRADE_MODE
from core.db import get_conn, state_get, state_set
from core.logger import get_logger
from core.notify import TAG_ENTRY, TAG_ALERT
from core.timeutil import now_kst, today_kst
from strategy.iris_v3_params import IRIS_V3_VERSION, CAPITAL_PER_TRADE

logger = get_logger("iris_v3_entry", "IRIS_V3")


def _entered_key(date: str) -> str:
    return f"iris_v3_entered_{date}"


def _get_price_safe(ticker: str) -> dict | None:
    from core.kis_api import get_price
    for i in range(3):
        try:
            return get_price(ticker)
        except Exception as e:
            if i < 2:
                time.sleep(1)
            else:
                logger.warning(f"시세조회 최종 실패 {ticker}: {e}")
    return None


def _today_winner(date: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT ticker, name, avg_val AS prev_close, score AS gap, total_amt AS tv_ma5
               FROM candidates WHERE trade_date=? AND strategy='iris_v3' AND rank_no=1""",
            (date,),
        ).fetchone()
    return dict(row) if row else None


def _open_position_exists() -> bool:
    """v3는 오버나잇 보유라 어제 산 포지션이 오늘 아침 청산 실패(pending)로
    아직 열려있을 수 있다 — date_open=오늘로만 걸러내면 그 경우를 못 잡아
    이중매수 위험이 있었다(2026-08-18 실거래 개시(08-19) 전날 코드감사에서 발견,
    v2는 당일청산 구조라 이 문제 자체가 없었음). 날짜 무관하게 open 포지션
    자체가 있는지만 확인하도록 수정."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM positions WHERE status='open' AND strategy='iris_v3'"
        ).fetchone()
    return row is not None


def _record_entry(date: str, ticker: str, name: str, entry_price: float, shares: int, mode: str, gap: float):
    now = now_kst()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO positions
               (ticker, name, date_open, time_open, entry_price, shares,
                mode, status, strategy_version, strategy, score)
               VALUES (?,?,?,?,?,?,?,'open',?,?,?)""",
            (ticker, name, date, now.strftime("%H%M%S"), entry_price, shares,
             mode, IRIS_V3_VERSION, "iris_v3", gap),
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

    if TRADE_MODE == "real":
        from core.config import validate
        validate()

    from core.order_exec_v3 import find_unresolved_halt
    unresolved = find_unresolved_halt()
    if unresolved:
        logger.critical(
            f"{unresolved['date']} v3 halt가 아직 해제 안 됨(사유={unresolved.get('action')}, "
            f"종목={unresolved.get('ticker')}) — 오늘({date}) 매수를 건너뜀."
        )
        if not dry_run:
            _tg(
                f"{unresolved['date']}에 발생한 v3 halt(사유={unresolved.get('action')}, "
                f"종목={unresolved.get('ticker')})가 아직 해제 안 됨 — 오늘({date}) 매수를 건너뜀. "
                f"KIS 앱에서 실제 잔고 확인 후 수동 해제 필요",
                TAG_ALERT,
            )
        return

    if not dry_run and state_get(_entered_key(date)) is not None:
        logger.info(f"{date} 이미 매수판단 완료(idempotency) — 종료")
        return

    if not dry_run and _open_position_exists():
        logger.info(f"{date} 이미 open 포지션 존재(다른 날짜분 포함) — 종료")
        return

    winner = _today_winner(date)
    if winner is None:
        logger.info(f"{date} 오늘의 1위 후보 없음 — 매수 없음")
        if not dry_run:
            state_set(_entered_key(date), "no_candidates")
        return

    price = _get_price_safe(winner["ticker"])
    if price is None:
        logger.error(f"{date} {winner['ticker']} 시세조회 실패 — 매수 불가")
        if not dry_run:
            state_set(_entered_key(date), "price_fetch_failed")
            _tg(f"{winner['name']}({winner['ticker']}) 시세조회 실패 — 오늘 매수 불가", TAG_ALERT)
        return
    ref_price = price["price"]

    real_mode = TRADE_MODE == "real"
    if real_mode:
        from core.order_exec import compute_buy_qty
        shares = compute_buy_qty(winner["ticker"], ref_price, CAPITAL_PER_TRADE,
                                  base_price=winner["prev_close"])
    else:
        shares = int(CAPITAL_PER_TRADE // ref_price) if ref_price > 0 else 0

    if shares <= 0:
        logger.warning(f"{date} {winner['ticker']} 자본 부족(shares=0) — 종료")
        if not dry_run:
            state_set(_entered_key(date), "insufficient_capital")
        return

    logger.info(f"매수판단: {winner['ticker']} {winner['name']} gap={winner['gap']:.4f} "
                f"ref_price={ref_price} shares={shares} [TRADE_MODE={TRADE_MODE}]")

    if dry_run:
        logger.info("[dry-run] DB 미기록·주문 미제출·텔레그램 미발송")
        return

    if real_mode:
        from core.order_exec_v3 import buy_conditional_limit_safe, set_halt, set_pending_entry
        state_set(_entered_key(date), f"buying:{winner['ticker']}")
        ok, odno, orgno = buy_conditional_limit_safe(winner["ticker"], shares, ref_price)
        if not ok:
            state_set(_entered_key(date), "buy_failed")
            set_halt(date, winner["ticker"], "매수주문실패", f"의도수량={shares}")
            _tg(f"매수 주문 실패: {winner['name']}({winner['ticker']}) {shares}주 — "
                f"오늘 v3 자동매매 중단, 수동 확인 필요", TAG_ALERT)
            return
        set_pending_entry(date, {
            "ticker": winner["ticker"], "name": winner["name"], "odno": odno, "orgno": orgno,
            "intended_qty": shares, "ref_price": ref_price, "gap": winner["gap"],
        })
        state_set(_entered_key(date), winner["ticker"])
        _tg(
            f"매수 주문 접수(조건부지정가): <b>{winner['name']}</b>({winner['ticker']})\n"
            f"갭 {winner['gap']*100:.2f}% · 지정가 {ref_price:,.0f}원 · {shares}주\n"
            f"주문번호 {odno} · 최종 체결은 15:30 동시호가 이후 확정됩니다",
            TAG_ENTRY,
        )
    else:
        _record_entry(date, winner["ticker"], winner["name"], ref_price, shares, "paper", winner["gap"])
        state_set(_entered_key(date), winner["ticker"])
        _tg(
            f"매수(페이퍼): <b>{winner['name']}</b>({winner['ticker']})\n"
            f"갭 {winner['gap']*100:.2f}% · {ref_price:,.0f}원 · {shares}주 "
            f"(시세조회만, 실제 주문 없음)",
            TAG_ENTRY,
        )


if __name__ == "__main__":
    from core.script_guard import run_guarded
    from core.proc_lock import script_lock, LockHeld
    _dry = "--dry-run" in sys.argv
    try:
        with script_lock("iris_v3_entry"):
            run_guarded(lambda: run(dry_run=_dry), "iris_v3_entry", logger, _dry)
    except LockHeld:
        logger.warning("이전 iris_v3_entry 실행이 아직 안 끝나 이번 실행 스킵(중복실행 방지)")
