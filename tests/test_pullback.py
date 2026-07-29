"""strategy/pullback.py の単体テスト。"""

import pandas as pd
import pytest

from strategy.pullback import (
    MarketFilterConfig,
    SignalResult,
    compute_deviation_pct,
    detect_pullback_signal,
)


def _make_df(closes: list) -> pd.DataFrame:
    return pd.DataFrame({"close": closes})


def test_raises_when_not_enough_data() -> None:
    df = _make_df([100.0] * 19)  # ma_window(20) 未満

    with pytest.raises(ValueError):
        detect_pullback_signal("TEST", df, ma_window=20)


def test_no_signal_when_flat() -> None:
    df = _make_df([100.0] * 20)

    result = detect_pullback_signal("TEST", df, ma_window=20, threshold_pct=5.0)

    assert result.should_buy is False
    assert result.deviation_pct == pytest.approx(0.0)
    assert result.latest_close == 100.0
    assert result.moving_average == pytest.approx(100.0)


def test_no_signal_when_price_rises() -> None:
    closes = [100.0] * 19 + [110.0]
    df = _make_df(closes)

    result = detect_pullback_signal("TEST", df, ma_window=20, threshold_pct=5.0)

    assert result.should_buy is False
    assert result.deviation_pct > 0


def test_buy_signal_on_large_drop() -> None:
    closes = [100.0] * 19 + [80.0]
    df = _make_df(closes)

    result = detect_pullback_signal("TEST", df, ma_window=20, threshold_pct=5.0)

    assert result.should_buy is True
    assert result.deviation_pct < -5.0


def test_boundary_at_exact_threshold_triggers_buy() -> None:
    # 19本を100.0に固定し、最終値だけ調整して乖離率をちょうど-5.0%にする。
    ma_window = 20
    threshold_pct = 5.0
    base = 100.0
    t = threshold_pct / 100.0
    n = ma_window
    # MA = ((n-1)*base + last_close) / n かつ (last_close - MA) / MA == -t
    # を last_close について解いた式。
    last_close = (n - 1) * base * (1 - t) / (n - 1 + t)
    closes = [base] * (ma_window - 1) + [last_close]
    df = _make_df(closes)

    result = detect_pullback_signal(
        "TEST", df, ma_window=ma_window, threshold_pct=threshold_pct
    )

    assert result.deviation_pct == pytest.approx(-threshold_pct, abs=1e-6)
    assert result.should_buy is True  # 境界値は <= なので買いシグナル


def test_only_last_ma_window_rows_affect_result() -> None:
    ma_window = 20
    old_noise = [500.0, 10.0, 300.0, 1.0, 250.0]  # ウィンドウ外の古いデータ
    recent = [100.0] * 19 + [80.0]
    df = _make_df(old_noise + recent)

    result = detect_pullback_signal("TEST", df, ma_window=ma_window, threshold_pct=5.0)

    assert result.moving_average == pytest.approx(99.0)  # (19*100+80)/20
    assert result.should_buy is True


def test_returns_signal_result_with_expected_fields() -> None:
    closes = [100.0] * 19 + [80.0]
    df = _make_df(closes)

    result = detect_pullback_signal("AAPL", df, ma_window=20, threshold_pct=5.0)

    assert isinstance(result, SignalResult)
    assert result.symbol == "AAPL"
    assert result.latest_close == 80.0


def test_custom_threshold_pct() -> None:
    closes = [100.0] * 19 + [97.0]  # 乖離率は-5%未満(閾値を下回らない)
    df = _make_df(closes)

    # 閾値2%なら買いシグナル、閾値5%なら買いシグナルなし
    loose = detect_pullback_signal("TEST", df, ma_window=20, threshold_pct=2.0)
    strict = detect_pullback_signal("TEST", df, ma_window=20, threshold_pct=5.0)

    assert loose.should_buy is True
    assert strict.should_buy is False


# --- 市場フィルター（指数の乖離率による追加条件） -------------------------------


def _drop_df() -> pd.DataFrame:
    """絶対乖離だけなら買いシグナルが出る形（-19.5%）。"""
    return _make_df([100.0] * 19 + [80.0])


def test_market_filter_disabled_by_default_keeps_signal() -> None:
    result = detect_pullback_signal(
        "TEST", _drop_df(), ma_window=20, threshold_pct=5.0,
        market_deviation_pct=-10.0, market_filter=MarketFilterConfig(),
    )

    assert result.should_buy is True
    # フィルターを課さなくても、指数の乖離率と相対乖離は観測値として残す。
    assert result.market_deviation_pct == pytest.approx(-10.0)
    assert result.relative_deviation_pct == pytest.approx(result.deviation_pct + 10.0)


def test_regime_filter_blocks_entry_when_market_falls_too_far() -> None:
    config = MarketFilterConfig(min_deviation_pct=-3.0)

    assert detect_pullback_signal(
        "TEST", _drop_df(), market_deviation_pct=-5.0, market_filter=config,
    ).should_buy is False
    assert detect_pullback_signal(
        "TEST", _drop_df(), market_deviation_pct=-1.0, market_filter=config,
    ).should_buy is True


def test_panic_filter_requires_market_to_be_down() -> None:
    config = MarketFilterConfig(max_deviation_pct=-1.0)

    assert detect_pullback_signal(
        "TEST", _drop_df(), market_deviation_pct=0.5, market_filter=config,
    ).should_buy is False
    assert detect_pullback_signal(
        "TEST", _drop_df(), market_deviation_pct=-2.0, market_filter=config,
    ).should_buy is True


def test_relative_threshold_requires_idiosyncratic_drop() -> None:
    config = MarketFilterConfig(relative_threshold_pct=3.0)

    # 個別-19.5% に対し指数-18%: 差は-1.5%しかなく、市場全体の下げで説明できる。
    assert detect_pullback_signal(
        "TEST", _drop_df(), market_deviation_pct=-18.0, market_filter=config,
    ).should_buy is False
    # 指数-1%: 差は-18.5%で、その銘柄固有の下げ。
    assert detect_pullback_signal(
        "TEST", _drop_df(), market_deviation_pct=-1.0, market_filter=config,
    ).should_buy is True


def test_missing_market_deviation_blocks_entry_when_filter_enabled() -> None:
    """指数の乖離率が無いときは見送る（分からないものを有利側に倒さない）。"""
    result = detect_pullback_signal(
        "TEST", _drop_df(),
        market_deviation_pct=None, market_filter=MarketFilterConfig(min_deviation_pct=-3.0),
    )

    assert result.should_buy is False
    assert result.relative_deviation_pct is None


def test_market_filter_does_not_create_signal_without_absolute_pullback() -> None:
    """フィルターは条件を絞るだけで、乖離が浅い銘柄を買いに変えてはならない。"""
    result = detect_pullback_signal(
        "TEST", _make_df([100.0] * 20), market_deviation_pct=-20.0,
        market_filter=MarketFilterConfig(max_deviation_pct=-1.0),
    )

    assert result.should_buy is False


def test_compute_deviation_pct_matches_signal_deviation() -> None:
    df = _drop_df()

    assert compute_deviation_pct(df["close"], 20) == pytest.approx(
        detect_pullback_signal("TEST", df, ma_window=20).deviation_pct
    )
