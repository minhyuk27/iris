"""
iris v2 — 09:31~12:59 매분 포지션 감시 (Stage A/B 겸용).
TP(+15%)/SL(-4.5%) 연속감시 + 11:30 1회 중간평가(미회복시 조기청산) + +8% 도달시
1차 부분익절(보유 50% 매도, v2.5 신규). 크론이 매분 이 짧은 스크립트를 반복호출하는
방식(상시루프 데몬 아님) — iris v1과 같은 "1회성 스크립트+cron" 스타일 유지, 다운돼도
다음 틱에 자동복구.

TRADE_MODE(.env)로 Stage A/B 분기(2026-07-22 추가):
  paper(기본) — 기존 그대로. 시세조회만, 실제 주문 없음.
  real        — core.order_exec으로 실제 매도(전량/부분) 제출 + 잔고폴링 확인.
                체결 미확인시 core.order_exec.set_halt로 그날 자동매매 전체를 멈춘다
                (halt 상태면 이 스크립트는 아무 것도 안 하고 조용히 종료 — 사람이
                bot_state의 iris_v2_halt_{date} 키를 직접 확인/삭제하기 전까지).

cron: 31-59 9,10,11 * * 1-5 및 0-59 12 * * 1-5 (09:31~12:59 KST 매분, v2.5에서 12:59까지 연장)
실행: python3 -m bot.iris_v2_monitor [--dry-run]
"""
import sys
import time

from core.config import IRIS_V2_TRADE_MODE as TRADE_MODE
from core.db import get_conn, state_get, state_set
from core.logger import get_logger
from core.notify import TAG_ENTRY, TAG_PARTIAL, TAG_WARN, TAG_ALERT, exit_tag
from core.timeutil import now_kst, today_kst
from strategy.iris_v2_params import (
    IRIS_V2_VERSION, MID_TIME, TP_PCT, SL_PCT, MID_THRESHOLD,
    PARTIAL_TP_PCT, PARTIAL_TP_FRACTION,
    SPLIT_SELL_ENABLED, SPLIT_SELL_TRANCHES,
)

logger = get_logger("iris_v2_monitor", "IRIS_V2")
ORDER_RETRY = 3


def _midcheck_key(date: str) -> str:
    return f"iris_v2_midcheck_{date}"


def _pricefail_key(date: str) -> str:
    return f"iris_v2_pricefail_streak_{date}"


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
            """SELECT id, ticker, name, entry_price, shares, partial_exit_price, partial_shares
               FROM positions WHERE status='open' AND strategy='iris_v2' AND date_open=?""",
            (date,),
        ).fetchone()
    return dict(row) if row else None


def _take_partial(pos_id: int, remaining_shares: int, partial_shares: int, partial_price: float):
    """DB에 부분익절 결과 기록: shares는 잔여수량으로 갱신, partial_*는 매도분 기록."""
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
    """이전 틱에서 미확인이었던 매도(2026-08-07 신규, pending exit)를 재확인.
    pending이 아예 없으면 False(정상 흐름 진행). pending이 있었으면(재확인
    결과가 done/pending/timeout 무엇이든) True — 이번 틱은 여기서 끝."""
    from core.order_exec import retry_pending_exit
    result = retry_pending_exit(date)
    if result is None:
        return False

    if result["status"] == "pending":
        logger.info(f"{result['ticker']} 매도체결 재확인 {result['retry_count']}회차 — 아직 미확인, 다음 틱 재시도")
        return True

    if result["status"] == "timeout":
        from core.order_exec import PENDING_RETRY_MAX
        _tg(f"청산({result['reason']}) 체결 재확인 {PENDING_RETRY_MAX}회 시도 후에도 실패: "
            f"<b>{result['ticker']}</b> — 수동 확인 필요", TAG_ALERT)
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
    else:  # partial
        _take_partial(pos["id"], result["remaining"], result["actual_qty"], result["exit_price"])
        logger.info(f"pending 재확인으로 1차 부분익절 지연확정: {result['ticker']} "
                    f"price={result['exit_price']} 매도수량={result['actual_qty']}")
        _tg(
            f"부분익절(지연확정): <b>{pos['name']}</b>({result['ticker']})\n"
            f"{pos['entry_price']:,.0f}원 → {result['exit_price']:,.0f}원 · {result['actual_qty']}주 매도"
            f"(잔여 {result['remaining']}주 계속보유)\n주문번호 {result['odno']}",
            TAG_PARTIAL,
        )
    return True


def _add_tranche2(pos_id: int, tranche2_qty: int, tranche2_price: float) -> tuple[int, float]:
    """2026-08-08 신규(SPLIT_BUY_ENABLED) — 트랑쉬2 체결분을 포지션에 합산,
    entry_price를 트랑쉬1/2 가중평균으로 재계산."""
    with get_conn() as conn:
        row = conn.execute("SELECT entry_price, shares FROM positions WHERE id=?", (pos_id,)).fetchone()
        old_shares, old_price = row["shares"], row["entry_price"]
        new_shares = old_shares + tranche2_qty
        new_price = (old_shares * old_price + tranche2_qty * tranche2_price) / new_shares
        conn.execute("UPDATE positions SET shares=?, entry_price=? WHERE id=?",
                     (new_shares, round(new_price, 2), pos_id))
    return new_shares, new_price


def _handle_pending_entry_tranche(date: str) -> bool:
    """트랑쉬2(2026-08-08 신규, SPLIT_BUY_ENABLED) 매수 대기 처리 — pending_exit과
    동일 원리(core.order_exec.retry_pending_entry_tranche). pending이 아예 없으면
    False(정상 흐름 진행). 있었으면(done/pending/gave_up 무엇이든) True — 이번
    틱은 여기서 끝(SL/TP 판단은 안 함).

    **2026-08-08 코드감사에서 재확인·수정**: 이 True 반환이 SL/TP 감시를 그
    틱만큼 건너뛰게 하는 건 사실이고, 트랑쉬2가 여러 틱에 걸쳐 재시도되면
    (PENDING_RETRY_MAX=5, 최대 ~5분) 그 시간만큼 감시공백이 생긴다 — 원래
    주석은 이게 "항상 1틱 안에 끝난다"는 불변식이라 주장했는데 이건 틀렸다
    (재시도 메커니즘 자체가 여러 틱을 전제로 설계돼 있음). **그럼에도 이
    early-return 구조를 그대로 유지하는 게 맞다** — 만약 대신 트랑쉬2가
    미확정인 채로 SL/TP를 평가해 즉시 매도해버리면, 트랑쉬2 매수주문이
    그 뒤에 체결 확정될 경우 "방금 판 종목을 다시 사들이는" 훨씬 심각한
    레이스가 생긴다(실제로 이 함수 구조를 바꿔 시도해봤다가 그 위험을
    뒤늦게 발견하고 되돌림). 즉 지금 구조는 "SL/TP 반응이 최대 5분 늦어질
    수 있는 리스크"와 "판 종목을 다시 사들이는 리스크" 중 더 안전한 쪽을
    의도적으로 선택한 것 — 실제로는 시장가 주문이라 거의 항상 같은 틱
    안에 끝난다(SPLIT_TRANCHE2_FILL_WAIT_SECS 총 10초 폴링 예산)."""
    from core.order_exec import retry_pending_entry_tranche, resolve_entry_price
    result = retry_pending_entry_tranche(date)
    if result is None:
        return False
    if result["status"] == "pending":
        logger.info(f"{result['ticker']} 트랑쉬2 매수 재확인 {result['retry_count']}회차 — 아직 미확인")
        return True
    pos = _open_position(date)
    if pos is None:
        # 트랑쉬2가 done/gave_up으로 확정됐는데 포지션이 이미 없음(closed) —
        # 이론상 일어날 수 없어야 하는 상태(위 docstring 참조)지만, 만에 하나
        # 발생하면 실제 계좌엔 방금 산 주식이 남아있는데 positions 테이블엔
        # 전혀 기록이 안 되는 "고아 보유분"이 생긴다 — 로그만으로는 아무도
        # 못 알아챔, 반드시 사람이 즉시 확인해야 해서 알림도 함께 보낸다
        # (2026-08-08 코드감사에서 추가 — 원래는 로그만 남기고 조용히 넘어갔음).
        logger.error(f"트랑쉬2 처리됐지만 open 포지션을 못 찾음(비정상): {result}")
        _tg(f"트랑쉬2 매수가 확정됐는데 포지션 기록을 못 찾음(비정상 상태) — "
            f"실제 계좌에 고아 보유분이 있을 수 있음, 즉시 확인 필요: {result}", TAG_ALERT)
        return True
    if result["status"] == "gave_up":
        if result["qty"] > 0:
            tranche2_price = resolve_entry_price(result["ticker"], pos["entry_price"], result.get("odno", ""))
            new_shares, new_price = _add_tranche2(pos["id"], result["qty"], tranche2_price)
            logger.warning(f"트랑쉬2 재시도소진 — 부분체결 {result['qty']}주만 반영: {result['ticker']} "
                            f"최종 {new_shares}주@{new_price:.0f}")
            _tg(f"2차 매수 일부만 체결({result['qty']}주) — 현재 {new_shares}주 보유중으로 감시 계속: "
                f"<b>{pos['name']}</b>({result['ticker']})", TAG_WARN)
        else:
            logger.warning(f"트랑쉬2 매수 결국 실패 — 트랑쉬1({pos['shares']}주)만으로 감시 계속: {result['ticker']}")
            _tg(f"2차 매수 실패 — 1차분({pos['shares']}주)만으로 감시 계속: "
                f"<b>{pos['name']}</b>({result['ticker']})", TAG_WARN)
        return True
    # status == "done"
    tranche2_price = resolve_entry_price(result["ticker"], pos["entry_price"], result["odno"])
    new_shares, new_price = _add_tranche2(pos["id"], result["qty"], tranche2_price)
    logger.info(f"트랑쉬2 매수 확정: {result['ticker']} {result['qty']}주@{tranche2_price:.0f} "
                f"→ 합계{new_shares}주@평균{new_price:.0f}")
    _tg(f"진입(2/2차 완료): <b>{pos['name']}</b>({result['ticker']}) {result['qty']}주 @ {tranche2_price:,.0f}원\n"
        f"합계 {new_shares}주 · 평균단가 {new_price:,.0f}원", TAG_ENTRY)
    return True


def _handle_pending_split_sell(date: str) -> bool:
    """분할매도(2026-08-08 신규, SPLIT_SELL_ENABLED) 대기 처리 — pending_exit과
    동일 패턴이나 전량청산(부분익절 아님) 전용. pending 없으면 False."""
    from core.order_exec import retry_pending_split_sell
    result = retry_pending_split_sell(date)
    if result is None:
        return False
    if result["status"] == "pending":
        logger.info(f"{result['ticker']} 분할매도 재확인 대기중")
        return True
    if result["status"] == "halted":
        _tg(f"분할매도({result['reason']}) 처리 실패: <b>{result['ticker']}</b> — "
            f"오늘 자동매매 중단, 수동 확인 필요", TAG_ALERT)
        return True
    # status == "done"
    pos = _open_position(date)
    if pos is None:
        logger.error(f"분할매도 완료됐지만 open 포지션을 못 찾음(비정상): {result}")
        return True
    pnl_pct = _close_position(pos["id"], result["exit_price"], result["reason"])
    logger.info(f"분할매도 완료: {result['ticker']} {result['reason']} price={result['exit_price']:.0f}")
    _tg(
        f"청산({result['reason']}): <b>{pos['name']}</b>({result['ticker']})\n"
        f"{pos['entry_price']:,.0f}원 → {result['exit_price']:,.0f}원(블렌드) · 손익 {pnl_pct:+.2f}% (분할매도)\n"
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
            logger.warning(f"halt 상태(사유={halt.get('action')}) — 자동매매 중단 유지, 수동 확인 대기")
            return

        # 2026-08-07: 이전 틱에서 매도체결 확인이 안 끝난 채(pending) 남아있으면
        # 이번 틱은 오직 그 재확인만 하고 끝낸다 — 신선한 SL/TP 재평가를 먼저 하면
        # (가격이 그새 회복했다든지) 이미 나가있는 매도주문을 놔둔 채 다른 판단을
        # 내릴 위험이 있어서, pending 해소가 항상 최우선.
        if _handle_pending_exit(date):
            return

        # 2026-08-08 신규(SPLIT_SELL_ENABLED) — 분할매도 대기가 남아있으면 그것부터
        # (매도측이 항상 매수측보다 급함, pending_exit과 같은 순서원칙).
        if _handle_pending_split_sell(date):
            return

        # 2026-08-08 신규(SPLIT_BUY_ENABLED) — 트랑쉬2 매수 대기 처리(09:31 첫틱에서
        # 가장 먼저 걸림). 매도측 pending을 전부 비운 뒤에 처리 — 포지션이 이미
        # SL/TP로 청산 트리거된 상태라면 트랑쉬2를 더 사들일 이유가 없어야 하는데,
        # 실제로는 트랑쉬2가 걸려있다는 것 자체가 아직 포지션이 막 열린 지 1틱
        # 이내(오늘 새 진입)라는 뜻이라 이 시점엔 청산 트리거가 날 수 없음(시간상
        # 불변식) — 그래도 안전하게 매도 pending을 먼저 비우는 순서를 유지.
        if _handle_pending_entry_tranche(date):
            return

    pos = _open_position(date)
    if pos is None:
        return  # 빠른 no-op — 오늘 진입 없었거나 이미 청산됨

    price = _get_price_safe(pos["ticker"])
    if price is None:
        if dry_run:
            logger.warning(f"{pos['ticker']} 시세조회 실패 — 이번 분 스킵, 다음 분 재시도")
            return
        streak = (state_get(_pricefail_key(date)) or 0) + 1
        state_set(_pricefail_key(date), streak)
        logger.warning(f"{pos['ticker']} 시세조회 실패(연속 {streak}분) — 이번 분 스킵, 다음 분 재시도")
        # 3분 연속 실패 시 최초 1회, 이후로도 지속되면 10분 간격으로 재알림(스팸 방지 + 장기방치 방지)
        if streak == 3 or (streak > 3 and streak % 10 == 0):
            _tg(f"{pos['name']}({pos['ticker']}) 시세조회 {streak}분 연속 실패 — "
                f"포지션 감시 중단 상태, 수동 확인 필요", TAG_ALERT)
        return

    if not dry_run and (state_get(_pricefail_key(date)) or 0) > 0:
        state_set(_pricefail_key(date), 0)
        logger.info(f"{pos['ticker']} 시세조회 복구됨")

    cur = price["price"]
    ret = (cur - pos["entry_price"]) / pos["entry_price"]

    reason = None
    exit_price = None
    if ret <= SL_PCT:
        reason, exit_price = "손절", cur
    elif ret >= TP_PCT:
        reason, exit_price = "익절", cur
    else:
        hhmm = now_kst().strftime("%H%M")
        # v2.5.2 버그수정: `hhmm == MID_TIME`(정확히 11:30 틱에서만)이었는데, 그 1분의
        # 틱이 시세조회 실패·cron 지연·일시 장애로 넘어가면 그날 중간평가가 통째로
        # 사라졌음. `>=`로 바꿔 11:30 이후 최초로 성공한 틱에서 1회 수행(idempotency
        # 플래그가 중복 수행을 막음).
        if hhmm >= MID_TIME and state_get(_midcheck_key(date)) is None:
            if not dry_run:
                state_set(_midcheck_key(date), "done")
            if ret < MID_THRESHOLD:
                reason, exit_price = "중간청산", cur
            logger.info(f"11:30 중간평가(실행시각 {hhmm}): ret={ret:.4f} threshold={MID_THRESHOLD} → "
                        f"{'조기청산' if reason else '보유유지'}")

    if reason is None:
        # 전량청산 조건은 아니지만, 1차 부분익절(+8%) 도달했고 아직 발동 전이면 절반만 선매도
        if ret >= PARTIAL_TP_PCT and not pos["partial_shares"]:
            intended_qty = int(pos["shares"] * PARTIAL_TP_FRACTION)
            if intended_qty <= 0 or intended_qty >= pos["shares"]:
                return  # 수량이 너무 적어 분할 무의미 — 스킵
            if dry_run:
                logger.info(f"[dry-run] {pos['ticker']} 1차 부분익절 조건 충족(ret={ret:.4f}) — 미체결")
                return
            if real_mode:
                from core.order_exec import execute_partial_sell
                result = execute_partial_sell(pos["ticker"], intended_qty, pos["shares"], pos["entry_price"], date)
                if result == "pending":
                    # 2026-08-07: 즉시 halt 아님 — 다음 틱에서 retry_pending_exit()로 이어서 확인
                    logger.info(f"{pos['ticker']} 1차 부분익절 체결 미확인 — pending 등록, 다음 틱 재확인 예정")
                    return
                if result is None:
                    _tg(f"1차 부분익절 체결 실패: <b>{pos['name']}</b>({pos['ticker']}) — "
                        f"오늘 자동매매 중단, 수동 확인 필요", TAG_ALERT)
                    return
                actual_qty, remaining, exit_p, odno = result
                _take_partial(pos["id"], remaining, actual_qty, exit_p)
                partial_note = f" (목표{intended_qty}주 중 {actual_qty}주 체결)" if actual_qty != intended_qty else ""
                logger.info(f"1차 부분익절(실거래): {pos['ticker']} ret={ret:.4f} price={exit_p} 매도수량={actual_qty}")
                _tg(
                    f"부분익절: <b>{pos['name']}</b>({pos['ticker']})\n"
                    f"{pos['entry_price']:,.0f}원 → {exit_p:,.0f}원 · {actual_qty}주 매도{partial_note}"
                    f"(잔여 {remaining}주 계속보유)\n주문번호 {odno}",
                    TAG_PARTIAL,
                )
            else:
                remaining = pos["shares"] - intended_qty
                _take_partial(pos["id"], remaining, intended_qty, cur)
                logger.info(f"1차 부분익절: {pos['ticker']} ret={ret:.4f} price={cur} 매도수량={intended_qty}")
                _tg(
                    f"부분익절: <b>{pos['name']}</b>({pos['ticker']})\n"
                    f"{pos['entry_price']:,.0f}원 → {cur:,.0f}원(+{ret:.1%}) · {intended_qty}주 매도(잔여 계속보유)",
                    TAG_PARTIAL,
                )
            return
        logger.info(f"{pos['ticker']} 감시중 ret={ret:.4f} (TP={TP_PCT} SL={SL_PCT})")
        return

    logger.info(f"청산판단: {pos['ticker']} {reason} ret={ret:.4f} price={exit_price}")
    if dry_run:
        logger.info("[dry-run] DB 미기록·텔레그램 미발송")
        return

    if real_mode:
        if SPLIT_SELL_ENABLED:
            # 2026-08-08 신규 — 강제청산(force_close.py)에는 안 씀(데드라인 우선,
            # 설계결정), 부분익절(+8%)과도 별개(위 execute_partial_sell 그대로).
            from core.order_exec import execute_split_close
            result = execute_split_close(pos["ticker"], pos["shares"], pos["entry_price"],
                                          reason, date, SPLIT_SELL_TRANCHES)
        else:
            from core.order_exec import execute_full_close
            result = execute_full_close(pos["ticker"], pos["shares"], pos["entry_price"], reason, date)
        if result == "pending":
            # 2026-08-07: 즉시 halt 아님 — 다음 틱에서 retry_pending_exit()로 이어서 확인
            logger.info(f"{pos['ticker']} 청산({reason}) 체결 미확인 — pending 등록, 다음 틱 재확인 예정")
            return
        if result is None:
            _tg(f"청산({reason}) 체결 실패: <b>{pos['name']}</b>({pos['ticker']}) — "
                f"오늘 자동매매 중단, 수동 확인 필요", TAG_ALERT)
            return
        exit_price, odno = result
        pnl_pct = _close_position(pos["id"], exit_price, reason)
        _tg(
            f"청산({reason}): <b>{pos['name']}</b>({pos['ticker']})\n"
            f"{pos['entry_price']:,.0f}원 → {exit_price:,.0f}원 · 손익 {pnl_pct:+.2f}%\n"
            f"주문번호 {odno}",
            exit_tag(pnl_pct),
        )
    else:
        pnl_pct = _close_position(pos["id"], exit_price, reason)
        _tg(
            f"청산({reason}, 페이퍼): <b>{pos['name']}</b>({pos['ticker']})\n"
            f"{pos['entry_price']:,.0f}원 → {exit_price:,.0f}원 · 손익 {pnl_pct:+.2f}%",
            exit_tag(pnl_pct),
        )


if __name__ == "__main__":
    from core.script_guard import run_guarded
    from core.proc_lock import script_lock, LockHeld
    _dry = "--dry-run" in sys.argv
    try:
        with script_lock("iris_v2_monitor"):
            run_guarded(lambda: run(dry_run=_dry), "iris_v2_monitor", logger, _dry)
    except LockHeld:
        logger.warning("이전 iris_v2_monitor 실행이 아직 안 끝나 이번 틱 스킵(중복실행 방지)")
