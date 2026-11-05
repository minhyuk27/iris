"""
전략 레지스트리 — strategy/*_strategy.py 자동 발견
새 전략 추가: strategy/k4_strategy.py 파일에 STRATEGY_META + run_daily_backtest() 정의하면 끝.
"""
import importlib
from pathlib import Path

_STRATEGY_DIR = Path(__file__).parent


def discover_strategies() -> dict[str, dict]:
    """
    strategy/*_strategy.py 파일을 스캔해 {id: {meta, module}} dict 반환.
    STRATEGY_META dict와 run_daily_backtest(trade_date) 함수가 없으면 건너뜀.
    """
    result = {}
    for f in sorted(_STRATEGY_DIR.glob("*_strategy.py")):
        mod_name = f"strategy.{f.stem}"
        try:
            mod  = importlib.import_module(mod_name)
            meta = getattr(mod, "STRATEGY_META", None)
            fn   = getattr(mod, "run_daily_backtest", None)
            if meta and isinstance(meta, dict) and callable(fn):
                sid = meta.get("id", f.stem.replace("_strategy", ""))
                result[sid] = {"meta": meta, "module": mod}
        except Exception as e:
            import logging
            logging.getLogger("registry").warning(f"전략 로드 실패 [{mod_name}]: {e}")
    return result


def list_strategies() -> list[dict]:
    """STRATEGY_META 목록만 반환 (모듈 제외)"""
    return [v["meta"] for v in discover_strategies().values()]


def get_strategy(sid: str) -> dict | None:
    """특정 id의 {meta, module} 반환. 없으면 None."""
    return discover_strategies().get(sid)


def run_backtest_for(sid: str, trade_date: str) -> dict | None:
    """해당 전략의 run_daily_backtest 호출. 오류 시 None."""
    entry = get_strategy(sid)
    if entry is None:
        return None
    try:
        return entry["module"].run_daily_backtest(trade_date)
    except Exception as e:
        import logging
        logging.getLogger("registry").error(f"백테스트 오류 [{sid}]: {e}", exc_info=True)
        return None
