"""ヒストリカルデータを取得し、バックテスト/ウォークフォワード検証を実行するCLI。

データ源は2通り:
  - IBKR（既定）  … 本番と同じバーを使う。IB Gatewayへの接続が必要。
  - CSV (--csv)   … 外部データ（yfinance/Stooq等）。接続不要でオフライン検証できる。

実行例:
    python -m backtest.run --symbol RIVN --duration "2 Y" --bar-size "1 day"
    python -m backtest.run --symbol RIVN --mode backtest --duration "2 Y"
    python -m backtest.run --csv data/RIVN.csv --mode backtest
    python -m backtest.run --csv data/RIVN.csv --no-costs   # コスト影響の比較用
"""

import argparse
import asyncio
import logging
import os
from dataclasses import replace
from typing import Optional

import pandas as pd

from backtest.costs import ZERO_COST, CostModel
from backtest.csv_source import load_bars_from_csv
from backtest.engine import BacktestConfig, run_backtest
from backtest.metrics import PerformanceMetrics, compute_metrics
from backtest.walk_forward import (
    DEFAULT_MIN_TRADES_FOR_SELECTION,
    ParameterGrid,
    run_walk_forward,
)
from core.connection import IBKRConnection
from data.market_data import get_historical_bars_async, qualify_stock_async

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="プルバック戦略のバックテスト/ウォークフォワード検証")
    parser.add_argument(
        "--symbol",
        help="銘柄シンボル。--csv 指定時は省略可（ファイル名から推定する）。",
    )
    parser.add_argument(
        "--csv",
        help="ヒストリカルバーのCSVパス。指定するとIBKRへ接続せずこのファイルを使う。",
    )
    parser.add_argument(
        "--price-column",
        help="CSVで終値として使う列名。既定は close（無ければ adj close）。",
    )
    parser.add_argument("--duration", default="2 Y", help="reqHistoricalDataのdurationStr（IBKR使用時のみ）")
    parser.add_argument("--bar-size", default="1 day", help="reqHistoricalDataのbarSizeSetting（IBKR使用時のみ）")
    parser.add_argument("--mode", choices=["backtest", "walk-forward"], default="walk-forward")
    parser.add_argument("--train-bars", type=int, default=252)
    parser.add_argument("--test-bars", type=int, default=63)
    parser.add_argument(
        "--step-bars", type=int, default=None,
        help="ウィンドウを1回に前進させるバー数。既定は --test-bars"
             "（検証期間が重複せず隙間なく並ぶ）。",
    )
    parser.add_argument(
        "--min-trades", type=int, default=DEFAULT_MIN_TRADES_FOR_SELECTION,
        help="学習期間でこの回数以上トレードした設定だけを選定対象にする。",
    )

    costs = parser.add_argument_group("取引コスト（既定値はIBKR Tiered・米国株相当）")
    costs.add_argument("--commission-per-share", type=float, default=CostModel.commission_per_share)
    costs.add_argument("--min-commission", type=float, default=CostModel.min_commission_per_order)
    costs.add_argument(
        "--max-commission-pct", type=float, default=CostModel.max_commission_pct_of_notional,
        help="約定代金に対する手数料の上限(%%)。0以下で上限なし。",
    )
    costs.add_argument(
        "--slippage-pct", type=float, default=CostModel.slippage_pct,
        help="片道あたりのスリッページ(%%)。買いは高く、売りは安く約定するものとして扱う。",
    )
    costs.add_argument(
        "--no-costs", action="store_true",
        help="手数料・スリッページを一切かけない（コスト影響の比較専用。収益性の判断には使わないこと）。",
    )

    args = parser.parse_args()

    if not args.csv and not args.symbol:
        parser.error("--symbol は必須です（--csv を使う場合のみ省略できます）。")

    return args


def _build_cost_model(args: argparse.Namespace) -> CostModel:
    if args.no_costs:
        logger.warning(
            "コストを無視して検証します(--no-costs)。この結果は実運用の収益性の"
            "判断材料にはなりません。"
        )
        return ZERO_COST

    return CostModel(
        commission_per_share=args.commission_per_share,
        min_commission_per_order=args.min_commission,
        max_commission_pct_of_notional=args.max_commission_pct,
        slippage_pct=args.slippage_pct,
    )


def _resolve_symbol(args: argparse.Namespace) -> str:
    if args.symbol:
        return args.symbol
    # --csv のみ指定された場合、ファイル名（拡張子を除く）を銘柄名として扱う。
    # ここはログ表示とTradeレコードのラベルにしか使わないため、推定で足りる。
    return os.path.splitext(os.path.basename(args.csv))[0].upper()


async def _load_bars_async(args: argparse.Namespace) -> pd.DataFrame:
    if args.csv:
        return load_bars_from_csv(args.csv, price_column=args.price_column)

    connection = IBKRConnection()
    try:
        ib = await connection.connect_async()
        contract = await qualify_stock_async(ib, args.symbol)
        return await get_historical_bars_async(
            ib, contract, duration=args.duration, bar_size=args.bar_size,
        )
    finally:
        await connection.disconnect_async()


def _log_metrics(metrics: PerformanceMetrics, total_commission: Optional[float] = None) -> None:
    logger.info(
        "trades=%d win_rate=%.1f%% profit_factor=%.2f total_return=%.2f%% "
        "max_drawdown=%.2f%% sharpe=%.2f",
        metrics.num_trades, metrics.win_rate_pct, metrics.profit_factor,
        metrics.total_return_pct, metrics.max_drawdown_pct, metrics.sharpe_ratio,
    )
    if total_commission is not None:
        logger.info("支払い手数料の合計=%.2f USD（上記の損益は控除後）", total_commission)


async def main() -> None:
    args = _parse_args()
    symbol = _resolve_symbol(args)
    cost_model = _build_cost_model(args)

    df = await _load_bars_async(args)

    if df.empty:
        logger.error("%s のヒストリカルデータを取得できませんでした。", symbol)
        return

    if args.mode == "backtest":
        config = replace(BacktestConfig(), costs=cost_model)
        result = run_backtest(symbol, df, config)
        _log_metrics(compute_metrics(result), sum(t.commission for t in result.trades))
        return

    grid = ParameterGrid()
    wf_result = run_walk_forward(
        symbol, df, grid, train_bars=args.train_bars, test_bars=args.test_bars,
        costs=cost_model, step_bars=args.step_bars,
        min_trades_for_selection=args.min_trades,
    )
    for window in wf_result.windows:
        logger.info(
            "検証期間[%d:%d] best_config=%s test_return=%.2f%% test_trades=%d",
            window.test_start_index, window.test_end_index, window.best_config,
            window.test_metrics.total_return_pct, window.test_metrics.num_trades,
        )

    logger.info("=== Out-of-sample 集計（全ウィンドウの検証期間トレードを合算） ===")
    if wf_result.skipped_windows:
        logger.warning(
            "%d個のウィンドウは、学習期間で%d回以上トレードした候補が無かったため見送りました。",
            wf_result.skipped_windows, args.min_trades,
        )
    summary = wf_result.combined_test_summary
    logger.info(
        "trades=%d win_rate=%.1f%% profit_factor=%.2f total_pnl=%.2f",
        summary.num_trades, summary.win_rate_pct, summary.profit_factor, summary.total_pnl,
    )
    logger.info(
        "支払い手数料の合計=%.2f USD（上記の損益は控除後）",
        sum(t.commission for w in wf_result.windows for t in w.test_trades),
    )


if __name__ == "__main__":
    asyncio.run(main())
