"""
iris v1 실거래 봇 — 목요일종가신호/금요일시가체결, 3주(3세대 트랜칭) 리밸런싱.
──────────────────────────────────────────────────────────────
1회성 스크립트(상시루프 아님) — cron이 매 거래일 09:05 KST에 1회 실행. 신호 자체가
전날 장마감 후 이미 확정돼있어(당일 실시간 판단 불필요) 상시감시 루프가 필요없다.
Oracle의 iris_v1_prefilter.py가 전날 장마감 후 이미 계산해둔 candidates
(strategy='iris_v1_gen{N}': 정규 리밸일에만 기록 / 'iris_v1_signal': 매일 공용시그널)를
오늘 날짜로 조회해 그대로 체결한다 — 신호 재계산 없음, 캘린더 판단도 Oracle이
단일 진실원(이 봇은 GEN_OFFSET/REBAL_INTERVAL 같은 상수를 직접 갖지 않는다).

세대별(gen0/gen1/gen2) 매일 체크:
  1. 게이트 OFF(KODEX200 100일선 아래, 로컬 price_daily 어제까지 데이터로 계산,
     lookahead 불가능) → 보유청산 후 그 세대 sleeve_cash 전액으로 나스닥100
     ETF(133690, 2026-07-11 국고채→나스닥100 전환 확정, iris DECISIONS 참조)
     매수, gateoff_parked=true
  2. 파킹중 + 게이트 ON 복귀 → 강제 캐치업(오늘의 iris_v1_signal로 재진입), 플래그 해제
  3. 정규 리밸일(오늘자 iris_v1_gen{N} candidates 존재) + 파킹 아님 → 보유청산 후 재진입
  4. 위 아무것도 아니면 → 무동작

⚠️ 페이퍼모드 없음(사용자 명시적 결정, 2026-07-10 iris DECISIONS 참조) — 항상 실거래.
⚠️ positions.mode는 'real' 고정, positions.strategy로 세대 구분
   ('iris_v1_gen0'/'iris_v1_gen1'/'iris_v1_gen2').
⚠️ sleeve_cash(strategy='iris_v1_gen{N}', account='KIS')가 세대별 독립 배정자본의
   단일 진실원 — 3세대가 계좌 하나를 공유하므로 "전체가용현금×비중"으로 매번
   재계산하면 안 됨(다른 세대가 주식으로 들고 있는 돈까지 잘못 포함됨).
   최초 시딩은 scripts/iris_v1_init_sleeves.py가 1회 수행.

텔레그램 4종 체계(2026-07-11 확정, iris DECISIONS 참조):
  1. 매일 데이터수집 결과 — Oracle bot.report, 이 봇과 무관, 기존 그대로
  2. 리밸런싱 예고(전날 저녁) — Oracle iris_v1_prefilter.py가 신호계산 직후 발송
  3. 실제 매매결과(당일) — 이 봇이 리밸런싱/게이트전환 발생시에만 발송
  4. 주간요약(그 주 마지막 거래일) — scripts/iris_v1_weekly_report.py 별도 실행
     ("금요일"이 아니라 그 주 실제 마지막 거래일 동적 판정, 휴장일 대응)
  +. 오류/CRITICAL — 매수·매도 실패, 부분체결, 현금정합성 불일치 등 즉시·무제한

실행: python3 -m bot.iris_v1_bot [--force]
드라이런(실제 매매·DB쓰기·KIS 주문 전부 없이 오늘 판단만 미리보기):
  python3 -m bot.iris_v1_bot --dry-run
"""
import sys
import time
from datetime import datetime, timezone, timedelta

from core.db import get_conn, state_get, state_set, init_db
from core.logger import get_logger
from strategy.version import STRATEGY_VERSION

logger = get_logger("iris_v1", "BOT")
KST = timezone(timedelta(hours=9))

STRATEGY_PREFIX = "iris_v1"
N_GEN = 3
GATEOFF_TICKER = "133690"
GATEOFF_NAME = "TIGER 미국나스닥100"
ORDER_RETRY = 3


def _now() -> datetime:
    return datetime.now(KST)


def _today() -> str:
    return _now().strftime("%Y%m%d")


def _gen_strategy(gen: int) -> str:
    return f"{STRATEGY_PREFIX}_gen{gen}"


def _tg(msg: str):
    from core.notify import send_telegram
    send_telegram(msg, prefix="[IRIS-v1\U0001F534] ")


# ── KIS API 헬퍼 (재시도 포함, iris_bot.py 패턴 재사용) ─────────

def _get_price_safe(ticker: str) -> dict | None:
    for i in range(ORDER_RETRY):
        try:
            from core.kis_api import get_price
            return get_price(ticker)
        except Exception as e:
            if i < ORDER_RETRY - 1:
                time.sleep(2 ** i)
            else:
                logger.warning(f"시세조회 최종 실패 {ticker}: {e}")
    return None


def _buy_safe(ticker: str, qty: int) -> tuple[bool, str]:
    if qty <= 0:
        return False, ""
    for i in range(ORDER_RETRY):
        try:
            from core.kis_api import buy_market
            result = buy_market(ticker, qty)
            odno = result.get("output", {}).get("ODNO", "?")
            logger.info(f"매수 주문 완료: {ticker} {qty}주 주문번호={odno}")
            return True, odno
        except Exception as e:
            logger.warning(f"매수 시도 {i+1}/{ORDER_RETRY} 실패: {ticker} — {e}")
            if i < ORDER_RETRY - 1:
                time.sleep(2 ** i)
    logger.error(f"매수 최종 실패: {ticker}")
    _tg(f"⚠️ 매수 실패: {ticker} {qty}주 수동 확인 필요!")
    return False, ""


def _sell_safe(ticker: str, qty: int, reason: str = "") -> tuple[bool, str]:
    if qty <= 0:
        return True, ""
    for i in range(ORDER_RETRY):
        try:
            from core.kis_api import sell_market
            result = sell_market(ticker, qty)
            odno = result.get("output", {}).get("ODNO", "?")
            logger.info(f"매도 주문 완료: {ticker} {qty}주 주문번호={odno} ({reason})")
            return True, odno
        except Exception as e:
            logger.warning(f"매도 시도 {i+1}/{ORDER_RETRY} 실패: {ticker} — {e}")
            if i < ORDER_RETRY - 1:
                time.sleep(2 ** i)
    logger.error(f"매도 최종 실패: {ticker} ({reason})")
    _tg(f"⚠️ 매도 실패: {ticker} 수동 확인 필요! ({reason})")
    return False, ""


def _get_actual_holdings() -> dict | None:
    try:
        from core.kis_api import get_balance
        bal = get_balance()
        return {h["ticker"]: h["qty"] for h in bal["holdings"]}
    except Exception as e:
        logger.error(f"보유 주식 조회 실패: {e}")
        return None


def _wait_fill_gone(ticker: str) -> bool:
    """매도 체결 확인 — 잔고에서 해당 종목이 사라졌는지 최대 10초 대기."""
    for wait in (2, 3, 5):
        time.sleep(wait)
        holdings = _get_actual_holdings()
        if holdings is None:
            continue
        if ticker not in holdings:
            return True
    logger.error(f"체결 미확인: {ticker} — 잔고에 여전히 존재")
    _tg(f"\U0001F6A8 매도 미체결 의심: {ticker} 수동 확인 필요!")
    return False


def _verify_buy_fill(ticker: str, expected_qty: int) -> int:
    """매수 체결 확인 — 잔고의 실제 보유수량을 최대 10초 폴링해 반환.
    목표수량보다 적으면(부분체결/미체결) 실제 수량만 반환하고 알림(자동 재시도는
    안 함 — 소액 세대에서 과설계 판단, 2026-07-11 iris DECISIONS 참조)."""
    actual = 0
    for wait in (2, 3, 5):
        time.sleep(wait)
        holdings = _get_actual_holdings()
        if holdings is None:
            continue
        actual = holdings.get(ticker, 0)
        if actual >= expected_qty:
            return expected_qty
    if actual <= 0:
        logger.error(f"매수 체결 미확인: {ticker} 기대={expected_qty}주 실제=0주")
        _tg(f"\U0001F6A8 매수 미체결 의심: {ticker} 기대{expected_qty}주 수동 확인 필요!")
    else:
        logger.warning(f"매수 부분체결: {ticker} 기대={expected_qty}주 실제={actual}주")
        _tg(f"⚠️ 매수 부분체결: {ticker} 기대{expected_qty}주 → 실제{actual}주 체결")
    return actual


# ── sleeve_cash (세대별 독립 배정자본) ─────────────────────────

def _sleeve_cash(gen: int) -> float:
    strategy = _gen_strategy(gen)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT cash FROM sleeve_cash WHERE strategy=? AND account='KIS'", (strategy,)
        ).fetchone()
    if row is None:
        raise RuntimeError(f"{strategy} sleeve_cash 없음 — scripts/iris_v1_init_sleeves.py 먼저 실행 필요")
    return float(row["cash"])


def _adjust_sleeve_cash(gen: int, delta: float):
    from core.timeutil import iso_kst
    strategy = _gen_strategy(gen)
    current = _sleeve_cash(gen)
    with get_conn() as conn:
        conn.execute(
            "UPDATE sleeve_cash SET cash=?, updated_at=? WHERE strategy=? AND account='KIS'",
            (current + delta, iso_kst(), strategy),
        )


def _cash_reconcile_ok() -> bool:
    """실행 시작 시 1회: 실제 계좌 매수가능금액이 DB sleeve_cash 합계보다 눈에
    띄게 부족하면(배당입금 오차, 수동개입, 이전 매도정산 오류 등) 오늘 매매 전체를
    중단한다. sleeve_cash 합계보다 실제잔고가 '많은' 쪽(계좌에 iris_v1 몫이 아닌
    여유자금이 더 있는 경우)은 정상 시나리오라 알림 대상 아님 — 부족한 쪽만 위험.
    2026-07-11 iris DECISIONS 참조."""
    try:
        from core.kis_api import get_available_cash
        actual = get_available_cash()
    except Exception as e:
        logger.error(f"실제 잔고 조회 실패 — 정합성 확인 불가: {e}")
        _tg(f"\U0001F6A8 실제 계좌 잔고 조회 실패 — 정합성 확인 불가, 오늘 매매 전체 중단! ({e})")
        return False

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT cash FROM sleeve_cash WHERE strategy LIKE ? AND account='KIS'",
            (f"{STRATEGY_PREFIX}_gen%",),
        ).fetchall()
    db_total = sum(r["cash"] for r in rows)
    shortfall = db_total - actual
    threshold = max(50_000, db_total * 0.01)

    if shortfall > threshold:
        logger.error(f"현금 정합성 불일치: 실제가용={actual:,.0f}원 DB합계={db_total:,.0f}원 "
                      f"부족분={shortfall:,.0f}원(임계값 {threshold:,.0f}원)")
        _tg(f"\U0001F6A8 현금 정합성 불일치: 실제가용 {actual:,.0f}원 vs DB sleeve_cash합계 "
            f"{db_total:,.0f}원 (부족분 {shortfall:,.0f}원) — 오늘 매매 전체 중단, "
            f"원인 확인 후 수동 재개 필요!")
        return False

    logger.info(f"현금 정합성 확인 OK: 실제가용={actual:,.0f}원 DB합계={db_total:,.0f}원")
    return True


# ── positions (세대별, strategy 컬럼으로 구분) ────────────────

def _open_positions(gen: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, ticker, name, entry_price, shares
            FROM positions WHERE status='open' AND strategy=? AND mode='real'
        """, (_gen_strategy(gen),)).fetchall()
    return [dict(r) for r in rows]


def _open_position(gen: int, ticker: str, name: str, entry: float, shares: int):
    now = _now()
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO positions
              (ticker, name, date_open, time_open, entry_price, shares,
               score, target_pct, stop_pct, mode, status, strategy_version, strategy)
            VALUES (?,?,?,?,?,?,0,0,0,'real','open',?,?)
        """, (ticker, name, now.strftime("%Y%m%d"), now.strftime("%H%M%S"),
              entry, shares, STRATEGY_VERSION, _gen_strategy(gen)))
    _adjust_sleeve_cash(gen, -entry * shares)


def _close_position(gen: int, pos_id: int, exit_price: float, reason: str):
    now = _now()
    with get_conn() as conn:
        row = conn.execute("SELECT entry_price, shares FROM positions WHERE id=?", (pos_id,)).fetchone()
        if not row:
            return
        entry, shares = row["entry_price"], row["shares"]
        pnl_pct = (exit_price - entry) / entry * 100
        pnl_amount = (exit_price - entry) * shares
        conn.execute("""
            UPDATE positions SET status='closed', exit_price=?, date_close=?, time_close=?,
                   pnl_pct=?, pnl_amount=?, exit_reason=? WHERE id=?
        """, (exit_price, now.strftime("%Y%m%d"), now.strftime("%H%M%S"),
              round(pnl_pct, 2), round(pnl_amount, 0), reason, pos_id))
    _adjust_sleeve_cash(gen, exit_price * shares)


def _liquidate_generation(gen: int, reason: str):
    for pos in _open_positions(gen):
        ticker = pos["ticker"]
        ok, odno = _sell_safe(ticker, pos["shares"], reason=reason)
        if not ok:
            continue
        filled = _wait_fill_gone(ticker)
        exit_price = None
        if filled and odno and odno != "?":
            try:
                from core.kis_api import get_filled_price
                exit_price = get_filled_price(odno, ticker)
            except Exception:
                pass
        if not exit_price:
            p = _get_price_safe(ticker)
            exit_price = p["price"] if (p and p["price"] > 0) else pos["entry_price"]
            logger.warning(f"체결가 조회 실패 — 현재가({exit_price:,}원)로 대체: {ticker}")
        _close_position(gen, pos["id"], exit_price, reason)


def _buy_targets(gen: int, targets: list[dict]):
    """targets: [{'ticker','name','weight'}] — sleeve_cash(gen) × weight로 매수.
    1주도 못 사는 고가주는 스킵(재분배 안 함 — 단순화, iris DECISIONS 2026-07-10 참조)."""
    cash = _sleeve_cash(gen)
    for t in targets:
        ticker, name, weight = t["ticker"], t.get("name") or t["ticker"], t["weight"]
        budget = cash * weight
        p = _get_price_safe(ticker)
        if not p or p["price"] <= 0:
            logger.warning(f"[{_gen_strategy(gen)}] 시세조회 실패 스킵: {ticker}")
            continue
        cur = p["price"]
        shares = int(budget / cur)

        # 시장가 매수는 KIS가 상한가 기준으로 현금을 검증하므로 실제 주문가능수량으로 상한 보정
        try:
            from core.kis_api import get_order_psbl_qty
            max_qty = get_order_psbl_qty(ticker, cur)
            if max_qty > 0:
                shares = min(shares, max_qty)
        except Exception as e:
            logger.warning(f"주문가능수량 조회 실패({ticker}) — 예산기준 수량 사용: {e}")

        if shares <= 0:
            logger.info(f"[{_gen_strategy(gen)}] 스킵(1주도 못삼): {name}({ticker}) "
                        f"예산{budget:,.0f}원 vs 주가{cur:,}원")
            continue

        ok, _ = _buy_safe(ticker, shares)
        if not ok:
            continue
        actual_qty = _verify_buy_fill(ticker, shares)
        if actual_qty <= 0:
            continue
        p2 = _get_price_safe(ticker)
        entry_price = p2["price"] if p2 and p2["price"] > 0 else cur
        _open_position(gen, ticker, name, entry_price, actual_qty)
        partial_note = f" (부분체결, 목표{shares}주)" if actual_qty < shares else ""
        logger.info(f"[{_gen_strategy(gen)}] 매수: {name}({ticker}) {entry_price:,}원×{actual_qty}주{partial_note}")


# ── candidates 로드 (Oracle이 전날 밤 계산, sync_to_iris로 이미 동기화됨) ──

def _load_gen_candidates(gen: int, today: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT c.ticker, COALESCE(NULLIF(c.name,''), u.name, c.ticker) AS name, c.score AS weight
            FROM candidates c LEFT JOIN universe u ON c.ticker = u.ticker
            WHERE c.trade_date=? AND c.strategy=?
            ORDER BY c.rank_no ASC
        """, (today, _gen_strategy(gen))).fetchall()
    return [dict(r) for r in rows]


def _load_signal_candidates(today: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT c.ticker, COALESCE(NULLIF(c.name,''), u.name, c.ticker) AS name, c.score AS weight
            FROM candidates c LEFT JOIN universe u ON c.ticker = u.ticker
            WHERE c.trade_date=? AND c.strategy=?
            ORDER BY c.rank_no ASC
        """, (today, f"{STRATEGY_PREFIX}_signal")).fetchall()
    return [dict(r) for r in rows]


# ── 시장위험게이트 ───────────────────────────────────────────

def _gate_on() -> bool:
    """KODEX200(069500) 종가 vs 100일선 — 어제까지 데이터만 사용(오늘 종가는 아직
    미확정이라 lookahead 구조적으로 불가능). 로컬 price_daily(Oracle에서 매일
    동기화됨)로 계산 — 장 시작 전 실시간 API 호출 불필요."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT date, close FROM price_daily WHERE ticker='069500' AND date < ?
            ORDER BY date DESC LIMIT 100
        """, (_today(),)).fetchall()
    if len(rows) < 100:
        logger.warning(f"069500 데이터 부족({len(rows)}/100) — 안전기본값 게이트ON 사용")
        return True
    closes = [r["close"] for r in reversed(rows)]
    sma100 = sum(closes) / len(closes)
    latest = closes[-1]
    return latest > sma100


# ── gateoff_parked(게이트OFF 자산 파킹) / 중복실행방지 상태 ─────

def _is_parked(gen: int) -> bool:
    return bool(state_get(f"{_gen_strategy(gen)}_gateoff_parked", False))


def _set_parked(gen: int, val: bool):
    state_set(f"{_gen_strategy(gen)}_gateoff_parked", val)


def _already_ran_today() -> bool:
    return state_get(f"{STRATEGY_PREFIX}_last_run_date") == _today()


def _mark_ran_today():
    state_set(f"{STRATEGY_PREFIX}_last_run_date", _today())


# ── 진입 전 대사 ────────────────────────────────────────────

def _reconcile():
    holdings = None
    for gen in range(N_GEN):
        open_pos = _open_positions(gen)
        if not open_pos:
            continue
        if holdings is None:
            holdings = _get_actual_holdings()
            if holdings is None:
                logger.error("보유 주식 조회 실패 — 대사 건너뜀")
                return
        for pos in open_pos:
            if pos["ticker"] not in holdings:
                logger.warning(f"대사 불일치[{_gen_strategy(gen)}]: "
                                f"{pos['name']}({pos['ticker']}) DB=open 계좌=없음 → 정리")
                _tg(f"⚠️ 잔여 포지션 정리[{_gen_strategy(gen)}]: {pos['name']}({pos['ticker']})")
                _close_position(gen, pos["id"], pos["entry_price"], "대사정리(계좌없음)")


# ── 세대별 처리 ───────────────────────────────────────────────

def _process_generation(gen: int, today: str, gate_on: bool):
    strategy = _gen_strategy(gen)
    parked = _is_parked(gen)

    if not gate_on:
        if parked:
            logger.debug(f"[{strategy}] 게이트 OFF 유지 — 이미 {GATEOFF_NAME} 보유중, 무동작")
            return
        logger.info(f"[{strategy}] 게이트 OFF — {GATEOFF_NAME} 전환")
        _liquidate_generation(gen, f"게이트OFF({GATEOFF_NAME}전환)")
        _buy_targets(gen, [{"ticker": GATEOFF_TICKER, "name": GATEOFF_NAME, "weight": 1.0}])
        _set_parked(gen, True)
        _tg(f"[{strategy}] \U0001F535 게이트 OFF — {GATEOFF_NAME}로 전환")
        return

    if parked:
        logger.info(f"[{strategy}] 게이트 ON 복귀 — 캐치업 리밸런싱")
        targets = _load_signal_candidates(today)
        if not targets:
            logger.warning(f"[{strategy}] 캐치업 보류 — 오늘자 iris_v1_signal 후보 없음")
            return
        _liquidate_generation(gen, "게이트ON복귀(캐치업)")
        _buy_targets(gen, targets)
        _set_parked(gen, False)
        names = ", ".join(t["name"] for t in targets)
        _tg(f"[{strategy}] \U0001F7E2 게이트 ON 복귀 — 캐치업 리밸런싱: {names}")
        return

    gen_targets = _load_gen_candidates(gen, today)
    if not gen_targets:
        logger.debug(f"[{strategy}] 오늘 리밸일 아님 — 무동작")
        return

    logger.info(f"[{strategy}] 정규 리밸런싱")
    _liquidate_generation(gen, "정규리밸런싱")
    _buy_targets(gen, gen_targets)
    names = ", ".join(t["name"] for t in gen_targets)
    _tg(f"[{strategy}] \U0001F504 정규 리밸런싱: {names}")


# ── 드라이런 (실제 매매·DB쓰기·KIS 주문 API 전부 미실행, 읽기전용 미리보기) ──

def _dry_run_report():
    """--dry-run 전용: 오늘 각 세대가 어떤 판단을 내릴지 실제 DB(gate/candidates/
    sleeve_cash)를 그대로 읽어서 로그로만 보여준다. _buy_safe/_sell_safe/포지션
    갱신은 전혀 호출하지 않으므로 KIS 주문도, DB 변경도 발생하지 않는다."""
    today = _today()
    logger.info(f"=== [DRY-RUN] iris v1 미리보기 — 오늘({today}) 실행하면 무슨 일이 벌어질지 ===")

    gate = _gate_on()
    logger.info(f"[DRY-RUN] 게이트 상태: {'ON(정상)' if gate else 'OFF(방어)'}")

    try:
        if not _cash_reconcile_ok():
            logger.warning("[DRY-RUN] 현금 정합성 체크 실패 — 실제 실행이었다면 오늘 매매 전체가 중단됐을 것")
    except Exception as e:
        logger.warning(f"[DRY-RUN] 현금 정합성 체크 중 오류(무시하고 계속): {e}")

    for gen in range(N_GEN):
        strategy = _gen_strategy(gen)
        parked = _is_parked(gen)
        try:
            cash = _sleeve_cash(gen)
        except RuntimeError as e:
            logger.info(f"[DRY-RUN][{strategy}] {e}")
            continue

        if not gate:
            action = f"무동작(이미 {GATEOFF_NAME} 파킹중)" if parked else f"{GATEOFF_NAME}({GATEOFF_TICKER}) 전환 예정 — 예산 {cash:,.0f}원"
        elif parked:
            targets = _load_signal_candidates(today)
            if targets:
                preview = ", ".join(f"{t['name']}({t['ticker']})" for t in targets)
                action = f"게이트ON복귀 캐치업 리밸런싱 예정: {preview}"
            else:
                action = "캐치업 보류(오늘자 iris_v1_signal 후보 없음 — 동기화 지연 가능성)"
        else:
            targets = _load_gen_candidates(gen, today)
            if targets:
                preview = ", ".join(
                    f"{t['name']}({t['ticker']}) 예산≈{cash*t['weight']:,.0f}원" for t in targets
                )
                action = f"정규 리밸런싱 예정: {preview}"
            else:
                action = "무동작(오늘 리밸일 아님)"

        logger.info(f"[DRY-RUN][{strategy}] sleeve_cash={cash:,.0f}원 parked={parked} → {action}")

    logger.info("=== [DRY-RUN] 종료 — 실제 주문·DB변경 없음 ===")


# ── 메인 ─────────────────────────────────────────────────────

def run():
    from core.config import validate as _validate_config
    _validate_config()

    init_db()
    logger.info("=" * 62)
    logger.info(f"iris v1 봇 시작 v{STRATEGY_VERSION}")
    logger.info("=" * 62)

    today = _today()
    try:
        from core.kis_api import is_trading_day
        trading = is_trading_day(today)
    except Exception as e:
        trading = _now().weekday() < 5
        logger.warning(f"거래일 API 실패 ({e}) — 주말 폴백: {'거래일' if trading else '주말'}")
    if not trading:
        logger.info(f"오늘({today}) 휴장일 — iris v1 미실행")
        return

    if state_get(f"{STRATEGY_PREFIX}_manual_stop", False):
        logger.info("수동 정지 플래그 설정됨 — 미실행")
        return

    if _already_ran_today():
        if "--force" not in sys.argv:
            logger.warning(f"오늘({today}) 이미 실행됨 — 중복실행 방지 (강제실행: --force)")
            return
        logger.warning("--force 지정됨 — 중복실행 강행")

    _reconcile()

    if not _cash_reconcile_ok():
        # 오늘 매매 전혀 없었으므로 _mark_ran_today() 호출 안 함 — 원인 해결 후
        # 재실행(수동 또는 다음 cron)이 --force 없이도 정상 동작하도록.
        return

    gate = _gate_on()
    logger.info(f"게이트 상태: {'ON(정상)' if gate else 'OFF(방어)'}")

    for gen in range(N_GEN):
        try:
            _process_generation(gen, today, gate)
        except Exception as e:
            logger.error(f"[{_gen_strategy(gen)}] 처리 오류: {e}", exc_info=True)
            _tg(f"\U0001F6A8 [{_gen_strategy(gen)}] 처리 오류: {e}")

    _mark_ran_today()
    logger.info("iris v1 봇 완료")


if __name__ == "__main__":
    if "--dry-run" in sys.argv:
        init_db()
        _dry_run_report()
    else:
        run()
