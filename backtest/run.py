"""ヒストリカルデータを取得し、バックテスト/ウォークフォワード検証を実行するCLI。

実行例:
    python -m backtest.run --symbol RIVN --duration "2 Y" --bar-size "1 day"
    python -m backtest.run --symbol RIVN --mode backtest --duration "2 Y"
"""

import argparse
import asyncio
import logging

from backtest.engine import BacktestConfig, run_backtest
from backtest.metrics import PerformanceMetrics, compute_metrics
from backtest.walk_forward import ParameterGrid, run_walk_forward
from core.connection import IBKRConnection
from data.market_data import get_historical_bars_async, qualify_stock_async

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="プルバック戦略のバックテスト/ウォークフォワード検証")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--duration", default="2 Y", help="reqHistoricalDataのdurationStr")
    parser.add_argument("--bar-size", default="1 day", help="reqHistoricalDataのbarSizeSetting")
    parser.add_argument("--mode", choices=["backtest", "walk-forward"], default="walk-forward")
    parser.add_argument("--train-bars", type=int, default=252)
    parser.add_argument("--test-bars", type=int, default=63)
    return parser.parse_args()


def _log_metrics(metrics: PerformanceMetrics) -> None:
    logger.info(
        "trades=%d win_rate=%.1f%% profit_factor=%.2f total_return=%.2f%% "
        "max_drawdown=%.2f%% sharpe=%.2f",
        metrics.num_trades, metrics.win_rate_pct, metrics.profit_factor,
        metrics.total_return_pct, metrics.max_drawdown_pct, metrics.sharpe_ratio,
    )


async def main() -> None:
    args = _parse_args()

    connection = IBKRConnection()
    try:
        ib = await connection.connect_async()
        contract = await qualify_stock_async(ib, args.symbol)
        df = await get_historical_bars_async(
            ib, contract, duration=args.duration, bar_size=args.bar_size,
        )
    finally:
        await connection.disconnect_async()

    if df.empty:
        logger.error("%s のヒストリカルデータを取得できませんでした。", args.symbol)
        return

    if args.mode == "backtest":
        result = run_backtest(args.symbol, df, BacktestConfig())
        _log_metrics(compute_metrics(result))
        return

    grid = ParameterGrid()
    wf_result = run_walk_forward(
        args.symbol, df, grid, train_bars=args.train_bars, test_bars=args.test_bars,
    )
    for window in wf_result.windows:
        logger.info(
            "検証期間[%d:%d] best_config=%s test_return=%.2f%% test_trades=%d",
            window.test_start_index, window.test_end_index, window.best_config,
            window.test_metrics.total_return_pct, window.test_metrics.num_trades,
        )

    logger.info("=== Out-of-sample 集計（全ウィンドウの検証期間トレードを合算） ===")
    summary = wf_result.combined_test_summary
    logger.info(
        "trades=%d win_rate=%.1f%% profit_factor=%.2f total_pnl=%.2f",
        summary.num_trades, summary.win_rate_pct, summary.profit_factor, summary.total_pnl,
    )


if __name__ == "__main__":
    asyncio.run(main())
