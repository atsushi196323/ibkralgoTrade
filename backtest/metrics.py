"""バックテスト結果からパフォーマンス指標を算出する。"""

import math
from dataclasses import dataclass
from typing import List

import pandas as pd

from backtest.engine import BacktestResult, Trade


@dataclass
class PerformanceMetrics:
    num_trades: int
    win_rate_pct: float
    profit_factor: float
    avg_win_pct: float
    avg_loss_pct: float
    total_return_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float


@dataclass
class TradeSummary:
    num_trades: int
    win_rate_pct: float
    profit_factor: float
    avg_win_pct: float
    avg_loss_pct: float
    total_pnl: float


def compute_metrics(result: BacktestResult, periods_per_year: int = 252) -> PerformanceMetrics:
    trade_summary = summarize_trades(result.trades)

    total_return_pct = (
        (result.final_equity - result.initial_equity) / result.initial_equity * 100.0
    )

    return PerformanceMetrics(
        num_trades=trade_summary.num_trades,
        win_rate_pct=trade_summary.win_rate_pct,
        profit_factor=trade_summary.profit_factor,
        avg_win_pct=trade_summary.avg_win_pct,
        avg_loss_pct=trade_summary.avg_loss_pct,
        total_return_pct=total_return_pct,
        max_drawdown_pct=_max_drawdown_pct(result.equity_curve),
        sharpe_ratio=_sharpe_ratio(result.equity_curve, periods_per_year),
    )


def summarize_trades(trades: List[Trade]) -> TradeSummary:
    num_trades = len(trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]

    win_rate_pct = (len(wins) / num_trades * 100.0) if num_trades > 0 else 0.0
    avg_win_pct = (sum(t.pnl_pct for t in wins) / len(wins)) if wins else 0.0
    avg_loss_pct = (sum(t.pnl_pct for t in losses) / len(losses)) if losses else 0.0

    gross_profit = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = math.inf if gross_profit > 0 else 0.0

    total_pnl = sum(t.pnl for t in trades)

    return TradeSummary(
        num_trades=num_trades,
        win_rate_pct=win_rate_pct,
        profit_factor=profit_factor,
        avg_win_pct=avg_win_pct,
        avg_loss_pct=avg_loss_pct,
        total_pnl=total_pnl,
    )


def _max_drawdown_pct(equity_curve: pd.Series) -> float:
    if equity_curve.empty:
        return 0.0
    running_max = equity_curve.cummax()
    drawdown = (equity_curve - running_max) / running_max * 100.0
    return float(drawdown.min())


def _sharpe_ratio(equity_curve: pd.Series, periods_per_year: int) -> float:
    if len(equity_curve) < 2:
        return 0.0
    returns = equity_curve.pct_change().dropna()
    if returns.empty or returns.std() == 0:
        return 0.0
    return float(returns.mean() / returns.std() * math.sqrt(periods_per_year))
