"""
iris v3 파라미터 — 갭업[15~30%) 반락→오버나잇롱.

값 변경 시 반드시:
  1. IRIS_V3_VERSION bump
  2. 변경된 상수에 버전+날짜 주석 추가
  3. Obsidian iris DECISIONS.md에 변경 이유/근거 기록

버전 명명규칙은 iris_v2_params.py와 동일(A.B.C, A=핵심접근방식 고정 "3",
B=신호·판단로직 변경, C=결과에 영향없는 기술수정).

positions/candidates 테이블에 strategy='iris_v3'로 기록되어 v2와 같은
테이블을 공유하면서도 완전히 구분된다(core/db.py의 strategy 컬럼 관례).
"""

IRIS_V3_VERSION = "3.0.2"
# 2026-08-25(v3.0.2, C급): positions.score(=매수당시 갭%) 기록 버그 수정 —
# bot/iris_v3_entry.py의 set_pending_entry()·페이퍼모드 _record_entry()와
# bot/iris_v3_confirm_entry.py의 _record_entry() 전부 score 컬럼을 안 채우고
# 있어서(INSERT문에 컬럼 자체가 없었음) 실거래 확정된 포지션마다 score가
# 항상 DB 기본값 0으로 남아있었음(08-24 첫 실거래 100790에서 발견 — 실제
# 매수 당시 갭 22.28%인데 거래일지엔 +0.00%로 표시됨). 손익·거래판단·
# 사이징 등 실거래 로직 자체엔 전혀 영향 없는 순수 기록버그(score는 표시·
# 사후분석 용도로만 쓰임). pending_entry dict에 "gap" 키 추가 + 양쪽
# _record_entry()에 gap 파라미터 추가해 INSERT에 포함시켜 수정. 회귀테스트
# 4건 추가(paper/real 양쪽 기록 확인 + 구버전 pending 하위호환 확인).
# 기존에 이미 score=0으로 잘못 기록된 과거 행(08-24 100790 등)은 DB에서
# 직접 UPDATE로 보정(상세 iris DECISIONS 2026-08-25).
# 2026-08-18: 최초 구현. 신호 자체는 2026-08-17 밤 자율연구에서 발견돼
# 실현가능성 감사(①~⑯)·실행설계 검증(⑰~㉒)까지 전부 끝난 뒤(iris
# DECISIONS 2026-08-17(11~18차)·2026-08-18(1~25차) 참조) 그대로 코드로
# 옮긴 것 — 파라미터 값 자체는 이미 그 검증에서 확정된 값을 그대로 사용,
# 이번 커밋에서 새로 튜닝한 값 없음.
# 2026-08-18(v3.0.1, C급, 실거래 개시(08-19) 전날 최종 코드감사): 이중매도/
# 이중매수 위험 2건 발견·수정(신호·파라미터 무변경, 실행안전성만) —
# ①bot/iris_v3_entry.py의 open-포지션 체크가 date_open=오늘로만 걸러
# 어제자 포지션이 pending으로 아직 열려있어도 못 잡던 문제(오버나잇
# 보유 구조상 v2엔 없던 v3 고유 리스크) ②core/order_exec_v3.py의
# pending_exit 키가 exit.py 실행일 기준이라, pending이 자정을 넘기면
# 다음날 재실행에서 못 찾아 새 매도주문을 또 낼 위험 — "1일1종목"
# 불변식을 이용해 날짜무관 고정키로 변경. 상세: iris DECISIONS
# 2026-08-18.

# ── 후보 필터(저녁 사전필터 + 장시작 직후 갭확정, 2단계) ────────────────
TV_MA5_LOOKBACK = 5           # 거래대금 이동평균 기간(거래일)
TV_MA5_MIN = 12_500_000_000   # 125억원 — iris DECISIONS 2026-08-18(6차),
                               # 200억보다 EV·거래량·MDD 전부 우월해 채택
GAP_MIN, GAP_MAX = 0.15, 0.30  # 오늘시가/전일종가-1, [15%,30%) — 상한 근접
                                # 거래가 반락폭도 커서 회복도 밴드상한 근처에서
                                # 막힘(95%ile≈99%ile), 30% 자체는 자연스러운 컷

# ── 청산 ──────────────────────────────────────────────────────────────
# v3는 v2와 달리 청산사유가 "익일 시가" 단 하나뿐 — TP/SL/중간평가 없음
# (iris v3 다각도 전문가 분석.md 6번 관점, 매분 모니터링 자체가 불필요).
SELL_TIMEOUT_SECS = 180  # 지정가 매도 시도 후 이 시간 지나면 시장가 전환
                          # (iris DECISIONS 2026-08-18(20차): 3분이 실측상
                          # 최적점 — 1~2분은 잔량이 많이 남아 오히려 나쁨)

# ── 체결·사이징 ───────────────────────────────────────────────────────
# 매수: 조건부지정가(core.kis_api.buy_conditional_limit, ORD_DVSN=02).
# 매도: 지정가(core.order_exec.sell_safe_limit) + SELL_TIMEOUT_SECS 후
#       core.kis_api.revise_to_market()로 잔량 시장가 전환.
PARTICIPATION_CAP = 0.20
# ⚠️ 잠정치(iris DECISIONS 2026-08-18(20차) TWAP 시뮬레이션에서 쓴 값) —
# 호가창(오더북) 데이터 없이는 정밀화 불가, 페이퍼트레이딩 단계 실측
# 필요. 현재 코드에서는 참고용 상수일 뿐 직접 소비하는 로직 없음(조건부
# 지정가는 거래소가 알아서 동시호가로 흡수하므로 수동 트랑쉬분할 불필요 —
# 이 캡을 실제로 쓰는 건 매도측 sell_safe_limit 실측튜닝 시점).

CAPITAL_PER_TRADE = 1_000_000
# 2026-08-18: 소액테스트 시작값(100만원) — 목표금액은 3,000만원이지만
# "소액부터 단계적 확대"가 명시적 결정(iris DECISIONS 2026-08-18(17차)
# 최종권장구성, v2의 10만원 스모크테스트 선례를 따름). 2026-08-18 사용자가
# "v2·v3 모두 100만원 고정금으로 2주 테스트"를 명시적으로 확정(v2도 동일
# 시점에 fixed 100만원으로 전환) — 이 값으로 cron 등록 및 실거래 개시.

ROUNDTRIP_COST = 0.003  # 백테스트 검증용 참고치(실거래 손익은 실제
                         # 체결가 기준 자동 반영, 이 상수를 직접 빼지 않음)

EXCL_KW = ["ETF", "ETN", "스팩", "리츠", "SPAC", "인버스", "레버리지"]
EXCL_SECTOR_KW = ["ETF", "ETN", "리츠", "REITs", "스팩", "SPAC"]
EXCL_SUFFIX = ("우", "우B", "1우", "2우", "우C")


def is_excluded(name, sector: str = "") -> bool:
    # strategy/iris_v2_params.py의 is_excluded()와 동일 로직(2026-07-22
    # NaN 크래시 수정 포함) — v3도 같은 유니버스를 다루므로 그대로 복제.
    name = name if isinstance(name, str) else ""
    sector = sector if isinstance(sector, str) else ""
    if any(k in name for k in EXCL_KW):
        return True
    if any(k in sector for k in EXCL_SECTOR_KW):
        return True
    if name.endswith(EXCL_SUFFIX):
        return True
    return False
