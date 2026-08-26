"""シグナル単体の情報量を測るイベントスタディ。

**守っている不変条件は2つで、どちらも破ると「情報がある」ように見える。**
シグナルを出したバーの終値で建てないこと（ルックアヘッド）と、母集団自身の
超過リターンを銘柄選択の力と取り違えないこと（等ウェイト指数）。
"""

import numpy as np
import pandas as pd
import pytest

from backtest.signal_study import build_equal_weight_index, study_signal


def _bars(closes, start="2020-01-01"):
    days = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame({"date": days, "open": closes, "close": closes})


def test_the_entry_is_the_next_bar_not_the_signal_bar() -> None:
    """シグナルバーの終値で建てると、その終値が判定に入っているため未来を見る。"""
    prices = [100.0, 100.0, 200.0, 200.0, 200.0]
    bars = {"A": _bars(prices)}
    flat = _bars([100.0] * 5)

    # 2本目（index=1）でシグナル。翌バーの始値200で建てるので、急騰は取れない。
    def signal(frame):
        return np.array([False, True, False, False, False])

    study = study_signal(bars, flat, signal, "test", horizons=[2])

    assert study.horizons[0].n == 1
    assert study.horizons[0].mean_excess_pct == pytest.approx(0.0)


def test_the_excess_is_measured_against_the_benchmark_over_the_same_span() -> None:
    bars = {"A": _bars([100.0, 100.0, 110.0])}
    benchmark = _bars([50.0, 50.0, 52.5])  # ベンチマークも+5%

    study = study_signal(
        bars, benchmark, lambda f: np.array([True, False, False]), "test", horizons=[1]
    )

    assert study.horizons[0].mean_excess_pct == pytest.approx(5.0)


def test_the_t_statistic_is_clustered_by_date() -> None:
    """同じ日に立った627銘柄ぶんのイベントを独立として数えてはならない。"""
    days = pd.bdate_range("2020-01-01", periods=6)
    bars = {
        f"S{i}": pd.DataFrame(
            {"date": days, "open": [100.0] * 6, "close": [100.0, 100.0, 101.0 + i * 0.1] + [100.0] * 3}
        )
        for i in range(50)
    }
    benchmark = pd.DataFrame({"date": days, "close": [100.0] * 6})

    study = study_signal(
        bars, benchmark, lambda f: np.array([True] + [False] * 5), "test", horizons=[1]
    )
    horizon = study.horizons[0]

    assert horizon.n == 50
    assert horizon.n_days == 1  # 全イベントが同じ日
    assert horizon.t_stat == 0.0  # 独立な観測が1つでは検定できない
    assert horizon.naive_t_stat > 5.0  # 独立と仮定すると有意に見える


def test_the_equal_weight_index_tracks_the_average_symbol() -> None:
    days = pd.bdate_range("2020-01-01", periods=3)
    bars = {
        "UP": pd.DataFrame({"date": days, "close": [100.0, 110.0, 121.0]}),
        "FLAT": pd.DataFrame({"date": days, "close": [100.0, 100.0, 100.0]}),
    }

    index = build_equal_weight_index(bars)

    assert len(index) == 2
    assert index["close"].iloc[0] == pytest.approx(105.0)


def test_an_index_cannot_be_built_without_symbols() -> None:
    with pytest.raises(ValueError):
        build_equal_weight_index({})
