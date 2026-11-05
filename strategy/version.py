"""
전략 파라미터 버전

값 변경(그리드서치 재최적화 등) 시 반드시:
  1. STRATEGY_VERSION을 bump
  2. 변경된 상수에 버전+날짜 주석 추가
  3. Obsidian iris DECISIONS.md에 변경 이유/근거 기록

positions / strategy_perf / candidates 테이블에
strategy_version으로 기록되어, 어떤 파라미터로 거래했는지 추적 가능.
(iris v1 파라미터 버전 — iris v1은 2026-07-22 은퇴·아카이브됨, 이 파일 자체가
지금 어디서도 import 안 되는 죽은 코드임을 2026-08-08 K3/K4 DB 정리 중 확인,
iris DECISIONS 참조)
"""

STRATEGY_VERSION = "1.1"   # 2026-06-15: v1.0(갭1.5~5%·익절+15%·손절-6%·09:55진입·14:30강제청산)
# + cp2 체크포인트(12:30, 진입 이후 (고가-저가)/시가 범위가 2~6%면 그 시점 가격으로 즉시 익절).
# run_daily_backtest 16일 기준 EV +2.196%→+2.652%, 누적 +35.80%→+46.29%, MaxDD 15.0%→12.7%
# (0종목변경, 그리드 16/16·이웃강건 15/16). 상세: Obsidian iris DECISIONS 2026-06-15
