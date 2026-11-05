"""
IRIS v2 Stage B — 실제 KIS 주문 실행 (매수/매도 공통 헬퍼, 2026-07-22 신규).

아카이브된 iris_v1_bot.py(2026-07-11, 실계좌 자금 투입 전 은퇴)의 검증된 패턴 +
K3.1 시절(2026-06 초) 실제로 겪은 두 사고의 교훈을 반영한다(둘 다 iris DECISIONS에
기록됨, 2026-07-22 사용자가 재차 강조):

  1. **2026-06-10 예수금 폴백 버그**: `get_available_cash()`(매수가능금액, T+2
     정산반영이라 보수적)가 간헐적 500 에러를 내자 `get_balance()["cash"]`(예수금
     총액, T+2 미반영이라 더 큰 값)로 그대로 대체해 예산을 계산 → 실제 주문가능금액을
     초과해 매수가 거부(APBK0952)됐음. 교훈 반영: `_safe_available_cash()`가
     get_available_cash() 자체에 재시도를 걸고, 그래도 실패하면 get_balance()["cash"]
     에 3% 안전마진(×0.97, 당시와 동일 수치)을 곱해 과대평가를 보정한다.
  2. **2026-06-05 매도 미체결 오판**: 15:20 강제청산에서 매도 주문이 접수(ODNO 발급)만
     되고 실제 미체결이었는데 봇이 주문접수=체결로 오판해 포지션을 지우고 "청산 성공"
     알림을 보냄 — 사용자가 수동으로 손실 처리. 교훈 반영: **주문 응답이 아니라 실제
     잔고(get_balance)를 폴링해 체결을 확인**하는 원칙(아래 전체 구조) 자체가 이 사고의
     직접적인 재발방지책. 2026-06-15엔 `inquire-balance` 자체도 간헐적 500이 나고
     한번은 6분간 지속된 전례가 있어(2026-06-05), 폴링 시도횟수·대기시간을 넉넉히 잡음
     (FILL_WAIT_SECS, 아래).

  - 매수가는 get_filled_price가 아니라 주문 직후 재조회한 현재가로 근사(iris v1과
    동일 단순화 — 시장가 즉시체결 가정하에 오차가 작고, get_filled_price는 정산지연으로
    신뢰도가 낮을 때가 있음).
  - 매도가는 get_filled_price로 실제 체결가를 우선 쓰고, 실패시 현재가→진입가 순으로
    폴백(정확한 손익계산이 매도 쪽에서 더 중요).

**미확인 상태에서는 추가 주문을 절대 내지 않는다** — 폴링으로 체결을 못 밝혀내면
호출부가 iris_v2_halt 플래그를 세워 자동매매를 멈추고 사람 확인을 기다리게 한다
(이중주문보다 "멈춤"이 항상 안전).
"""
import math
import time

from core.logger import get_logger

logger = get_logger("order_exec", "IRIS_V2")

ORDER_RETRY = 3
CASH_RETRY = 3
# 잔고 재확인 대기 간격 — iris v1의 (2,3,5)=10초 → 35초(2026-07-22) → 45초(2026-07-24).
# KRX 시장가주문은 미체결 잔량이 즉시 죽는 게 아니라 장중 내내 살아서 체결을 계속
# 시도하므로(매수시장가=상한가 지정가 취급), 35초 컷은 "체결 진행중"을 "실패"로
# 오판해 불필요한 halt를 낼 수 있음. 단 monitor가 cron 매분 실행이라 한 틱이 60초를
# 넘기면 다음 틱과 겹쳐 이중주문 위험 — 총 sleep 45초 + API 왕복 오버헤드로 1분 예산
# 안에서만 확장(더 긴 대기가 필요하면 pending 상태 관리 재설계가 선행돼야 함).
FILL_WAIT_SECS = (2, 3, 5, 10, 10, 15)
# 2026-08-08 신규(분할매수/매도, SPLIT_BUY_ENABLED/SPLIT_SELL_ENABLED): 트랑쉬가
# 여러 개면 트랑쉬마다 FILL_WAIT_SECS(45초)를 다 쓸 수 없음 — monitor 1틱(60초
# 예산) 안에 트랑쉬 1개도 못 넣고 다음 틱이 LockHeld로 스킵될 위험(이론검증 발견,
# iris DECISIONS 2026-08-08 참조). 대신 트랑쉬당 짧게 확인하고, 그래도 안 되면
# halt 대신 pending으로 등록해 다음 monitor 틱에서 이어감(PENDING_RETRY_MAX 재사용).
SPLIT_SELL_FILL_WAIT_SECS = (2, 3, 5)      # 매도 트랑쉬당(합10초)
SPLIT_TRANCHE2_FILL_WAIT_SECS = (2, 3, 5)  # 매수 트랑쉬2당(합10초)
# 2026-08-08(같은 날, 최종검증) 신규 — 트랑쉬1(지정가) 최초 확인 예산. 기존
# FILL_WAIT_SECS(45초)를 그대로 쓰면 미체결 판정까지 너무 오래 걸려 이후
# 시장가전환+재확인 여유가 없어짐(entry.py는 monitor.py 09:31 첫틱 전에 끝나야
# 함) — 짧게 확인하고 안 되면 곧장 cancel_or_convert_to_market()로 넘어감
# (buy_tranche1_with_fallback 참조). 09차 지정가 체결가능성 분석(81.7%가 같은
# 분 안에 체결)과 정합 — 대부분은 이 20초 안에 이미 잡힘.
TRANCHE1_LIMIT_WAIT_SECS = (2, 3, 5, 10)   # 트랑쉬1 지정가 최초확인(합20초)


def _get_price_safe(ticker: str) -> dict | None:
    from core.kis_api import get_price
    for i in range(ORDER_RETRY):
        try:
            return get_price(ticker)
        except Exception as e:
            if i < ORDER_RETRY - 1:
                time.sleep(2 ** i)
            else:
                logger.warning(f"시세조회 최종 실패 {ticker}: {e}")
    return None


def _get_actual_holdings() -> dict | None:
    try:
        from core.kis_api import get_balance
        bal = get_balance()
        return {h["ticker"]: h["qty"] for h in bal["holdings"]}
    except Exception as e:
        logger.error(f"보유 주식 조회 실패: {e}")
        return None


def buy_safe(ticker: str, qty: int) -> tuple[bool, str]:
    """매수 주문 제출(재시도 포함). 반환: (성공여부, 주문번호)."""
    if qty <= 0:
        return False, ""
    from core.kis_api import buy_market
    for i in range(ORDER_RETRY):
        try:
            result = buy_market(ticker, qty)
            odno = result.get("output", {}).get("ODNO", "?")
            logger.info(f"매수 주문 제출: {ticker} {qty}주 주문번호={odno}")
            return True, odno
        except Exception as e:
            logger.warning(f"매수 시도 {i+1}/{ORDER_RETRY} 실패: {ticker} — {e}")
            if i < ORDER_RETRY - 1:
                time.sleep(2 ** i)
    logger.error(f"매수 최종 실패: {ticker}")
    return False, ""


def sell_safe(ticker: str, qty: int, reason: str = "") -> tuple[bool, str]:
    """매도 주문 제출(재시도 포함). 반환: (성공여부, 주문번호)."""
    if qty <= 0:
        return True, ""
    from core.kis_api import sell_market
    for i in range(ORDER_RETRY):
        try:
            result = sell_market(ticker, qty)
            odno = result.get("output", {}).get("ODNO", "?")
            logger.info(f"매도 주문 제출: {ticker} {qty}주 주문번호={odno} ({reason})")
            return True, odno
        except Exception as e:
            logger.warning(f"매도 시도 {i+1}/{ORDER_RETRY} 실패: {ticker} — {e}")
            if i < ORDER_RETRY - 1:
                time.sleep(2 ** i)
    logger.error(f"매도 최종 실패: {ticker} ({reason})")
    return False, ""


# ── 지정가 주문(2026-08-07 신규, ④지정가 주문 리서치 — iris DECISIONS
# 2026-08-07 (9차)~(12차) 참조) ────────────────────────────────────────────
# 2026-08-08부터 buy_tranche1_with_fallback()(SPLIT_BUY_ENABLED 시 entry.py가
# 실거래로 호출)이 이 아래 buy_safe_limit/cancel_or_convert_to_market을 실제로
# 씀 — "빌딩블록만, 실거래 미연결"은 이제 낡은 설명. 매수 외 나머지(강제청산
# 등 항상 시장가만 쓰는 경로)에서 지정가로 전환하는 건 여전히 별도 단계.

TICK_TABLE = [
    (2_000, 1), (5_000, 5), (20_000, 10), (50_000, 50),
    (200_000, 100), (500_000, 500),
]  # (미만 상한, 그 구간 호가단위) — 50만원 이상 구간(1000원)은 _tick_size()에서
   # 기본값으로 처리. 2023-01-25 시행, 코스피·코스닥 공통(삼성증권 공식공지 +
   # 별도조사 교차검증, iris DECISIONS 2026-08-07 (10차)).


def _tick_size(price: float) -> int:
    for upper, tick in TICK_TABLE:
        if price < upper:
            return tick
    return 1000


def round_to_tick(price: float, direction: str = "nearest") -> int:
    """호가단위에 맞게 가격을 반올림. 매수 지정가(상한 캡)는 direction="down"
    (의도한 상한보다 비싸지지 않도록), 매도 지정가(하한 캡)는 direction="up"
    (의도한 하한보다 싸지지 않도록) 쓸 것 — buy_safe_limit/sell_safe_limit이
    각각 이렇게 자동 적용함."""
    tick = _tick_size(price)
    if direction == "down":
        return int(price // tick) * tick
    elif direction == "up":
        return math.ceil(price / tick) * tick
    else:
        return round(price / tick) * tick


def buy_safe_limit(ticker: str, qty: int, price: float) -> tuple[bool, str, str]:
    """지정가 매수 주문 제출(재시도 포함). price는 자동으로 호가단위(내림)에
    맞춰 반올림됨. 반환: (성공여부, 주문번호, 주문점KRX_FWDG_ORD_ORGNO) — 주문점은
    미체결시 취소/정정에 필요(core.kis_api.cancel_order/revise_to_market 참조).
    주문 응답에서 바로 캡처해두면 나중에 cancel_or_convert_to_market() 호출시
    정정취소가능주문조회를 또 안 해도 됨(주문점만은 — psbl_qty는 잔여수량이라
    그 시점에 다시 조회해야 함, 아래 참조)."""
    if qty <= 0:
        return False, "", ""
    tick_price = round_to_tick(price, direction="down")
    from core.kis_api import buy_limit
    for i in range(ORDER_RETRY):
        try:
            result = buy_limit(ticker, qty, tick_price)
            output = result.get("output", {})
            odno = output.get("ODNO", "?")
            orgno = output.get("KRX_FWDG_ORD_ORGNO", "")
            logger.info(f"지정가 매수 주문 제출: {ticker} {qty}주 @{tick_price} 주문번호={odno}")
            return True, odno, orgno
        except Exception as e:
            logger.warning(f"지정가 매수 시도 {i+1}/{ORDER_RETRY} 실패: {ticker} — {e}")
            if i < ORDER_RETRY - 1:
                time.sleep(2 ** i)
    logger.error(f"지정가 매수 최종 실패: {ticker}")
    return False, "", ""


def sell_safe_limit(ticker: str, qty: int, price: float, reason: str = "") -> tuple[bool, str, str]:
    """지정가 매도 주문 제출(재시도 포함). price는 자동으로 호가단위(올림)에
    맞춰 반올림됨. 반환: (성공여부, 주문번호, 주문점)."""
    if qty <= 0:
        return True, "", ""
    tick_price = round_to_tick(price, direction="up")
    from core.kis_api import sell_limit
    for i in range(ORDER_RETRY):
        try:
            result = sell_limit(ticker, qty, tick_price)
            output = result.get("output", {})
            odno = output.get("ODNO", "?")
            orgno = output.get("KRX_FWDG_ORD_ORGNO", "")
            logger.info(f"지정가 매도 주문 제출: {ticker} {qty}주 @{tick_price} 주문번호={odno} ({reason})")
            return True, odno, orgno
        except Exception as e:
            logger.warning(f"지정가 매도 시도 {i+1}/{ORDER_RETRY} 실패: {ticker} — {e}")
            if i < ORDER_RETRY - 1:
                time.sleep(2 ** i)
    logger.error(f"지정가 매도 최종 실패: {ticker} ({reason})")
    return False, "", ""


def cancel_or_convert_to_market(odno: str, krx_fwdg_ord_orgno: str = "") -> dict | None:
    """미체결 지정가 주문을 정정취소가능주문조회로 확인한 뒤 시장가로 정정
    전환(취소 후 재주문 대신 정정 1건으로 처리 — KIS 공식문서 "정정은 원주문에
    대한 주문단가 혹은 주문구분을 변경하는 사항"으로 주문구분 전환이 가능함을
    확인, iris DECISIONS 2026-08-07 (12차)). krx_fwdg_ord_orgno를 이미 알고
    있으면(주문 제출시 buy_safe_limit/sell_safe_limit이 반환한 값) 넘겨서 그걸
    우선 쓰되, 잔여수량(psbl_qty)은 그 시점 최신값이 필요해 조회는 항상 수행함.

    반환:
      {"status": "converted", "psbl_qty": 잔여수량} — 정정 성공.
      {"status": "already_done"} — 이미 전량체결/취소돼 처리할 게 없음.
      None — 조회/정정 자체가 실패(호출부가 안전하게 halt 등으로 처리해야 함,
             "미확인 상태에서는 추가 주문을 절대 내지 않는다" 원칙 그대로 적용)."""
    from core.kis_api import get_cancelable_order, revise_to_market
    try:
        info = get_cancelable_order(odno)
    except Exception as e:
        logger.warning(f"정정취소가능주문조회 실패: odno={odno} — {e}")
        return None
    if info is None or info["psbl_qty"] <= 0:
        return {"status": "already_done"}
    orgno = krx_fwdg_ord_orgno or info["krx_fwdg_ord_orgno"]
    try:
        revise_to_market(odno, orgno, info["psbl_qty"])
        logger.info(f"지정가→시장가 정정 완료: odno={odno} 잔여{info['psbl_qty']}주")
        return {"status": "converted", "psbl_qty": info["psbl_qty"]}
    except Exception as e:
        logger.warning(f"시장가 정정 실패: odno={odno} — {e}")
        return None


def buy_tranche1_with_fallback(ticker: str, qty: int, ref_price: float) -> tuple[int, str]:
    """2026-08-08(최종검증에서 발견·수정) — 트랑쉬1을 지정가로 제출하고 짧게
    확인, 미체결/부분체결이면 즉시 cancel_or_convert_to_market()로 시장가
    전환해 마저 확인. **이전 버전은 이 폴백이 없어서, 지정가가 45초 안에
    하나도 안 체결되면 그냥 halt하고 그날 진입을 통째로 놓쳤음** — 지정가
    효과분석(9차)이 애초에 가정했던 "미체결시 시장가전환"과 실제 구현이
    달랐던 논리적 결함(iris DECISIONS 2026-08-08 최종검증 항목 참조).

    반환: (최종 확인된 체결수량, 주문번호). odno==""이면 주문 제출 자체가
    실패한 것(재시도 소진) — 그 외엔 이 함수가 할 수 있는 데까지(지정가+
    시장가전환) 다 해본 뒤의 최종 확인치, 그래도 0이면 진짜 실패."""
    ok, odno, orgno = buy_safe_limit(ticker, qty, ref_price)
    if not ok:
        return 0, ""
    actual = verify_buy_fill(ticker, qty, odno=odno, wait_secs=TRANCHE1_LIMIT_WAIT_SECS)
    if actual >= qty:
        return actual, odno
    conv = cancel_or_convert_to_market(odno, orgno)
    if conv is None:
        # 조회/정정 자체 실패 — 안전하게 지금까지 확인된 수량만 반환(추가 시도
        # 없음, "미확인 상태에서는 추가 주문을 절대 내지 않는다" 원칙 그대로).
        logger.warning(f"트랑쉬1 시장가전환 시도 실패(조회불가) — 지정가 체결분만 인정: "
                        f"{ticker} odno={odno} 현재확인{actual}주")
        return actual, odno
    if conv["status"] == "already_done":
        # 정정 시도 직전에 지정가로 마저 체결됐거나 이미 취소된 상태 — 실제
        # 보유수량을 다시 확인(짧게 1회)해서 최종치 확정.
        actual = verify_buy_fill(ticker, qty, odno=odno, wait_secs=(1,))
        return actual, odno
    # converted — 시장가로 바뀐 잔량이 빠르게 체결될 것으로 기대, 짧게 재확인.
    actual = verify_buy_fill(ticker, qty, odno=odno, wait_secs=SPLIT_TRANCHE2_FILL_WAIT_SECS)
    if actual < qty:
        logger.warning(f"트랑쉬1 시장가전환 후에도 미체결/부분체결: {ticker} odno={odno} "
                        f"목표{qty}주 확인{actual}주")
    return actual, odno


def _order_status_qty(odno: str, ticker: str) -> int | None:
    """주문체결원장 조회(fast-path, 2026-08-07 신규) — 잔고폴링보다 먼저 체결을
    알 수 있는 경우가 있음(2026-08-07 halt 사고: 45초 잔고폴링으론 못 밝혀냈지만
    같은 원장을 보는 get_filled_price는 바로 조회됐음). odno 없거나 조회 실패/
    파싱불가면 None — 호출부는 반드시 잔고폴링과 병행하고 이 값 하나만으로
    최종판단하지 말 것(이 함수는 확인을 "앞당길 수만" 있고, 신뢰의 최종 근거는
    여전히 실제 잔고)."""
    if not odno or odno == "?":
        return None
    try:
        from core.kis_api import get_order_fill_qty
        return get_order_fill_qty(odno, ticker)
    except Exception:
        return None


def verify_buy_fill(ticker: str, expected_qty: int, odno: str = "",
                     wait_secs: tuple = FILL_WAIT_SECS) -> int:
    """실제 잔고를 폴링해 매수 체결수량 확인. 부분체결이면 실제 수량만 반환하고
    자동 재시도는 안 함(iris v1 설계 그대로 — 소액 세대에서 과설계 판단).
    최종까지 0이면 0 반환(호출부가 halt 처리).
    2026-08-07: odno를 넘기면 매 폴링마다 주문체결원장도 함께 확인(fast-path) —
    잔고 반영보다 원장 반영이 더 빠른 경우 그만큼 일찍 확정할 수 있다. 원장쪽이
    모호하거나 실패해도 잔고폴링 결과는 그대로 유효(안전망 그대로 유지).
    2026-08-08: wait_secs 파라미터화(기본값은 기존과 동일) — 분할매수 트랑쉬2가
    짧은 예산으로 여러 틱에 걸쳐 재확인할 때 씀(retry_pending_entry_tranche 참조)."""
    actual = 0
    for wait in wait_secs:
        time.sleep(wait)
        order_qty = _order_status_qty(odno, ticker)
        if order_qty is not None:
            actual = max(actual, order_qty)
            if actual >= expected_qty:
                return expected_qty
        holdings = _get_actual_holdings()
        if holdings is None:
            continue
        actual = max(actual, holdings.get(ticker, 0))
        if actual >= expected_qty:
            return expected_qty
    if actual <= 0:
        logger.error(f"매수 체결 미확인: {ticker} 기대={expected_qty}주 실제=0주")
    else:
        logger.warning(f"매수 부분체결: {ticker} 기대={expected_qty}주 실제={actual}주")
    return actual


def poll_remaining_shares(ticker: str, expect_at_most: int, odno: str = "",
                           shares_before: int | None = None,
                           wait_secs: tuple = FILL_WAIT_SECS) -> int | None:
    """매도 후 실제 잔고를 폴링해 남은 수량 반환(부분매도의 실제 체결수량 역산에 사용).
    폴링이 끝나도 기대치까지 줄지 않으면 None(미확인 — 호출부가 halt 처리해야 함).
    2026-08-07: odno+shares_before(매도 전 보유수량)를 넘기면 매 폴링마다 주문체결
    원장으로 "매도 전 수량 - 체결수량 = 남은 수량"도 함께 확인(fast-path) —
    verify_buy_fill과 같은 이유(잔고 반영이 원장보다 늦을 수 있음). 원장쪽이
    모호하면 그냥 무시하고 잔고폴링 결과만 신뢰(안전망 그대로 유지).
    2026-08-08: wait_secs를 파라미터화(기본값은 기존과 동일 FILL_WAIT_SECS) —
    분할매도(execute_split_close)가 트랑쉬당 더 짧은 예산(SPLIT_SELL_FILL_WAIT_SECS)을
    쓰기 위함(한 트랑쉬가 45초를 다 쓰면 monitor 1틱 예산을 트랑쉬 1개만으로
    넘길 수 있어서)."""
    remaining = None
    for wait in wait_secs:
        time.sleep(wait)
        if shares_before is not None:
            order_qty = _order_status_qty(odno, ticker)
            if order_qty is not None:
                remaining_est = max(shares_before - order_qty, 0)
                if remaining_est <= expect_at_most:
                    return remaining_est
        holdings = _get_actual_holdings()
        if holdings is None:
            continue
        remaining = holdings.get(ticker, 0)
        if remaining <= expect_at_most:
            return remaining
    logger.error(f"매도 체결 미확인: {ticker} (마지막 확인 잔고={remaining})")
    return None


def resolve_exit_price(ticker: str, odno: str, entry_price: float) -> float:
    """실제 체결가 조회(우선) → 실패시 현재가 → 그마저 실패시 진입가(최후 폴백)."""
    if odno and odno != "?":
        try:
            from core.kis_api import get_filled_price
            price = get_filled_price(odno, ticker)
            if price and price > 0:
                return price
        except Exception:
            pass
    p = _get_price_safe(ticker)
    if p and p["price"] > 0:
        logger.warning(f"체결가 조회 실패 — 현재가({p['price']:,}원)로 대체: {ticker}")
        return p["price"]
    logger.warning(f"체결가·현재가 모두 조회 실패 — 진입가({entry_price:,}원)로 대체: {ticker}")
    return entry_price


def resolve_entry_price(ticker: str, ref_price: float, odno: str = "") -> float:
    """2026-08-07: 실제 체결가 조회(get_filled_price, 매도쪽과 동일 방식) 우선 시도
    → 실패시 매수 직후 재조회한 현재가로 근사(iris v1 원래 방식, odno 없을 때도
    동작) → 그마저 실패시 참조가(ref_price) 최후 폴백."""
    if odno and odno != "?":
        try:
            from core.kis_api import get_filled_price
            price = get_filled_price(odno, ticker)
            if price and price > 0:
                return price
        except Exception:
            pass
    p = _get_price_safe(ticker)
    return p["price"] if (p and p["price"] > 0) else ref_price


def halt_key(date: str) -> str:
    return f"iris_v2_halt_{date}"


def set_halt(date: str, ticker: str, action: str, detail: str):
    """체결 미확인 등 위험 상태 발생시 그날 자동매매를 전부 멈춘다(이중주문 방지 —
    확실하지 않으면 재시도하지 말고 멈추는 쪽이 항상 안전)."""
    from core.db import state_set
    from core.timeutil import iso_kst
    state_set(halt_key(date), {"ticker": ticker, "action": action, "detail": detail, "time": iso_kst()})


def is_halted(date: str) -> dict | None:
    from core.db import state_get
    return state_get(halt_key(date))


def find_unresolved_halt() -> dict | None:
    """halt 키는 날짜별로 분리돼있어(halt_key) is_halted(date)는 그 날짜만 본다 —
    **어제 이전에 설정된 halt가 --clear 없이 남아있어도 새 거래일에는 아무도
    이걸 못 본다**는 구조적 허점이 있었다(2026-08-08 코드감사에서 발견).
    entry.py는 halt 상태를 아예 확인하지 않았고, monitor.py/force_close.py도
    당일 키만 봐서 전날 halt를 못 잡았음 — "halt=사람이 확인하기 전까진 자동매매
    재개 안 함" 원칙이 실제로는 하루짜리 유효기간이었던 셈.

    이 함수는 bot_state 전체에서 'iris_v2_halt_'로 시작하는 키를 스캔해
    --clear로 지워지지 않은(=아직 미해결) halt가 하나라도 있으면 반환한다
    (여러 날짜에 걸쳐 있으면 가장 최근 것). entry.py가 새 거래일 진입 판단
    전에 반드시 호출해야 halt가 날짜를 넘어서도 실제로 유효하게 작동한다.
    iris_v2_halt.py --clear는 해당 키를 DELETE하므로, 정상 해제된 halt는
    이 스캔에 절대 안 걸린다."""
    from core.db import get_conn
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT key FROM bot_state WHERE key LIKE 'iris_v2_halt_%' ORDER BY key DESC"
        ).fetchall()
    if not rows:
        return None
    latest_key = rows[0]["key"]
    date = latest_key.replace("iris_v2_halt_", "")
    halt = is_halted(date)
    if halt is None:
        return None
    return {**halt, "date": date}


# ── 미확인 매도의 다음-틱 재확인(2026-08-07 신규, pending exit) ──────────────
#
# 2026-08-07 halt 사고: 45초 잔고폴링으로 못 밝혀낸 체결이 그 이후(halt 걸린 채로)
# 실제로는 정상 체결돼 있었음(get_filled_price로 뒤늦게 확인, 1,645원). 45초는
# "그 자리에서 한번에" 확인하려니 나온 예산 제약(monitor가 1분마다 새 프로세스로
# 도는 cron 구조라 한 틱이 60초를 넘기면 다음 틱과 겹침)이었을 뿐 — 실제 KRX
# 체결이 45초보다 오래 걸리는 경우 자체를 없앨 수는 없다. 그래서 "한 틱 안에서
# 더 오래 기다리기" 대신 "여러 틱에 걸쳐 짧게 여러 번 재확인"으로 바꾼다:
# 매도 제출 직후 1회 확인(기존 45초 폴링 그대로, proc_lock이 있어 이 자체는
# 안전) → 그래도 안 잡히면 halt 대신 pending 상태로 등록하고 다음 분 틱에서
# 다시 확인 → PENDING_RETRY_MAX(5)번(총 약 5분)까지도 못 밝혀내면 그때 halt.
# pending 동안은 신규 매도주문을 다시 내지 않음(이중매도 방지 원칙 그대로 유지 —
# 그냥 "halt까지 가는 시점"만 뒤로 미루는 것).
PENDING_RETRY_MAX = 5


def _pending_exit_key(date: str) -> str:
    return f"iris_v2_pending_exit_{date}"


def get_pending_exit(date: str) -> dict | None:
    from core.db import state_get
    return state_get(_pending_exit_key(date))


def _set_pending_exit(date: str, data: dict):
    from core.db import state_set
    state_set(_pending_exit_key(date), data)


def _clear_pending_exit(date: str):
    from core.db import state_set
    state_set(_pending_exit_key(date), None)


def execute_full_close(ticker: str, shares: int, entry_price: float, reason: str, date: str) -> tuple[float, str] | None | str:
    """전량매도 실행+확인. 성공시 (체결가, 주문번호) 반환, 확실히 실패시(주문
    자체가 거부) halt 세우고 None, **체결여부가 아직 불명이면 "pending" 반환**
    (halt 아님 — 다음 monitor 틱에서 `retry_pending_exit()`로 이어서 확인)."""
    ok, odno = sell_safe(ticker, shares, reason=reason)
    if not ok:
        set_halt(date, ticker, "매도주문실패", f"사유={reason} 의도수량={shares}")
        return None
    # 2026-08-18: poll_remaining_shares(최대45초) 전에 먼저 pending 등록 —
    # 그 사이 프로세스가 죽어도 다음 monitor 틱이 이 기록을 찾아 이어서
    # 확인하게 함(이중매도 방지, 08-10(17차) 발견2 수정)
    _set_pending_exit(date, {"kind": "full", "ticker": ticker, "odno": odno, "reason": reason,
                              "shares_before": shares, "entry_price": entry_price, "retry_count": 0})
    remaining = poll_remaining_shares(ticker, expect_at_most=0, odno=odno, shares_before=shares)
    if remaining is None or remaining > 0:
        logger.warning(f"매도 체결 미확인 — pending 등록(다음 틱 재확인 예정): {ticker} 사유={reason} "
                        f"주문번호={odno} 잔여추정={remaining}")
        return "pending"
    _clear_pending_exit(date)
    exit_price = resolve_exit_price(ticker, odno, entry_price)
    return exit_price, odno


def execute_partial_sell(ticker: str, intended_qty: int, total_shares: int, entry_price: float,
                          date: str) -> tuple[int, int, float, str] | None | str:
    """부분매도 실행+확인(intended_qty는 호출부가 이미 유효성 검증했다고 가정).
    성공시 (실제매도수량, 잔여수량, 체결가, 주문번호) 반환, 확실히 실패시 halt
    세우고 None, **체결여부가 아직 불명이면 "pending" 반환**(halt 아님)."""
    ok, odno = sell_safe(ticker, intended_qty, reason="1차부분익절")
    if not ok:
        set_halt(date, ticker, "부분익절주문실패", f"의도수량={intended_qty}")
        return None
    # 2026-08-18: poll_remaining_shares(최대45초) 전에 먼저 pending 등록 —
    # execute_full_close와 동일 원칙(08-10(17차) 발견2 수정)
    _set_pending_exit(date, {"kind": "partial", "ticker": ticker, "odno": odno, "reason": "1차부분익절",
                              "shares_before": total_shares, "total_shares": total_shares,
                              "intended_qty": intended_qty, "entry_price": entry_price, "retry_count": 0})
    expect_at_most = total_shares - intended_qty
    remaining = poll_remaining_shares(ticker, expect_at_most=expect_at_most,
                                       odno=odno, shares_before=total_shares)
    if remaining is None:
        logger.warning(f"부분익절 체결 미확인 — pending 등록(다음 틱 재확인 예정): {ticker} "
                        f"주문번호={odno} 의도수량={intended_qty}")
        return "pending"
    _clear_pending_exit(date)
    actual_qty = total_shares - remaining
    exit_price = resolve_exit_price(ticker, odno, entry_price)
    return actual_qty, remaining, exit_price, odno


def retry_pending_exit(date: str) -> dict | None:
    """pending 상태(위 execute_full_close/execute_partial_sell이 "pending" 반환한
    경우)를 다음 monitor 틱에서 이어서 확인. pending 없으면 None.
    반환 dict의 "status": "done"(확정, 호출부가 DB반영+알림) /
    "pending"(아직 불명, 이번 틱은 조용히 넘어감) /
    "timeout"(재시도한도 초과, 여기서 halt 세움 — 호출부는 알림만)."""
    pending = get_pending_exit(date)
    if pending is None:
        return None
    ticker, odno, reason, kind = pending["ticker"], pending["odno"], pending["reason"], pending["kind"]
    if kind == "full":
        expect_at_most = 0
    else:
        expect_at_most = pending["total_shares"] - pending["intended_qty"]
    remaining = poll_remaining_shares(ticker, expect_at_most=expect_at_most,
                                       odno=odno, shares_before=pending["shares_before"])
    still_unconfirmed = remaining is None or (kind == "full" and remaining > 0)
    if still_unconfirmed:
        retry_count = pending["retry_count"] + 1
        if retry_count >= PENDING_RETRY_MAX:
            _clear_pending_exit(date)
            set_halt(date, ticker, "매도체결미확인",
                      f"사유={reason} 주문번호={odno} {retry_count}회 재시도(약 {retry_count}분) 후에도 미확인")
            return {"status": "timeout", "kind": kind, "ticker": ticker, "reason": reason, "odno": odno}
        _set_pending_exit(date, {**pending, "retry_count": retry_count})
        logger.warning(f"pending 재확인 {retry_count}/{PENDING_RETRY_MAX}회차 — 여전히 미확인: "
                        f"{ticker} 사유={reason} 주문번호={odno}")
        return {"status": "pending", "kind": kind, "ticker": ticker, "reason": reason, "retry_count": retry_count}
    _clear_pending_exit(date)
    exit_price = resolve_exit_price(ticker, odno, pending["entry_price"])
    result = {"status": "done", "kind": kind, "ticker": ticker, "odno": odno,
              "exit_price": exit_price, "reason": reason}
    if kind == "partial":
        actual_qty = pending["total_shares"] - remaining
        result["actual_qty"] = actual_qty
        result["remaining"] = remaining
    return result


# ── 분할매수(트랑쉬2) — 다음-틱 재시도(2026-08-08 신규, SPLIT_BUY_ENABLED) ────
#
# 이론검증(2026-08-08) 결론: 트랑쉬1이 확정되는 즉시 그 수량만으로 포지션을
# 'open'으로 기록하고 SL/TP 감시를 바로 시작한다(호출부인 entry.py 책임) —
# 트랑쉬2는 이 모듈이 "백그라운드 증액 시도"로만 다룬다. 그래서 positions
# 테이블에 새 상태('pending_entry' 등)를 신설하지 않는다 — pending_exit과
# 완전히 같은 원리로 bot_state에만 상태를 둔다. 트랑쉬2가 끝내 안 되면(재시도
# 소진) **halt가 아니라** "gave_up"으로 조용히 끝낸다 — 매수는 언더필=리스크
# 축소라는 기존 원칙 그대로(pending_exit의 halt-on-timeout과 의도적으로 다름,
# 매도는 미확인 상태를 halt해야 하지만 매수 언더필은 위험하지 않다).

def _pending_tranche2_key(date: str) -> str:
    return f"iris_v2_pending_tranche2_{date}"


def get_pending_tranche2(date: str) -> dict | None:
    from core.db import state_get
    return state_get(_pending_tranche2_key(date))


def _set_pending_tranche2(date: str, data: dict):
    from core.db import state_set
    state_set(_pending_tranche2_key(date), data)


def _clear_pending_tranche2(date: str):
    from core.db import state_set
    state_set(_pending_tranche2_key(date), None)


def register_pending_tranche2(date: str, ticker: str, qty: int):
    """트랑쉬1 확정 직후 entry.py가 호출 — 트랑쉬2(시장가) 매수를 monitor.py의
    다음 틱들이 이어받아 처리하도록 등록만 해둠(여기선 주문을 내지 않음 —
    실제 제출은 retry_pending_entry_tranche가 monitor 틱에서 함)."""
    if qty <= 0:
        return
    _set_pending_tranche2(date, {"ticker": ticker, "qty": qty, "retry_count": 0})


def retry_pending_entry_tranche(date: str) -> dict | None:
    """monitor.py 매 틱 시작 시(halt 체크 다음, SL/TP 평가보다 먼저) 호출 —
    대기 중인 트랑쉬2를 시장가로 매수 시도. pending 없으면 None.
    반환 dict의 "status":
      "done"(체결확정 — qty주 확보, 호출부가 포지션 shares/entry_price 갱신)
      "pending"(아직불명, 이번 틱은 조용히 넘어감 — 정상 SL/TP 평가는 다음 틱부터)
      "gave_up"(재시도한도 초과 또는 주문자체 실패 — **halt 아님**, qty(0일 수도
                있음)만큼만 확보된 채 트랑쉬1(+부분체결분)만으로 감시 재개,
                호출부가 CRITICAL 알림)."""
    pending = get_pending_tranche2(date)
    if pending is None:
        return None
    ticker, qty = pending["ticker"], pending["qty"]
    odno = pending.get("odno")
    if odno is None:
        ok, odno = buy_safe(ticker, qty)
        if not ok:
            _clear_pending_tranche2(date)
            logger.error(f"트랑쉬2 매수주문 실패 — 트랑쉬1만으로 확정: {ticker} {qty}주")
            return {"status": "gave_up", "ticker": ticker, "qty": 0}
        pending = {**pending, "odno": odno}
        _set_pending_tranche2(date, pending)
    actual = verify_buy_fill(ticker, qty, odno=odno, wait_secs=SPLIT_TRANCHE2_FILL_WAIT_SECS)
    if actual >= qty:
        _clear_pending_tranche2(date)
        return {"status": "done", "ticker": ticker, "qty": qty, "odno": odno}
    retry_count = pending["retry_count"] + 1
    if retry_count >= PENDING_RETRY_MAX:
        _clear_pending_tranche2(date)
        logger.warning(f"트랑쉬2 매수 {retry_count}회 재시도 후에도 미확인 — "
                        f"트랑쉬1{'+부분체결 ' + str(actual) + '주' if actual else ''}만으로 감시 재개: {ticker}")
        return {"status": "gave_up", "ticker": ticker, "qty": actual, "odno": odno}
    _set_pending_tranche2(date, {**pending, "retry_count": retry_count})
    logger.info(f"트랑쉬2 매수 미확인 — 재시도 {retry_count}/{PENDING_RETRY_MAX}: {ticker}")
    return {"status": "pending", "ticker": ticker, "retry_count": retry_count}


# ── 분할매도(다중 트랑쉬) — 다음-틱 재시도(2026-08-08 신규, SPLIT_SELL_ENABLED) ──
#
# execute_full_close와 정확히 같은 반환계약: (블렌드체결가, 마지막주문번호) |
# None(주문거부, halt) | "pending"(다음 틱에서 retry_pending_split_sell로 이어감).
# 강제청산·부분익절(+8%)에는 안 씀(설계 결정, 2026-08-08) — 이 둘은 각자의 기존
# 로직 그대로. 트랑쉬별 확인은 SPLIT_SELL_FILL_WAIT_SECS(짧은 예산)를 써서 —
# 기존 FILL_WAIT_SECS(45초)를 트랑쉬마다 다 쓰면 monitor 1틱(60초예산)을
# 트랑쉬 1개만으로 넘겨 다음 틱이 LockHeld로 스킵될 위험이 있음(이론검증 발견).

def _pending_split_sell_key(date: str) -> str:
    return f"iris_v2_pending_split_sell_{date}"


def get_pending_split_sell(date: str) -> dict | None:
    from core.db import state_get
    return state_get(_pending_split_sell_key(date))


def _set_pending_split_sell(date: str, data: dict):
    from core.db import state_set
    state_set(_pending_split_sell_key(date), data)


def _clear_pending_split_sell(date: str):
    from core.db import state_set
    state_set(_pending_split_sell_key(date), None)


def _tranche_sizes(total_shares: int, n: int) -> list[int]:
    """total_shares를 n개로 최대한 균등분할(나머지는 마지막 트랑쉬에 몰아줌).
    total_shares < n이면 트랑쉬 수가 자연히 줄어듦(0주 트랑쉬 제거)."""
    if n <= 1 or total_shares <= 0:
        return [total_shares] if total_shares > 0 else []
    base = total_shares // n
    sizes = [base] * n
    sizes[-1] += total_shares - base * n
    return [s for s in sizes if s > 0]


def execute_split_close(ticker: str, shares: int, entry_price: float, reason: str,
                         date: str, n_tranches: int) -> tuple[float, str] | None | str:
    """전량청산을 n_tranches개로 나눠 순차 시장가 매도. execute_full_close와 같은
    반환계약(위 설명 참조). 정상적으로는 pending_split_sell이 없는 상태에서
    호출되지만(호출 순서 불변식: monitor.py가 매 틱 시작시 retry_pending_split_sell로
    먼저 비워둠), 방어적으로 기존 pending이 있으면 그것부터 이어감."""
    pending = get_pending_split_sell(date)
    if pending is not None and pending.get("ticker") == ticker:
        return _resume_split_sell(date, pending)
    sizes = _tranche_sizes(shares, n_tranches)
    if not sizes:
        return None
    state = {"ticker": ticker, "reason": reason, "entry_price": entry_price,
             "sizes": sizes, "idx": 0, "completed": [], "current_odno": None, "retry_count": 0}
    return _resume_split_sell(date, state)


def _resume_split_sell(date: str, state: dict) -> tuple[float, str] | None | str:
    ticker, reason, entry_price = state["ticker"], state["reason"], state["entry_price"]
    sizes, idx, completed = state["sizes"], state["idx"], list(state["completed"])
    while idx < len(sizes):
        qty = sizes[idx]
        odno = state.get("current_odno")
        if odno is None:
            ok, odno = sell_safe(ticker, qty, reason=f"{reason}(분할{idx+1}/{len(sizes)})")
            if not ok:
                _clear_pending_split_sell(date)
                set_halt(date, ticker, "분할매도주문실패",
                          f"사유={reason} 트랑쉬{idx+1}/{len(sizes)} 의도수량={qty}")
                return None
            state = {**state, "current_odno": odno}
        shares_before_this = sum(sizes[idx:])
        expect_at_most = sum(sizes[idx + 1:])
        remaining = poll_remaining_shares(ticker, expect_at_most=expect_at_most, odno=odno,
                                           shares_before=shares_before_this,
                                           wait_secs=SPLIT_SELL_FILL_WAIT_SECS)
        still_unconfirmed = remaining is None or remaining > expect_at_most
        if still_unconfirmed:
            retry_count = state["retry_count"] + 1
            if retry_count >= PENDING_RETRY_MAX:
                _clear_pending_split_sell(date)
                set_halt(date, ticker, "분할매도체결미확인",
                          f"사유={reason} 트랑쉬{idx+1}/{len(sizes)} 주문번호={odno} "
                          f"{retry_count}회 재시도 후에도 미확인")
                return None
            state = {**state, "retry_count": retry_count}
            _set_pending_split_sell(date, state)
            logger.warning(f"분할매도 트랑쉬{idx+1}/{len(sizes)} 미확인 — pending 등록"
                            f"(재시도 {retry_count}/{PENDING_RETRY_MAX}): {ticker}")
            return "pending"
        fill_price = resolve_exit_price(ticker, odno, entry_price)
        completed.append({"qty": qty, "price": fill_price, "odno": odno})
        idx += 1
        state = {**state, "idx": idx, "completed": completed, "current_odno": None, "retry_count": 0}
    _clear_pending_split_sell(date)
    total_qty = sum(c["qty"] for c in completed)
    blended_price = sum(c["qty"] * c["price"] for c in completed) / total_qty
    last_odno = completed[-1]["odno"]
    return blended_price, last_odno


def retry_pending_split_sell(date: str) -> dict | None:
    """monitor.py 매 틱 시작 시(halt·pending_exit 다음) 호출 — 대기 중인 분할매도를
    이어감. pending 없으면 None. 반환 dict의 "status":
      "done"(전체 트랑쉬 확정 — 호출부가 포지션 종료+블렌드가 반영)
      "pending"(아직 진행중, 이번 틱 조용히 넘어감)
      "halted"(주문거부 또는 재시도소진 — 이미 halt 세워짐, 호출부는 알림만)."""
    pending = get_pending_split_sell(date)
    if pending is None:
        return None
    ticker, reason = pending["ticker"], pending["reason"]
    result = _resume_split_sell(date, pending)
    if result is None:
        return {"status": "halted", "ticker": ticker, "reason": reason}
    if result == "pending":
        return {"status": "pending", "ticker": ticker, "reason": reason}
    exit_price, odno = result
    return {"status": "done", "ticker": ticker, "reason": reason, "exit_price": exit_price, "odno": odno}


def _safe_available_cash() -> float | None:
    """get_available_cash()(매수가능금액, T+2 정산반영) 재시도 후, 그래도 실패하면
    get_balance()["cash"](예수금 총액, T+2 미반영이라 더 큰 값)에 3% 안전마진을 곱해
    폴백(2026-06-10 K3.1 사고 재발방지 — 그때는 안전마진 없이 그대로 써서 매수가
    거부됐음). 둘 다 실패하면 None(호출부가 매수 자체를 포기해야 함)."""
    from core.kis_api import get_available_cash
    for i in range(CASH_RETRY):
        try:
            return get_available_cash()
        except Exception as e:
            logger.warning(f"가용현금 조회 시도 {i+1}/{CASH_RETRY} 실패: {e}")
            if i < CASH_RETRY - 1:
                time.sleep(2 ** i)
    logger.error("가용현금(get_available_cash) 최종 실패 — 예수금 총액으로 안전마진(3%) 적용 폴백")
    try:
        from core.kis_api import get_balance
        raw_cash = get_balance()["cash"]
        return raw_cash * 0.97
    except Exception as e:
        logger.error(f"예수금 총액 폴백도 실패: {e}")
        return None


def compute_buy_qty(ticker: str, ref_price: float, target_notional: float,
                     base_price: float | None = None, size_factor: float = 1.0) -> int:
    """실제 가용현금·KIS 상한가기준 주문가능수량을 감안한 매수 수량 계산.

    2026-06-11 K3.1 사고(3거래일 연속 매수 전량거부, APBK0952) 재발방지: 시장가
    매수(ORD_DVSN=01)는 KIS가 **현재가가 아닌 상한가(기준가×1.30)** 기준으로 현금을
    예약/검증한다 — `budget/현재가`로 사이징하면 항상 과도하게 큰 수량이 나와 거부된다.
    당시 수정은 `get_order_psbl_qty()`(KIS가 직접 상한가 기준으로 계산)를 쓰고, 그
    조회가 실패하면 기존(현재가 기준) 방식으로 폴백했는데 — **그 폴백 자체가 바로
    이 버그의 원인 계산식이라 조회 실패시 문제가 그대로 재현될 수 있었음**(이번에
    재점검하며 발견). 이제 get_order_psbl_qty 실패시에도 base_price(전일종가 등
    기준가)가 있으면 상한가(base_price×1.30)를 직접 추정해 캡을 씌운다.

    target_notional(예산)과 실제 가용현금 중 작은 쪽을 쓰고, KIS의
    get_order_psbl_qty로 최종 상한(우선), 실패시 상한가 직접추정으로 대체 캡.

    size_factor(v2.8 신규, mom5 사이징캡용): min(target_notional, cash) 이후에
    곱하는 배율. target_notional 자체를 줄이는 방식은 잔고전액모드(target_notional=inf)에서
    inf*factor가 여전히 inf라 cash 캡에 가려져 무효화되는 버그가 있어(구현 중 발견,
    iris DECISIONS 2026-08-07 (3차) 참조) budget 산출 이후에 적용하도록 함."""
    from core.kis_api import get_order_psbl_qty
    cash = _safe_available_cash()
    if cash is None:
        return 0
    budget = min(target_notional, cash) * size_factor
    qty = int(budget // ref_price) if ref_price > 0 else 0
    try:
        max_qty = get_order_psbl_qty(ticker, ref_price)
        if max_qty > 0:
            qty = min(qty, max_qty)
    except Exception as e:
        logger.warning(f"주문가능수량 조회 실패({ticker}) — 상한가 직접추정으로 보정: {e}")
        if base_price and base_price > 0:
            upper_limit_est = base_price * 1.30
            qty = min(qty, int(budget // upper_limit_est))
        else:
            logger.warning(f"기준가 정보도 없어 예산기준(상한가 미보정) 수량 사용 — 매수거부 위험: {ticker}")
    return max(qty, 0)
