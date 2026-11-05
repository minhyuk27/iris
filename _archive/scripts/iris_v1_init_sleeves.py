"""
iris v1 sleeve_cash 최초 시딩 — 1회성 스크립트.
실제 계좌 가용현금(get_available_cash)을 확인한 뒤, 사용자가 지정한 총자본을
3세대(gen0/gen1/gen2)에 균등분배해 sleeve_cash 테이블에 기록한다.

⚠️ 이미 sleeve_cash 행이 있으면(=이미 시딩됐으면) 아무것도 하지 않고 현재 상태만
출력 — 실수로 재실행해도 기존 배분을 덮어쓰지 않는다. 재분배가 필요하면 DB를
직접 확인하고 수동으로 처리할 것(자동 재분배 금지 — 이미 보유 포지션과의
정합성이 깨질 수 있음).

실행: python3 -m scripts.iris_v1_init_sleeves --capital 10000000
"""
import argparse
import sys

from core.db import get_conn, init_db
from core.timeutil import iso_kst

STRATEGY_PREFIX = "iris_v1"
N_GEN = 3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, required=True, help="총 투입자본(원)")
    ap.add_argument("--yes", action="store_true", help="확인 프롬프트 생략")
    args = ap.parse_args()

    init_db()

    with get_conn() as conn:
        existing = conn.execute(
            "SELECT strategy, cash FROM sleeve_cash WHERE strategy LIKE 'iris_v1_gen%' AND account='KIS'"
        ).fetchall()
    if existing:
        print("이미 시딩되어 있습니다 — 아무 작업도 하지 않습니다. 현재 상태:")
        for row in existing:
            print(f"  {row['strategy']}: {row['cash']:,.0f}원")
        total = sum(r["cash"] for r in existing)
        print(f"  합계: {total:,.0f}원")
        sys.exit(0)

    from core.kis_api import get_available_cash
    try:
        actual_cash = get_available_cash()
        print(f"실제 계좌 가용현금: {actual_cash:,.0f}원")
        if args.capital > actual_cash:
            print(f"⚠️ 경고: 지정한 자본({args.capital:,.0f}원)이 실제 가용현금({actual_cash:,.0f}원)보다 큽니다.")
    except Exception as e:
        print(f"⚠️ 실제 계좌 조회 실패({e}) — 확인 없이 진행합니다.")

    per_gen = args.capital / N_GEN
    print(f"\n총자본 {args.capital:,.0f}원 → {N_GEN}세대 균등분배: 세대당 {per_gen:,.0f}원")
    for gen in range(N_GEN):
        print(f"  iris_v1_gen{gen}: {per_gen:,.0f}원")

    if not args.yes:
        confirm = input("\n이대로 sleeve_cash에 기록할까요? (yes 입력): ")
        if confirm.strip().lower() != "yes":
            print("취소되었습니다.")
            sys.exit(1)

    now = iso_kst()
    with get_conn() as conn:
        for gen in range(N_GEN):
            strategy = f"{STRATEGY_PREFIX}_gen{gen}"
            conn.execute("""
                INSERT INTO sleeve_cash(strategy, account, cash, updated_at) VALUES (?, 'KIS', ?, ?)
                ON CONFLICT(strategy, account) DO NOTHING
            """, (strategy, per_gen, now))

    print("\n완료.")


if __name__ == "__main__":
    main()
