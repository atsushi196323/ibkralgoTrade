"""backtest/market_reference.py の単体テスト（指数の乖離率の付与と日付の突き合わせ）。"""

import math

import pandas as pd
import pytest

from backtest.engine import BacktestConfig, run_backtest
from backtest.market_reference import (
    MARKET_DEVIATION_COLUMN,
    attach_market_deviation,
    compute_market_deviation,
)


def _make_bars(closes: list, start: str = "2024-01-01") -> pd.DataFrame:
    dates = pd.date_range(start, periods=len(closes), freq="D")
    return pd.DataFrame({"date": dates, "close": closes})


def test_computes_deviation_from_moving_average() -> None:
    # 直近3本の平均が (100+100+94)/3 = 98.0 なので乖離率は (94-98)/98*100。
    result = compute_market_deviation(_make_bars([100.0, 100.0, 94.0]), ma_window=3)

    assert math.isnan(result[MARKET_DEVIATION_COLUMN].iloc[0])
    assert math.isnan(result[MARKET_DEVIATION_COLUMN].iloc[1])
    assert result[MARKET_DEVIATION_COLUMN].iloc[2] == pytest.approx(-4.0 / 98.0 * 100.0)


def test_requires_date_and_close_columns_on_market_bars() -> None:
    with pytest.raises(ValueError):
        compute_market_deviation(pd.DataFrame({"close": [1.0, 2.0]}), ma_window=2)


def test_attach_aligns_on_date_not_position() -> None:
    """銘柄側に欠損日があっても、指数の値が日付でずれずに付くこと。

    位置インデックスで突き合わせると未来の指数値を参照してしまい（ルックアヘッド）、
    しかも例外にならず成績が良く見えるだけなので、ここが最重要のテスト。
    """
    market = _make_bars([100.0, 100.0, 100.0, 90.0, 80.0])
    # 銘柄側は3日目(2024-01-03)が欠けている。
    symbol_df = _make_bars([10.0, 10.0, 10.0, 10.0, 10.0]).drop(index=2).reset_index(drop=True)

    merged = attach_market_deviation(symbol_df, market, ma_window=3)

    market_deviation = compute_market_deviation(market, ma_window=3)
    expected_last = market_deviation[MARKET_DEVIATION_COLUMN].iloc[4]
    # 最終行(2024-01-05)には、位置が1つずれた1/4の値ではなく1/5の値が付く。
    assert merged[MARKET_DEVIATION_COLUMN].iloc[-1] == pytest.approx(expected_last)
    assert merged["date"].iloc[-1] == pd.Timestamp("2024-01-05")


def test_attach_requires_date_column_on_symbol_bars() -> None:
    with pytest.raises(ValueError):
        attach_market_deviation(
            pd.DataFrame({"close": [1.0, 2.0, 3.0]}), _make_bars([1.0, 2.0, 3.0]), ma_window=2,
        )


def test_unmatched_dates_become_nan() -> None:
    market = _make_bars([100.0, 100.0, 100.0], start="2024-02-01")
    symbol_df = _make_bars([10.0, 10.0, 10.0], start="2024-01-01")

    merged = attach_market_deviation(symbol_df, market, ma_window=2)

    assert merged[MARKET_DEVIATION_COLUMN].isna().all()


# --- エンジン側の取り扱い -------------------------------------------------------


def _falling_bars() -> pd.DataFrame:
    """37本目で大きく下げ、絶対乖離だけなら買いシグナルが出るバー。

    最終バーで下げさせるとエントリー直後にデータが尽き、決済されないまま
    トレードが記録されない。決済まで進むよう後ろに数本残してある。
    """
    return _make_bars([100.0] * 37 + [80.0, 80.0, 80.0])


def test_engine_raises_when_filter_enabled_without_market_column() -> None:
    """黙ってフィルター無しで走ると、フィルター有りの成績と取り違えるため落とす。"""
    config = BacktestConfig(ma_window=20, relative_threshold_pct=3.0)

    with pytest.raises(ValueError):
        run_backtest("TEST", _falling_bars(), config)


def test_engine_blocks_entry_when_market_filter_rejects() -> None:
    df = _falling_bars()
    # 指数も同じだけ下げている＝市場全体の下げなので、相対乖離では買わない。
    market = _make_bars([100.0] * 37 + [80.0, 80.0, 80.0])
    merged = attach_market_deviation(df, market, ma_window=20)

    unfiltered = run_backtest("TEST", merged, BacktestConfig(ma_window=20))
    filtered = run_backtest(
        "TEST", merged, BacktestConfig(ma_window=20, relative_threshold_pct=3.0),
    )

    assert len(unfiltered.trades) == 1
    assert filtered.trades == []


def test_engine_allows_entry_when_drop_is_idiosyncratic() -> None:
    df = _falling_bars()
    market = _make_bars([100.0] * 40)  # 指数は横ばい＝その銘柄固有の下げ
    merged = attach_market_deviation(df, market, ma_window=20)

    result = run_backtest(
        "TEST", merged, BacktestConfig(ma_window=20, relative_threshold_pct=3.0),
    )

    assert len(result.trades) == 1
