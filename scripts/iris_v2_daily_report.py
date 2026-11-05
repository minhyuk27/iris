"""
iris v2 — 13:05 KST 일간 성과 리포트 (리포트봇 전용).
운영봇(후보예고/진입/청산/장애알림)과는 완전히 분리된 채널 — core/notify.py의
send_report() 사용.

TRADE_MODE=real(2026-07-24, v2.5.5): **잔고 기준**으로 갱신 — 매 거래 손익률을
복리로 재투자한다고 가정한 시뮬레이션 수치 대신, 13:05(강제청산 직후, 정상이면
포지션이 비어있어야 함)에 KIS 실제 잔고를 조회해 그대로 기록·비교한다. 사이징
모드가 고정금액이고 계좌가 그 값보다 커지면 초과분이 재투자 안 되므로, 예전
방식(거래손익률 복리시뮬레이션)은 실제 잔고 증가율과 점점 벌어질 수 있었음
(2026-07-24 발견, 상세: iris DECISIONS 참조). 총입금액은 `scripts/iris_v2_capital.py`
로 기록해야 정확한 누적수익률이 나옴 — 미기록이면 리포트에 안내 문구만 표시.

TRADE_MODE=paper: 실제 잔고 개념이 없어 기존 거래손익률 기반 리포트 그대로 유지.

cron: 5 4 * * 1-5 (KST 13:05, 강제청산 13:00 직후)
실행: python3 -m scripts.iris_v2_daily_report [--dry-run]

휴장일(주말+공휴일)엔 is_trading_day() 체크로 리포트 발송 자체를 생략한다
(2026-08-17 발견·수정 — entry.py는 이미 이 체크가 있었는데 여기는 빠져있어서,
휴장일에도 "오늘: 청산거래 없음"뿐인 무의미한 리포트가 나갔음. C급, 순수 알림
로직 수정).
"""
import sys
import time

import pandas as pd

from core.config import IRIS_V2_TRADE_MODE as TRADE_MODE
from core.db import get_conn
from core.logger import get_logger
from core.timeutil import today_kst

logger = get_logger("iris_v2_daily_report", "IRIS_V2")

# 2026-08-18: v2(100만원 고정금 전환)·v3(신규) 동시 2주 테스트 기간 사용자 확정
# (iris DECISIONS 2026-08-18 참조) — 리포트에 명시적 종료일을 박아 언제까지
# "테스트"인지 매번 헷갈리지 않게 한다.
TEST_PERIOD_END = "20260828"
TEST_PERIOD_END_DISP = "2026-08-28(금)"


def _closed_trades() -> pd.DataFrame:
    # TRADE_MODE에 해당하는 기록만 집계(페이퍼/실거래 혼재 방지).
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT date_close, ticker, name, pnl_pct, pnl_amount, exit_reason
               FROM positions
               WHERE strategy='iris_v2' AND mode=? AND status='closed'
               ORDER BY date_close""",
            (TRADE_MODE,),
        ).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def _compound(pcts: pd.Series) -> float:
    """일별 평균수익률(%) 시리즈를 복리로 환산한 누적수익률(%). paper 모드 전용
    (실잔고가 없어 거래손익률 시뮬레이션 외엔 방법이 없음)."""
    return ((1 + pcts / 100).prod() - 1) * 100


def _has_open_position(date: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM positions WHERE status='open' AND strategy='iris_v2' AND date_open=?",
            (date,),
        ).fetchone()
    return row is not None


def _safe_get_balance():
    """실제 KIS 잔고 조회, 최대 3회 재시도(다른 실거래 API 호출과 동일 원칙)."""
    from core.kis_api import get_balance
    for i in range(3):
        try:
            return get_balance()
        except Exception as e:
            logger.warning(f"잔고조회 시도 {i+1}/3 실패: {e}")
            if i < 2:
                time.sleep(2 ** i)
    logger.error("잔고조회 최종 실패 — 잔고기준 리포트 생략, 거래기록만 표시")
    return None


def build_paper_report(today: str, df: pd.DataFrame) -> str:
    """TRADE_MODE=paper 전용(기존 방식 유지) — 실잔고가 없어 거래손익률 복리시뮬레이션."""
    if df.empty:
        return f"<b>{today} iris v2 성과 리포트</b> (페이퍼)\n아직 청산된 거래가 없습니다."

    daily = df.groupby("date_close").agg(
        n=("pnl_pct", "size"),
        win=("pnl_pct", lambda s: (s > 0).mean()),
        avg_pct=("pnl_pct", "mean"),
        amt=("pnl_amount", "sum"),
    ).sort_index()

    lines = [f"<b>{today} iris v2 성과 리포트</b> (페이퍼)"]

    if today in daily.index:
        t = daily.loc[today]
        lines.append(
            f"\n<b>오늘</b>: {int(t['n'])}건 · 승률 {t['win']:.0%} · "
            f"평균 {t['avg_pct']:+.2f}% · 손익 {t['amt']:+,.0f}원"
        )
    else:
        lines.append("\n<b>오늘</b>: 거래 없음")

    recent = daily.tail(5)
    lines.append("\n<b>최근 5거래일</b>")
    for date, row in recent.iterrows():
        lines.append(f"  {date}: {int(row['n'])}건 · {row['avg_pct']:+.2f}% · {row['amt']:+,.0f}원")
    lines.append(f"  → 5일 복리환산 {_compound(recent['avg_pct']):+.2f}%")

    month = today[:6]
    month_daily = daily[daily.index.str.startswith(month)]
    if len(month_daily):
        month_win = df[df["date_close"].str.startswith(month)]["pnl_pct"].gt(0).mean()
        lines.append(
            f"\n<b>이번달({month})</b>: {int(month_daily['n'].sum())}건 · "
            f"승률 {month_win:.0%} · 복리환산 {_compound(month_daily['avg_pct']):+.2f}%"
        )

    total_win = df["pnl_pct"].gt(0).mean()
    total_amt = df["pnl_amount"].sum()
    lines.append(
        f"\n<b>전체누적</b>({daily.index.min()}~{daily.index.max()}, {len(daily)}거래일): "
        f"{len(df)}건 · 승률 {total_win:.0%} · 복리환산 {_compound(daily['avg_pct']):+.1f}% · "
        f"실현손익 {total_amt:+,.0f}원"
    )
    return "\n".join(lines)


def _period_line(label: str, natural_start: dict | None, tracking: dict | None,
                  today: str, balance: float, capital_events_between) -> str:
    """기간수익률 한 줄 — natural_start(자연스러운 기간 시작 스냅샷)가 공식기록
    시작점(tracking)보다 이전이면 tracking 시작점으로 대체(테스트기간 손익이
    공식 기간수익률에 섞이지 않도록, v2.5.6)."""
    if tracking and (natural_start is None or natural_start["date"] < tracking["date"]):
        base_date, base_balance = tracking["date"], tracking["anchor_balance"]
        label = f"공식기록 시작({base_date}) 이후"
    elif natural_start:
        base_date, base_balance = natural_start["date"], natural_start["balance"]
    else:
        return f"<b>{label}</b>: 스냅샷 축적 중(데이터 부족)"

    if base_date == today or base_balance <= 0:
        return f"<b>{label}</b>: 스냅샷 축적 중(데이터 부족)"

    flow = capital_events_between(base_date, today)
    pct = (balance - base_balance - flow) / base_balance * 100
    note = f" (구간중 입출금 {flow:+,.0f}원 반영)" if flow else ""
    return f"<b>{label}</b>: {base_balance:,.0f}원 → {balance:,.0f}원 ({pct:+.2f}%){note}"


def build_balance_report(today: str, df: pd.DataFrame, dry_run: bool = False) -> str:
    """TRADE_MODE=real 전용 — 실제 KIS 잔고 기준(v2.5.5), 테스트/공식 기간 구분(v2.5.6)."""
    from core.db import (
        record_balance_snapshot, recent_balance_snapshots,
        first_balance_snapshot_of_month, capital_events_between, total_capital,
        get_live_tracking,
    )
    from core.order_exec import is_halted

    today_df = df[df["date_close"] == today] if not df.empty else df
    tracking = get_live_tracking()
    mode_label = "실거래·잔고기준·공식" if tracking else "실거래·잔고기준·테스트"
    lines = [f"<b>{today} iris v2 성과 리포트</b> ({mode_label})"]
    if not tracking:
        period_note = (
            f"(~{TEST_PERIOD_END_DISP}까지 2주 테스트기간)" if today <= TEST_PERIOD_END
            else "(2주 테스트기간 종료일 지남)"
        )
        lines.append(
            f"⚠️ <b>아직 테스트 기간 {period_note} — 공식 성과로 안 침.</b> 준비되면 "
            "python3 -m scripts.iris_v2_start_tracking 로 공식 기록 시작"
        )

    bal = _safe_get_balance()
    if bal is None:
        lines.append("\n⚠️ 잔고조회 실패 — 정확한 수치 산출 불가, 거래기록만 표시")
        if len(today_df):
            lines.append(f"오늘 실현손익: {today_df['pnl_amount'].sum():+,.0f}원 ({len(today_df)}건)")
        else:
            lines.append("오늘 청산거래 없음")
        return "\n".join(lines)

    balance = bal["final_cash"]
    halted = is_halted(today) is not None
    has_open = _has_open_position(today)
    reliable = not halted and not has_open
    if dry_run:
        lines.append("\n[dry-run] 스냅샷 DB 미기록(조회값만 표시)")
    else:
        record_balance_snapshot(today, balance, reliable=reliable)

    if not reliable:
        reason = "halt 상태" if halted else "포지션 미청산"
        lines.append(f"\n⚠️ 오늘 스냅샷 신뢰도 낮음({reason}) — 잔고에 미청산분이 섞여있을 수 있음")

    lines.append(f"\n<b>현재 잔고</b>: {balance:,.0f}원")
    principal = total_capital(as_of_date=today)
    if principal > 0:
        lines.append(f"<b>총입금액</b>: {principal:,.0f}원")
    else:
        lines.append(
            "<b>총입금액 미기록</b> — python3 -m scripts.iris_v2_capital --deposit <금액> 으로 기록할 것"
        )

    if tracking:
        live_principal = tracking["anchor_balance"] + capital_events_between(tracking["date"], today)
        if live_principal > 0:
            cum_pct = (balance - live_principal) / live_principal * 100
            lines.append(f"<b>누적수익률(공식, {tracking['date']}~)</b>: {cum_pct:+.2f}%")

    if len(today_df):
        lines.append(f"\n<b>오늘</b>: {len(today_df)}건 · 실현손익 {today_df['pnl_amount'].sum():+,.0f}원")
    else:
        lines.append("\n<b>오늘</b>: 청산거래 없음")

    # v2.5.7: 잔고정합성 검사 — 어제잔고+오늘실현손익+오늘기록된입출금이 오늘 실제
    # 잔고와 크게 다르면(1만원 초과) 입출금 기록 누락 등을 의심할 근거 — 지금까지는
    # 이런 불일치를 아무도 알아챌 방법이 없었음(총입금액/누적수익률이 조용히
    # 틀어질 뿐).
    if reliable:
        from core.db import latest_balance_snapshot_before
        yday = latest_balance_snapshot_before(today)
        if yday:
            today_realized = today_df["pnl_amount"].sum() if len(today_df) else 0.0
            today_flow = capital_events_between(yday["date"], today)
            expected = yday["balance"] + today_realized + today_flow
            diff = balance - expected
            if abs(diff) > 10_000:
                lines.append(
                    f"\n⚠️ <b>잔고 불일치 감지</b>: 예상 {expected:,.0f}원(어제잔고+오늘실현손익"
                    f"+기록된입출금) vs 실제 {balance:,.0f}원 (차이 {diff:+,.0f}원) — "
                    f"입출금 기록을 빠뜨렸을 수 있음, iris_v2_capital --list로 확인할 것"
                )

    recent5 = recent_balance_snapshots(5)
    natural_start = recent5[0] if len(recent5) >= 2 else None
    lines.append("\n" + _period_line(f"최근{max(len(recent5),1)}거래일", natural_start, tracking,
                                      today, balance, capital_events_between))

    month = today[:6]
    month_start = first_balance_snapshot_of_month(month)
    lines.append(_period_line(f"이번달({month})", month_start, tracking,
                               today, balance, capital_events_between))

    return "\n".join(lines)


def build_report(today: str, df: pd.DataFrame, dry_run: bool = False) -> str:
    if TRADE_MODE == "real":
        return build_balance_report(today, df, dry_run=dry_run)
    return build_paper_report(today, df)


def run(dry_run: bool = False):
    from core.kis_api import is_trading_day
    today = today_kst()

    if not is_trading_day(today):
        logger.info(f"{today} 휴장일 — 종료(리포트 미발송)")
        return

    df = _closed_trades()
    msg = build_report(today, df, dry_run=dry_run)
    logger.info(msg.replace("\n", " | "))
    if dry_run:
        logger.info("[dry-run] 텔레그램 미발송")
        return
    from core.notify import send_report
    send_report(msg)


if __name__ == "__main__":
    from core.script_guard import run_guarded
    from core.proc_lock import script_lock, LockHeld
    _dry = "--dry-run" in sys.argv
    try:
        with script_lock("iris_v2_daily_report"):
            run_guarded(lambda: run(dry_run=_dry), "iris_v2_daily_report", logger, _dry)
    except LockHeld:
        logger.warning("이전 iris_v2_daily_report 실행이 아직 안 끝나 이번 실행 스킵(중복실행 방지)")
