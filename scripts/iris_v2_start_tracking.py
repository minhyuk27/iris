"""
iris v2 — 공식 실적 기록 시작 (2026-07-24 신규, v2.5.6).

며칠간의 최소금액 스모크테스트 기간(실제 소액이 오가지만 "과정에 문제없는지"
확인용)과, 그 이후 "진짜 실적으로 카운트하는" 공식 기록 기간을 구분한다. 이
스크립트를 실행하는 순간의 실제 KIS 잔고를 "공식 기록 시작 잔고(anchor)"로 고정 —
`daily_report`의 누적수익률(공식)은 이 시점부터 다시 0%에서 시작한다.

**테스트 기간 매매도 실제 KIS 계좌의 진짜 손익이라 잔고 자체에는 이미 반영돼
있음**(가짜 돈이 아님) — 이 스위치는 회계상 "원금을 재설정"하는 게 아니라, 오직
"성과 리포트에서 어디부터를 공식 트랙레코드로 볼지"만 정한다. 총입금액
(`iris_v2_capital.py`)과는 별개 개념 — 그건 실제 입출금 사실 그대로, 이건 리포팅
기준점.

실행:
  python3 -m scripts.iris_v2_start_tracking            # 시작(이미 시작됐으면 상태만 출력)
  python3 -m scripts.iris_v2_start_tracking --force     # 이미 시작됐어도 지금 시점으로 재설정
  python3 -m scripts.iris_v2_start_tracking --status    # 상태만 조회, 변경 없음
"""
import argparse

from core.db import get_live_tracking, start_live_tracking
from core.timeutil import today_kst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="이미 시작됐어도 지금 시점으로 재설정")
    ap.add_argument("--status", action="store_true", help="상태만 조회, 변경 안 함")
    args = ap.parse_args()

    current = get_live_tracking()

    if args.status:
        if current:
            print(f"공식 기록 시작됨: {current['date']} (기준잔고 {current['anchor_balance']:,.0f}원)")
        else:
            print("아직 테스트 기간 — 공식 기록 미시작")
        return

    if current and not args.force:
        print(f"이미 공식 기록 시작됨: {current['date']} (기준잔고 {current['anchor_balance']:,.0f}원)")
        print("재설정하려면 --force 추가")
        return

    from core.kis_api import get_balance
    bal = get_balance()
    balance = bal["final_cash"]
    date = today_kst()
    start_live_tracking(date, balance)
    print(f"✅ 공식 기록 시작됨: {date}, 기준잔고 {balance:,.0f}원")
    print("이 시점부터 daily_report의 누적수익률이 여기서부터 다시 계산됩니다.")
    print("(그 이전 테스트 매매 손익은 실제 잔고엔 이미 반영돼 있지만 공식 성과에선 제외됨)")


if __name__ == "__main__":
    main()
