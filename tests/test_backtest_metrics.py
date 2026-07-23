"""backtest/metrics.py の単体テスト。"""

import math

import pandas as pd
import pytest

from backtest.engine import BacktestResult, Trade
from backtest.metrics import compute_metrics, summarize_trades


def _trade(pnl: float, pnl_pct: float, reason: str = "TAKE_PROFIT") -> Trade:
    return Trade(
        symbol="TEST",
        entry_date=0,
        entry_price=100.0,
        exit_date=1,
        exit_price=100.0 + pnl_pct,
        quantity=1,
        reason=reason,
        pnl=pnl,
        pnl_pct=pnl_pct,
    )


# --- summarize_trades ----------------------------------------------------------


def test_summarize_trades_empty() -> None:
    summary = summarize_trades([])

    assert summary.num_trades == 0
    assert summary.win_rate_pct == 0.0
    assert summary.profit_factor == 0.0
    assert summary.total_pnl == 0.0


def test_summarize_trades_mixed_wins_and_losses() -> None:
    trades = [
        _trade(pnl=100.0, pnl_pct=10.0),
        _trade(pnl=200.0, pnl_pct=20.0),
        _trade(pnl=-50.0, pnl_pct=-5.0, reason="STOP_LOSS"),
    ]

    summary = summarize_trades(trades)

    assert summary.num_trades == 3
    assert summary.win_rate_pct == pytest.approx(200 / 3)
    assert summary.profit_factor == pytest.approx(300.0 / 50.0)
    assert summary.avg_win_pct == pytest.approx(15.0)
    assert summary.avg_loss_pct == pytest.approx(-5.0)
    assert summary.total_pnl == pytest.approx(250.0)


def test_summarize_trades_all_wins_gives_infinite_profit_factor() -> None:
    trades = [_trade(pnl=100.0, pnl_pct=10.0)]

    summary = summarize_trades(trades)

    assert summary.profit_factor == math.inf


def test_summarize_trades_all_losses() -> None:
    trades = [_trade(pnl=-100.0, pnl_pct=-10.0, reason="STOP_LOSS")]

    summary = summarize_trades(trades)

    assert summary.win_rate_pct == 0.0
    assert summary.profit_factor == 0.0


# --- compute_metrics ------------------------------------------------------------


def test_compute_metrics_no_trades_flat_equity() -> None:
    equity_curve = pd.Series([100_000.0] * 5)
    result = BacktestResult(
        symbol="TEST", config=None, initial_equity=100_000.0, final_equity=100_000.0,
        trades=[], equity_curve=equity_curve,
    )

    metrics = compute_metrics(result)

    assert metrics.num_trades == 0
    assert metrics.total_return_pct == 0.0
    assert metrics.max_drawdown_pct == 0.0
    assert metrics.sharpe_ratio == 0.0


def test_compute_metrics_positive_return_and_drawdown() -> None:
    # 100,000 -> 110,000 -> 90,000(最大ドローダウン) -> 120,000
    equity_curve = pd.Series([100_000.0, 110_000.0, 90_000.0, 120_000.0])
    trades = [_trade(pnl=20_000.0, pnl_pct=20.0)]
    result = BacktestResult(
        symbol="TEST", config=None, initial_equity=100_000.0, final_equity=120_000.0,
        trades=trades, equity_curve=equity_curve,
    )

    metrics = compute_metrics(result)

    assert metrics.total_return_pct == pytest.approx(20.0)
    # (90,000 - 110,000) / 110,000 * 100
    assert metrics.max_drawdown_pct == pytest.approx((90_000 - 110_000) / 110_000 * 100)
    assert metrics.num_trades == 1
    assert metrics.win_rate_pct == 100.0


def test_compute_metrics_sharpe_zero_when_no_variance() -> None:
    equity_curve = pd.Series([100_000.0, 101_000.0, 102_010.0])  # 常に+1%
    result = BacktestResult(
        symbol="TEST", config=None, initial_equity=100_000.0, final_equity=102_010.0,
        trades=[], equity_curve=equity_curve,
    )

    metrics = compute_metrics(result)

    # リターンが一定(分散0)の場合はシャープレシオを0として扱う
    assert metrics.sharpe_ratio == 0.0
