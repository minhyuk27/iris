"""
iris v2 — 09:30 정밀타이밍 진입 (Stage A/B 겸용).

전날 저녁 scripts.iris_v2_candidates가 미리 순위매겨둔 후보를 순서대로(1위부터) 실시간
갭만 확인 — 순위→체크→대체(fallback) 방식. 이 방식이 백테스트의 "필터먼저→순위" 방식과
동일한 종목을 고른다는 것을 2026-07-21 감사(230/230 일치)로 확인함. API 호출을 최소화해
체결지연을 줄이는 게 핵심(iris DECISIONS 2026-07-21 감사표 참조 — 1분 지연만으로 엣지
약 12% 손실).

TRADE_MODE(.env)로 Stage A/B 분기(2026-07-22 추가):
  paper(기본) — 기존 그대로. 시세조회만, 실제 주문 없음, positions.mode='paper'.
  real        — core.order_exec으로 실제 KIS 매수 주문 제출 + 잔고폴링으로 체결확인
                (아카이브된 iris_v1_bot.py 패턴 재사용), positions.mode='real'.
                체결 미확인시 그날 자동매매 전체를 halt(core.order_exec.set_halt).

cron: 29 9 * * 1-5 (내부에서 09:30:00 KST까지 정밀 sleep 후 즉시 실행)
실행: python3 -m bot.iris_v2_entry [--dry-run]
"""
import sys
import time

from core.config import IRIS_V2_TRADE_MODE as TRADE_MODE
from core.db import get_conn, state_get, state_set
from core.logger import get_logger
from core.notify import TAG_ENTRY, TAG_WARN, TAG_ALERT
from core.timeutil import now_kst, today_kst, iso_kst
from strategy.iris_v2_params import (
    IRIS_V2_VERSION, ENTRY_TIME, GAP_MIN, GAP_MAX, CAPITAL_PER_TRADE,
    TP_PCT, SL_PCT, MOM5_CAP_ENABLED, MOM5_CAP_THRESHOLD, MOM5_CAP_SIZE_FACTOR,
    SPLIT_BUY_ENABLED, SPLIT_BUY_TRANCHE1_RATIO,
)

logger = get_logger("iris_v2_entry", "IRIS_V2")
ORDER_RETRY = 3
# v2.5.3: 2026-07-24 real모드 안전테스트 중 09:30:00 정밀대기 종료 후 실제 갭조회가
# 09:31:23에야 찍히는(83초 지연) 원인불명 사례 발생 — cron 자체 지연인지 대기루프
# 내부 문제인지 서버 로그(syslog에 CRON 기록 자체가 비활성, journalctl 권한 제한)로
# 확정 불가했음. 재발시 원인 진단이 가능하도록 대기 종료 직후 실제시각을 항상 로그로
# 남기고, 지연 정도에 따라 경보/스킵하는 안전장치 신규. 임계값은 2026-07-24 체결지연
# 분해검증(매수지연 실측은 5분까지만 수행, EV+1.39%/t=3.77로 유의성 유지)에 근거 —
# 검증된 범위를 벗어나는 지연은 신뢰 못할 전제 위에서 매매하는 셈이라 스킵이 더 안전.
DELAY_WARN_SECS = 30    # 이 이상이면 경보(텔레그램), 매매는 계속 진행
DELAY_ABORT_SECS = 300  # 5분(체결지연 검증범위 상한) 초과시 그날 진입 스킵


def _entered_key(date: str) -> str:
    return f"iris_v2_entered_{date}"


def _sleep_until_entry() -> float:
    """09:30:00.000 KST까지 정밀 대기 (초단위 지연 최소화).
    반환값: 대기 종료 시점에 목표시각을 이미 몇 초 지나 있었는지(v2.5.3, 지연진단용).
    정상이면 0에 가까운 값 — 이 값을 여기서 직접 계산해 반환해야, run()이 별도로
    now_kst()를 다시 불러 "지금이 실제로 몇 시인지"에 의존하지 않아도 된다(테스트에서
    이 함수 자체를 통째로 모킹하면 그 반환값만으로 지연 시나리오를 재현 가능)."""
    target_h, target_m = int(ENTRY_TIME[:2]), int(ENTRY_TIME[2:])
    while True:
        now = now_kst()
        target = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
        remaining = (target - now).total_seconds()
        if remaining <= 0:
            return -remaining
        time.sleep(min(remaining, 5))


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


def _today_candidates(date: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT ticker, name, avg_val AS prev_close, score AS vol_ratio, rank_no,
                      val_ratio AS mom5
               FROM candidates WHERE trade_date=? AND strategy='iris_v2'
               ORDER BY rank_no""",
            (date,),
        ).fetchall()
    return [dict(r) for r in rows]


def _open_position_exists(date: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM positions WHERE status='open' AND strategy='iris_v2' AND date_open=?",
            (date,),
        ).fetchone()
    return row is not None


def _record_entry(date: str, ticker: str, name: str, entry_price: float, shares: int, vol_ratio: float, mode: str):
    now = now_kst()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO positions
               (ticker, name, date_open, time_open, entry_price, shares, score,
                target_pct, stop_pct, mode, status, strategy_version, strategy)
               VALUES (?,?,?,?,?,?,?,?,?,?,'open',?,?)""",
            # v2.5.2: target/stop을 params에서 가져옴 — 이전엔 15.0/-4.0 하드코딩이라
            # v2.5의 SL 변경(-4%→-4.5%)이 기록 컬럼에 반영 안 되고 있었음(정보성 컬럼).
            (ticker, name, date, now.strftime("%H%M%S"), entry_price, shares, vol_ratio,
             TP_PCT * 100, SL_PCT * 100, mode, IRIS_V2_VERSION, "iris_v2"),
        )


def _tg(msg: str, tag: str = None):
    from core.notify import send_ops, TAG_INFO
    send_ops(msg, tag or TAG_INFO)


def run(dry_run: bool = False):
    from core.kis_api import is_trading_day
    date = today_kst()

    if not is_trading_day(date):
        logger.info(f"{date} 휴장일 — 종료")
        return

    if TRADE_MODE == "real":
        from core.config import validate
        validate()  # KIS_APP_KEY/SECRET/ACCOUNT 누락시 여기서 곧바로 명확히 실패시킴
                    # (2026-08-08 코드감사 — 예전엔 이 함수가 어디서도 호출 안 돼
                    # 누락시 KIS API 호출 중간에서야 불명확한 에러로 실패했음)

    # 2026-08-08 코드감사에서 발견: halt 키가 날짜별로 분리돼있어(core.order_exec
    # .halt_key) is_halted(오늘)만 봐서는 "어제 halt가 안 풀린 채 남아있음"을
    # 아무도 못 봄 — entry.py는 지금까지 halt 자체를 아예 확인하지 않았음.
    # find_unresolved_halt()는 --clear로 지워지지 않은 halt를 날짜 무관하게 찾아,
    # halt의 "사람이 확인하기 전까진 자동매매 재개 안 함" 원칙이 날짜를 넘어서도
    # 실제로 지켜지게 한다. 매일 09:29 한 번씩만 실행되므로 해제 전까지 하루에
    # 한 번씩 알림이 계속 감(잊지 않도록 하는 부수효과).
    from core.order_exec import find_unresolved_halt
    unresolved = find_unresolved_halt()
    if unresolved:
        logger.critical(
            f"{unresolved['date']} halt가 아직 해제 안 됨(사유={unresolved.get('action')}, "
            f"종목={unresolved.get('ticker')}) — 오늘({date}) 진입판단 자체를 건너뜀. "
            f"'python3 -m scripts.iris_v2_halt --date {unresolved['date']} --clear' 확인 필요"
        )
        if not dry_run:
            _tg(
                f"{unresolved['date']}에 발생한 halt(사유={unresolved.get('action')}, "
                f"종목={unresolved.get('ticker')})가 아직 해제 안 됨 — 오늘({date}) 자동매매 "
                f"진입을 건너뜀. 반드시 KIS 앱에서 실제 잔고 확인 후 수동 해제 필요",
                TAG_ALERT,
            )
        return

    if not dry_run:
        # v2.5.4: 09:29 시작 직후(정밀대기 진입 전) 텔레그램 명령(포지션 사이징 모드
        # 전환 등)을 짧게 폴링·반영. 이 시점에 넣어야 09:30:00 정밀대기의 타이밍
        # 정확도에 영향을 주지 않음(대기루프는 매 반복 실제 시각을 다시 계산하므로
        # 이 폴링에 걸린 시간은 그냥 대기시간에서 자연히 상쇄됨).
        from core.telegram_commands import process_pending_commands
        process_pending_commands()

    if not dry_run and (prev_state := state_get(_entered_key(date))) is not None:
        logger.info(f"{date} 이미 진입판단 완료(idempotency, state={prev_state}) — 종료")
        return

    if not dry_run and _open_position_exists(date):
        logger.info(f"{date} 이미 open 포지션 존재 — 종료")
        return

    candidates = _today_candidates(date)
    if not candidates:
        logger.warning(f"{date} 후보 없음(전날 저녁 배치 미실행 또는 0건) — 종료")
        if not dry_run:
            state_set(_entered_key(date), "no_candidates")
        return

    if not dry_run:
        delay = _sleep_until_entry()
        if delay > DELAY_ABORT_SECS:
            logger.error(f"{date} 정밀대기 종료 후 목표시각({ENTRY_TIME})보다 {delay:.0f}초 지연"
                         f" — 체결지연 검증범위(5분) 초과, 오늘 진입 스킵")
            state_set(_entered_key(date), f"aborted_delay_{delay:.0f}s")
            _tg(f"{date} 진입판단이 목표시각보다 {delay:.0f}초나 지연돼 오늘 진입을 건너뜀"
                f"(cron/시스템 지연 의심) — 원인 확인 필요", TAG_ALERT)
            return
        elif delay > DELAY_WARN_SECS:
            logger.warning(f"{date} 정밀대기 종료 후 목표시각보다 {delay:.0f}초 지연 — 매매는 계속 진행")
            _tg(f"{date} 진입판단이 목표시각보다 {delay:.0f}초 지연됨(매매는 계속 진행, 참고용)", TAG_WARN)
        else:
            logger.info(f"{date} 정밀대기 종료, 지연 {delay:.1f}초(정상범위)")

    t0 = time.monotonic()
    # v2.5.2: 후보 전원의 시세를 병렬로 선조회(스냅샷)한 뒤 순위순으로 평가.
    # N_CANDIDATES가 50으로 늘어난 뒤(v2.5.1) 순차 조회는 하위 순위 도달까지 수십 초가
    # 걸려 체결지연 민감성(1분≈EV-12%)을 그대로 갉아먹음. 병렬 스냅샷은 백테스트의
    # "전 후보를 같은 09:30 시점 가격으로 평가" 가정과도 더 정합적.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=10) as ex:
        prices = list(ex.map(lambda c: _get_price_safe(c["ticker"]), candidates))

    picked = None
    price_fail_count = 0
    for cand, price in zip(candidates, prices):
        if price is None:
            price_fail_count += 1
            logger.warning(f"  #{cand['rank_no']} {cand['ticker']} {cand['name']} 시세조회 실패")
            continue
        # v2.5.2 버그수정: 갭 기준가는 price["open"](stck_oprc = 당일 09:00 시가)이 아니라
        # **09:30 시점 현재가(price["price"])** — 백테스트·일치성검증(98.3%)의 갭 정의
        # (09:30 분봉가 vs 전일종가)와 일치시킴. 이전 코드는 09:00 시가를 써서 09:00~09:30
        # 사이 가격변동만큼 갭을 체계적으로 잘못 판정했음(실측 사례 2025-07-01 056090:
        # 09:00 시가 기준 갭 -1.5%로 밴드 안 오판 vs 실제 09:30 가격 기준 +7.6%로 밴드 밖).
        gap = (price["price"] - cand["prev_close"]) / cand["prev_close"] if cand["prev_close"] else None
        logger.info(f"  #{cand['rank_no']} {cand['ticker']} {cand['name']} gap={gap:.4f}" if gap is not None else
                    f"  #{cand['rank_no']} {cand['ticker']} 갭계산불가(prev_close=0)")
        if gap is not None and GAP_MIN <= gap <= GAP_MAX:
            picked = {**cand, "gap": gap, "entry_price": price["price"]}
            break

    elapsed = time.monotonic() - t0

    if picked is None:
        # 후보 전원이 "밴드 밖"이 아니라 "시세조회 자체가 실패"였다면 정상적인 무진입과
        # 구분되는 API 장애 가능성 — 조용히 묻히지 않도록 CRITICAL 알림
        if candidates and price_fail_count == len(candidates):
            logger.error(f"{date} 후보 {len(candidates)}건 전부 시세조회 실패 — API 장애 의심")
            if not dry_run:
                _tg(f"{date} 진입판단 실패: 후보 {len(candidates)}건 전부 시세조회 불가"
                    f"(API 장애 의심) — 수동 확인 필요", TAG_ALERT)
        else:
            logger.info(f"{date} 밴드({GAP_MIN}~{GAP_MAX}) 통과 후보 없음 — 오늘 진입 없음 "
                        f"(조회소요 {elapsed:.2f}s, 시세조회실패 {price_fail_count}/{len(candidates)}건)")
            # v2.9: 지금까지 이 경로는 로그만 남기고 텔레그램이 안 갔음 — "왜 09:30에
            # 아무 알림도 안 왔지"로 헷갈릴 수 있어(halt·API장애와 구분 안 됨) 신규 추가.
            if not dry_run:
                _tg(f"{date} 진입판단 완료 — 갭밴드({GAP_MIN*100:.0f}~{GAP_MAX*100:.0f}%) "
                    f"통과 후보 없음, 오늘 무진입 (후보 {len(candidates)}건 확인)")
        if not dry_run:
            state_set(_entered_key(date), "no_entry")
        return

    real_mode = TRADE_MODE == "real"

    # v2.5.4: 포지션 사이징 모드(잔고전액/고정금액) — 텔레그램 /mode 명령으로 전환,
    # 기본값은 잔고전액(기존 동작과 동일: 가용현금이 CAPITAL_PER_TRADE보다 작을 땐
    # 사실상 잔고전액이었으므로 지금 계좌규모에선 행동 변화 없음).
    from core.telegram_commands import get_position_size_mode, MODE_FULL
    size_mode, fixed_amount = get_position_size_mode()
    target_notional = float("inf") if size_mode == MODE_FULL else fixed_amount

    # mom5(진입전5일모멘텀) 사이징캡 — 설계·구현·pytest 검증까지 끝났으나 **배포는
    # 보류 중**(MOM5_CAP_ENABLED=False, 2026-08-07 사용자 판단). 정본 230건
    # 데이터셋 재검증에서 비용(EV-16.7%)이 최초추정(-9%)보다 컸고 이 표본에선
    # 고mom5군 실현손실이 2건뿐이라 방어효과가 실현되지 않아 추가 리서치 후
    # 재검토하기로 함 — 상세는 iris DECISIONS 2026-08-07 (3차)·(5차) 참조.
    # 잔고전액모드(target_notional=inf)에서는 target_notional 자체를 줄여도 무효
    # (inf*factor=inf가 실제 가용현금 캡에 가려짐)라 size_factor로 별도 전달 —
    # 실제 배율 적용은 compute_buy_qty/아래 paper분기에서.
    mom5 = picked.get("mom5")
    mom5_factor = 1.0
    if MOM5_CAP_ENABLED and mom5 is not None and mom5 >= MOM5_CAP_THRESHOLD:
        mom5_factor = MOM5_CAP_SIZE_FACTOR
        logger.info(f"mom5 사이징캡 적용: mom5={mom5:.4f} >= {MOM5_CAP_THRESHOLD} → 사이징 x{mom5_factor}")

    if real_mode:
        from core.order_exec import compute_buy_qty
        shares = compute_buy_qty(picked["ticker"], picked["entry_price"], target_notional,
                                  base_price=picked["prev_close"], size_factor=mom5_factor)
    else:
        # paper 모드는 실제 잔고 개념이 없어 "잔고전액"을 그대로 적용 못 함 — fixed
        # 모드가 아니면 CAPITAL_PER_TRADE(명목자본) 그대로 사용.
        notional = fixed_amount if size_mode != MODE_FULL else CAPITAL_PER_TRADE
        notional = notional * mom5_factor
        shares = int(notional // picked["entry_price"])

    if shares <= 0:
        logger.warning(f"{date} {picked['ticker']} 진입가 대비 자본 부족(shares=0) — 종료")
        if not dry_run:
            state_set(_entered_key(date), "insufficient_capital")
        return

    size_label = "잔고전액" if size_mode == MODE_FULL else f"고정{fixed_amount:,}원"
    logger.info(
        f"진입: {picked['ticker']} {picked['name']} gap={picked['gap']:.4f} "
        f"entry={picked['entry_price']} shares={shares} (조회~판단 소요 {elapsed:.2f}s) "
        f"[TRADE_MODE={TRADE_MODE}, 사이징={size_label}]"
    )

    if dry_run:
        logger.info("[dry-run] DB 미기록·텔레그램 미발송")
        return

    if real_mode:
        from core.order_exec import buy_safe, verify_buy_fill, resolve_entry_price, set_halt
        # v2.5.2: 매수 직전 선기록 — 주문 제출~DB기록 사이에 프로세스가 죽고 수동
        # 재실행되면 이중매수가 날 수 있음. 미리 진입시도 상태를 남겨 재실행을 차단
        # (그날 진입을 놓치는 쪽이 이중매수보다 항상 안전). 성공/실패 경로가 아래에서
        # 각각 최종 상태로 덮어씀.
        state_set(_entered_key(date), f"buying:{picked['ticker']}")

        # 2026-08-08 신규(SPLIT_BUY_ENABLED, 기본 False): 트랑쉬1(지정가, 여기서)
        # + 트랑쉬2(시장가, monitor.py 09:31 첫틱)로 분할. 트랑쉬1이 확정되는 즉시
        # 그 수량만으로 포지션을 open 기록해 SL/TP 감시를 바로 시작한다 — 트랑쉬2는
        # core.order_exec의 pending_tranche2 메커니즘(pending_exit과 동일 원리)이
        # 백그라운드로 이어받는다(이론검증 결론, iris DECISIONS 2026-08-08).
        if SPLIT_BUY_ENABLED and shares >= 2:
            # 2026-08-08(최종검증에서 발견·수정): 지정가가 안 채워지면 그냥 halt하는
            # 대신 buy_tranche1_with_fallback()이 자동으로 시장가 전환까지 시도함
            # (예전엔 이 폴백이 없어서 지정가 미체결시 그날 진입을 통째로 놓쳤음).
            from core.order_exec import buy_tranche1_with_fallback, register_pending_tranche2
            tranche1_qty = max(1, round(shares * SPLIT_BUY_TRANCHE1_RATIO))
            tranche2_qty = shares - tranche1_qty
            actual_qty, odno = buy_tranche1_with_fallback(picked["ticker"], tranche1_qty, picked["entry_price"])
            if not odno:
                state_set(_entered_key(date), "buy_failed")
                set_halt(date, picked["ticker"], "매수주문실패(분할트랑쉬1)", f"의도수량={tranche1_qty}")
                _tg(f"매수 주문 실패(분할1/2 중 1차): {picked['name']}({picked['ticker']}) "
                    f"{tranche1_qty}주 — 오늘 자동매매 중단, 수동 확인 필요", TAG_ALERT)
                return
            if actual_qty <= 0:
                state_set(_entered_key(date), "buy_unfilled")
                set_halt(date, picked["ticker"], "매수체결미확인(분할트랑쉬1, 시장가전환후에도)",
                          f"주문번호={odno} 의도수량={tranche1_qty}")
                _tg(f"매수 체결 미확인(분할1/2 중 1차, 지정가+시장가전환 모두 시도): "
                    f"{picked['name']}({picked['ticker']}) 주문번호 {odno} — "
                    f"오늘 자동매매 중단, 수동 확인 필요", TAG_ALERT)
                return
            entry_price = resolve_entry_price(picked["ticker"], picked["entry_price"], odno)
            partial_note = f" (목표{tranche1_qty}주 중 일부만 체결)" if actual_qty < tranche1_qty else ""
            _record_entry(date, picked["ticker"], picked["name"], entry_price, actual_qty, picked["vol_ratio"], "real")
            state_set(_entered_key(date), picked["ticker"])
            tranche2_note = ""
            # 트랑쉬1이 목표만큼 다 안 찼으면 트랑쉬2는 등록 안 함(단순화 — 언더필
            # 그대로 허용, 매수는 부족한 쪽이 항상 안전하다는 기존 원칙)
            if tranche2_qty > 0 and actual_qty >= tranche1_qty:
                register_pending_tranche2(date, picked["ticker"], tranche2_qty)
                tranche2_note = f" · 나머지 {tranche2_qty}주는 09:31 시장가로 이어집니다"
            _tg(
                f"진입(1/2차): <b>{picked['name']}</b>({picked['ticker']})\n"
                f"갭 {picked['gap']*100:.2f}% · {entry_price:,.0f}원 · {actual_qty}주{partial_note}\n"
                f"주문번호 {odno} · 소요 {elapsed:.2f}초{tranche2_note}",
                TAG_ENTRY,
            )
            return

        ok, odno = buy_safe(picked["ticker"], shares)
        if not ok:
            state_set(_entered_key(date), "buy_failed")
            set_halt(date, picked["ticker"], "매수주문실패", f"의도수량={shares}")
            _tg(f"매수 주문 실패: {picked['name']}({picked['ticker']}) {shares}주 — "
                f"오늘 자동매매 중단, 수동 확인 필요", TAG_ALERT)
            return
        actual_qty = verify_buy_fill(picked["ticker"], shares, odno)
        if actual_qty <= 0:
            state_set(_entered_key(date), "buy_unfilled")
            set_halt(date, picked["ticker"], "매수체결미확인", f"주문번호={odno} 의도수량={shares}")
            _tg(f"매수 체결 미확인: {picked['name']}({picked['ticker']}) 주문번호 {odno} — "
                f"오늘 자동매매 중단, 수동 확인 필요", TAG_ALERT)
            return
        entry_price = resolve_entry_price(picked["ticker"], picked["entry_price"], odno)
        partial_note = f" (목표{shares}주 중 일부만 체결)" if actual_qty < shares else ""
        _record_entry(date, picked["ticker"], picked["name"], entry_price, actual_qty, picked["vol_ratio"], "real")
        state_set(_entered_key(date), picked["ticker"])
        _tg(
            f"진입: <b>{picked['name']}</b>({picked['ticker']})\n"
            f"갭 {picked['gap']*100:.2f}% · {entry_price:,.0f}원 · {actual_qty}주{partial_note}\n"
            f"주문번호 {odno} · 소요 {elapsed:.2f}초",
            TAG_ENTRY,
        )
    else:
        _record_entry(date, picked["ticker"], picked["name"], picked["entry_price"], shares, picked["vol_ratio"], "paper")
        state_set(_entered_key(date), picked["ticker"])
        _tg(
            f"진입(페이퍼): <b>{picked['name']}</b>({picked['ticker']})\n"
            f"갭 {picked['gap']*100:.2f}% · {picked['entry_price']:,.0f}원 · {shares}주\n"
            f"소요 {elapsed:.2f}초 (시세조회만, 실제 주문 없음)",
            TAG_ENTRY,
        )


if __name__ == "__main__":
    from core.script_guard import run_guarded
    from core.proc_lock import script_lock, LockHeld
    _dry = "--dry-run" in sys.argv
    try:
        with script_lock("iris_v2_entry"):
            run_guarded(lambda: run(dry_run=_dry), "iris_v2_entry", logger, _dry)
    except LockHeld:
        logger.warning("이전 iris_v2_entry 실행이 아직 안 끝나 이번 실행 스킵(중복실행 방지)")
