"""
iris v2 — 포지션 사이징 모드 확인/설정 CLI (2026-07-24 신규, v2.5.4).
텔레그램 `/mode` 명령과 동일한 상태(bot_state)를 로컬에서 직접 확인·설정한다.
디버깅·수동확인용 — 실제 운영 중 원격 전환은 텔레그램 `/mode` 명령을 쓸 것
(core/telegram_commands.py 참조).

실행:
  python3 -m scripts.iris_v2_mode                       # 현재 모드 확인만
  python3 -m scripts.iris_v2_mode --set full             # 잔고전액 모드로 전환
  python3 -m scripts.iris_v2_mode --set fixed --amount 3000000  # 고정금액 모드로 전환
"""
import argparse

from core.telegram_commands import (
    MODE_FIXED, MODE_FULL, MAX_FIXED_AMOUNT, MIN_FIXED_AMOUNT,
    get_position_size_mode,
)
from core.db import state_set
from core.telegram_commands import MODE_KEY, FIXED_AMOUNT_KEY


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=["full", "fixed"], default=None)
    ap.add_argument("--amount", type=int, default=None, help="--set fixed 사용시 금액(원)")
    args = ap.parse_args()

    if args.set == "full":
        state_set(MODE_KEY, MODE_FULL)
        print("✅ 잔고전액 모드로 전환됨")
    elif args.set == "fixed":
        if args.amount is None:
            print("--set fixed 사용시 --amount 필수")
            return
        if not (MIN_FIXED_AMOUNT <= args.amount <= MAX_FIXED_AMOUNT):
            print(f"금액은 {MIN_FIXED_AMOUNT:,}원~{MAX_FIXED_AMOUNT:,}원 범위여야 함")
            return
        state_set(MODE_KEY, MODE_FIXED)
        state_set(FIXED_AMOUNT_KEY, args.amount)
        print(f"✅ 고정금액 {args.amount:,}원 모드로 전환됨")

    mode, amount = get_position_size_mode()
    label = "잔고전액" if mode == MODE_FULL else f"고정금액({amount:,}원)"
    print(f"현재 모드: {label}")


if __name__ == "__main__":
    main()
