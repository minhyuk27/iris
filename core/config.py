"""
IRIS - 설정 로더
모든 모듈은 이 파일에서만 설정을 가져온다.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# .env 위치: iris/ 루트
_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)


def _get(key: str, default=None, cast=str):
    val = os.getenv(key, default)
    if val is None:
        return None
    try:
        return cast(val)
    except (ValueError, TypeError):
        return default


def _bool(key: str, default: bool) -> bool:
    val = os.getenv(key, str(default)).strip().lower()
    return val in ("true", "1", "yes")


# ── 경로 ──────────────────────────────────────────────────────
BASE_DIR  = Path(os.getenv("BASE_DIR", Path.home() / "iris"))
DB_PATH   = BASE_DIR / "iris.db"
TOKEN_PATH = BASE_DIR / "config" / "token.json"
LOG_DIR   = BASE_DIR / "logs"

# ── KIS API ───────────────────────────────────────────────────
KIS_APP_KEY    = os.getenv("KIS_APP_KEY", "")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET", "")
KIS_ACCOUNT    = os.getenv("KIS_ACCOUNT", "")
KIS_BASE_URL   = os.getenv("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443")
KIS_IS_VIRTUAL = _bool("KIS_IS_VIRTUAL", False)

# ── 텔레그램 ──────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")       # 운영봇: 실시간·운영 메시지
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_TOKEN_REPORT   = os.getenv("TELEGRAM_TOKEN_REPORT", "")     # 리포트봇: 성과요약 전용
TELEGRAM_CHAT_ID_REPORT = os.getenv("TELEGRAM_CHAT_ID_REPORT", "")

# ── 거래 모드 ─────────────────────────────────────────────────
# iris_v1_bot.py는 이 값을 참조하지 않음 — 항상 실거래 고정 설계라 이 변수로
# 거래여부가 갈리는 봇은 현재 없음(과거 다중포트폴리오 실험용, 2026-07-11 삭제).
# v3는 이 TRADE_MODE를 그대로 씀.
TRADE_MODE = os.getenv("TRADE_MODE", "paper")  # "paper" | "real"

# 2026-08-19: v2·v3가 그동안 같은 TRADE_MODE를 공유해서 둘 중 하나만 real/paper로
# 바꿀 방법이 없었음 — "v2는 시장 상황상 페이퍼(=결과만 기록)로 돌리고 v3만 실거래
# 유지"가 필요해져 v2 전용 변수를 분리함. 미설정시 기존 TRADE_MODE를 그대로
# 따르므로(하위호환) v2/v3가 항상 같이 움직이던 이전 동작과 100% 동일하게 유지된다.
IRIS_V2_TRADE_MODE = os.getenv("IRIS_V2_TRADE_MODE", TRADE_MODE)  # "paper" | "real"

# ── 백테스트 ──────────────────────────────────────────────────
INITIAL_CAPITAL = _get("INITIAL_CAPITAL", 10_000_000, int)  # bot/daily_backtest.py 누적계산 기준


def validate():
    """시작 시 필수값 확인 — 실거래봇 run() 진입부에서 호출."""
    missing = []
    if not KIS_APP_KEY:    missing.append("KIS_APP_KEY")
    if not KIS_APP_SECRET: missing.append("KIS_APP_SECRET")
    if not KIS_ACCOUNT:    missing.append("KIS_ACCOUNT")
    if missing:
        raise RuntimeError(f".env 필수값 누락: {', '.join(missing)}")
