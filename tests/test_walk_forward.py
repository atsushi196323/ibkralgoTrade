"""backtest/walk_forward.py の単体テスト。"""

import math

import pandas as pd
import pytest

from backtest.engine import BacktestConfig
from backtest.walk_forward import (
    DEFAULT_MIN_TRADES_FOR_SELECTION,
    ParameterGrid,
    run_walk_forward,
)


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

    result = run_walk_forward(
        "TEST", df, grid, train_bars=30, test_bars=10, initial_equity=100_000.0,
        min_trades_for_selection=1,
    )

    # test_barsずつスライドするため start=0,10,20,30,40 の5ウィンドウ
    # （start+40 <= 80 を満たす範囲）。
    assert len(result.windows) == 5
    for window in result.windows:
        assert window.best_config.ma_window == 5
        assert window.test_metrics.num_trades >= 0

    assert result.combined_test_summary.num_trades >= 0
    assert result.symbol == "TEST"


def test_test_windows_tile_without_overlap() -> None:
    """検証期間が重複しないこと（out-of-sampleトレードの二重計上を防ぐ）。"""
    df = _make_cyclical_df(8)
    grid = ParameterGrid(
        ma_window=(5,), threshold_pct=(5.0,), take_profit_pct=(10.0,),
        stop_loss_pct=(5.0,), trailing_stop_pct=(50.0,), risk_per_trade_pct=(1.0,),
    )

    result = run_walk_forward(
        "TEST", df, grid, train_bars=30, test_bars=10, min_trades_for_selection=1,
    )

    starts = [w.test_start_index for w in result.windows]
    ends = [w.test_end_index for w in result.windows]
    assert starts == [30, 40, 50, 60, 70]
    assert all(end < next_start for end, next_start in zip(ends, starts[1:]))


def test_step_bars_overrides_the_slide_width() -> None:
    """step_bars=train+test は旧来の「ウィンドウを重ねない」進め方に相当する。"""
    df = _make_cyclical_df(8)
    grid = ParameterGrid(
        ma_window=(5,), threshold_pct=(5.0,), take_profit_pct=(10.0,),
        stop_loss_pct=(5.0,), trailing_stop_pct=(50.0,), risk_per_trade_pct=(1.0,),
    )

    result = run_walk_forward(
        "TEST", df, grid, train_bars=30, test_bars=10, step_bars=40,
        min_trades_for_selection=1,
    )

    # 同じデータでも、重ねない進め方ではウィンドウが2個しか取れない。
    assert len(result.windows) == 2


def test_raises_on_non_positive_step_bars() -> None:
    df = _make_cyclical_df(8)
    grid = ParameterGrid(ma_window=(5,))

    with pytest.raises(ValueError):
        run_walk_forward("TEST", df, grid, train_bars=30, test_bars=10, step_bars=0)


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

    result = run_walk_forward(
        "TEST", df, grid, train_bars=30, test_bars=10, initial_equity=100_000.0,
        min_trades_for_selection=1,
    )

    assert len(result.windows) == 5
    for window in result.windows:
        assert window.best_config.take_profit_pct == 10.0


def test_no_windows_when_data_shorter_than_one_full_step() -> None:
    df = _make_cyclical_df(2)  # 20本
    grid = ParameterGrid(ma_window=(5,))

    result = run_walk_forward("TEST", df, grid, train_bars=30, test_bars=10)

    assert result.windows == []
    assert result.combined_test_summary.num_trades == 0
    # データ不足であって、トレード数不足による見送りではない。
    assert result.skipped_windows == 0


# --- 最低トレード数によるフィルター ------------------------------------------------


def test_windows_are_skipped_when_no_candidate_meets_min_trades() -> None:
    """学習期間のトレードが少なすぎる場合、その設定を検証せず見送ること。

    ウォークフォワードは過剰最適化の検出が目的なのに、少数トレードの偶然で
    選んだ設定を検証してしまうと、目的そのものが崩れる。
    """
    df = _make_cyclical_df(8)  # 学習期間30本 = 3サイクル -> 最大3トレード
    grid = ParameterGrid(
        ma_window=(5,), threshold_pct=(5.0,), take_profit_pct=(10.0,),
        stop_loss_pct=(5.0,), trailing_stop_pct=(50.0,), risk_per_trade_pct=(1.0,),
    )

    result = run_walk_forward(
        "TEST", df, grid, train_bars=30, test_bars=10, min_trades_for_selection=5,
    )

    assert result.windows == []
    assert result.skipped_windows == 5
    assert result.combined_test_summary.num_trades == 0


def test_min_trades_default_is_strict_enough_to_reject_single_lucky_trades() -> None:
    assert DEFAULT_MIN_TRADES_FOR_SELECTION >= 2


# --- スコアリング -------------------------------------------------------------------


def test_infinite_profit_factor_is_broken_by_total_return() -> None:
    """profit_factorがinf同士のとき、総リターンで決着すること。

    負けトレードが0件だとprofit_factorはinf（metrics.py）。inf同士は
    大小がつかず、放置するとグリッドの並び順で機械的に選ばれてしまう。
    """
    # 全トレードが利確で終わる（＝負け無し）データ。
    df = _make_cyclical_df(8)
    grid = ParameterGrid(
        ma_window=(5,),
        threshold_pct=(5.0,),
        # どちらも負けないが、10.0%の方が1トレードあたりの利益が大きい。
        take_profit_pct=(1.0, 10.0),
        stop_loss_pct=(50.0,),  # 損切りは発火させない
        trailing_stop_pct=(50.0,),
        risk_per_trade_pct=(1.0,),
    )

    result = run_walk_forward(
        "TEST", df, grid, train_bars=30, test_bars=10,
        optimize_metric="profit_factor", min_trades_for_selection=1,
    )

    assert result.windows
    for window in result.windows:
        assert window.train_metrics.profit_factor == math.inf
        assert window.best_config.take_profit_pct == 10.0
