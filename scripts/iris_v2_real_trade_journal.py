"""
iris v2 — 실거래 거래일지 생성 (2026-07-30 신규, TRADE_MODE=real 전용).

`positions`(mode='real', status='closed')에서 체결된 거래를 뽑고, **거래가
없었던 실제 거래일도 왜 없었는지**(갭밴드 통과 후보 0건, 자본부족 등) 함께
정리한다.

⚠️ **왜 candidates 테이블이 아니라 로그를 파싱하는가**: Oracle→Odroid 야간동기화
(`sync_to_iris.py`)가 `candidates`를 "전체교체(DELETE ALL+INSERT ALL)" 테이블로
취급해서, Odroid가 그날 저녁 직접 써넣은 iris_v2 후보 기록이 다음날 19:00
동기화 때 지워진다(2026-07-30 발견, 상세는 iris DECISIONS 참조 — Oracle 쪽
sync_to_iris.py 수정 필요, 아직 미반영). 그래서 과거 날짜의 "왜 후보가 없었나/
왜 진입 안 했나"는 DB가 아니라 `logs/iris_v2_entry.log`(로그는 안 지워짐)를
파싱해서 복원한다. 실제 매매판단은 항상 09:29~09:31 사이에만 찍힌다(그 시각
cron만 진짜 실행) — 그 시간대 밖의 로그(수동 테스트·paper모드 실험 흔적)는
전부 걸러낸다. 날짜는 "정밀대기 종료"가 아니라 그날의 **최종 판단 로그 줄**
(밴드통과없음/자본부족/진입 중 하나, 셋 다 날짜가 메시지에 박혀있음)에서
직접 추출한다 — v2.5.3(정밀대기 지연로그) 이전 날짜(07-23·07-24)엔 "정밀대기
종료" 줄 자체가 없어서 그것만 보면 그 이틀을 놓친다.

주말·공휴일은 로그 자체가 없어(휴장일엔 entry.py가 조용히 종료) 자동 제외된다.
각 행에 **국면 태그**(테스트기간/공식기록, `iris_v2_start_tracking` 기준)를 붙인다.
백테스트 거래일지와는 완전히 별개(이건 진짜 내 돈, 실제 손익).

**자동 갱신**: crontab에 `iris_v2_daily_report`(13:05) 직후 13:06 등록 제안 —
사용자 확인 후 등록. 이 스크립트 자체는 등록 여부와 무관하게 언제든 바로 실행
가능.

실행: python3 -m scripts.iris_v2_real_trade_journal
출력: reports/real_trade_journal.md
"""
import os
import re
from collections import OrderedDict

import pandas as pd

from core.db import get_conn, get_live_tracking

LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "logs", "iris_v2_entry.log")
OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "reports", "real_trade_journal.md")

REAL_HHMM = ("09:29", "09:30", "09:31")

RE_LINE = re.compile(r"^\d{4}-\d{2}-\d{2} (\d\d:\d\d):\d\d \[(\w+)\] (.*)$")
RE_CAND = re.compile(r"^\s*#([\d.]+) (\d{6}) (\S+) gap=(-?[\d.]+)")
RE_NO_ENTRY = re.compile(r"^(\d{8}) 밴드\(([-\d.]+)~([-\d.]+)\) 통과 후보 없음.*?시세조회실패 (\d+)/(\d+)건")
RE_ZERO_CAND = re.compile(r"^(\d{8}) 후보 없음\(전날 저녁 배치 미실행 또는 0건\)")
RE_CAPITAL_SHORT = re.compile(r"^(\d{8}) (\d{6}) 진입가 대비 자본 부족")
RE_ENTER_REAL = re.compile(r"^진입: (\d{6}) (\S+) gap=(-?[\d.]+).*?\[TRADE_MODE=real")
RE_IDEMPOTENT = re.compile(r"^(\d{8}) 이미 진입판단 완료")


def _dedup_candidates(candidates: list) -> list:
    """cron 재실행/이중로깅으로 같은 (ticker) 줄이 중복 기록되는 경우 제거."""
    seen = set()
    out = []
    for c in candidates:
        ticker = c[0]
        if ticker in seen:
            continue
        seen.add(ticker)
        out.append(c)
    return out


def _phase(date: str, tracking: dict | None) -> str:
    if tracking and date >= tracking["date"]:
        return "공식기록"
    return "테스트기간"


def parse_entry_log() -> dict:
    """실제(09:29~09:31) 판단 블록만 골라 {date: {candidates, outcome}} 반환.
    날짜는 그날의 최종 판단줄(밴드없음/자본부족/진입/idempotent)에서 직접 추출 —
    '정밀대기 종료' 줄 유무와 무관하게 항상 동작한다."""
    per_day = OrderedDict()
    pending_candidates = []
    resolved_dates_today_block = set()

    if not os.path.exists(LOG_PATH):
        return per_day

    with open(LOG_PATH, encoding="utf-8", errors="ignore") as f:
        for raw in f:
            m = RE_LINE.match(raw)
            if not m or m.group(1) not in REAL_HHMM:
                continue
            msg = m.group(3)

            mc = RE_CAND.match(msg)
            if mc:
                pending_candidates.append((mc.group(2), mc.group(3), float(mc.group(4))))
                continue

            mi = RE_IDEMPOTENT.match(msg)
            if mi:
                pending_candidates = []  # 중복 재실행(idempotency) — 새 블록으로 취급 안 함
                continue

            mz = RE_ZERO_CAND.match(msg)
            if mz:
                d = mz.group(1)
                if d not in per_day:
                    per_day[d] = {
                        "candidates": [], "outcome": "후보없음",
                        "gap_min": None, "gap_max": None, "total": 0, "detail": None,
                    }
                pending_candidates = []
                continue

            mn = RE_NO_ENTRY.match(msg)
            if mn:
                d = mn.group(1)
                if d not in per_day:
                    per_day[d] = {
                        "candidates": _dedup_candidates(pending_candidates), "outcome": "밴드통과없음",
                        "gap_min": float(mn.group(2)), "gap_max": float(mn.group(3)),
                        "total": int(mn.group(5)), "detail": None,
                    }
                pending_candidates = []
                continue

            mcap = RE_CAPITAL_SHORT.match(msg)
            if mcap:
                d, ticker = mcap.group(1), mcap.group(2)
                if d not in per_day:
                    per_day[d] = {
                        "candidates": _dedup_candidates(pending_candidates), "outcome": "자본부족",
                        "detail": ticker, "gap_min": None, "gap_max": None, "total": len(pending_candidates),
                    }
                pending_candidates = []
                continue

            me = RE_ENTER_REAL.match(msg)
            if me:
                # 체결정보(매수/매도가·손익)는 positions 테이블(DB)이 신뢰가능한 원천이라 그대로 씀.
                # 다만 그날의 후보 갭 목록(extend_precise_candidates가 백테스트 CSV 연장에 씀)은
                # 로그에만 있으므로, 줄 자체의 타임스탬프(raw[:10])에서 날짜를 직접 뽑아 기록한다.
                d = raw[:10].replace("-", "")
                if d not in per_day:
                    per_day[d] = {
                        "candidates": _dedup_candidates(pending_candidates), "outcome": "진입성공",
                        "detail": me.group(1), "gap_min": None, "gap_max": None,
                        "total": len(pending_candidates),
                    }
                pending_candidates = []
                continue

    return per_day


def build():
    with get_conn() as conn:
        trades = conn.execute(
            """SELECT date_open, ticker, name, entry_price, exit_price, score,
                      pnl_pct, pnl_amount, exit_reason, partial_exit_price
               FROM positions
               WHERE strategy='iris_v2' AND mode='real' AND status='closed'
               ORDER BY date_open"""
        ).fetchall()

    tracking = get_live_tracking()
    log_by_day = parse_entry_log()
    trades_by_day = {r["date_open"]: r for r in trades}

    real_days = sorted(set(log_by_day) | set(trades_by_day))
    real_days = [d for d in real_days if d >= "20260723"]

    lines = []
    lines.append("# iris v2.5 실거래 거래일지 (실제 KIS 계좌, TRADE_MODE=real)")
    lines.append("")
    lines.append("> **자동 생성 파일 — 직접 수정하지 말 것.** "
                  "`python3 -m scripts.iris_v2_real_trade_journal`로 재생성됨. "
                  "체결정보는 `positions`(`mode='real'`), 무진입 사유는 "
                  "`logs/iris_v2_entry.log`(09:29~09:31 실제판단만) 파싱 — "
                  "candidates 테이블은 야간동기화로 유실되는 버그가 있어 안 씀"
                  "(상세는 [[iris DECISIONS]] 2026-07-30 참조). "
                  "백테스트 거래일지와 무관, 전부 실제 손익. 주말·공휴일은 로그 자체가 없어 자동 제외.")
    lines.append("")

    if trades:
        n = len(trades)
        win_rate = sum(1 for r in trades if r["pnl_pct"] and r["pnl_pct"] > 0) / n * 100
        total_amount = sum(r["pnl_amount"] or 0 for r in trades)
        avg_pct = sum(r["pnl_pct"] or 0 for r in trades) / n
        lines.append(f"- 실거래 체결: **{n}건** / 확인된 실제 판단일 {len(real_days)}일 중")
        lines.append(f"- 거래당 평균수익률: **{avg_pct:+.2f}%**  ·  승률: **{win_rate:.1f}%**")
        lines.append(f"- 누적 실현손익: **{total_amount:+,.0f}원**")
    else:
        lines.append(f"- 아직 종결된 실거래 없음 (확인된 실제 판단일 {len(real_days)}일)")
    if tracking:
        lines.append(f"- 공식기록 시작일: **{tracking['date']}** (그 전=테스트기간, 그 후=공식기록)")
    else:
        lines.append("- 공식기록 아직 미시작 — 전부 **테스트기간**")
    lines.append("")
    lines.append("| 날짜 | 국면 | 종목/결과 | 진입사유 | 매수가 | 매도가 | 청산사유 | 수익률 | 손익금액 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")

    for d in real_days:
        phase = _phase(d, tracking)
        d_fmt = pd.to_datetime(d, format="%Y%m%d").strftime("%Y-%m-%d")
        if d in trades_by_day:
            r = trades_by_day[d]
            reason = f"거래량비율 {r['score']:.2f}배(당일 1위)" if r["score"] is not None else "-"
            entry = f"{r['entry_price']:,.0f}"
            if r["partial_exit_price"]:
                exit_p = f"{r['partial_exit_price']:,.0f}(부분)+{r['exit_price']:,.0f}"
            else:
                exit_p = f"{r['exit_price']:,.0f}" if r["exit_price"] else "-"
            pnl_pct_s = f"{r['pnl_pct']:+.2f}%" if r["pnl_pct"] is not None else "-"
            pnl_amt_s = f"{r['pnl_amount']:+,.0f}원" if r["pnl_amount"] is not None else "-"
            lines.append(f"| {d_fmt} | {phase} | {r['name']}({r['ticker']}) | {reason} | {entry} | "
                         f"{exit_p} | {r['exit_reason'] or '-'} | {pnl_pct_s} | {pnl_amt_s} |")
            continue

        info = log_by_day.get(d)
        if not info:
            lines.append(f"| {d_fmt} | {phase} | **무진입** | 사유 미확인(로그 없음) | - | - | - | - | - |")
        elif info["outcome"] == "후보없음":
            lines.append(f"| {d_fmt} | {phase} | **무진입** | "
                         f"전날 저녁 예비후보 자체가 0개(TV70억↑·거래량비율2배↑·자기50일선위·"
                         f"모멘텀10%↑ 전부 통과한 종목 없음) | - | - | - | - | - |")
        elif info["outcome"] == "자본부족":
            lines.append(f"| {d_fmt} | {phase} | **무진입** | "
                         f"{info['detail']} 갭통과했으나 진입가 대비 자본 부족(사이징=0주) | - | - | - | - | - |")
        elif info["outcome"] == "밴드통과없음":
            cands = info["candidates"]
            gaps = [c[2] for c in cands] if cands else None
            gap_txt = f"(갭범위 {min(gaps)*100:+.1f}%~{max(gaps)*100:+.1f}%)" if gaps else ""
            lines.append(f"| {d_fmt} | {phase} | **무진입** | "
                         f"후보 {info['total']}개, 갭밴드({info['gap_min']*100:.0f}~{info['gap_max']*100:.0f}%) "
                         f"통과 0건{gap_txt} | - | - | - | - | - |")
        else:
            lines.append(f"| {d_fmt} | {phase} | **무진입** | 사유 미확인 | - | - | - | - | - |")

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"작성 완료: {OUT_PATH} ({len(trades)}건 체결 / {len(real_days)}일)")


if __name__ == "__main__":
    build()
