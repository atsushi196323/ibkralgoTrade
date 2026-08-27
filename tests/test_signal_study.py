"""シグナル単体の情報量を測るイベントスタディ。

**守っている不変条件は2つで、どちらも破ると「情報がある」ように見える。**
シグナルを出したバーの終値で建てないこと（ルックアヘッド）と、母集団自身の
超過リターンを銘柄選択の力と取り違えないこと（等ウェイト指数）。
"""

import numpy as np
import pandas as pd
import pytest

from backtest.signal_study import (
    add_cross_sectional_percentile,
    build_equal_weight_index,
    study_signal,
)


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


# --- 横断ランク -----------------------------------------------------------------


def _dated(days, values):
    """日付と値だけを持つバー（横断ランクの検証用）。"""
    return pd.DataFrame({"date": pd.to_datetime(days), "close": values})


def _value_is_close(frame):
    """close をそのまま順位付けの元にする（テストで意図した順位を作るため）。"""
    return frame["close"].astype(float)


def test_the_cross_sectional_rank_is_joined_by_date_not_by_position() -> None:
    """順位を日付で突き合わせること。位置で揃えてはならない。

    銘柄側に1行でも欠損があると、位置で揃えた瞬間に日付がずれる。**ずれは
    例外にならず、未来の順位を見て判定する形で成績を良く見せる**
    （`backtest/market_reference.py` と同じ理由）。

    Bだけ2日目が欠けている。位置で揃えるとBの2行目(1/3)に1/2の順位が付く。
    """
    bars = {
        "A": _dated(["2026-01-01", "2026-01-02", "2026-01-03"], [1.0, 1.0, 3.0]),
        "B": _dated(["2026-01-01", "2026-01-03"], [2.0, 1.0]),
        "C": _dated(["2026-01-01", "2026-01-02", "2026-01-03"], [3.0, 2.0, 2.0]),
    }

    out = add_cross_sectional_percentile(bars, _value_is_close, "r", min_symbols_per_day=2)

    # 1/3 は A=3.0 > C=2.0 > B=1.0 なので B が最下位。
    b = out["B"]
    assert b["r"].iloc[1] == pytest.approx(1 / 3)
    # 1/1 は B が真ん中（A=1 < B=2 < C=3）。位置で揃えるとここが 1/3 になる。
    assert b["r"].iloc[0] == pytest.approx(2 / 3)


def test_a_day_with_too_few_symbols_gets_no_rank() -> None:
    """母集団が薄い日に順位を付けないこと。

    データの先頭では値を算出できる銘柄が数件しかない。「3銘柄中の1位」を
    「上位10%」として扱うと、その期間だけシグナルが乱発される。
    """
    bars = {
        "A": _dated(["2026-01-01", "2026-01-02"], [1.0, 1.0]),
        "B": _dated(["2026-01-02"], [2.0]),
    }

    out = add_cross_sectional_percentile(bars, _value_is_close, "r", min_symbols_per_day=2)

    # 1/1 は A しか居ないので順位なし。1/2 は2銘柄そろうので付く。
    assert pd.isna(out["A"]["r"].iloc[0])
    assert out["A"]["r"].iloc[1] == pytest.approx(0.5)


def test_a_symbol_without_a_rank_column_is_filled_with_nan() -> None:
    """順位を作れなかった銘柄も、列だけは持たせること。

    列が無いとシグナル関数側が銘柄ごとに分岐することになり、そこで
    「列が無い＝シグナルなし」と「順位が低い」が混ざる。
    """
    bars = {"A": _dated(["2026-01-01"], [1.0])}

    out = add_cross_sectional_percentile(bars, _value_is_close, "r", min_symbols_per_day=5)

    assert "r" in out["A"].columns
    assert out["A"]["r"].isna().all()


def test_the_original_frames_are_not_modified() -> None:
    """元のDataFrameを書き換えないこと（同じバーを他の測定でも使うため）。"""
    bars = {"A": _dated(["2026-01-01", "2026-01-02"], [1.0, 2.0])}

    add_cross_sectional_percentile(bars, _value_is_close, "r", min_symbols_per_day=1)

    assert "r" not in bars["A"].columns


# --- 右の裾の検算 ---------------------------------------------------------------


def test_a_single_multibagger_flips_the_arithmetic_mean_but_not_the_log_mean() -> None:
    """算術平均が裾の1件で動くこと、対数と中央値は動かないことを固定する。

    2026-08-27に横断モメンタムの下位10%で実測した形そのもの: 勝率42%なのに
    60日の算術平均が +31% になり、対数では -5.75% と符号が逆になった。
    **算術平均だけを見ると、この裾を「銘柄選択の情報」として読む。**
    """
    # 9銘柄は半値、1銘柄だけ6倍になる。
    #   算術平均 = (9×-50% + 500%) / 10 = +5%   （プラス）
    #   対数平均 = (9×ln0.5 + ln6) / 10        （マイナス）
    bars = {f"L{i}": _bars([100.0, 100.0, 50.0]) for i in range(9)}
    bars["W"] = _bars([100.0, 100.0, 600.0])
    flat = _bars([100.0] * 3)

    def always(frame):
        return np.array([True, False, False])

    study = study_signal(bars, flat, always, "tail", horizons=[1])
    result = study.horizons[0]

    # 算術平均は6倍の1件に引っ張られてプラス。
    assert result.mean_excess_pct > 0.0
    # 中央値と対数平均は、大多数である半値側を指す。**符号が逆になる。**
    assert result.median_excess_pct == pytest.approx(-50.0)
    assert result.log_daily_mean_pct < 0.0


def test_the_log_t_stat_is_zero_without_enough_independent_observations() -> None:
    """実効観測が足りないときに t値を出さないこと（0で返す）。"""
    bars = {"A": _bars([100.0, 100.0, 110.0])}
    flat = _bars([100.0] * 3)

    study = study_signal(bars, flat, lambda f: np.array([True, False, False]), "x", horizons=[1])

    assert study.horizons[0].log_t_stat == 0.0
