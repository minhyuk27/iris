"""
iris v2 — 13:00 강제청산 (Stage A/B 겸용). v2.5에서 12:00→13:00 연장.
09:31~12:59 매분 감시(bot.iris_v2_monitor)에서 TP/SL/중간청산 어느것도 안 걸리고
still open이면 여기서 시가(현재가)로 강제청산. 1차 부분익절(+8%, v2.5 신규)이 이미
발동된 포지션이면 잔여 보유분만 이 가격으로 청산하고 손익은 두 체결가를 블렌드해 계산.

TRADE_MODE(.env)로 Stage A/B 분기(2026-07-22 추가) — monitor.py와 동일 원칙:
real 모드에서 halt 상태면 아무 것도 안 하고 종료(사람 확인 대기).

cron: 0 13 * * 1-5 (KST)
실행: python3 -m bot.iris_v2_force_close [--dry-run]
"""
import sys
import time

from core.config import IRIS_V2_TRADE_MODE as TRADE_MODE
from core.db import get_conn
from core.logger import get_logger
from core.notify import TAG_ALERT, exit_tag
from core.timeutil import now_kst, today_kst
from strategy.iris_v2_params import SPLIT_SELL_ENABLED, SPLIT_SELL_TRANCHES

logger = get_logger("iris_v2_force_close", "IRIS_V2")
ORDER_RETRY = 3


def _get_price_safe(ticker: str) -> dict | None:
    for i in range(ORDER_RETRY):
        try:
            from core.kis_api import get_price
            return get_price(ticker)
        except Exception as e:
            if i < ORDER_RETRY - 1:
                time.sleep(1)
            else:
                logger.warning(f"시세조회 최종 실패 {ticker}: {e}")
    return None


def _open_position(date: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT id, ticker, name, entry_price, shares FROM positions
               WHERE status='open' AND strategy='iris_v2' AND date_open=?""",
            (date,),
        ).fetchone()
    return dict(row) if row else None


def _take_partial(pos_id: int, remaining_shares: int, partial_shares: int, partial_price: float):
    """DB에 부분익절 결과 기록(monitor.py와 동일 로직 — 2026-08-07: force_close 시점에
    monitor의 마지막 틱이 남긴 partial pending을 이어받아 확정할 때 필요)."""
    now = now_kst()
    with get_conn() as conn:
        conn.execute(
            """UPDATE positions SET shares=?, partial_exit_price=?, partial_shares=?, partial_time=?
               WHERE id=?""",
            (remaining_shares, partial_price, partial_shares, now.strftime("%H%M%S"), pos_id),
        )


def _close_position(pos_id: int, exit_price: float, reason: str):
    """잔여 보유분을 exit_price로 청산. 1차 부분익절(partial_*)이 있었으면 블렌드해 손익 계산."""
    now = now_kst()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT entry_price, shares, partial_exit_price, partial_shares FROM positions WHERE id=?",
            (pos_id,),
        ).fetchone()
        entry, shares = row["entry_price"], row["shares"]
        p_price, p_shares = row["partial_exit_price"], row["partial_shares"]
        if p_shares:
            total_shares = shares + p_shares
            proceeds = shares * exit_price + p_shares * p_price
            cost = total_shares * entry
            pnl_pct = (proceeds / cost - 1) * 100
            pnl_amount = proceeds - cost
        else:
            pnl_pct = (exit_price - entry) / entry * 100
            pnl_amount = (exit_price - entry) * shares
        conn.execute(
            """UPDATE positions SET status='closed', exit_price=?, date_close=?, time_close=?,
                      pnl_pct=?, pnl_amount=?, exit_reason=? WHERE id=?""",
            (exit_price, today_kst(), now.strftime("%H%M%S"), round(pnl_pct, 2), round(pnl_amount, 0),
             reason, pos_id),
        )
    return pnl_pct


def _tg(msg: str, tag: str = None):
    from core.notify import send_ops, TAG_INFO
    send_ops(msg, tag or TAG_INFO)


def _handle_pending_exit(date: str) -> bool:
    """2026-08-07 신규(monitor.py와 동일 로직) — 13:00 직전 monitor의 마지막 틱이
    매도체결을 못 밝혀내 pending으로 남겨둔 게 있으면, 강제청산 시도 전에 그것부터
    이어서 확인한다(이미 나가있는 주문을 무시하고 새 강제청산 주문을 또 내면
    이중매도이므로 반드시 우선)."""
    from core.order_exec import retry_pending_exit
    result = retry_pending_exit(date)
    if result is None:
        return False

    if result["status"] == "pending":
        logger.info(f"{result['ticker']} 매도체결 재확인 {result['retry_count']}회차 — 아직 미확인, "
                    f"이번 강제청산 실행은 보류(다음 재시도로 넘김)")
        return True

    if result["status"] == "timeout":
        from core.order_exec import PENDING_RETRY_MAX
        _tg(f"청산({result['reason']}) 체결 재확인 {PENDING_RETRY_MAX}회 시도 후에도 실패: "
            f"<b>{result['ticker']}</b> — 수동 확인 필요 (장마감 임박, 서둘러 확인)", TAG_ALERT)
        return True

    # status == "done"
    pos = _open_position(date)
    if pos is None:
        logger.error(f"pending 체결 확정됐지만 open 포지션을 못 찾음(비정상): {result}")
        return True
    if result["kind"] == "full":
        pnl_pct = _close_position(pos["id"], result["exit_price"], result["reason"])
        logger.info(f"pending 재확인으로 지연확정: {result['ticker']} {result['reason']} "
                    f"price={result['exit_price']}")
        _tg(
            f"청산({result['reason']}): <b>{pos['name']}</b>({result['ticker']})\n"
            f"{pos['entry_price']:,.0f}원 → {result['exit_price']:,.0f}원 · 손익 {pnl_pct:+.2f}% (지연확정)\n"
            f"주문번호 {result['odno']}",
            exit_tag(pnl_pct),
        )
    else:  # partial — 확정만 하고 잔여물량 강제청산은 이번 틱엔 안 함(다음 크론 실행 없음이라
        # 13:00 이후 잔여분은 halt 없이 열린 채 남게 됨 — 극히 드문 시나리오이므로 일단
        # 알림으로 수동확인 유도. 상세는 아래 pos_reopen 알림.
        _take_partial(pos["id"], result["remaining"], result["actual_qty"], result["exit_price"])
        logger.warning(f"pending 재확인으로 1차 부분익절 지연확정(13:00 이후) — 잔여 {result['remaining']}주가 "
                        f"강제청산 안 된 채 남음, 수동 확인 필요: {result['ticker']}")
        _tg(
            f"부분익절(지연확정, 13:00 이후): <b>{pos['name']}</b>({result['ticker']})\n"
            f"{result['actual_qty']}주 매도 확정됐으나 잔여 {result['remaining']}주는 강제청산 못 함 — "
            f"장마감 임박, 수동 확인 필요", TAG_ALERT,
        )
    return True


def _handle_pending_split_sell(date: str) -> bool:
    """2026-08-08 신규(분할매도, SPLIT_SELL_ENABLED) — monitor.py 마지막 틱이
    남긴 분할매도 pending이 있으면 강제청산 새로 시도하기 전에 이 자리에서
    끝까지 반복 재확인(force_close는 다음 틱이 없음 — _handle_pending_exit와
    동일 원칙). 지정가는 안 씀(연구 결론, iris DECISIONS 2026-08-08 참조) —
    이 함수가 다루는 건 순수 시장가 분할매도의 잔여 트랑쉬 확인뿐."""
    from core.order_exec import get_pending_split_sell, retry_pending_split_sell
    if get_pending_split_sell(date) is None:
        return False
    logger.warning(f"{date} 분할매도 pending 발견 — 강제청산 전에 이 자리에서 반복 재확인 시작")
    while True:
        result = retry_pending_split_sell(date)
        if result is None:
            return False
        if result["status"] == "pending":
            continue
        if result["status"] == "halted":
            _tg(f"분할매도({result['reason']}) 처리 실패: <b>{result['ticker']}</b> — "
                f"수동 확인 필요 (오늘 장 마감 임박, 서둘러 확인)", TAG_ALERT)
            return True
        # done
        pos = _open_position(date)
        if pos is None:
            logger.error(f"분할매도 완료됐지만 open 포지션을 못 찾음(비정상): {result}")
            return True
        pnl_pct = _close_position(pos["id"], result["exit_price"], result["reason"])
        logger.info(f"분할매도 지연확정: {result['ticker']} {result['reason']} price={result['exit_price']:.0f}")
        _tg(
            f"청산({result['reason']}): <b>{pos['name']}</b>({result['ticker']})\n"
            f"{pos['entry_price']:,.0f}원 → {result['exit_price']:,.0f}원(블렌드) · 손익 {pnl_pct:+.2f}% (분할매도·지연확정)\n"
            f"주문번호 {result['odno']}",
            exit_tag(pnl_pct),
        )
        return True


def run(dry_run: bool = False):
    from core.kis_api import is_trading_day
    date = today_kst()

    if not is_trading_day(date):
        return

    real_mode = TRADE_MODE == "real"

    if real_mode:
        from core.config import validate
        validate()  # KIS_APP_KEY/SECRET/ACCOUNT 누락시 여기서 곧바로 명확히 실패
                    # (2026-08-08 코드감사 — 예전엔 어디서도 호출 안 됐음)

    if real_mode and not dry_run:
        from core.order_exec import is_halted
        halt = is_halted(date)
        if halt:
            logger.warning(f"halt 상태(사유={halt.get('action')}) — 강제청산 보류, 수동 확인 대기")
            return

        # 2026-08-07: monitor.py 마지막 틱이 남긴 pending(미확인 매도)이 있으면
        # 강제청산 새로 시도하기 전에 그것부터 이어서 확인(이중매도 방지 최우선).
        if _handle_pending_exit(date):
            return
        # 2026-08-08 신규(분할매도) — 위와 같은 이유로, 분할매도 쪽 pending도 먼저 비움.
        if _handle_pending_split_sell(date):
            return

        # 2026-08-08 코드감사(방어적 점검) — 트랑쉬2 대기(pending_tranche2)는
        # monitor.py가 최대 PENDING_RETRY_MAX(5)틱(~5분) 안에 반드시 정리하므로
        # 09:31 진입 기준 09:36경엔 항상 비어있어야 하고, 13:00 강제청산까지
        # 남아있는 건 이론상 불가능해야 함(monitor cron이 몇 시간째 안 돈 것 같은
        # 심각한 운영장애가 아니고서는). force_close는 이걸 직접 재시도하는
        # 로직은 없음(설계 범위 밖) — 혹시라도 남아있으면 강제청산은 그대로
        # 진행하되(기록된 수량 기준), 실제 계좌에 별도로 체결된 트랑쉬2 물량이
        # 남아있을 수 있다는 걸 사람이 반드시 확인하도록 크게 알린다.
        from core.order_exec import get_pending_tranche2
        stale_tranche2 = get_pending_tranche2(date)
        if stale_tranche2:
            logger.critical(f"13:00 강제청산 시점까지 트랑쉬2 pending이 안 풀림(비정상): {stale_tranche2}")
            _tg(f"트랑쉬2 매수 대기가 13:00까지 안 풀림(비정상 — monitor 장애 의심): "
                f"{stale_tranche2} — 강제청산은 기록된 수량대로 진행하지만, 실제 계좌에 "
                f"별도로 체결된 물량이 남아있을 수 있음, 반드시 확인 필요", TAG_ALERT)

    pos = _open_position(date)
    if pos is None:
        logger.info(f"{date} open 포지션 없음(이미 청산됐거나 오늘 진입 없음) — 종료")
        return

    if dry_run or not real_mode:
        price = _get_price_safe(pos["ticker"])
        if price is None:
            logger.error(f"{pos['ticker']} 13:00 시세조회 실패 — 강제청산 불가, CRITICAL")
            if not dry_run:
                _tg(f"강제청산 실패: {pos['ticker']} 시세조회 불가 — 수동 확인 필요", TAG_ALERT)
            return
        exit_price = price["price"]
        logger.info(f"강제청산: {pos['ticker']} price={exit_price}")
        if dry_run:
            logger.info("[dry-run] DB 미기록·텔레그램 미발송")
            return
        pnl_pct = _close_position(pos["id"], exit_price, "강제청산")
        _tg(
            f"청산(강제청산, 페이퍼): <b>{pos['name']}</b>({pos['ticker']})\n"
            f"{pos['entry_price']:,.0f}원 → {exit_price:,.0f}원 · 손익 {pnl_pct:+.2f}%",
            exit_tag(pnl_pct),
        )
        return

    # real_mode
    # 2026-08-08 신규: SPLIT_SELL_ENABLED면 강제청산도 시장가 분할매도 적용(연구
    # 결론 — 강제청산은 비용이 사실상 0, iris DECISIONS 2026-08-08 참조). 지정가는
    # 강제청산에 안 씀(같은 연구에서 오히려 손해로 확인 — 이 청산유형은 대체로 가격이
    # 안 움직여 지정가가 지켜줄 게 적고, 어쩌다 한번 나는 역풍만 비용이 됨).
    if SPLIT_SELL_ENABLED:
        from core.order_exec import execute_split_close
        result = execute_split_close(pos["ticker"], pos["shares"], pos["entry_price"],
                                      "강제청산", date, SPLIT_SELL_TRANCHES)
    else:
        from core.order_exec import execute_full_close
        result = execute_full_close(pos["ticker"], pos["shares"], pos["entry_price"], "강제청산", date)
    if result == "pending":
        # 2026-08-07: force_close는 monitor.py와 달리 "다음 틱"이 없는 그날 마지막
        # 스크립트라 — pending을 그냥 남겨두면 아무도 다시 안 봐줌. 그래서 여기서만
        # retry_pending_exit()/retry_pending_split_sell()을 결과가 나올 때까지(done
        # 또는 timeout/halted) 이 자리에서 직접 반복 호출한다(매 호출이 자체 폴링
        # 예산 포함, 최대 PENDING_RETRY_MAX회 — force_close는 그날 유일 실행이라
        # 길게 블로킹해도 다음 실행과 안 겹침).
        logger.warning(f"{pos['ticker']} 강제청산 체결 미확인 — pending, 이 자리에서 반복 재확인 시작")
        if SPLIT_SELL_ENABLED:
            from core.order_exec import retry_pending_split_sell
            while True:
                follow_up = retry_pending_split_sell(date)
                if follow_up is None:
                    break
                if follow_up["status"] == "pending":
                    continue
                if follow_up["status"] == "halted":
                    _tg(f"강제청산(분할매도) 체결 재확인 실패: <b>{pos['name']}</b>({pos['ticker']}) — "
                        f"수동 확인 필요 (오늘 장 마감, 재시도 소진)", TAG_ALERT)
                    return
                # done
                exit_price, odno = follow_up["exit_price"], follow_up["odno"]
                pnl_pct = _close_position(pos["id"], exit_price, "강제청산")
                _tg(
                    f"청산(강제청산): <b>{pos['name']}</b>({pos['ticker']})\n"
                    f"{pos['entry_price']:,.0f}원 → {exit_price:,.0f}원(블렌드) · 손익 {pnl_pct:+.2f}% (분할매도·지연확정)\n"
                    f"주문번호 {odno}",
                    exit_tag(pnl_pct),
                )
                return
            return
        from core.order_exec import retry_pending_exit
        while True:
            follow_up = retry_pending_exit(date)
            if follow_up is None or follow_up["status"] == "pending":
                if follow_up is None:
                    break
                continue
            if follow_up["status"] == "timeout":
                _tg(f"강제청산 체결 재확인 실패: <b>{pos['name']}</b>({pos['ticker']}) — "
                    f"수동 확인 필요 (오늘 장 마감, 재시도 소진)", TAG_ALERT)
                return
            # done
            exit_price, odno = follow_up["exit_price"], follow_up["odno"]
            pnl_pct = _close_position(pos["id"], exit_price, "강제청산")
            _tg(
                f"청산(강제청산): <b>{pos['name']}</b>({pos['ticker']})\n"
                f"{pos['entry_price']:,.0f}원 → {exit_price:,.0f}원 · 손익 {pnl_pct:+.2f}% (지연확정)\n"
                f"주문번호 {odno}",
                exit_tag(pnl_pct),
            )
            return
        return
    if result is None:
        _tg(f"강제청산 체결 실패: <b>{pos['name']}</b>({pos['ticker']}) — "
            f"수동 확인 필요 (오늘 장 마감 임박, 서둘러 확인)", TAG_ALERT)
        return
    exit_price, odno = result
    pnl_pct = _close_position(pos["id"], exit_price, "강제청산")
    _tg(
        f"청산(강제청산): <b>{pos['name']}</b>({pos['ticker']})\n"
        f"{pos['entry_price']:,.0f}원 → {exit_price:,.0f}원 · 손익 {pnl_pct:+.2f}%\n"
        f"주문번호 {odno}",
        exit_tag(pnl_pct),
    )


if __name__ == "__main__":
    from core.script_guard import run_guarded
    from core.proc_lock import script_lock, LockHeld
    _dry = "--dry-run" in sys.argv
    try:
        with script_lock("iris_v2_force_close"):
            run_guarded(lambda: run(dry_run=_dry), "iris_v2_force_close", logger, _dry)
    except LockHeld:
        logger.warning("이전 iris_v2_force_close 실행이 아직 안 끝나 이번 실행 스킵(중복실행 방지)")
