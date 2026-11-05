"""
IRIS - DB 연결 및 스키마
SQLite 단일 파일(iris.db)로 모든 상태 관리
"""
import sqlite3
import json
from contextlib import contextmanager
from pathlib import Path

from core.config import DB_PATH


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")   # 동시 읽기 성능
    conn.execute("PRAGMA synchronous=NORMAL") # 속도/안전 균형
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_conn():
    """컨텍스트 매니저 — with get_conn() as conn:"""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """DB 스키마 초기화 (최초 1회 또는 마이그레이션 시 실행)"""
    with get_conn() as conn:
        conn.executescript("""
        -- ── 일봉 가격 ───────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS price_daily (
            ticker  TEXT    NOT NULL,
            date    TEXT    NOT NULL,   -- YYYYMMDD
            open    INTEGER NOT NULL,
            high    INTEGER NOT NULL,
            low     INTEGER NOT NULL,
            close   INTEGER NOT NULL,
            volume  INTEGER NOT NULL,
            PRIMARY KEY (ticker, date)
        );
        CREATE INDEX IF NOT EXISTS idx_price_date   ON price_daily(date);
        CREATE INDEX IF NOT EXISTS idx_price_ticker ON price_daily(ticker);

        -- ── 기관/외인 수급 ───────────────────────────────────────
        CREATE TABLE IF NOT EXISTS supply_daily (
            ticker      TEXT    NOT NULL,
            date        TEXT    NOT NULL,
            inst_net    INTEGER DEFAULT 0,
            inst_amt    REAL    DEFAULT 0,   -- 백만원
            foreign_net INTEGER DEFAULT 0,
            foreign_amt REAL    DEFAULT 0,   -- 백만원
            PRIMARY KEY (ticker, date)
        );
        CREATE INDEX IF NOT EXISTS idx_supply_date ON supply_daily(date);

        -- ── 유니버스 ─────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS universe (
            ticker  TEXT PRIMARY KEY,
            name    TEXT NOT NULL,
            sector  TEXT DEFAULT '기타',
            market  TEXT DEFAULT '',    -- KOSPI / KOSDAQ
            updated TEXT NOT NULL
        );

        -- ── 전날 선정 후보 ───────────────────────────────────────
        CREATE TABLE IF NOT EXISTS candidates (
            trade_date  TEXT    NOT NULL,   -- 거래 당일 (YYYYMMDD)
            ticker      TEXT    NOT NULL,
            name        TEXT    NOT NULL,
            sector      TEXT    DEFAULT '',
            score       REAL    DEFAULT 0,
            total_amt   REAL    DEFAULT 0,  -- 기관+외인 합산 순매수 억원
            val_ratio   REAL    DEFAULT 0,  -- 거래대금 비율
            avg_val     REAL    DEFAULT 0,  -- 20일 평균 거래대금 억원
            strategy    TEXT    NOT NULL DEFAULT 'k2',  -- k2 / k3
            strategy_version TEXT,  -- 후보 선정 시점 전략 파라미터 버전 (예: '1.0')
            PRIMARY KEY (trade_date, ticker, strategy)
        );

        -- ── 포지션 / 거래 이력 ───────────────────────────────────
        CREATE TABLE IF NOT EXISTS positions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker      TEXT    NOT NULL,
            name        TEXT    NOT NULL,
            date_open   TEXT    NOT NULL,
            time_open   TEXT    NOT NULL,
            date_close  TEXT,
            time_close  TEXT,
            entry_price REAL    NOT NULL,
            exit_price  REAL,
            shares      INTEGER NOT NULL,
            score       REAL    DEFAULT 0,
            target_pct  REAL    DEFAULT 0,
            stop_pct    REAL    DEFAULT 0,
            pnl_pct     REAL,
            pnl_amount  REAL,
            exit_reason TEXT,
            mode        TEXT    NOT NULL,   -- paper / real
            status      TEXT    NOT NULL DEFAULT 'open',  -- open / closed
            strategy_version TEXT,  -- 진입 시점 전략 파라미터 버전 (예: '1.0')
            strategy    TEXT,   -- iris v1 세대 id('iris_v1_gen0'/'gen1'/'gen2'), 과거 K3.1 행은 NULL 유지
            partial_exit_price REAL,  -- iris v2.5: 1차 부분익절 체결가(미발동이면 NULL)
            partial_shares     INTEGER,  -- iris v2.5: 1차 부분익절로 매도한 수량(미발동이면 NULL)
            partial_time       TEXT  -- iris v2.5: 1차 부분익절 체결시각(HHMMSS)
        );
        CREATE INDEX IF NOT EXISTS idx_pos_status    ON positions(status);
        CREATE INDEX IF NOT EXISTS idx_pos_date_open ON positions(date_open);

        -- ── 세대별 배정자본/현금 추적 (iris v1 gen0/1/2) ─────────
        CREATE TABLE IF NOT EXISTS sleeve_cash (
            strategy   TEXT    NOT NULL,   -- 'iris_v1_gen0'/'gen1'/'gen2'
            account    TEXT    NOT NULL,   -- 'KIS' 고정
            cash       REAL    NOT NULL,
            updated_at TEXT    NOT NULL,
            PRIMARY KEY (strategy, account)
        );

        -- ── 봇 상태 (key-value) ───────────────────────────────────
        CREATE TABLE IF NOT EXISTS bot_state (
            key     TEXT PRIMARY KEY,
            value   TEXT NOT NULL,
            updated TEXT NOT NULL
        );

        -- ── 시스템/거래 로그 ─────────────────────────────────────
        CREATE TABLE IF NOT EXISTS trade_log (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       TEXT    NOT NULL,
            level    TEXT    NOT NULL,   -- INFO / WARNING / ERROR
            category TEXT    NOT NULL,   -- BOT / PREFILTER / COLLECT / SYSTEM
            message  TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_log_ts ON trade_log(ts);

        -- ── 시장 지수 일봉 ───────────────────────────────────────
        CREATE TABLE IF NOT EXISTS market_index_daily (
            date       TEXT PRIMARY KEY,  -- YYYYMMDD
            kospi_cls  REAL DEFAULT 0,
            kosdaq_cls REAL DEFAULT 0,
            kospi_ret  REAL DEFAULT 0,    -- 전일 대비 수익률 (%)
            kosdaq_ret REAL DEFAULT 0
        );

        -- ── 전략별 일간 백테스트 성과 ────────────────────────────
        CREATE TABLE IF NOT EXISTS strategy_perf (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date        TEXT    NOT NULL,      -- 백테스트 실행일 (YYYYMMDD)
            trade_date      TEXT    NOT NULL,      -- 거래 시뮬레이션 날짜
            strategy        TEXT    NOT NULL,      -- 'k2' | 'k3' | ...
            trades          INTEGER DEFAULT 0,     -- 진입 종목 수
            wins            INTEGER DEFAULT 0,
            losses          INTEGER DEFAULT 0,
            pnl_pct         REAL    DEFAULT 0.0,  -- 당일 평균 손익률 (%)
            cumulative_pct  REAL    DEFAULT 0.0,  -- 누적 손익률 (%)
            capital         REAL    DEFAULT 0.0,  -- 누적 자본금 (원)
            strategy_version TEXT,  -- 백테스트에 사용된 전략 파라미터 버전 (예: '1.0')
            UNIQUE(trade_date, strategy)
        );
        CREATE INDEX IF NOT EXISTS idx_sp_trade_date ON strategy_perf(trade_date);
        CREATE INDEX IF NOT EXISTS idx_sp_strategy   ON strategy_perf(strategy);

        -- ── 계좌 원금 입출금 이력 (2026-07-24, iris v2.5.5) ──────
        -- 잔고기준 수익률 계산의 분모(총입금액)를 알려면 입출금 사실을 직접
        -- 기록해야 함(KIS API로는 "이체"와 "매매손익"을 구분할 수 없음).
        CREATE TABLE IF NOT EXISTS capital_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            date       TEXT    NOT NULL,   -- YYYYMMDD
            amount     REAL    NOT NULL,   -- 입금 양수, 출금 음수(원)
            note       TEXT    DEFAULT '',
            created_at TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_capev_date ON capital_events(date);

        -- ── 일별 실제잔고 스냅샷 (2026-07-24, iris v2.5.5) ────────
        -- daily_report가 13:05(강제청산 직후, 포지션이 비어있어야 정상)에
        -- KIS 실제 잔고를 조회해 기록 — 거래손익률을 복리로 시뮬레이션하지
        -- 않고 "진짜 잔고가 실제로 어떻게 움직였는지"를 그대로 추적한다.
        CREATE TABLE IF NOT EXISTS balance_snapshot (
            date        TEXT PRIMARY KEY,  -- YYYYMMDD
            balance     REAL    NOT NULL,  -- 실제 잔고(원, get_balance()["final_cash"], v2.5.8부터)
            reliable    INTEGER NOT NULL DEFAULT 1,  -- 0이면 그날 포지션이 안 비어있었거나
                                                       -- halt 등으로 신뢰 못 할 스냅샷(참고용 보존)
            captured_at TEXT    NOT NULL
        );

        -- ── KOSDAQ 시장전체 일별 거래대금 캐시 (2026-08-17, v2.9.2) ──────────────
        -- core/market_breadth.py의 kosdaq_liquidity_percentile()이 매번 "그 시점의
        -- 최신 universe"로 과거 날짜의 시장전체 거래대금까지 다시 계산하던 문제 —
        -- universe는 매주 월요일 갱신되는데, 그 갱신 전후로 "같은 과거 날짜"를 다시
        -- 계산하면 대상 종목 구성이 달라져 값이 흔들렸다(2026-08-17 실거래 중
        -- 발견 — 08-14 데이터 기준 유동성필터 발동여부가 금요일저녁 계산 19%ile,
        -- 월요일저녁 재계산 25%ile로 갈려 필터 발동 자체가 뒤집힘, 대상거래일은
        -- 휴장일 때문에 우연히 겹친 20260818). 한 번 계산된 날짜의 총거래대금은
        -- 이 테이블에 고정 저장되고, 이후 universe가 바뀌어도 다시 계산하지 않음
        -- (그 날짜를 "그 값이 처음 확정된 시점의 universe 스냅샷" 기준으로 동결).
        CREATE TABLE IF NOT EXISTS market_liquidity_daily (
            date           TEXT PRIMARY KEY,  -- YYYYMMDD
            kosdaq_total_tv REAL   NOT NULL,  -- KOSDAQ 전종목 그날 거래대금 합계(원)
            computed_at    TEXT   NOT NULL
        );
        """)
    # candidates 테이블 마이그레이션: PK를 (trade_date, ticker, strategy)로 변경
    with get_conn() as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='candidates'"
        ).fetchone()
        if row and "strategy" not in row["sql"].split("PRIMARY KEY")[1]:
            # PK에 strategy가 없으면 재구성
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS candidates_new (
                    trade_date  TEXT    NOT NULL,
                    ticker      TEXT    NOT NULL,
                    name        TEXT    NOT NULL,
                    sector      TEXT    DEFAULT '',
                    score       REAL    DEFAULT 0,
                    total_amt   REAL    DEFAULT 0,
                    val_ratio   REAL    DEFAULT 0,
                    avg_val     REAL    DEFAULT 0,
                    marcap_bil  REAL    DEFAULT 0,
                    rank_no     INTEGER DEFAULT 0,
                    skip_reason TEXT    DEFAULT '',
                    strategy    TEXT    NOT NULL DEFAULT 'k2',
                    PRIMARY KEY (trade_date, ticker, strategy)
                );
                INSERT OR IGNORE INTO candidates_new
                    SELECT trade_date, ticker, name, sector, score,
                           total_amt, val_ratio, avg_val,
                           COALESCE(marcap_bil, 0),
                           COALESCE(rank_no, 0),
                           COALESCE(skip_reason, ''),
                           COALESCE(strategy, 'k2')
                    FROM candidates;
                DROP TABLE candidates;
                ALTER TABLE candidates_new RENAME TO candidates;
            """)

    # strategy_version 컬럼 추가 (기존 DB 마이그레이션)
    with get_conn() as conn:
        for table in ("positions", "strategy_perf", "candidates"):
            cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if "strategy_version" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN strategy_version TEXT")

    # positions.strategy 컬럼 추가 (기존 DB 마이그레이션 — 현재 iris v1 세대구분용, 과거 K3.1 행은 NULL 유지)
    with get_conn() as conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(positions)").fetchall()]
        if "strategy" not in cols:
            conn.execute("ALTER TABLE positions ADD COLUMN strategy TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pos_strategy ON positions(strategy)")

    # positions.partial_* 컬럼 추가 (기존 DB 마이그레이션 — iris v2.5 부분익절 지원)
    with get_conn() as conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(positions)").fetchall()]
        for col, coltype in (("partial_exit_price", "REAL"), ("partial_shares", "INTEGER"), ("partial_time", "TEXT")):
            if col not in cols:
                conn.execute(f"ALTER TABLE positions ADD COLUMN {col} {coltype}")


# ── bot_state 헬퍼 ────────────────────────────────────────────

def state_get(key: str, default=None):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM bot_state WHERE key=?", (key,)
        ).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return row["value"]


def state_set(key: str, value):
    from core.timeutil import iso_kst
    serialized = json.dumps(value, ensure_ascii=False, default=str)
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO bot_state(key, value, updated)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated
        """, (key, serialized, iso_kst()))


# ── 가격 조회 헬퍼 ────────────────────────────────────────────

def get_price_df(ticker: str, n: int = 250):
    """
    price_daily → pandas DataFrame 반환 (날짜 오름차순)
    컬럼: date(str), open, high, low, close, volume
    """
    import pandas as pd
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT date, open, high, low, close, volume
            FROM price_daily
            WHERE ticker = ?
            ORDER BY date DESC
            LIMIT ?
        """, (ticker, n)).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    return df.sort_values("date").reset_index(drop=True)


def get_supply(ticker: str, date: str) -> dict:
    """특정 날짜 수급 조회"""
    with get_conn() as conn:
        row = conn.execute("""
            SELECT inst_net, inst_amt, foreign_net, foreign_amt
            FROM supply_daily
            WHERE ticker=? AND date=?
        """, (ticker, date)).fetchone()
    if row is None:
        return {"inst_net": 0, "inst_amt": 0.0, "foreign_net": 0, "foreign_amt": 0.0}
    return dict(row)


def init_minute_table():
    """분봉 테이블 추가 (최초 1회 또는 누락 시)"""
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS price_minute (
            ticker TEXT    NOT NULL,
            dt     TEXT    NOT NULL,   -- YYYYMMDDHHMMSS
            open   INTEGER NOT NULL,
            high   INTEGER NOT NULL,
            low    INTEGER NOT NULL,
            close  INTEGER NOT NULL,
            volume INTEGER NOT NULL,
            PRIMARY KEY (ticker, dt)
        );
        CREATE INDEX IF NOT EXISTS idx_minute_ticker ON price_minute(ticker);
        CREATE INDEX IF NOT EXISTS idx_minute_dt     ON price_minute(dt);
        """)


# ── strategy_perf 헬퍼 ────────────────────────────────────────

def get_last_perf(strategy: str, before_date: str = None) -> dict | None:
    """특정 전략의 직전 성과 레코드 반환 (누적 계산용).
    before_date 지정 시 해당 날짜 이전의 최신 레코드 반환.
    """
    with get_conn() as conn:
        if before_date:
            row = conn.execute("""
                SELECT cumulative_pct, capital
                FROM strategy_perf
                WHERE strategy=? AND trade_date < ?
                ORDER BY trade_date DESC LIMIT 1
            """, (strategy, before_date)).fetchone()
        else:
            row = conn.execute("""
                SELECT cumulative_pct, capital
                FROM strategy_perf
                WHERE strategy=?
                ORDER BY trade_date DESC LIMIT 1
            """, (strategy,)).fetchone()
    return dict(row) if row else None


def upsert_perf(record: dict):
    """strategy_perf INSERT OR REPLACE"""
    from datetime import datetime as _dt
    record = {**record, "strategy_version": record.get("strategy_version")}
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO strategy_perf
                (run_date, trade_date, strategy, trades, wins, losses,
                 pnl_pct, cumulative_pct, capital, strategy_version)
            VALUES
                (:run_date, :trade_date, :strategy, :trades, :wins, :losses,
                 :pnl_pct, :cumulative_pct, :capital, :strategy_version)
            ON CONFLICT(trade_date, strategy) DO UPDATE SET
                run_date=excluded.run_date,
                trades=excluded.trades, wins=excluded.wins, losses=excluded.losses,
                pnl_pct=excluded.pnl_pct,
                cumulative_pct=excluded.cumulative_pct,
                capital=excluded.capital,
                strategy_version=excluded.strategy_version
        """, record)


# ── 원금 입출금 / 잔고 스냅샷 헬퍼 (2026-07-24, iris v2.5.5) ──────────────

def add_capital_event(date: str, amount: float, note: str = ""):
    """입금(양수)/출금(음수) 기록. scripts/iris_v2_capital.py에서 호출."""
    from core.timeutil import iso_kst
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO capital_events(date, amount, note, created_at) VALUES (?,?,?,?)",
            (date, amount, note, iso_kst()),
        )


def total_capital(as_of_date: str | None = None) -> float:
    """누적 총입금액(원). as_of_date 지정시 그 날짜까지(포함)만 합산."""
    with get_conn() as conn:
        if as_of_date:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount),0) s FROM capital_events WHERE date<=?", (as_of_date,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COALESCE(SUM(amount),0) s FROM capital_events").fetchone()
    return float(row["s"])


def list_capital_events() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT date, amount, note, created_at FROM capital_events ORDER BY date, id"
        ).fetchall()
    return [dict(r) for r in rows]


def record_balance_snapshot(date: str, balance: float, reliable: bool = True):
    from core.timeutil import iso_kst
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO balance_snapshot(date, balance, reliable, captured_at)
               VALUES (?,?,?,?)
               ON CONFLICT(date) DO UPDATE SET
                   balance=excluded.balance, reliable=excluded.reliable,
                   captured_at=excluded.captured_at""",
            (date, balance, 1 if reliable else 0, iso_kst()),
        )


def get_balance_snapshot(date: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT date, balance, reliable, captured_at FROM balance_snapshot WHERE date=?", (date,)
        ).fetchone()
    return dict(row) if row else None


def latest_balance_snapshot_before(date: str) -> dict | None:
    """date 이전(미포함) 가장 최근의 신뢰가능한 스냅샷 — 기간수익률 계산의 기준점."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT date, balance, reliable, captured_at FROM balance_snapshot
               WHERE date<? AND reliable=1 ORDER BY date DESC LIMIT 1""", (date,)
        ).fetchone()
    return dict(row) if row else None


def recent_balance_snapshots(n: int) -> list[dict]:
    """최신 n개의 신뢰가능한 스냅샷을 오래된순으로 반환(기간수익률 시작점 탐색용)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT date, balance FROM balance_snapshot WHERE reliable=1 ORDER BY date DESC LIMIT ?", (n,)
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def first_balance_snapshot_of_month(month: str) -> dict | None:
    """month(YYYYMM)의 첫 신뢰가능한 스냅샷."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT date, balance FROM balance_snapshot
               WHERE date LIKE ? AND reliable=1 ORDER BY date LIMIT 1""", (month + "%",)
        ).fetchone()
    return dict(row) if row else None


def capital_events_between(start_date_exclusive: str, end_date_inclusive: str) -> float:
    """(start, end] 구간의 입출금 순액 — 기간수익률 계산시 입금분을 제외하기 위함."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount),0) s FROM capital_events WHERE date>? AND date<=?",
            (start_date_exclusive, end_date_inclusive),
        ).fetchone()
    return float(row["s"])


# ── 공식 실적 기록 시작/종료 (2026-07-24, iris v2.5.6) ────────────────────
# 최소금액 스모크테스트 기간(실제 소액이 오가지만 "공식 성과"로는 안 침)과, 그
# 이후 "진짜로 카운트하는" 기간을 구분하기 위한 스위치. bot_state를 그대로 재사용
# (별도 테이블 불필요 — 값 하나만 있으면 됨).

_LIVE_TRACKING_KEY = "iris_v2_live_tracking"


def start_live_tracking(date: str, anchor_balance: float):
    """공식 기록 시작 — 그 시점 실제잔고를 기준점(anchor)으로 고정."""
    state_set(_LIVE_TRACKING_KEY, {"date": date, "anchor_balance": anchor_balance})


def get_live_tracking() -> dict | None:
    """None이면 아직 테스트 기간(공식 기록 미시작)."""
    return state_get(_LIVE_TRACKING_KEY)
