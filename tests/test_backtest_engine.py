"""backtest/engine.py の単体テスト。"""

import pandas as pd
import pytest

from backtest.engine import BacktestConfig, Trade, run_backtest
from strategy.exit_signal import REASON_STOP_LOSS, REASON_TAKE_PROFIT


def _make_df(closes: list) -> pd.DataFrame:
    return pd.DataFrame({"close": closes})


def _config(**overrides) -> BacktestConfig:
    base = dict(
        ma_window=5,
        threshold_pct=5.0,
        take_profit_pct=10.0,
        stop_loss_pct=5.0,
        trailing_stop_pct=3.0,
        risk_per_trade_pct=1.0,
        initial_equity=100_000.0,
    )
    base.update(overrides)
    return BacktestConfig(**base)


def test_raises_on_empty_df() -> None:
    with pytest.raises(ValueError):
        run_backtest("TEST", pd.DataFrame(), _config())


def test_raises_when_close_column_missing() -> None:
    df = pd.DataFrame({"open": [1.0, 2.0]})
    with pytest.raises(ValueError):
        run_backtest("TEST", df, _config())


def test_no_trades_when_never_enough_data_for_ma_window() -> None:
    df = _make_df([100.0, 100.0])  # ma_window(5)未満のまま終了
    result = run_backtest("TEST", df, _config())

    assert result.trades == []
    assert result.final_equity == result.initial_equity
    assert len(result.equity_curve) == len(df)


def test_take_profit_trade_realizes_gain() -> None:
    # 5本フラット(100)でMA形成 -> 6本目に急落(90, 買いシグナル) -> 7本目に急騰(100, 利確)
    df = _make_df([100.0] * 5 + [90.0, 100.0])
    result = run_backtest("TEST", df, _config())

    assert len(result.trades) == 1
    trade: Trade = result.trades[0]
    assert trade.reason == REASON_TAKE_PROFIT
    assert trade.entry_price == 90.0
    assert trade.exit_price == 100.0
    # risk_amount=100,000*1%=1,000 / per_share_risk=90*5%=4.5 -> qty=222
    assert trade.quantity == 222
    assert trade.pnl == pytest.approx((100.0 - 90.0) * 222)
    assert result.final_equity == pytest.approx(result.initial_equity + trade.pnl)
    assert len(result.equity_curve) == len(df)
    assert result.equity_curve.iloc[-1] == pytest.approx(result.final_equity)


def test_stop_loss_trade_realizes_loss() -> None:
    # 6本目で買いシグナル(90) -> 7本目に大きく下落(80, 損切り)
    df = _make_df([100.0] * 5 + [90.0, 80.0])
    result = run_backtest("TEST", df, _config())

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.reason == REASON_STOP_LOSS
    assert trade.pnl < 0


def test_force_closes_open_position_at_end_of_data() -> None:
    # 6本目で買いシグナル(90) -> 7本目はTP/SLどちらにも届かない小幅上昇(92)で終了
    df = _make_df([100.0] * 5 + [90.0, 92.0])
    result = run_backtest("TEST", df, _config())

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.reason == "END_OF_DATA"
    assert trade.exit_price == 92.0


def test_no_trade_when_position_size_rounds_to_zero() -> None:
    # 買いシグナルは出るが、口座資金が小さすぎて数量が0になるケース
    df = _make_df([100.0] * 5 + [90.0, 100.0])
    result = run_backtest("TEST", df, _config(initial_equity=1.0))

    assert result.trades == []
    assert result.final_equity == 1.0


def test_uses_default_config_when_none_given() -> None:
    df = _make_df([100.0] * 25)
    result = run_backtest("TEST", df)

    assert result.config.ma_window == 20
    assert result.trades == []
