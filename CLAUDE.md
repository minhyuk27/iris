# CLAUDE.md — IRIS (Odroid, 🔴 2026-08-19 실거래봇 oracle1로 완전 이전 — 이 사본은 참고용, 더 이상 실행 안 됨)

**🔄 2026-09-07 추가**: `stellaria_research/` 서브디렉터리가 새로 생겼다 —
독립 프로젝트였던 stellaria(자동화매매 전략 백테스트 연구, a1~a6)가
iris의 연구/후보 트랙으로 편입되며 `~/stellaria/`에서 물리이동된
것(자체 git 히스토리 보유, 이 상위 iris 레포와는 독립된 별개 repo).
가이드는 `stellaria_research/CLAUDE.md` 참조, vault는 `1 Project/
iris/stellaria 연구/`. 아래 본문은 이 서브디렉터리와 무관.

**⚠️ 2026-08-19 20:40 KST — iris 실거래봇이 oracle1로 완전히 이전됐다.**
Odroid의 크론은 전부 제거됐고, 이 디렉터리(`/home/odroid/iris`)는 이제
실행되지 않는 참고용 사본이다. 실제 운영 코드·`.env`·크론은
`oracle1(ubuntu@100.78.233.125)`의 `/home/ubuntu/iris`에 있다 —
그쪽 `CLAUDE.md`가 최신. 이전 경위는 obsidian-vault `iris DECISIONS.md`
2026-08-19 (2차) 참조. 아래 본문은 이전 전(Odroid가 실거래봇이던 시절)
기준으로 낡아있으니 "이 서버"·"Odroid" 언급은 전부 oracle1로 치환해서
읽을 것.

---

이 파일은 (이전 전) Odroid 서버에서 Claude Code가 `~/iris` 작업 시 읽던 가이드.
**2026-07-24 전면 재작성 — 이전 버전은 iris v1(월간 리밸런싱) 기준으로 완전히 낡아있었음.**

---

## 서버 및 프로세스 명명

| 이름 | 실체 |
|------|------|
| **Odroid** | 이 서버 (`odroid@`, Tailscale `100.65.192.62`) |
| **Oracle** | 상대 서버 (`ubuntu@`, Tailscale `100.78.233.125`) — SSH alias `oracle1` |
| **iris v2** | 이 서버에서 실행되는 전략 세대 중 하나(09:30 갭다운역추세
  데이트레이딩) — 현재 버전 `v2.10`, **2026-08-19부터 페이퍼 전환**(아래 참조),
  명명규칙은 아래 "버전 명명규칙" 절 참조 |
| **iris v3** | 2026-08-19부터 가동되는 두 번째 실거래 전략 세대
  (갭업[15~30%)반락→익일시가청산 오버나잇롱) — 버전 `v3.0.1`, 상세는
  obsidian-vault `iris v3.md` |
| **Ivy** | Oracle에서 실행되는 데이터 수집·백테스트 프로세스 |

**운용 전략: iris v2.10 + iris v3.0.1 동시 운용, 단 거래모드는 분리됨**
(2026-08-19부터). v2·v3가 예전엔 같은 `.env`의 `TRADE_MODE`를 공유해 항상
같이 real/paper로 움직였는데, **v2 전용 `IRIS_V2_TRADE_MODE` 변수를
새로 분리**해(`core/config.py`, 미설정시 `TRADE_MODE`를 그대로 따르므로
하위호환) 이제 독립적으로 제어 가능. **v2는 현재 시장 상황과 안 맞는다는
사용자 판단으로 `IRIS_V2_TRADE_MODE=paper`(페이퍼— 실주문 없이 "만약
거래했다면"의 시뮬레이션 결과만 기록·리포트)로 전환, v3는
`TRADE_MODE=real`(Stage B, 실제 KIS 주문 실행) 그대로 유지**. 2026-08-19
~2026-08-28 100만원 고정금 **2주 테스트 기간** 자체는 변동 없음(공식 성과
미집계, 상세는 obsidian-vault `iris STATE.md`/`iris DECISIONS.md`
2026-08-18·2026-08-19 참조). iris v1(OBV+모멘텀 3세대 트랜칭, 월간리밸런싱)은
**2026-07-22 은퇴·아카이브됨**(실계좌 자금 투입 이력 없음, 코드는
`_archive/`로 이동) — 과거 문서에서 "iris v1이 활성 전략"이라고 나오면 전부 낡은
정보로 간주할 것. K2/K3/K3.1/K4·다중포지션 포트폴리오·웹대시보드는 2026-07-11에
전부 삭제됨(코드·systemd·크론·로그 전체 제거). 상세 경위는 obsidian-vault
`1 Project/iris/iris DECISIONS.md` 참조.

---

## 버전 명명규칙 A.B.C (2026-08-05 확정)

`strategy/iris_v2_params.py`의 `IRIS_V2_VERSION`은 `A.B.C` 형식(A는 사실상 고정 "2"):

- **A** — 핵심접근방식(프로젝트 세대 전체, 사실상 안 바뀜)
- **B** — 전략 신호·청산로직·자본배분 등 **실거래 판단에 영향을 주는** 변경
- **C** — 로깅·알림·진단도구 등 **백테스트 수치나 실거래 판단결과에 아무 영향 없는**
  기타 기술수정. 조금이라도 판단결과가 달라질 수 있으면 C가 아니라 B로 올릴 것.

**표시 구분**: 사용자 대상(Obsidian 요약문서·텔레그램 알림)은 `A.B`까지만("v2.9").
내부(이 코드베이스·Obsidian DECISIONS 상세기록)는 `A.B.C`까지("v2.9.0")로 세분.

현재 `v2.9`(2026-08-11, **유동성 필터 신규** — 전일 KOSDAQ 전종목 거래대금 합계가
최근 250거래일 분포의 하위25%ile이면 그날 후보 자체를 생성하지 않음(무진입).
09:30 진입판단 시점에 이미 알 수 있는 전일값만 써서 룩어헤드 없음, 구현은
`core/market_breadth.py`의 `kosdaq_liquidity_percentile()` +
`scripts/iris_v2_candidates.py`의 `build_candidates()`. 실거래 연속손실 조사
중 발견 — 235건(정본230+아웃오브샘플5) 검증상 EV+1.30%→+1.81%·승률48.1%→
51.7%·누적+1476%→+1756%·MDD-17.9%→-14.3% 4개지표 동시개선, 3중검증(최근구간
제외 재현·split-half·몬테카를로100%ile) 통과. halt 발동시점과 무관하지만
그날 매매여부 자체가 바뀌는 변경이라 B급, 상세: iris DECISIONS 2026-08-11
(64차)~(68차)). v2.8(2026-08-08, 분할매수/분할매도 배포 — 매수 지정가트랑쉬1+
시장가트랑쉬2, 매도 시장가 2분할, 상세는 `strategy/iris_v2_params.py`
changelog). v2.6은 2026-08-05에 그 이전 쌓여있던 C급 변경들을 정리하며 선언한
베이스라인(iris DECISIONS 2026-08-05).
**다음 예정된 B급 변경 후보 — mom5 사이징캡 시 v2.10**(v2.8·v2.9가 먼저
선점해 번호 밀림): 진입전5일모멘텀이 임계값(0.40) 이상인 고모멘텀 종목의
목표금액을 절반으로 축소하는 안. 설계·구현·pytest 검증까지 끝났으나
(`strategy/iris_v2_params.py`의 `MOM5_CAP_ENABLED=False`로 꺼둔 채 코드 보존)
**정본 230건 데이터셋 재검증 결과 비용(EV-16.7%, 13개월 복리누적 -38%)이
최초추정(-9%)보다 훨씬 크고, 이 표본에선 고mom5군 실현손실이 2건뿐이라
방어효과가 아직 실현되지 않아 사용자 판단으로 배포 보류·추가 리서치
중**(iris DECISIONS 2026-08-07 (3차)·(5차) 참조). 그 다음 자본확대+VWAP/TWAP
분할집행은 v2.11.

---

## iris v2 전략 요약 (신호 자체는 v2.5에서 확정된 뒤 v2.7까지 불변)

09:30 완만한 갭다운(-1~-3%) KOSDAQ 종목(거래대금≥70억·거래량비율≥2배·자기50일선
위·진입전5일모멘텀≥10%) 중 거래량비율 1위를 매수 → 11:30 중간평가(수익률+2%미만이면
조기청산) → +8%도달시 보유50% 부분익절 → 13:00 강제청산(TP+15%/SL-4.5%). 전략
스펙·백테스트 성과·한계는 obsidian-vault `1 Project/iris/iris v2.md` 참조 — **여기서
파라미터 수치를 다시 옮겨적지 말 것, 그 문서가 원본**(값이 어긋나면 둘 중 하나가
낡은 것이므로 vault 쪽을 최신으로 신뢰).

**일과(KST, 평일)**: 19:10 후보계산 → 09:29 진입판단(09:30:00 정밀대기) → 09:31~12:59
매분 감시 → 13:00 강제청산 → 13:05 성과리포트.

---

## 이 서버(Odroid)의 역할

**실행:** iris v2.5의 4개 봇/배치 스크립트(`scripts/iris_v2_candidates.py`,
`bot/iris_v2_entry.py`, `bot/iris_v2_monitor.py`, `bot/iris_v2_force_close.py`,
`scripts/iris_v2_daily_report.py`)뿐. 전부 "1회성 스크립트 + cron" 방식(상시루프
데몬 아님) — `core/script_guard.run_guarded()`가 전부 감싸서 예상 못한 예외도 반드시
텔레그램 알림 후 재발생시킴.

**실행하지 않는 것 (Oracle 전담):** 데이터 수집(collect_*), 백테스트
(daily_backtest/evening_backtest), 분봉 주간 압축 아카이브, iris.db 정본 관리(Oracle
DB → 매일 19:00 SSH 동기화 수신).

**웹대시보드는 2026-07-11 완전 폐기됨** — 텔레그램 전용 운영(운영봇: 후보예고/진입/
청산/부분익절/장애알림, 리포트봇 `IRIS_REPORT`: 13:05 성과요약). nginx 시스템서비스
중지만 사용자 직접 실행 필요(sudo 권한, Claude가 못 건드림).

---

## Stage B(실거래) 핵심 안전장치 — 반드시 이해하고 있을 것

- **체결확인은 주문 응답이 아니라 실제 잔고 폴링**(`core/order_exec.py`) — 2026-06-05
  매도미체결오판 사고의 재발방지책. `FILL_WAIT_SECS`(현재 45초, `(2,3,5,10,10,15)`)
  안에서 못 밝혀내면 halt.
- **halt = 그날 자동매매 전체 중단.** `python3 -m scripts.iris_v2_halt`로 상태 확인,
  `--clear`는 **반드시 KIS 앱에서 실제 잔고 대조 후에만** 실행. "확실하지 않으면
  이중주문보다 멈춤이 항상 안전"이 이 시스템 전체를 관통하는 원칙.
- `compute_buy_qty()`는 `min(CAPITAL_PER_TRADE, 실제가용현금)`으로 사이징 — 계좌에
  CAPITAL_PER_TRADE(1,000만원)보다 적게 입금돼 있으면 자동으로 그 규모로 거래됨(코드
  변경 불필요).
- 시장가 주문(ORD_DVSN=01) 미체결 잔량은 즉시 취소되지 않고 **장마감까지 살아서
  체결을 계속 시도**함(매수시장가=상한가 지정가 취급) — halt 임계시간을 늘릴 땐
  monitor cron의 1분 틱 예산을 넘기지 말 것(넘으면 다음 틱과 겹쳐 이중주문 위험).
- 후보 목록(`N_CANDIDATES=50`)은 저녁에 전부 미리 계산, 09:30 당일엔 순위대로
  실시간 갭(현재가 vs 전일종가, **09:00 시가 아님** — 2026-07-24 v2.5.2에서 이 버그
  수정됨)을 병렬조회해 첫 통과 종목 선택.
- 09:29 시작 직후(정밀대기 진입 전) 목표시각 대비 지연을 항상 로그·30초 초과시
  경보·**5분 초과시 그날 진입 스킵**(2026-07-24 v2.5.3, 원인불명 83초 지연 사례
  재발방지 — cron/시스템 지연을 확정 못 했으나 안전장치는 걸어둠).

---

## 포지션 사이징 모드 — 텔레그램 `/mode` 명령 (v2.5.4, 2026-07-24)

매매당 투입금액을 **잔고전액**(기본값) 또는 **고정금액**(계좌잔액과 무관하게 지정한
금액) 중 선택 가능. 상시 리스너 없이 `bot/iris_v2_entry.py`가 매일 09:29 시작
직후(정밀대기 전) 텔레그램 getUpdates를 짧게 폴링해 명령을 반영 — **그날 반영되려면
09:29 KST 이전에 메시지를 보내야 함**, 그 이후엔 다음 거래일부터 적용.

텔레그램 명령(운영봇, `.env`의 `TELEGRAM_CHAT_ID`와 정확히 일치하는 채팅만 처리):
```
/mode                    현재 모드 조회
/mode full                잔고전액 모드로 전환
/mode fixed <금액>        고정금액 모드로 전환(예: /mode fixed 3000000)
```
로컬 CLI로도 동일 상태 확인/설정 가능: `python3 -m scripts.iris_v2_mode [--set full|fixed --amount N]`.
구현: `core/telegram_commands.py`. 지금 계좌규모(300만원)에선 두 모드가 사실상
동일하게 동작(가용현금이 옛 고정캡보다 작으면 결과가 같으므로) — 계좌가 커지면
차이가 실질적으로 나타남.

---

## 성과 리포트는 잔고 기준 (v2.5.5, 2026-07-24)

**입출금이 있을 때마다 반드시 기록할 것** — 안 하면 누적수익률이 틀어짐:
```bash
python3 -m scripts.iris_v2_capital --deposit 3000000 [--note "1차 입금"]
python3 -m scripts.iris_v2_capital --withdraw 500000
python3 -m scripts.iris_v2_capital --list          # 이력 + 총입금액 조회
```
(텔레그램 명령 아님 — 실제 원금 기준값을 바꾸는 조작이라 `/mode`보다 신중하게 CLI
전용으로 제한.)

`scripts/iris_v2_daily_report.py`(13:05)가 강제청산 직후 **실제 KIS 잔고**를 조회해
`balance_snapshot`에 기록. 예전 방식(거래손익률을 매일 100% 재투자한다고 가정한
복리 시뮬레이션)은 고정금액 사이징모드에서 계좌가 그 값보다 커지면 실제 증가율과
벌어질 수 있어 폐기. 포지션이 안 비어있거나 halt 상태였던 날의 스냅샷은
`reliable=0`으로 표시(잔고에 미청산분이 섞여있을 수 있음). paper 모드는 실잔고가
없어 예전 방식 그대로 유지.

## 테스트 기간 vs 공식 기록 (v2.5.6, 2026-07-24)

최소금액 스모크테스트 기간(며칠간 소액으로 과정 검증)과 "진짜 실적으로 카운트"하는
공식 기록 기간을 구분한다. **기본은 항상 테스트 기간**(리포트 제목에 "테스트"
표시, 누적수익률 계산 자체를 안 함) — 준비되면 딱 한 번:
```bash
python3 -m scripts.iris_v2_start_tracking            # 지금 잔고를 공식 기준점으로 고정
python3 -m scripts.iris_v2_start_tracking --status    # 상태만 확인
python3 -m scripts.iris_v2_start_tracking --force     # 기준점 재설정(신중히)
```
그 순간의 실제 잔고가 "공식 기록 시작 잔고(anchor)"로 고정되고, 그 이후
`daily_report`의 누적수익률·기간수익률은 전부 이 시점부터 다시 계산된다. **테스트
기간 매매도 실제 KIS 계좌의 진짜 손익이라 잔고 자체엔 이미 반영돼 있음**(가짜 돈
아님) — 이 스위치는 회계상 원금을 바꾸는 게 아니라 오직 "리포트에서 어디부터를
공식 성과로 볼지"만 정한다.

**`/mode fixed` 최소 입력값 50만원→10만원**(2026-07-24) — 175건 실측 최고가
32,550원 기준 10만원이면 어떤 종목이 나와도 1주는 확실히 사고, 부분익절(2주
이상 필요) 로직도 테스트 가능.

---

## 절대 금지 — 이 서버(Odroid)에서 하지 말 것

- Oracle 전담 스크립트를 이 서버에서 실행(데이터 수집·백테스트)
- `iris.db` 삭제·초기화·DROP
- `obsidian-vault` 마스터 경로(`/home/odroid/obsidian-vault/`) 외부에서 볼트 파일 수정
- `.env`의 `TRADE_MODE`·`CAPITAL_PER_TRADE` 등 실거래 파라미터를 사용자 확인 없이 변경
- `strategy/iris_v2_params.py`의 전략 신호 파라미터(GAP/TV/SL/TP 등)를 백테스트
  재검증 없이 임의 변경 — 바꾸면 반드시 `IRIS_V2_VERSION` bump + 변경사유를
  `iris DECISIONS.md`에 기록(파일 상단 주석 규칙 참조)

---

## 코드 구조

```
core/
  kis_api.py       KIS Open API 래퍼(시세/주문/잔고/토큰캐시/거래일)
  order_exec.py     Stage B 실주문 실행(buy_safe/sell_safe/체결확인/halt). **2026-08-03
                    FileHandler 제거 이후 `logs/order_exec.log`는 갱신 안 됨(전용
                    cron 진입점이 없는 공유 모듈이라, 실제 로그는 그때그때 호출한
                    스크립트의 로그(iris_v2_entry/monitor/force_close.log)에 섞여
                    들어감)** — 통합 조회는 trade_log DB 테이블(category='IRIS_V2')
                    사용할 것, 2026-08-08 재확인(iris DECISIONS 참조)
  db.py            SQLite 연결·스키마·마이그레이션
  config.py        .env 로드
  logger.py        파일+콘솔+DB 동시 로깅, KST 타임스탬프 강제변환
  notify.py        텔레그램(운영봇/리포트봇 2원화)
  script_guard.py  예상 못한 예외 최종 안전망(전 스크립트에 적용)
  proc_lock.py     cron 틱 중복실행 방지(flock, v2.5.7, 전 스크립트에 적용)
  timeutil.py      KST 시간 유틸, 거래일 계산
  telegram_commands.py  텔레그램 `/mode` 명령 처리(포지션 사이징 모드 전환, v2.5.4)
  order_exec_v3.py  iris v3 전용 주문실행(halt·pending 상태를 v2와 완전 격리,
                    2026-08-18 신규) — 상태없는 순수함수(round_to_tick·
                    poll_remaining_shares·resolve_exit_price·sell_safe_limit·
                    compute_buy_qty 등)는 order_exec.py에서 그대로 재사용,
                    상태있는 함수(halt·pending_entry·pending_exit)만 `iris_v3_`
                    접두사로 새로 작성(v2와 상태 충돌 없음)
strategy/
  iris_v2_params.py  전략 파라미터 중앙관리(변경 이력 전부 상단 주석에 누적)
  iris_v3_params.py  iris v3 파라미터(2026-08-18 신규, 같은 관례)
bot/
  iris_v2_entry.py       09:29 cron — 09:30:00 정밀대기 후 진입판단
  iris_v2_monitor.py     09:31~12:59 매분 cron — TP/SL/중간평가/부분익절
  iris_v2_force_close.py 13:00 cron — 강제청산
  iris_v3_entry.py         15:00 cron — 조건부지정가 매수 주문(2026-08-18 신규,
                          2026-08-18 cron 등록·real 전환 완료)
  iris_v3_confirm_entry.py 15:35 cron — 동시호가 종료 후 매수 체결확정
  iris_v3_exit.py          09:00 cron — 익일 지정가매도+3분타임아웃+시장가전환
scripts/
  iris_v2_candidates.py    19:10 cron — 저녁 후보계산
  iris_v2_daily_report.py  13:05 cron — 성과리포트(리포트봇)
  iris_v2_halt.py          halt 상태 확인/해제 CLI
  iris_v2_mode.py          포지션 사이징 모드 확인/설정 CLI(v2.5.4, 텔레그램 대안)
  iris_v2_capital.py       원금 입출금 기록 CLI(v2.5.5, 잔고기준 리포트 필수 입력)
  iris_v2_start_tracking.py 테스트→공식 기록 전환 CLI(v2.5.6)
  iris_v2_diag.py          진단 종합조회 CLI(v2.5.7, 문제생기면 이거부터)
  iris_v2_real_trade_journal.py    13:06 cron — v2 실거래 거래일지(로그 파싱 기반, candidates 야간유실 우회)
  iris_v3_real_trade_journal.py    09:11 cron — v3 실거래 거래일지(2026-08-18 신규, bot_state 기반, 로그 파싱 불필요)
  iris_v2_sync_journals_to_vault.py 13:07·09:11 cron — v2·v3 거래일지 전부 Obsidian 볼트로 복사(2026-08-18 v3 추가)
  iris_v3_prefilter.py     19:15 cron — 저녁 tv_ma5 사전필터(2026-08-18 신규,
                          v3는 갭 자체가 당일 시가라 저녁엔 이것만 가능)
  iris_v3_candidates.py    09:05 cron — 장시작 직후 실시간 갭 확정, 1일1종목 선정
  iris_v3_daily_report.py  09:10 cron — v3 성과리포트(2026-08-18 신규, 거래기록기준, 리포트봇)
tests/
  test_iris_v2.py       엔트리/모니터/후보선정 로직 단위테스트
  test_order_exec.py    Stage B 주문실행 로직 단위테스트(KIS 함수 전부 모킹)
  test_iris_v3.py       iris v3 사전필터/후보/매수/확정/매도 로직 단위테스트
  test_order_exec_v3.py iris v3 주문실행·halt·pending 로직 단위테스트(v2와의
                        상태격리 검증 포함)
_archive/              은퇴한 iris v1 코드(bot/scripts/tests) — 참고용 보존
```

**iris v3 코드 상태(2026-08-18, 실거래 개시)**: 통계검증·실행설계 확정 후
코드+테스트까지 구현 완료, 같은 날 cron 등록·TRADE_MODE=real 전환까지
완료 — **2026-08-19(수)부터 실거래 시작**(사용자 명시적 승인, 페이퍼
트레이딩 단계는 건너뜀). v2(300만원 고정 → 100만원 고정 전환)와 함께
**2026-08-19~2026-08-28(금) 2주는 두 시스템 모두 명시적 테스트 기간**
(공식 성과 미집계, 텔레그램 리포트에 종료일 표시). 개시 전날 최종
코드감사(실제 코드 직접 재확인)에서 v3 고유의 이중매도/이중매수 위험
2건 발견·즉시수정(`IRIS_V3_VERSION` **3.0.1**, v2는 이상없음 확인) —
pytest **307건** 전부 통과. 상세: obsidian-vault `iris DECISIONS.md`
2026-08-18(27·28차), `iris STATE.md`, `iris v3.md`.

---

## 핵심 명령어

```bash
# 테스트 (파라미터·로직 수정 후 항상 먼저)
cd ~/iris && python3 -m pytest tests/ -q

# 각 스크립트 dry-run(DB 미기록·텔레그램 미발송)
python3 -m scripts.iris_v2_candidates --dry-run
python3 -m bot.iris_v2_entry --dry-run

# halt 상태 확인/해제
python3 -m scripts.iris_v2_halt
python3 -m scripts.iris_v2_halt --clear   # KIS 앱 잔고 대조 후에만

# 로그 확인(entry/monitor/force_close는 각자 파일 — order_exec 상세 로그는 이제
# order_exec.log가 아니라 아래 호출한 스크립트 파일 안에 섞여 있음, 2026-08-08 확인)
tail -50 logs/iris_v2_entry.log logs/iris_v2_monitor.log logs/iris_v2_force_close.log

# Oracle SSH
ssh oracle1 "명령어"

# DB 빠른 확인
python3 -c "from core.db import get_conn; c=get_conn().__enter__(); print(c.execute('SELECT MAX(date) FROM price_daily').fetchone())"
```

---

## systemd·cron·전략 파라미터 변경 원칙

enable·start·crontab 등록/수정, `strategy/iris_v2_params.py`의 신호 파라미터 변경
**전** 항상 변경 내역 먼저 보여주고 확인 대기. 구현 버그 수정(로직 자체는 안 바뀌고
코드가 스펙과 안 맞던 걸 맞추는 것, 예: 2026-07-24 v2.5.2)은 곧바로 진행하되 항상
`pytest` 전체 통과 확인 후 vault DECISIONS에 기록.

---

## 파이썬 환경

시스템 파이썬(`python3`), venv 없음. 패키지 추가: `pip install --break-system-packages <pkg>`.
cron·스크립트는 항상 `python3`.

---

## .env 보안

`.env` 키 값 출력 절대 금지. 구조만:
```bash
grep -v '=' ~/iris/.env       # 키 이름만
cat ~/iris/.env.template      # 안전한 템플릿
```
`TRADE_MODE=real`, `CAPITAL_PER_TRADE` — 변경 전 반드시 사용자 확인.

---

## 문제 생기면 여기부터

```bash
python3 -m scripts.iris_v2_diag              # halt/락/오늘후보·포지션/잔고스냅샷/
                                              # 공식기록상태/최근 CRITICAL·ERROR 로그/
                                              # 진입지연이력을 한 화면에
python3 -m scripts.iris_v2_diag --days 7     # 조회기간 늘리기
```
읽기전용, 아무것도 안 바꿈. 개별 로그파일(`logs/iris_v2_*.log` — `logs/order_exec.log`
자체는 2026-08-03 이후 갱신 안 됨, 위 코드구조 절 참조) 직접 보는 것보다 여기서
먼저 훑는 게 빠름.
