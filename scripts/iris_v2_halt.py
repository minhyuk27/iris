"""
iris v2 — halt 상태 확인/해제 (2026-07-22 신규, 2026-08-08 날짜무관 스캔 추가).
실거래(TRADE_MODE=real) 중 주문 체결을 확인 못 하면 core.order_exec.set_halt로 그날
자동매매가 전부 멈춘다. 이 스크립트로 halt 상태를 확인하고, KIS 앱 등에서 실제 상황을
직접 확인한 뒤 문제없으면 수동으로 해제한다.

**2026-08-08 변경**: halt 키가 날짜별이라 예전엔 --date 미지정시 "오늘"만 봤음 —
어제 이전에 발생한 halt가 안 풀린 채 남아있어도 이 스크립트로도 못 봤다(코드감사에서
발견). --date 미지정시 이제 미해결 halt를 날짜 무관하게 전부 스캔한다(가장 최근 것).

실행:
  python3 -m scripts.iris_v2_halt              # 미해결 halt 전체 스캔(날짜 무관)
  python3 -m scripts.iris_v2_halt --clear       # 위에서 찾은 halt 해제
  python3 -m scripts.iris_v2_halt --date 20260722 [--clear]  # 특정 날짜만 지정
"""
import argparse

from core.order_exec import halt_key, is_halted, find_unresolved_halt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="YYYYMMDD, 미지정시 미해결 halt 전체 스캔")
    ap.add_argument("--clear", action="store_true", help="halt 해제(반드시 실제 계좌 상태를 직접 확인한 뒤 실행할 것)")
    args = ap.parse_args()

    if args.date:
        date = args.date
        halt = is_halted(date)
    else:
        found = find_unresolved_halt()
        if found is None:
            print("미해결 halt 없음(오늘 포함 전체 스캔) — 정상")
            return
        date = found["date"]
        halt = found

    if halt is None:
        print(f"{date}: halt 상태 아님 — 정상")
        return

    print(f"{date}: \U0001F6A8 halt 상태")
    print(f"  종목: {halt.get('ticker')}")
    print(f"  사유: {halt.get('action')}")
    print(f"  상세: {halt.get('detail')}")
    print(f"  발생시각: {halt.get('time')}")

    if args.clear:
        from core.db import get_conn
        with get_conn() as conn:
            conn.execute("DELETE FROM bot_state WHERE key=?", (halt_key(date),))
        print(f"\n{date}: halt 해제 완료 — 다음 크론 실행부터 자동매매 재개됩니다.")
        print("주의: positions 테이블의 해당 종목 상태(shares/status 등)가 실제 계좌와")
        print("일치하는지 먼저 확인했어야 합니다. 불일치하면 직접 UPDATE로 맞춘 뒤 해제하세요.")
    else:
        print("\n해제하려면: python3 -m scripts.iris_v2_halt --clear")
        print("(먼저 KIS 앱/HTS에서 실제 잔고·체결내역을 확인하고, positions 테이블과")
        print(" 일치시킨 뒤 해제할 것 — 확인 없이 그냥 해제하면 이중주문 위험이 있습니다)")


if __name__ == "__main__":
    main()
