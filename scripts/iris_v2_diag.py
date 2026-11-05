"""
iris v2 — 진단 종합조회 CLI (2026-07-24 신규, v2.5.7).
문제가 생겼을 때 로그파일·DB 여기저기 흩어진 상태를 한 번에 보기 위한 도구.
읽기전용, 아무것도 바꾸지 않음.

실행: python3 -m scripts.iris_v2_diag [--days N]
"""
import argparse
from pathlib import Path

from core.db import get_conn
from core.timeutil import today_kst


def _section(title: str):
    print(f"\n=== {title} ===")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3, help="최근 며칠치 로그/스냅샷 볼지")
    args = ap.parse_args()

    today = today_kst()
    # 2026-08-08: 날짜 무관 전체 스캔으로 변경 — 예전엔 오늘 키만 봐서 어제 이전의
    # 미해결 halt를 놓칠 수 있었음(코드감사에서 발견). iris_v2_halt.py도 동일 수정.
    _section("halt 상태")
    from core.order_exec import find_unresolved_halt
    halt = find_unresolved_halt()
    if halt:
        print(f"🚨 halt 중(발생일 {halt.get('date')}): {halt}")
    else:
        print("halt 아님 — 정상")

    _section("잠금(lock) 상태 — 실제로 잠겨있으면 그 스크립트가 오래 실행중이거나 멈춰있을 수 있음")
    from core.config import BASE_DIR
    from core.proc_lock import script_lock, LockHeld
    lock_dir = BASE_DIR / "locks"
    script_names = ["iris_v2_candidates", "iris_v2_entry", "iris_v2_monitor",
                     "iris_v2_force_close", "iris_v2_daily_report"]
    if lock_dir.exists():
        held = []
        for name in script_names:
            if not (lock_dir / f"{name}.lock").exists():
                continue
            try:
                with script_lock(name):
                    pass  # 즉시 획득+해제 성공 -> 지금은 안 잠겨있음(정상)
            except LockHeld:
                held.append(name)
        if held:
            for name in held:
                print(f"  🚨 {name}: 현재 잠겨있음 — 실행중이거나 비정상종료로 멈춰있을 수 있음")
        else:
            print("  전부 정상(현재 잠겨있는 스크립트 없음)")
    else:
        print("  락 디렉토리 아직 생성 안 됨(정상 — 실행된 적 없음)")

    _section("오늘 candidates / 포지션")
    with get_conn() as conn:
        n_cand = conn.execute(
            "SELECT COUNT(*) c FROM candidates WHERE trade_date=? AND strategy='iris_v2'", (today,)
        ).fetchone()["c"]
        print(f"오늘 후보 수: {n_cand}")
        positions = conn.execute(
            "SELECT ticker, status, mode, entry_price, shares, pnl_pct, exit_reason FROM positions "
            "WHERE strategy='iris_v2' AND date_open=?", (today,)
        ).fetchall()
        if positions:
            for p in positions:
                print(f"  {dict(p)}")
        else:
            print("오늘 포지션 없음")

    _section("잔고 스냅샷 최근 기록")
    with get_conn() as conn:
        snaps = conn.execute(
            "SELECT date, balance, reliable, captured_at FROM balance_snapshot ORDER BY date DESC LIMIT ?",
            (args.days,),
        ).fetchall()
        if snaps:
            for s in snaps:
                flag = "" if s["reliable"] else " ⚠️신뢰도낮음"
                print(f"  {s['date']}: {s['balance']:,.0f}원{flag}")
        else:
            print("  기록 없음")

    _section("공식기록/입출금 상태")
    from core.db import get_live_tracking, total_capital
    tracking = get_live_tracking()
    print(f"공식기록: {tracking if tracking else '아직 테스트 기간'}")
    print(f"총입금액: {total_capital():,.0f}원")

    _section(f"최근 {args.days}일 CRITICAL/ERROR 로그(trade_log DB)")
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT ts, level, message FROM trade_log
               WHERE category='IRIS_V2' AND level IN ('CRITICAL','ERROR')
               ORDER BY ts DESC LIMIT 30"""
        ).fetchall()
        if rows:
            for r in rows:
                print(f"  [{r['level']}] {r['ts']} {r['message'][:150]}")
        else:
            print("  없음(최근 기록 범위 내)")

    _section("시장 여건 — KOSDAQ 50일선 위 비율(v2.5 후보풀 선행지표)")
    # 2026-08-04: v2.5의 above_ma50 필터가 폭락장에서 후보풀을 거의 다 걸러내는 게
    # 무진입의 주 원인으로 확인됨(iris DECISIONS 2026-08-04 참조) — 회복 추세를
    # 매번 diag로 바로 볼 수 있게 추가. 15개월 역사적 평균은 약 38%(최저 6%대,
    # 이번 폭락 저점 06~07월 10%대) — 하드코딩된 참고치일 뿐 매번 재계산하지
    # 않음(diag는 가벼워야 함). 2026-08-05: 같은 계산을 저녁 후보계산 텔레그램
    # 알림에서도 쓰게 돼 core.market_breadth로 공용화.
    from core.market_breadth import kosdaq_above_ma50_pct
    with get_conn() as conn:
        recent_dates = [r["date"] for r in conn.execute(
            "SELECT DISTINCT date FROM price_daily ORDER BY date DESC LIMIT 5"
        ).fetchall()]
        rows = [(d, kosdaq_above_ma50_pct(conn, d)) for d in sorted(recent_dates)]
    if rows:
        for d, frac in rows:
            if frac is None:
                continue
            print(f"  {d}: {frac*100:.1f}%")
        print("  참고: 15개월 평균 ~38%(최저 6%대), 이번 폭락 저점(06~07월) 10%대 —")
        print("  30%대 중반 이상 회복하면 v2.5 후보풀도 정상화 기대(2026-08-04 진단)")
    else:
        print("  price_daily 데이터 없음")

    _section("시장 유동성 — KOSDAQ 총거래대금 백분위(v2.9 유동성필터 판단기준)")
    # 2026-08-11 신규: v2.9 유동성필터(전일 백분위<25%ile이면 무진입)가 왜
    # 발동/비발동했는지 매번 diag로 바로 확인할 수 있게 추가 — 위 50일선 비율과
    # 동일 패턴, 사용자 요청.
    from strategy.iris_v2_params import (
        LIQUIDITY_LOOKBACK_DAYS, LIQUIDITY_MIN_PERIODS, LIQUIDITY_PERCENTILE_THRESHOLD,
    )
    from core.market_breadth import kosdaq_liquidity_percentile, percentile_label
    with get_conn() as conn:
        recent_dates = [r["date"] for r in conn.execute(
            "SELECT DISTINCT date FROM price_daily ORDER BY date DESC LIMIT 5"
        ).fetchall()]
        liq_rows = [(d, kosdaq_liquidity_percentile(conn, d, LIQUIDITY_LOOKBACK_DAYS, LIQUIDITY_MIN_PERIODS))
                    for d in sorted(recent_dates)]
    if liq_rows:
        for d, pct in liq_rows:
            if pct is None:
                continue
            flag = " ⚠️필터임계값 미만(다음날 무진입)" if pct < LIQUIDITY_PERCENTILE_THRESHOLD else ""
            print(f"  {d}: {percentile_label(pct)}{flag}")
        print(f"  참고: 하위 {LIQUIDITY_PERCENTILE_THRESHOLD*100:.0f}% 미만이면 다음 거래일 무진입"
              f"(최근250거래일 트레일링 기준, iris DECISIONS 2026-08-11 (67차)~(70차))")
    else:
        print("  price_daily 데이터 없음")

    _section("최근 진입 로그 지연시각(v2.5.3 지연감지)")
    log_path = BASE_DIR / "logs" / "iris_v2_entry.log"
    if log_path.exists():
        lines = [l for l in log_path.read_text(errors="ignore").splitlines() if "정밀대기 종료" in l]
        for l in lines[-5:]:
            print(f"  {l}")
        if not lines:
            print("  기록 없음")
    else:
        print("  로그파일 없음")


if __name__ == "__main__":
    main()
