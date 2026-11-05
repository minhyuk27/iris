"""
IRIS 일간 자동 백테스트
장 마감 후 실행 → 모든 등록 전략의 당일 시뮬레이션 결과를 strategy_perf에 저장.

실행:
  python -m bot.daily_backtest            # 오늘 날짜로 실행
  python -m bot.daily_backtest 20260522   # 특정 날짜로 실행

crontab: 장 마감 후 17:00 KST = 08:00 UTC (수급 수집 완료 후)
  0 8 * * 1-5 cd ~/iris && python -m bot.daily_backtest >> logs/daily_backtest.log 2>&1
"""
import sys
from datetime import datetime, timezone, timedelta

from core.db import init_db, upsert_perf, get_last_perf, get_conn
from core.logger import get_logger
from core.config import INITIAL_CAPITAL
from strategy.registry import discover_strategies

logger = get_logger("daily_backtest", "PREFILTER")

KST = timezone(timedelta(hours=9))


def _today_kst() -> str:
    return datetime.now(KST).strftime("%Y%m%d")


def _calc_cumulative(strategy: str, today_pnl: float, trade_date: str = None) -> tuple[float, float]:
    """직전 레코드 기반 복리 누적 계산 → (new_cum_pct, new_capital)"""
    last = get_last_perf(strategy, before_date=trade_date)
    if last is None:
        prev_capital = float(INITIAL_CAPITAL)
        prev_cum     = 0.0
    else:
        prev_capital = last["capital"] or float(INITIAL_CAPITAL)
        prev_cum     = last["cumulative_pct"] or 0.0

    new_capital  = prev_capital * (1 + today_pnl / 100)
    new_cum      = (new_capital / float(INITIAL_CAPITAL) - 1) * 100
    return round(new_cum, 2), round(new_capital, 0)


def _collect_index(trade_date: str):
    """KOSPI/KOSDAQ 일봉을 market_index_daily에 저장 (전일 대비 수익률 포함)"""
    try:
        from core.kis_api import get_index_daily
        start = (datetime.now(KST) - timedelta(days=10)).strftime("%Y%m%d")
        kospi  = {r["date"]: r["close"] for r in get_index_daily("0001", start)}
        kosdaq = {r["date"]: r["close"] for r in get_index_daily("1001", start)}
        dates = sorted(set(kospi) | set(kosdaq))
        with get_conn() as conn:
            for i, d in enumerate(dates):
                kp_cls = kospi.get(d, 0)
                kd_cls = kosdaq.get(d, 0)
                if i == 0:
                    kp_ret = kd_ret = 0.0
                else:
                    prev = dates[i - 1]
                    kp_prev = kospi.get(prev, 0)
                    kd_prev = kosdaq.get(prev, 0)
                    kp_ret = round((kp_cls / kp_prev - 1) * 100, 2) if kp_prev else 0.0
                    kd_ret = round((kd_cls / kd_prev - 1) * 100, 2) if kd_prev else 0.0
                conn.execute("""
                    INSERT INTO market_index_daily (date, kospi_cls, kosdaq_cls, kospi_ret, kosdaq_ret)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(date) DO UPDATE SET
                        kospi_cls=excluded.kospi_cls, kosdaq_cls=excluded.kosdaq_cls,
                        kospi_ret=excluded.kospi_ret, kosdaq_ret=excluded.kosdaq_ret
                """, (d, kp_cls, kd_cls, kp_ret, kd_ret))
        logger.info(f"지수 수집 완료: KOSPI {kospi.get(trade_date, 'N/A')}, KOSDAQ {kosdaq.get(trade_date, 'N/A')}")
    except Exception as e:
        logger.warning(f"지수 수집 실패 (무시): {e}")


def run_all(trade_date: str = None):
    if trade_date is None:
        trade_date = _today_kst()

    run_dt  = _today_kst()
    logger.info(f"=== 일간 백테스트 시작 | 거래일: {trade_date} ===")

    strategies = discover_strategies()
    if not strategies:
        logger.warning("등록된 전략 없음. strategy/*_strategy.py 파일 확인")
        return

    for sid, entry in strategies.items():
        try:
            result = entry["module"].run_daily_backtest(trade_date)
            if result is None:
                logger.warning(f"[{sid}] 백테스트 결과 없음 (candidates 미선정?)")
                continue

            pnl = result.get("pnl_pct", 0.0)
            cum_pct, capital = _calc_cumulative(sid, pnl, trade_date=trade_date)

            record = {
                "run_date":       run_dt,
                "trade_date":     trade_date,
                "strategy":       sid,
                "trades":         result.get("trades",  0),
                "wins":           result.get("wins",    0),
                "losses":         result.get("losses",  0),
                "pnl_pct":        pnl,
                "cumulative_pct": cum_pct,
                "capital":        capital,
                "strategy_version": result.get("strategy_version"),
            }
            upsert_perf(record)

            logger.info(
                f"[{sid}] 거래:{record['trades']} "
                f"승:{record['wins']} 패:{record['losses']} "
                f"당일:{pnl:+.2f}% 누적:{cum_pct:+.2f}% "
                f"자본:{capital/1e4:.0f}만원"
            )

        except Exception as e:
            logger.error(f"[{sid}] 오류: {e}", exc_info=True)

    _collect_index(trade_date)
    logger.info("=== 일간 백테스트 완료 ===")


if __name__ == "__main__":
    init_db()
    td = sys.argv[1] if len(sys.argv) > 1 else None
    run_all(td)
