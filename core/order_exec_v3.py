"""
IRIS v3 — 실제 KIS 주문 실행.

core/order_exec.py(v2)의 halt·pending 메커니즘은 상태 키가
`f"iris_v2_halt_{date}"`처럼 하드코딩돼있어 v3가 그대로 쓰면 같은
날짜에 v2와 상태가 충돌한다(iris DECISIONS 2026-08-18 구현계획 참조).
그래서 **상태를 갖는 함수(halt·pending)는 v3 전용으로 새로 작성**하고,
**상태 없는 순수 함수는 core.order_exec에서 그대로 재사용**한다 —
공유 모듈(core/order_exec.py)은 v2 실거래가 매일 쓰는 검증된 코드라
건드리지 않는 것이 회귀위험이 가장 낮다.

재사용(변경 없음): round_to_tick, sell_safe_limit, poll_remaining_shares,
resolve_exit_price, resolve_entry_price, cancel_or_convert_to_market,
compute_buy_qty, _safe_available_cash.

신규(v3 전용): buy_conditional_limit_safe, halt_key/set_halt/is_halted/
find_unresolved_halt, pending_entry(매수 확정대기 — 조건부지정가는
동시호가 결과가 15:30 이후에나 확정되므로), pending_exit(매도 확정대기,
v2와 같은 원리).

**"확실하지 않으면 이중주문보다 멈춤이 항상 안전"** 원칙은 v2와 동일하게
적용한다.
"""
import time

from core.logger import get_logger

logger = get_logger("order_exec_v3", "IRIS_V3")

ORDER_RETRY = 3
# 매도 지정가 시도 후 시장가 전환까지 총 대기시간(SELL_TIMEOUT_SECS=180과
# 합이 같아야 함, strategy/iris_v3_params.py 참조) — v2의 FILL_WAIT_SECS와
# 같은 이유(여러 번에 걸쳐 짧게 폴링, 한 번에 통으로 179초 sleep 안 함)로
# 세분화. v3의 매도 스크립트는 cron 1회 실행 안에서 이 전체 흐름을
# 동기적으로 끝낸다(v2 monitor.py처럼 매분 틱에 걸쳐 나누지 않음 — v3는
# 애초에 매분 모니터링이 없는 구조, iris v3 다각도 전문가 분석.md 6번 참조).
EXIT_WAIT_SECS = (10, 20, 30, 30, 30, 30, 30)  # 합 180초


# ── 조건부지정가 매수 ─────────────────────────────────────────────────
def buy_conditional_limit_safe(ticker: str, qty: int, price: float) -> tuple[bool, str, str]:
    """조건부지정가 매수 주문 제출(재시도 포함). price는 자동으로 호가단위
    (내림)에 맞춰 반올림됨. 반환: (성공여부, 주문번호, 주문점).
    미체결 잔량은 거래소가 15:20~15:30 동시호가에서 자동으로 시장가
    전환하므로(core.kis_api.buy_conditional_limit 참조), core.order_exec의
    cancel_or_convert_to_market() 같은 수동 전환 로직이 필요 없다 — 그
    결과 확인은 confirm_entry.py(동시호가 종료 후)의 몫."""
    if qty <= 0:
        return False, "", ""
    from core.order_exec import round_to_tick
    tick_price = round_to_tick(price, direction="down")
    from core.kis_api import buy_conditional_limit
    for i in range(ORDER_RETRY):
        try:
            result = buy_conditional_limit(ticker, qty, tick_price)
            output = result.get("output", {})
            odno = output.get("ODNO", "?")
            orgno = output.get("KRX_FWDG_ORD_ORGNO", "")
            logger.info(f"조건부지정가 매수 주문 제출: {ticker} {qty}주 @{tick_price} 주문번호={odno}")
            return True, odno, orgno
        except Exception as e:
            logger.warning(f"조건부지정가 매수 시도 {i+1}/{ORDER_RETRY} 실패: {ticker} — {e}")
            if i < ORDER_RETRY - 1:
                time.sleep(2 ** i)
    logger.error(f"조건부지정가 매수 최종 실패: {ticker}")
    return False, "", ""


# ── halt(v2와 독립된 상태, 키만 iris_v3_ 접두사) ──────────────────────
def halt_key(date: str) -> str:
    return f"iris_v3_halt_{date}"


def set_halt(date: str, ticker: str, action: str, detail: str):
    """체결 미확인 등 위험 상태 발생시 그날 v3 자동매매를 전부 멈춘다.
    v2의 halt와 독립적 — v3 문제가 v2를 막지 않고, 그 반대도 마찬가지."""
    from core.db import state_set
    from core.timeutil import iso_kst
    state_set(halt_key(date), {"ticker": ticker, "action": action, "detail": detail, "time": iso_kst()})


def is_halted(date: str) -> dict | None:
    from core.db import state_get
    return state_get(halt_key(date))


def find_unresolved_halt() -> dict | None:
    """core.order_exec.find_unresolved_halt()와 동일 원리(날짜 무관하게
    미해결 halt를 스캔) — 'iris_v3_halt_' 접두사만 다름."""
    from core.db import get_conn
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT key FROM bot_state WHERE key LIKE 'iris_v3_halt_%' ORDER BY key DESC"
        ).fetchall()
    if not rows:
        return None
    latest_key = rows[0]["key"]
    date = latest_key.replace("iris_v3_halt_", "")
    halt = is_halted(date)
    if halt is None:
        return None
    return {**halt, "date": date}


# ── 매수 확정 대기(pending entry) ─────────────────────────────────────
# 조건부지정가는 미체결 잔량이 15:20~15:30 동시호가에서 자동으로 시장가
# 전환되므로, entry.py 시점(~15:00~15:05)에는 최종 체결결과를 알 수
# 없다 — 주문 접수 직후 이 상태를 기록해두고, confirm_entry.py(동시호가
# 종료 후, ~15:35~15:40)가 실제 잔고를 확인해 최종 처리한다.
def _pending_entry_key(date: str) -> str:
    return f"iris_v3_pending_entry_{date}"


def get_pending_entry(date: str) -> dict | None:
    from core.db import state_get
    return state_get(_pending_entry_key(date))


def set_pending_entry(date: str, data: dict):
    from core.db import state_set
    state_set(_pending_entry_key(date), data)


def clear_pending_entry(date: str):
    from core.db import state_set
    state_set(_pending_entry_key(date), None)


# ── 매도 확정 대기(pending exit) — v2 execute_full_close와 동일 원칙 ──
def _pending_exit_key(date: str) -> str:
    """2026-08-18: 날짜 무관 고정키로 변경(실거래 개시(08-19) 전날 코드감사에서 발견) —
    execute_exit()은 bot/iris_v3_exit.py가 그날 today_kst()를 넘겨 호출하는데,
    pending으로 남은 채 그날 스크립트가 끝나면 다음날 exit.py가 다시 실행될 때는
    "오늘"이 하루 넘어가있어 원래 키(어제 날짜)를 못 찾는다 — v2와 달리 v3는
    force_close 같은 "그날 안에 끝까지 재시도" 장치가 없어(1회성 cron이라
    poll 이후 그냥 종료), pending이 자정을 넘기면 다음날 exit.py가 pending을
    못 보고 완전히 새로 매도주문을 내 이중매도 위험이 있었다. v3는 "1일1종목"
    설계라 시스템 전체에서 미결pending이 동시에 2건 이상 존재할 수 없으므로,
    date 매개변수를 무시하고 고정키를 쓰면 어느 날 재실행해도 항상 같은
    레코드를 찾는다(기존 호출부 시그니처는 그대로 유지해 변경범위 최소화)."""
    return "iris_v3_pending_exit"


def get_pending_exit(date: str) -> dict | None:
    from core.db import state_get
    return state_get(_pending_exit_key(date))


def _set_pending_exit(date: str, data: dict):
    from core.db import state_set
    state_set(_pending_exit_key(date), data)


def _clear_pending_exit(date: str):
    from core.db import state_set
    state_set(_pending_exit_key(date), None)


def execute_exit(ticker: str, shares: int, entry_price: float, ref_price: float,
                  date: str) -> tuple[float, str] | None | str:
    """익일 09:00 매도 실행 — 지정가로 시도 → EXIT_WAIT_SECS(합 180초=
    SELL_TIMEOUT_SECS) 동안 폴링 → 여전히 미체결 잔량 있으면
    core.order_exec.cancel_or_convert_to_market()로 시장가 전환 →
    재확인. 이 함수 호출 자체가 최대 180초+α 동안 블로킹된다(v3 매도
    스크립트는 cron 1회 실행 안에서 이 전체 흐름을 동기적으로 끝냄 —
    v2 monitor.py처럼 매분 틱에 걸쳐 나누지 않음).

    성공시 (체결가, 주문번호) 반환, 확실히 실패시(주문 자체가 거부)
    halt 세우고 None, **체결여부가 아직 불명이면 "pending" 반환**(halt
    아님 — 호출부가 실패로 처리하지 말고 그대로 알림만 보낼 것. 다음
    실행에서 retry_pending_exit()로 이어서 확인 가능)."""
    from core.order_exec import sell_safe_limit, poll_remaining_shares, cancel_or_convert_to_market, resolve_exit_price
    ok, odno, orgno = sell_safe_limit(ticker, shares, ref_price, reason="익일시가청산")
    if not ok:
        set_halt(date, ticker, "매도주문실패", f"의도수량={shares}")
        return None
    # 2026-08-18: v2(08-10(17차) 발견2) 교훈 그대로 — poll 전에 먼저 pending
    # 등록해, 이 함수 실행 도중 프로세스가 죽어도 다음 실행이 이어받게 함.
    _set_pending_exit(date, {"ticker": ticker, "odno": odno, "orgno": orgno,
                              "shares_before": shares, "entry_price": entry_price,
                              "stage": "limit_submitted", "retry_count": 0})
    remaining = poll_remaining_shares(ticker, expect_at_most=0, odno=odno,
                                       shares_before=shares, wait_secs=EXIT_WAIT_SECS)
    if remaining is None or remaining > 0:
        logger.warning(f"매도 지정가 타임아웃 — 시장가 전환 시도: {ticker} 주문번호={odno} "
                        f"잔여추정={remaining}")
        conv = cancel_or_convert_to_market(odno, orgno)
        if conv is None:
            logger.warning(f"시장가 전환 시도 자체 실패 — pending 유지(다음 실행에서 재확인): {ticker}")
            return "pending"
        if conv["status"] == "converted":
            remaining = poll_remaining_shares(ticker, expect_at_most=0, odno=odno,
                                               shares_before=shares, wait_secs=(5, 10, 15))
            if remaining is None or remaining > 0:
                logger.warning(f"시장가 전환 후에도 미확인 — pending 유지: {ticker} 잔여추정={remaining}")
                return "pending"
        # already_done이면 이미 전량 처리된 것 — 그대로 확정 단계로 진행
    _clear_pending_exit(date)
    exit_price = resolve_exit_price(ticker, odno, entry_price)
    return exit_price, odno


def retry_pending_exit(date: str) -> dict | None:
    """execute_exit()이 "pending"을 반환했거나 프로세스가 도중에 죽은 경우,
    다음 실행(수동 재실행 또는 향후 안전망 cron)에서 이어서 확인.
    core.order_exec.retry_pending_exit()과 동일 계약(status: done/pending/timeout)."""
    from core.order_exec import poll_remaining_shares, cancel_or_convert_to_market, resolve_exit_price
    pending = get_pending_exit(date)
    if pending is None:
        return None
    ticker, odno, orgno = pending["ticker"], pending["odno"], pending.get("orgno", "")
    remaining = poll_remaining_shares(ticker, expect_at_most=0, odno=odno,
                                       shares_before=pending["shares_before"])
    if remaining is not None and remaining <= 0:
        _clear_pending_exit(date)
        exit_price = resolve_exit_price(ticker, odno, pending["entry_price"])
        return {"status": "done", "ticker": ticker, "odno": odno, "exit_price": exit_price}
    # 아직 지정가 단계였다면 여기서도 시장가 전환을 한 번 더 시도
    conv = cancel_or_convert_to_market(odno, orgno)
    if conv and conv["status"] in ("converted", "already_done"):
        remaining = poll_remaining_shares(ticker, expect_at_most=0, odno=odno,
                                           shares_before=pending["shares_before"], wait_secs=(5, 10, 15))
        if remaining is not None and remaining <= 0:
            _clear_pending_exit(date)
            exit_price = resolve_exit_price(ticker, odno, pending["entry_price"])
            return {"status": "done", "ticker": ticker, "odno": odno, "exit_price": exit_price}
    retry_count = pending["retry_count"] + 1
    from core.order_exec import PENDING_RETRY_MAX
    if retry_count >= PENDING_RETRY_MAX:
        _clear_pending_exit(date)
        set_halt(date, ticker, "매도체결미확인", f"주문번호={odno} {retry_count}회 재시도 후에도 미확인")
        return {"status": "timeout", "ticker": ticker, "odno": odno}
    _set_pending_exit(date, {**pending, "retry_count": retry_count})
    return {"status": "pending", "ticker": ticker, "odno": odno, "retry_count": retry_count}
