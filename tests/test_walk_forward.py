"""backtest/walk_forward.py の単体テスト。"""

import pandas as pd
import pytest

from backtest.engine import BacktestConfig
from backtest.walk_forward import ParameterGrid, run_walk_forward


def _make_cyclical_df(num_cycles: int) -> pd.DataFrame:
    """5本フラット+急落+段階的な回復を1サイクル(10本)として繰り返す合成データ。"""
    cycle = [100.0] * 5 + [90.0, 92.0, 95.0, 98.0, 106.0]
    closes = cycle * num_cycles
    return pd.DataFrame({"close": closes})


# --- ParameterGrid ---------------------------------------------------------------


def test_parameter_grid_combinations_count() -> None:
    grid = ParameterGrid(
        ma_window=(5, 10),
        threshold_pct=(3.0, 5.0),
        take_profit_pct=(10.0,),
        stop_loss_pct=(5.0,),
        trailing_stop_pct=(3.0,),
        risk_per_trade_pct=(1.0,),
    )

    combos = grid.combinations()

    assert len(combos) == 4
    assert all(isinstance(c, BacktestConfig) for c in combos)


# --- run_walk_forward -------------------------------------------------------------


def test_raises_on_non_positive_bars() -> None:
    df = _make_cyclical_df(4)
    grid = ParameterGrid(ma_window=(5,))

    with pytest.raises(ValueError):
        run_walk_forward("TEST", df, grid, train_bars=0, test_bars=8)

    with pytest.raises(ValueError):
        run_walk_forward("TEST", df, grid, train_bars=8, test_bars=0)


def test_raises_on_empty_grid() -> None:
    df = _make_cyclical_df(4)
    empty_grid = ParameterGrid(
        ma_window=(), threshold_pct=(), take_profit_pct=(),
        stop_loss_pct=(), trailing_stop_pct=(), risk_per_trade_pct=(),
    )

    with pytest.raises(ValueError):
        run_walk_forward("TEST", df, empty_grid, train_bars=8, test_bars=8)


def test_walk_forward_produces_windows_and_out_of_sample_summary() -> None:
    df = _make_cyclical_df(8)  # 8サイクル x 10本 = 80本
    grid = ParameterGrid(
        ma_window=(5,),
        threshold_pct=(5.0,),
        take_profit_pct=(10.0,),
        stop_loss_pct=(5.0,),
        trailing_stop_pct=(50.0,),
        risk_per_trade_pct=(1.0,),
    )

    result = run_walk_forward("TEST", df, grid, train_bars=30, test_bars=10, initial_equity=100_000.0)

    assert len(result.windows) == 2  # (30+10)*2 = 80 <= 80
    for window in result.windows:
        assert window.best_config.ma_window == 5
        assert window.test_metrics.num_trades >= 0

    assert result.combined_test_summary.num_trades >= 0
    assert result.symbol == "TEST"


def test_walk_forward_selects_best_scoring_config_per_window() -> None:
    df = _make_cyclical_df(8)
    # take_profit=1.0%は回復の初動だけで利確してしまい大きな含み益を逃す設定、
    # take_profit=10.0%の方が1サイクルあたりの利益が大きくなる（実測で約11% vs 約1.3%）。
    grid = ParameterGrid(
        ma_window=(5,),
        threshold_pct=(5.0,),
        take_profit_pct=(1.0, 10.0),
        stop_loss_pct=(5.0,),
        trailing_stop_pct=(50.0,),  # トレーリングは発火させない
        risk_per_trade_pct=(1.0,),
    )

    result = run_walk_forward("TEST", df, grid, train_bars=30, test_bars=10, initial_equity=100_000.0)

    assert len(result.windows) == 2
    for window in result.windows:
        assert window.best_config.take_profit_pct == 10.0


def test_no_windows_when_data_shorter_than_one_full_step() -> None:
    df = _make_cyclical_df(2)  # 20本
    grid = ParameterGrid(ma_window=(5,))

    result = run_walk_forward("TEST", df, grid, train_bars=30, test_bars=10)

    assert result.windows == []
    assert result.combined_test_summary.num_trades == 0
