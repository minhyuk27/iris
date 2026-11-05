"""
iris v2 — 계좌 원금 입출금 기록 CLI (2026-07-24 신규, v2.5.5).
잔고기준 누적수익률(daily_report) 계산의 분모(총입금액)를 정확히 유지하려면 입출금이
있을 때마다 반드시 이 스크립트로 기록해야 한다 — KIS API는 "이체"와 "매매손익"을
구분해 알려주지 않으므로, 기록을 안 하면 입금액이 그대로 "수익"으로 잘못 잡힌다.

**텔레그램 명령이 아니라 CLI 전용**(의도적) — 실제 돈 계산의 기준값을 바꾸는 조작이라
`/mode`보다 신중해야 함(오타 위험), 로컬에서 직접 실행하도록 제한.

실행:
  python3 -m scripts.iris_v2_capital --deposit 3000000 [--note "1차 입금"]
  python3 -m scripts.iris_v2_capital --withdraw 500000 [--note "일부 출금"]
  python3 -m scripts.iris_v2_capital --list                 # 입출금 이력 + 총입금액 조회
"""
import argparse

from core.db import add_capital_event, list_capital_events, total_capital
from core.timeutil import today_kst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deposit", type=float, default=None, help="입금액(원)")
    ap.add_argument("--withdraw", type=float, default=None, help="출금액(원)")
    ap.add_argument("--date", default=None, help="YYYYMMDD, 기본값 오늘")
    ap.add_argument("--note", default="", help="메모(선택)")
    ap.add_argument("--list", action="store_true", help="입출금 이력 전체 조회")
    args = ap.parse_args()

    if args.list:
        events = list_capital_events()
        if not events:
            print("입출금 기록 없음")
        for e in events:
            sign = "입금" if e["amount"] >= 0 else "출금"
            print(f"  {e['date']} {sign} {abs(e['amount']):>12,.0f}원  {e['note']}")
        print(f"\n총입금액(순, 입금-출금): {total_capital():,.0f}원")
        return

    if args.deposit is None and args.withdraw is None:
        ap.error("--deposit, --withdraw, --list 중 하나는 지정해야 함")

    date = args.date or today_kst()
    if args.deposit is not None:
        add_capital_event(date, args.deposit, args.note)
        print(f"✅ {date} 입금 {args.deposit:,.0f}원 기록됨")
    if args.withdraw is not None:
        add_capital_event(date, -args.withdraw, args.note)
        print(f"✅ {date} 출금 {args.withdraw:,.0f}원 기록됨")

    print(f"현재 총입금액(순): {total_capital():,.0f}원")


if __name__ == "__main__":
    main()
