"""ウォークフォワード検証。

ヒストリカルデータを「学習期間(train)」と直後の「検証期間(test)」に区切り、
学習期間内でのみパラメータグリッドをグリッドサーチして最良の設定を選び、
その設定を未知の検証期間に適用した成績だけを記録する。
これを開始位置をずらしながら繰り返すことで、特定の閾値が
たまたまその期間に効いただけ（カーブフィッティング）ではなく、
将来の未知データでも通用するエッジを持つかどうかを検証する。
"""

import logging
from dataclasses import dataclass, replace
from itertools import product
from typing import List, Sequence

import pandas as pd

from backtest.engine import BacktestConfig, Trade, run_backtest
from backtest.metrics import PerformanceMetrics, TradeSummary, compute_metrics, summarize_trades

logger = logging.getLogger(__name__)


@dataclass
class ParameterGrid:
    ma_window: Sequence[int] = (10, 20, 30)
    threshold_pct: Sequence[float] = (3.0, 5.0, 7.0)
    take_profit_pct: Sequence[float] = (8.0, 10.0, 15.0)
    stop_loss_pct: Sequence[float] = (3.0, 5.0, 7.0)
    trailing_stop_pct: Sequence[float] = (5.0,)
    risk_per_trade_pct: Sequence[float] = (1.0,)

    def combinations(self) -> List[BacktestConfig]:
        fields = [
            "ma_window", "threshold_pct", "take_profit_pct",
            "stop_loss_pct", "trailing_stop_pct", "risk_per_trade_pct",
        ]
        value_lists = [getattr(self, name) for name in fields]
        return [BacktestConfig(**dict(zip(fields, combo))) for combo in product(*value_lists)]


@dataclass
class WalkForwardWindow:
    train_start_index: int
    train_end_index: int
    test_start_index: int
    test_end_index: int
    best_config: BacktestConfig
    train_metrics: PerformanceMetrics
    test_metrics: PerformanceMetrics
    test_trades: List[Trade]


@dataclass
class WalkForwardResult:
    symbol: str
    windows: List[WalkForwardWindow]
    combined_test_summary: TradeSummary


def run_walk_forward(
    symbol: str,
    df: pd.DataFrame,
    grid: ParameterGrid,
    train_bars: int,
    test_bars: int,
    initial_equity: float = 100_000.0,
    optimize_metric: str = "total_return_pct",
) -> WalkForwardResult:
    if train_bars <= 0 or test_bars <= 0:
        raise ValueError("train_bars, test_bars は正の整数である必要があります。")

    candidates = grid.combinations()
    if not candidates:
        raise ValueError("パラメータグリッドが空です。")

    windows: List[WalkForwardWindow] = []
    all_test_trades: List[Trade] = []

    step = train_bars + test_bars
    start = 0
    while start + step <= len(df):
        train_df = df.iloc[start: start + train_bars].reset_index(drop=True)
        test_df = df.iloc[start + train_bars: start + step].reset_index(drop=True)

        best_config: BacktestConfig = None
        best_score = float("-inf")
        best_train_metrics: PerformanceMetrics = None

        for candidate in candidates:
            config = replace(candidate, initial_equity=initial_equity)
            train_result = run_backtest(symbol, train_df, config)
            train_metrics = compute_metrics(train_result)
            score = getattr(train_metrics, optimize_metric)
            if score > best_score:
                best_score = score
                best_config = config
                best_train_metrics = train_metrics

        test_result = run_backtest(symbol, test_df, best_config)
        test_metrics = compute_metrics(test_result)

        windows.append(
            WalkForwardWindow(
                train_start_index=start,
                train_end_index=start + train_bars - 1,
                test_start_index=start + train_bars,
                test_end_index=start + step - 1,
                best_config=best_config,
                train_metrics=best_train_metrics,
                test_metrics=test_metrics,
                test_trades=test_result.trades,
            )
        )
        all_test_trades.extend(test_result.trades)

        start += step

    combined_test_summary = summarize_trades(all_test_trades)

    logger.info(
        "[%s] ウォークフォワード検証完了: windows=%d out-of-sample trades=%d win_rate=%.1f%% profit_factor=%.2f",
        symbol, len(windows), combined_test_summary.num_trades,
        combined_test_summary.win_rate_pct, combined_test_summary.profit_factor,
    )

    return WalkForwardResult(symbol=symbol, windows=windows, combined_test_summary=combined_test_summary)
