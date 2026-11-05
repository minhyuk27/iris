"""
iris v2/v3 — 거래일지를 Obsidian 볼트에도 복사 (2026-07-30 신규, 2026-08-18 v3 추가).

`reports/*.md`(코드 레포, 자동생성)는 원본 그대로 두고, 볼트에서도 볼 수
있도록 프론트매터를 붙여 `1 Project/iris/분석/`에 복사본을 만든다. 볼트
쪽은 읽기 전용 뷰 — 직접 수정하면 다음 동기화 때 덮어써짐. 원본(레포)이
항상 정본.

실행: python3 -m scripts.iris_v2_sync_journals_to_vault
"""
import os
from datetime import datetime

from core.timeutil import now_kst

VAULT_DIR = os.path.expanduser("~/obsidian-vault/1 Project/iris/분석")

JOBS = [
    (
        os.path.join(os.path.dirname(__file__), "..", "reports", "backtest_trade_journal.md"),
        os.path.join(VAULT_DIR, "iris 백테스트 거래일지.md"),
        "iris 백테스트 거래일지",
    ),
    (
        os.path.join(os.path.dirname(__file__), "..", "reports", "real_trade_journal.md"),
        os.path.join(VAULT_DIR, "iris 실거래 거래일지.md"),
        "iris 실거래 거래일지",
    ),
    (
        os.path.join(os.path.dirname(__file__), "..", "reports", "v3_real_trade_journal.md"),
        os.path.join(VAULT_DIR, "iris v3 실거래 거래일지.md"),
        "iris v3 실거래 거래일지",
    ),
]


def sync():
    updated = now_kst().strftime("%Y-%m-%d %H:%M")
    for src, dst, title in JOBS:
        if not os.path.exists(src):
            print(f"[스킵] 원본 없음: {src}")
            continue
        with open(src, encoding="utf-8") as f:
            body = f.read()
        frontmatter = (
            "---\n"
            f"title: {title}\n"
            f"updated: {updated}\n"
            "---\n\n"
            "> 자동 복사본 — 직접 수정하지 말 것. 정본은 코드 레포 "
            f"`iris/{os.path.relpath(src, os.path.dirname(__file__) + '/..')}` "
            "(`iris_v2_backtest_journal.py`/`iris_v2_real_trade_journal.py`가 생성, "
            "매일 13:06~13:07 cron으로 갱신).\n\n"
        )
        os.makedirs(VAULT_DIR, exist_ok=True)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(frontmatter + body)
        print(f"동기화 완료: {dst}")


if __name__ == "__main__":
    sync()
