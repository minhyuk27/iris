"""
iris v2 — 백테스트 후보갭 CSV 일일 연장 (2026-07-30 신규).

`data/backtest/all_candidates_precise_gap.csv`(과거 Oracle 09:30 정밀분봉 재구성,
2026-07-24까지)를 매 거래일 자동으로 하루씩 늘린다. 무거운 Oracle 분봉 재백테스트
파이프라인(~9분) 없이, 실거래봇이 그날 아침 09:29~09:31에 이미 실시간으로 조회한
09:30 정밀갭 로그(`logs/iris_v2_entry.log`의 `#순위 종목 gap=...` 줄, `real_trade_journal`이
쓰는 것과 동일 파서)를 그대로 재사용한다 — 사용자 지적대로 "전날 필터링한 후보들과
09:30 갭업 기준만 있으면 가볍게" 확장 가능.

⚠️ 컬럼 일부 결측: 로그엔 ticker/gap만 있고 prev_close/vol_ratio/tv/mom5/date(D-1
신호일)는 없음 — 이 스크립트가 추가하는 행은 그 컬럼들이 빈 값이다. 백테스트
일지(`iris_v2_backtest_journal.py`)는 무거래일 요약에 `gap`과 `trade_date`만
쓰므로 문제없음.

멱등성: 이미 CSV에 있는 trade_date는 다시 추가하지 않음(중복 실행 안전).

**자동 실행**: crontab에 real_trade_journal(13:06) 직후 13:07 등록, 이어서
iris_v2_backtest_journal도 재생성해 새 날짜가 반영되도록 함(사용자 확인 후 등록).

실행: python3 -m scripts.iris_v2_extend_precise_candidates
"""
import os

import numpy as np
import pandas as pd

from scripts.iris_v2_real_trade_journal import parse_entry_log

CAND_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "backtest", "all_candidates_precise_gap.csv")


def extend():
    df = pd.read_csv(CAND_PATH, dtype={"ticker": str, "date": str, "trade_date": str})
    existing_dates = set(df["trade_date"])

    log_by_day = parse_entry_log()
    new_rows = []
    for d, info in log_by_day.items():
        if d in existing_dates:
            continue
        if info["outcome"] == "진입성공":
            # 실제 체결일은 real_trade_journal.md가 이미 정확한 매수/매도가·손익을
            # 기록함 — 여기선 청산 시뮬레이션 데이터가 없어 "무거래"로 잘못
            # 표시될 수 있으므로 백테스트 CSV 연장 대상에서 제외.
            continue
        for ticker, name, gap in info["candidates"]:
            new_rows.append({
                "ticker": ticker, "date": np.nan, "trade_date": d,
                "prev_close": np.nan, "vol_ratio": np.nan, "tv": np.nan, "mom5": np.nan,
                "price_0930": np.nan, "gap": gap,
            })

    if not new_rows:
        print("추가할 신규 거래일 없음(이미 최신 또는 로그 없음)")
        return

    added_dates = sorted(set(r["trade_date"] for r in new_rows))
    df = pd.concat([df, pd.DataFrame(new_rows)], ignore_index=True)
    df.to_csv(CAND_PATH, index=False)
    print(f"연장 완료: {len(new_rows)}건 추가, 신규 거래일 {added_dates}")


if __name__ == "__main__":
    extend()
