"""backtest/engine.py の単体テスト。"""

import pandas as pd
import pytest

from backtest.costs import ZERO_COST, CostModel
from backtest.engine import BacktestConfig, Trade, run_backtest
from strategy.exit_signal import REASON_STOP_LOSS, REASON_TAKE_PROFIT


def _make_df(closes: list) -> pd.DataFrame:
    return pd.DataFrame({"close": closes})


def _config(**overrides) -> BacktestConfig:
    # シグナル判定そのものを検証するテストではコストを無効化し、
    # 期待値を手計算できるようにする。コストの反映自体は
    # 「--- 取引コスト ---」以下のテストで別途検証する。
    base = dict(
        ma_window=5,
        threshold_pct=5.0,
        take_profit_pct=10.0,
        stop_loss_pct=5.0,
        trailing_stop_pct=3.0,
        risk_per_trade_pct=1.0,
        initial_equity=100_000.0,
        costs=ZERO_COST,
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
    # 5本フラット(100)でMA形成 -> 6本目に急落(90, 買いシグナル) -> 7本目に急騰(100)
    df = _make_df([100.0] * 5 + [90.0, 100.0])
    result = run_backtest("TEST", df, _config())

    assert len(result.trades) == 1
    trade: Trade = result.trades[0]
    assert trade.reason == REASON_TAKE_PROFIT
    assert trade.entry_price == 90.0
    # ブローカーに置いた指値(建値の+10% = 99.0)で約定する。終値の100.0ではない。
    assert trade.exit_price == pytest.approx(99.0)
    # risk_amount=100,000*1%=1,000 / per_share_risk=90*5%=4.5 -> qty=222
    assert trade.quantity == 222
    assert trade.pnl == pytest.approx((99.0 - 90.0) * 222)
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


# --- 取引コスト -------------------------------------------------------------------


def test_default_config_includes_trading_costs() -> None:
    """既定でコストが効いていること。

    コスト無しのバックテストは実運用と乖離した楽観的な数字を出すため、
    既定値がZERO_COSTへ差し替えられていないことをテストで固定する。
    """
    config = BacktestConfig()

    assert config.costs.commission_per_share > 0
    assert config.costs.min_commission_per_order > 0
    assert config.costs.slippage_pct > 0


def test_entry_slippage_makes_the_buy_fill_worse_than_bar_close() -> None:
    costs = CostModel(
        commission_per_share=0.0, min_commission_per_order=0.0,
        max_commission_pct_of_notional=0.0, slippage_pct=1.0,
    )
    df = _make_df([100.0] * 5 + [90.0, 110.0])
    result = run_backtest("TEST", df, _config(costs=costs))

    # 新規建ては成行なので、終値より高く約定する。
    assert result.trades[0].entry_price == pytest.approx(90.0 * 1.01)


def test_stop_fill_takes_slippage_but_limit_fill_does_not() -> None:
    """逆指値は成行になるので滑り、指値は値段どおりに約定すること。

    損切りだけがスリッページを負うのは、逆指値がトリガー後に成行注文へ
    変わるため。指値(利確)は値段より不利な価格では約定しない。
    """
    costs = CostModel(
        commission_per_share=0.0, min_commission_per_order=0.0,
        max_commission_pct_of_notional=0.0, slippage_pct=1.0,
    )

    entry = 90.0 * 1.01
    # OHLCを与え、ギャップ無しでバーの中だけ注文の値段に触れたケースにする。
    tp_bars = _flat_bars(100.0, 5) + [(90.0, 90.0, 90.0, 90.0), (95.0, 110.0, 95.0, 105.0)]
    sl_bars = _flat_bars(100.0, 5) + [(90.0, 90.0, 90.0, 90.0), (90.0, 90.0, 80.0, 85.0)]

    tp_trade = run_backtest("TEST", _make_ohlc_df(tp_bars), _config(costs=costs)).trades[0]
    sl_trade = run_backtest("TEST", _make_ohlc_df(sl_bars), _config(costs=costs)).trades[0]

    assert tp_trade.reason == REASON_TAKE_PROFIT
    assert tp_trade.exit_price == pytest.approx(entry * 1.10)

    assert sl_trade.reason == REASON_STOP_LOSS
    assert sl_trade.exit_price == pytest.approx(entry * 0.95 * 0.99)


def test_commission_is_deducted_from_pnl_and_equity() -> None:
    costs = CostModel(
        commission_per_share=0.01, min_commission_per_order=1.0,
        max_commission_pct_of_notional=0.0, slippage_pct=0.0,
    )
    df = _make_df([100.0] * 5 + [90.0, 100.0])
    result = run_backtest("TEST", df, _config(costs=costs))

    trade = result.trades[0]
    expected_commission = 2 * max(trade.quantity * 0.01, 1.0)

    assert trade.commission == pytest.approx(expected_commission)
    # 利確の指値(90 * 1.10 = 99.0)で約定する。
    assert trade.exit_price == pytest.approx(99.0)
    assert trade.gross_pnl == pytest.approx((99.0 - 90.0) * trade.quantity)
    assert trade.pnl == pytest.approx(trade.gross_pnl - expected_commission)
    assert result.final_equity == pytest.approx(result.initial_equity + trade.pnl)


def test_costs_reduce_final_equity_versus_zero_cost_run() -> None:
    df = _make_df(([100.0] * 5 + [90.0, 100.0]) * 4)

    with_costs = run_backtest("TEST", df, _config(costs=CostModel()))
    without_costs = run_backtest("TEST", df, _config(costs=ZERO_COST))

    assert len(with_costs.trades) == len(without_costs.trades) > 0
    assert with_costs.final_equity < without_costs.final_equity


def test_pnl_pct_is_net_of_costs_and_agrees_in_sign_with_pnl() -> None:
    """手数料負けするトレードでは pnl_pct も負になること。

    pnl_pctをgross基準にすると「pnlは負なのにpnl_pctは正」という
    組み合わせが生じ、metrics側の勝敗判定(pnl基準)と食い違う。
    """
    # 建値からほぼ動かないまま最終バーで決済 -> スリッページと手数料の分だけ負ける。
    costs = CostModel(
        commission_per_share=0.01, min_commission_per_order=1.0,
        max_commission_pct_of_notional=0.0, slippage_pct=0.1,
    )
    df = _make_df([100.0] * 5 + [90.0, 90.0])
    result = run_backtest("TEST", df, _config(costs=costs))

    trade = result.trades[0]
    assert trade.reason == "END_OF_DATA"
    assert trade.pnl < 0
    assert trade.pnl_pct < 0


def test_open_position_equity_curve_excludes_paid_entry_commission() -> None:
    costs = CostModel(
        commission_per_share=0.0, min_commission_per_order=50.0,
        max_commission_pct_of_notional=0.0, slippage_pct=0.0,
    )
    # 6本目でエントリー、7本目は建値のまま（TP/SLに届かない）保有継続。
    df = _make_df([100.0] * 5 + [90.0, 90.0, 90.0])
    result = run_backtest("TEST", df, _config(costs=costs))

    # 保有中(7本目)の評価額は、含み損益0でも支払い済みの買い手数料の分だけ減る。
    assert result.equity_curve.iloc[6] == pytest.approx(result.initial_equity - 50.0)


# --- ブローカー側の待機注文 -----------------------------------------------------------


def _make_ohlc_df(bars: list) -> pd.DataFrame:
    """(open, high, low, close) のリストからOHLC付きDataFrameを作る。"""
    return pd.DataFrame(
        {
            "open": [b[0] for b in bars],
            "high": [b[1] for b in bars],
            "low": [b[2] for b in bars],
            "close": [b[3] for b in bars],
        }
    )


def _flat_bars(price: float, count: int) -> list:
    return [(price, price, price, price)] * count


def test_stop_fills_intrabar_even_when_close_recovers() -> None:
    """バーの中で逆指値を割ったら、終値が戻していても損切りされること。

    ブローカーに置いた逆指値は終値を待たない。終値だけで判定していた頃は
    この損切りを見逃し、損切りの発生頻度を過小評価していた。
    """
    # 6本目でエントリー(90) -> 7本目は安値84(逆指値85.5割れ)まで下げて終値89で戻す。
    bars = _flat_bars(100.0, 5) + [(90.0, 90.0, 90.0, 90.0), (89.5, 90.0, 84.0, 89.0)]
    result = run_backtest("TEST", _make_ohlc_df(bars), _config(costs=ZERO_COST))

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.reason == REASON_STOP_LOSS
    # 始値(89.5)は逆指値(85.5)を割っていないので、逆指値どおりに約定する。
    assert trade.exit_price == pytest.approx(90.0 * 0.95)


def test_gap_down_fills_the_stop_below_the_stop_price() -> None:
    """窓を開けて下落した場合、逆指値より不利な始値で約定すること。

    1トレードのリスクを口座の1%に収める前提が崩れる主因がこれ。
    逆指値どおりに約定するとモデル化すると、損失を過小評価する。
    """
    # 7本目が始値80で寄り付く（逆指値85.5を下回る）。
    bars = _flat_bars(100.0, 5) + [(90.0, 90.0, 90.0, 90.0), (80.0, 81.0, 79.0, 80.5)]
    result = run_backtest("TEST", _make_ohlc_df(bars), _config(costs=ZERO_COST))

    trade = result.trades[0]
    assert trade.reason == REASON_STOP_LOSS
    assert trade.exit_price == pytest.approx(80.0)  # 逆指値85.5ではなく始値80.0
    # 想定リスク(1株あたり4.5)より大きく負ける。
    assert trade.pnl < -4.5 * trade.quantity


def test_gap_up_fills_the_take_profit_above_the_limit_price() -> None:
    """窓を開けて上昇した場合、指値より有利な始値で約定すること。"""
    bars = _flat_bars(100.0, 5) + [(90.0, 90.0, 90.0, 90.0), (105.0, 106.0, 104.0, 105.5)]
    result = run_backtest("TEST", _make_ohlc_df(bars), _config(costs=ZERO_COST))

    trade = result.trades[0]
    assert trade.reason == REASON_TAKE_PROFIT
    assert trade.exit_price == pytest.approx(105.0)  # 指値99.0ではなく始値105.0


def test_stop_wins_when_both_orders_are_reachable_in_one_bar() -> None:
    """1本のバーで損切りと利確の両方に到達した場合は、損切りを優先すること。

    バーの情報だけではどちらが先か判別できないため、保守的な側に倒す。
    """
    bars = _flat_bars(100.0, 5) + [(90.0, 90.0, 90.0, 90.0), (90.0, 120.0, 80.0, 100.0)]
    result = run_backtest("TEST", _make_ohlc_df(bars), _config(costs=ZERO_COST))

    assert result.trades[0].reason == REASON_STOP_LOSS


# --- 同日中の再エントリー禁止 --------------------------------------------------------


def _intraday_df(closes: list, day: str = "2026-01-05") -> pd.DataFrame:
    """1日の中に複数バーが並ぶ日中足を模したDataFrame。"""
    return pd.DataFrame(
        {
            "date": pd.date_range(f"{day} 09:30", periods=len(closes), freq="5min"),
            "close": closes,
        }
    )


def test_same_day_reentry_is_blocked_after_an_exit() -> None:
    """損切りした当日は、シグナルが出ていても同じ銘柄を買い直さないこと。

    これが無いと下落トレンド中に「買う→損切り→また買う」を1日に何度も
    繰り返し、日次サーキットブレーカーに当たるまで損失を刻む。
    """
    # 90で買い -> 80で損切り -> その後も乖離が続き、シグナル自体は出続ける。
    closes = [100.0] * 5 + [90.0, 80.0, 79.0, 78.0]
    result = run_backtest("TEST", _intraday_df(closes), _config(costs=ZERO_COST))

    assert len(result.trades) == 1
    assert result.trades[0].reason == REASON_STOP_LOSS


def test_reentry_is_allowed_on_the_next_day() -> None:
    """翌日になれば同じ銘柄へ再エントリーできること（禁止は当日中のみ）。"""
    day1 = _intraday_df([100.0] * 5 + [90.0, 80.0], day="2026-01-05")
    day2 = _intraday_df([79.0, 78.0], day="2026-01-06")
    df = pd.concat([day1, day2], ignore_index=True)

    result = run_backtest("TEST", df, _config(costs=ZERO_COST))

    assert len(result.trades) == 2


def test_same_day_reentry_block_can_be_disabled() -> None:
    closes = [100.0] * 5 + [90.0, 80.0, 79.0, 78.0]
    df = _intraday_df(closes)

    result = run_backtest("TEST", df, _config(costs=ZERO_COST, block_same_day_reentry=False))

    assert len(result.trades) > 1
