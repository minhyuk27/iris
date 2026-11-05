"""
iris v2 — 백테스트 거래일지 생성 (2026-07-30 신규).

230건 재백테스트(2026-07-26, `data/backtest/rebacktest_trades_full.csv`)를 사람이
읽기 쉬운 마크다운 표로 변환한다. 그날 어떤 종목을, 왜(거래량비율 순위) 얼마에
사서 얼마에 팔았는지, 손익이 얼마인지를 한 줄씩 정리하고 — **거래가 없었던
백테스트기간 거래일도 왜 없었는지**(갭밴드 통과 후보 0건 등) 함께 정리한다.
실거래 거래일지와는 완전히 별개(이 파일은 과거 시뮬레이션, 실제 돈 아님).

각 행에 **국면 태그**를 붙인다 — `strategy/iris_v2_params.py` 그리드서치 당시
쓰인 학습/홀드아웃 분할(`iris_v2_stage3.py`의 TRAIN_END=2026-03-31,
HOLDOUT_START=2026-04-01)을 그대로 재사용: 그 이전=**학습기간**(파라미터를
고를 때 참고한 데이터), 그 이후=**홀드아웃**(파라미터 확정 후 사후검증 데이터,
2026-07-24까지의 실제 신규 거래일 포함).

무거래일 사유의 갭은 이제 정확치(09:30 정밀분봉) — 2026-07-30 이전 버전은
로컬 일봉(`price_daily`의 `open` 필드, 09:00 시가에 가까움)으로 근사 재계산했으나,
사용자 지시("백테스트결과는분봉만으로정확하게해")에 따라 폐기했다. 지금은 Oracle에서
`price_minute` 압축 아카이브(57주치)를 09:30 시각으로 직접 추출해 만든
`data/backtest/all_candidates_precise_gap.csv`(예비후보 전원의 정밀갭, 거래 성사
여부 무관, 생성 스크립트는 Oracle `/tmp/precise_daily_summary.py`)를 그대로 쓴다.
230건 실제 트레이드와 동일한 09:30가 기준이라 근사 오차가 없다.

가격제한폭(±30%) 밖 갭은 데이터 아티팩트로 제외 — 이 세션 앞부분(대자본
전략 리서치)에서도 동일하게 확인한 문제: 액면분할/병합 등으로 일봉·분봉에
비현실적 단일일 등락률이 남아있다(코스닥 상하한가는 ±30%). 7,305건의 예비후보
중 401건(5.5%)이 |갭|>30%였고, 그대로 노출하면 무거래일 설명에 "갭범위
-90%~+287%" 같은 오해를 부르는 표시가 나온다 — `|gap|<=0.30`으로 필터링한 뒤
갭범위/통과건수를 계산한다.

증분 갱신(설계, 아직 cron 미등록): `data/backtest/rebacktest_trades_full.csv`
(실제 거래 230건)나 `data/backtest/all_candidates_precise_gap.csv`(무거래일
설명용 전체 후보)를 교체(새 분봉이 쌓여서 백테스트를 확장 재실행한 경우)한 뒤
이 스크립트를 다시 실행하면 전체가 재생성된다 — 수동 편집 금지.

실행: python3 -m scripts.iris_v2_backtest_journal
출력: reports/backtest_trade_journal.md
"""
import os
import pandas as pd

from core.db import get_conn
from strategy.iris_v2_params import GAP_MIN, GAP_MAX

CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "backtest", "rebacktest_trades_full.csv")
CAND_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "backtest", "all_candidates_precise_gap.csv")
OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "reports", "backtest_trade_journal.md")

TRAIN_END = "20260331"      # strategy/iris_v2_params.py 그리드서치와 동일 분할
HOLDOUT_START = "20260401"

MAX_ABS_GAP = 0.30  # 코스닥 상하한가(±30%) — 이 밖은 액면분할 등 데이터 아티팩트로 간주해 제외


def _phase(date: str) -> str:
    return "학습기간" if date <= TRAIN_END else "홀드아웃"


def _load_precise_candidates():
    """예비후보 전원의 09:30 정밀갭(all_candidates_precise_gap.csv), 아티팩트 제외."""
    df = pd.read_csv(CAND_PATH, dtype={"ticker": str, "date": str, "trade_date": str})
    before = len(df)
    df = df[df["gap"].between(-MAX_ABS_GAP, MAX_ABS_GAP)].copy()
    removed = before - len(df)
    if removed:
        print(f"[정보] 가격제한폭(±{MAX_ABS_GAP*100:.0f}%) 밖 아티팩트 {removed}건 제외 "
              f"({before}건 중)")
    return df


def build():
    df = pd.read_csv(CSV_PATH, dtype={"date": str, "ticker": str})
    df = df.sort_values("date").reset_index(drop=True)

    with get_conn() as conn:
        uni = pd.read_sql("SELECT ticker, name FROM universe", conn)
    df = df.merge(uni, on="ticker", how="left")
    df["name"] = df["name"].fillna(df["ticker"])

    n = len(df)
    win_rate = (df["pnl_net"] > 0).mean() * 100
    ev = df["pnl_net"].mean() * 100
    cum = ((1 + df["pnl_net"]).prod() - 1) * 100

    cand = _load_precise_candidates()

    with get_conn() as conn:
        real_rows = conn.execute(
            "SELECT DISTINCT date_open FROM positions WHERE strategy='iris_v2' AND mode='real' AND status='closed'"
        ).fetchall()
    real_trade_dates = {r["date_open"] for r in real_rows}

    # trade_date(실제 진입판단일) 기준 그룹핑 — 백테스트 트레이드의 date 컬럼과 동일 축.
    # 실거래 체결일(real_trade_dates)은 청산 시뮬레이션 데이터가 없어 cand에 없을 수
    # 있으므로(무거래로 오인 방지) 날짜 목록에 강제로 합쳐 넣는다.
    all_trade_dates = sorted(set(cand["trade_date"].unique()) | real_trade_dates)

    backtest_start = "20250626"  # 아카이브 시작(2025-W26)
    relevant_trade_dates = [d for d in all_trade_dates if d >= backtest_start]

    lines = []
    lines.append("# iris v2.5 백테스트 거래일지")
    lines.append("")
    lines.append("> **자동 생성 파일 — 직접 수정하지 말 것.** "
                  "`python3 -m scripts.iris_v2_backtest_journal`로 재생성됨. "
                  "원자료: `data/backtest/rebacktest_trades_full.csv`(2026-07-26 재백테스트, "
                  "230건, 실거래와 무관한 과거 시뮬레이션) + "
                  "`data/backtest/all_candidates_precise_gap.csv`(무거래일 설명용, 09:30 정밀분봉 "
                  "기준 — 근사치 아님). 가격제한폭(±30%) 밖 갭은 데이터 아티팩트로 제외.")
    lines.append("")
    trade_end_fmt = pd.to_datetime(df["date"].max(), format="%Y%m%d").strftime("%Y-%m-%d")
    list_end_fmt = pd.to_datetime(relevant_trade_dates[-1], format="%Y%m%d").strftime("%Y-%m-%d")
    lines.append(f"- 거래건수: **{n}건** (2025-06-26 ~ {trade_end_fmt}) · "
                 f"무거래일 포함 전체 목록은 {list_end_fmt}까지 확장")
    lines.append(f"- 거래당 평균수익률(net, 왕복비용 0.3% 반영): **{ev:+.2f}%**")
    lines.append(f"- 승률: **{win_rate:.1f}%**")
    lines.append(f"- 복리누적: **{cum:+.1f}%**")
    lines.append(f"- 국면 분할: **학습기간**(~{TRAIN_END}) / **홀드아웃**({HOLDOUT_START}~) "
                 "— strategy/iris_v2_params.py 그리드서치 당시 분할과 동일")
    lines.append("")
    lines.append("| 날짜 | 국면 | 종목/결과 | 진입사유 | 갭 | 매수가 | 매도가 | 청산사유 | 손익률(net) |")
    lines.append("|---|---|---|---|---|---|---|---|---|")

    trade_by_target = {r["date"]: r for _, r in df.iterrows()}

    for td in relevant_trade_dates:
        phase = _phase(td)
        d_fmt = pd.to_datetime(td, format="%Y%m%d").strftime("%Y-%m-%d")
        if td in trade_by_target:
            r = trade_by_target[td]
            reason = f"거래량비율 {r['vol_ratio']:.1f}배(당일 1위)"
            gap_pct = f"{r['gap']*100:+.2f}%"
            entry = f"{r['entry_price']:,.0f}"
            exit_p = f"{r['exit_price']:,.0f}"
            pnl = f"{r['pnl_net']*100:+.2f}%"
            lines.append(f"| {d_fmt} | {phase} | {r['name']}({r['ticker']}) | {reason} | {gap_pct} | "
                         f"{entry} | {exit_p} | {r['exit_type']} | {pnl} |")
        elif td in real_trade_dates:
            lines.append(f"| {d_fmt} | {phase} | **실거래 진입일** | "
                         f"청산 시뮬레이션 데이터 없음(이 날은 실제 체결, `reports/real_trade_journal.md` 참조) "
                         f"| - | - | - | - | - |")
        else:
            day_cands = cand[cand["trade_date"] == td]
            n_cand = len(day_cands)
            if n_cand == 0:
                lines.append(f"| {d_fmt} | {phase} | **무거래** | 후보 자체 없음 | - | - | - | - | - |")
                continue
            gaps = day_cands["gap"]
            passed = ((gaps >= GAP_MIN) & (gaps <= GAP_MAX)).sum()
            lines.append(f"| {d_fmt} | {phase} | **무거래** | "
                         f"후보 {n_cand}개, 갭밴드({GAP_MIN*100:.0f}~{GAP_MAX*100:.0f}%) 통과 {passed}건 "
                         f"(갭범위 {gaps.min()*100:+.1f}%~{gaps.max()*100:+.1f}%) | - | - | - | - | - |")

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"작성 완료: {OUT_PATH} ({n}건 거래 / {len(relevant_trade_dates)}거래일)")


if __name__ == "__main__":
    build()
