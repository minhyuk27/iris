"""
iris v2 — sync_to_iris.py candidates 버그 수정 검증 + 전체 시스템 점검 (2026-07-30, 1회성).

2026-07-30 발견한 Oracle sync_to_iris.py의 candidates 전체교체 버그(Odroid의
iris_v2 실거래 후보 기록이 매일 19:00 동기화 때 지워지던 것)를 같은 날 수정·적용함
(iris DECISIONS 2026-07-30 참조). 이 스크립트는 다음날(2026-07-31) 그 수정이
실제로 먹혔는지 — candidates 테이블에 iris_v2 후보가 여러 날짜로 누적되어
있는지 — 확인하고, 기존 iris_v2_diag의 전체 점검 내용도 함께 텔레그램으로 보낸다.
읽기전용, 아무것도 바꾸지 않음. 1회성 — 상시 cron 아님(`at`으로 한 번만 예약).

실행: python3 -m scripts.iris_v2_sync_fix_verify
"""
import io
import contextlib

from core.db import get_conn
from core.notify import send_telegram, esc
import scripts.iris_v2_diag as diag


def check_sync_fix() -> str:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT trade_date, COUNT(*) c FROM candidates WHERE strategy='iris_v2' "
            "GROUP BY trade_date ORDER BY trade_date"
        ).fetchall()
    dates = [(r["trade_date"], r["c"]) for r in rows]
    lines = ["=== sync_to_iris.py candidates 버그 수정 검증 (2026-07-30 적용) ==="]
    if len(dates) >= 2:
        lines.append(f"✅ 성공 — iris_v2 후보가 {len(dates)}개 날짜로 누적됨 "
                      f"(수정 전엔 최근 1일치만 남았음):")
    else:
        lines.append(f"🚨 실패 — 여전히 {len(dates)}개 날짜만 남음. 버그가 재발했거나 "
                      f"수정이 적용 안 됐을 가능성(Oracle sync_to_iris.py 재확인 필요).")
    for d, c in dates:
        lines.append(f"  {d}: {c}건")
    return "\n".join(lines)


def main():
    sync_check = check_sync_fix()

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        diag.main()
    diag_output = buf.getvalue()

    full_report = sync_check + "\n\n" + "=== iris_v2_diag 전체 점검 ===" + diag_output
    print(full_report)
    send_telegram(f"<b>iris v2.5 — 자동점검 (sync버그 검증 포함)</b>\n\n"
                  f"<pre>{esc(full_report)}</pre>")


if __name__ == "__main__":
    main()
