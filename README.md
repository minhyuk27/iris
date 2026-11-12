# Iris: Production Automated Trading Platform for Korean Equities

Iris는 한국투자증권(KIS) OpenAPI를 기반으로 일간/분중 시세 분석, 리스크 관리, 자동 주문 집행 및 성과 저널링을 무인으로 수행하는 **프로덕션급 국내 주식 자동매매 시스템**입니다.

---

## 1. 프로젝트 개요 (Overview)

- **목적**: 엄격한 리스크 파라미터(손절, 목표수익, 강제청산) 하에 정량적 알고리즘 전략에 따라 전 과정을 자동화한 실전/페이퍼 트레이딩 시스템 구축
- **대상 시장**: 한국 주식 시장 (KOSPI, KOSDAQ 전 종목)
- **개발 형태**: 1인 단독 설계 및 풀스택 파이프라인 개발 (기여도 100%)
- **핵심 특징**:
  - 한국투자증권 OpenAPI 연동 (OAuth 토큰 자동 갱신, 주문/잔고 동기화)
  - 증권사 제한(초당 20건)을 엄격히 준수하는 슬라이딩 윈도우 기반 정밀 레이트 리미터
  - `requests.Session` + 커넥션 풀(`HTTPAdapter`) 재사용을 통한 시세 조회 TCP/TLS 지연 최소화
  - 프로세스 중복 방지(`proc_lock`), 장 운영 시간 검증(`script_guard`) 등 다계층 세이프가드
  - 매매 체결 즉시 텔레그램 알림 및 마크다운 기반 일간 트레이딩 저널 자동 발행

---

## 2. 시스템 아키텍처 (Architecture)

```
[한국투자증권 OpenAPI] ◄─────────────────────────────────────────────┐
          │                                                         │
          ▼ (시세 / 잔고 / 체결)                                     ▼ (주문 집행)
┌─────────────────────────────────────────────────────────────────────────┐
│                              Iris Core Engine                           │
│                                                                         │
│  [Script Guard] ──► [Rate Limiter] ──► [Token Manager]                  │
│       │                                      │                          │
│       ▼                                      ▼                          │
│  [Universe Pre-filter] ──► [Entry Scanner] ──► [Order Executor (v2/v3)] │
│       │                          │                     │                │
│       ▼                          ▼                     ▼                │
│  [Market Breadth Monitor]   [Strategy Engine]   [Position / Stop Monitor]│
└─────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
             [SQLite Storage] ──► [Trade Journal / Telegram Report]
```

---

## 3. 핵심 기술 스택 (Tech Stack)

| 영역 | 기술 스택 | 선택 및 구현 이유 |
|---|---|---|
| **Language & Core** | Python 3.10+, Asyncio | 동시 주문 처리 및 비동기 소켓/REST 제어 |
| **Broker API** | KIS OpenAPI (한국투자증권) | 실전/모의 계좌 주문 및 실시간 잔고 조회 |
| **Database** | SQLite3, Pandas | 전 유니버스 시세, 거래 체결 내역 및 저널 저장 |
| **Notification** | Telegram Bot API | 실시간 체결, 에러 알림, 일간 브리핑 자동 전송 |
| **Reliability** | File Locking, Pytest | 무인 가동 안정성을 위한 단위 테스트 및 동시성 락 |

---

## 4. 핵심 모듈 구성 (Project Structure)

```
iris/
├── core/                     # 인프라 및 브로커 연동 코어
│   ├── kis_api.py            # KIS OpenAPI REST 래퍼, 커넥션 풀, 토큰 관리자
│   ├── order_exec_v3.py      # 분할 주문, 슬리피지 제어 및 체결 추적기
│   ├── proc_lock.py          # 파일 락 기반 프로세스 단일 실행 보장
│   ├── script_guard.py       # 거래일/정규장 시간 검증 및 비정상 실행 차단
│   ├── notify.py             # 텔레그램 실시간 알림 디스패처
│   └── market_breadth.py     # 코스피/코스닥 ADR 등 시장 건전도 지표
├── bot/                      # 자동매매 실행 데몬 및 트리거
│   ├── iris_v3_entry.py      # 장 시작 전/초기 조건 부합 종목 발굴 및 진입
│   ├── iris_v3_exit.py       # 목표가/손절가 도달 감시 및 이익 실현
│   ├── iris_v2_force_close.py# 장 마감 직전 당일 오버나이트 방지 강제 청산
│   └── daily_backtest.py     # 당일 실체결과 시뮬레이션 간 괴리율 백테스트
├── strategy/                 # 전략 매개변수 및 등록 레지스트리
│   ├── iris_v3_params.py     # v3 전략 파라미터 (손익비, 진입 임계치)
│   └── registry.py           # 활성 전략 동적 로더
├── reports/                  # 마크다운 트레이딩 저널
└── tests/                    # 핵심 주문/API 로직 단위 테스트 (Pytest)
```

---

## 5. 주요 엔지니어링 과제 및 해결 방안 (Engineering Highlights)

1. **증권사 OpenAPI 레이트 리미팅 및 TCP 핸드셰이크 오버헤드 제거**:
   - `EGW00201`(초당 거래건수 초과) 에러를 방지하기 위해 스레드 락 기반의 18req/s 슬라이딩 윈도우 레이트 리미터를 구현했습니다.
   - 장 시작 직후(09:30) 최대 50종목 병렬 조회 시 매 요청마다 발생하는 TCP/TLS 핸드셰이크 지연을 제거하기 위해 `requests.Session` 및 `HTTPAdapter(pool_connections=20, pool_maxsize=20)` 커넥션 풀을 도입하여 레이턴시를 대폭 단축했습니다.
2. **무인 운용 환경에서의 다계층 안전 장치(Fail-Safe)**:
   - 파일 기반 상호 배제 락(`proc_lock`)으로 크론 중복 실행 및 주문 이중 제출을 원천 차단했습니다.
   - 휴장일, 임시 공휴일, 수능일 등 변동 거래 시간표를 거래소 달력과 동기화하는 `script_guard`를 구축하여 장 외 시간 오작동을 차단했습니다.
3. **체결 저널 자동화**:
   - 매매 완료 후 실제 체결 가격, 슬리피지, 거래 수수료/세금을 자동 계산하여 일간 마크다운 저널을 생성함으로써 매매 복기 파이프라인을 완성했습니다.

---

## 6. 빠른 시작 (Quickstart)

### 설치 및 환경 설정
```bash
# 저장소 복제
git clone https://github.com/minhyuk27/iris.git
cd iris

# 환경변수 템플릿 복사 후 설정
cp .env.example .env
```

### 단위 테스트 실행
핵심 안전 가드 및 거래일 판정 로직을 검증합니다:
```bash
pytest tests/test_script_guard.py tests/test_trading_day.py
```

---

## 7. 라이선스 (License)

본 프로젝트는 [MIT License](LICENSE)를 따릅니다.
