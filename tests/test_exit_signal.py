"""strategy/exit_signal.py の単体テスト。"""

import pytest

from strategy.exit_signal import (
    REASON_NONE,
    REASON_STOP_LOSS,
    REASON_TAKE_PROFIT,
    REASON_TRAILING_STOP,
    ExitSignalResult,
    detect_exit_signal,
    detect_resting_order_exit,
    resolve_stop_price,
    resolve_take_profit_price,
)


# --- 入力検証 -----------------------------------------------------------------


@pytest.mark.parametrize(
    "entry_price,current_price,highest_price",
    [
        (0.0, 100.0, 100.0),
        (-10.0, 100.0, 100.0),
        (100.0, 0.0, 100.0),
        (100.0, -5.0, 100.0),
        (100.0, 100.0, 0.0),
        (100.0, 100.0, -5.0),
    ],
)
def test_raises_on_non_positive_prices(entry_price, current_price, highest_price) -> None:
    with pytest.raises(ValueError):
        detect_exit_signal(
            "TEST",
            entry_price=entry_price,
            current_price=current_price,
            highest_price_since_entry=highest_price,
        )


# --- シグナルなし ---------------------------------------------------------------


def test_no_signal_when_pnl_is_small_and_no_pullback() -> None:
    result = detect_exit_signal(
        "TEST",
        entry_price=100.0,
        current_price=102.0,
        highest_price_since_entry=103.0,
        take_profit_pct=10.0,
        stop_loss_pct=5.0,
        trailing_stop_pct=3.0,
    )

    assert result.should_sell is False
    assert result.reason == REASON_NONE


# --- 利確 (Take Profit) ---------------------------------------------------------


def test_take_profit_triggers_above_threshold() -> None:
    result = detect_exit_signal(
        "TEST",
        entry_price=100.0,
        current_price=112.0,
        highest_price_since_entry=112.0,
        take_profit_pct=10.0,
    )

    assert result.should_sell is True
    assert result.reason == REASON_TAKE_PROFIT


def test_take_profit_triggers_at_exact_boundary() -> None:
    result = detect_exit_signal(
        "TEST",
        entry_price=100.0,
        current_price=110.0,
        highest_price_since_entry=110.0,
        take_profit_pct=10.0,
    )

    assert result.pnl_pct == pytest.approx(10.0)
    assert result.should_sell is True
    assert result.reason == REASON_TAKE_PROFIT


def test_no_take_profit_just_below_threshold() -> None:
    result = detect_exit_signal(
        "TEST",
        entry_price=100.0,
        current_price=109.9,
        highest_price_since_entry=109.9,
        take_profit_pct=10.0,
        trailing_stop_pct=50.0,  # トレーリングは発火させない
    )

    assert result.should_sell is False


# --- 損切り (Stop Loss) ---------------------------------------------------------


def test_stop_loss_triggers_below_threshold() -> None:
    result = detect_exit_signal(
        "TEST",
        entry_price=100.0,
        current_price=90.0,
        highest_price_since_entry=100.0,
        stop_loss_pct=5.0,
    )

    assert result.should_sell is True
    assert result.reason == REASON_STOP_LOSS


def test_stop_loss_triggers_at_exact_boundary() -> None:
    result = detect_exit_signal(
        "TEST",
        entry_price=100.0,
        current_price=95.0,
        highest_price_since_entry=100.0,
        stop_loss_pct=5.0,
    )

    assert result.pnl_pct == pytest.approx(-5.0)
    assert result.should_sell is True
    assert result.reason == REASON_STOP_LOSS


def test_no_stop_loss_just_above_threshold() -> None:
    result = detect_exit_signal(
        "TEST",
        entry_price=100.0,
        current_price=95.1,
        highest_price_since_entry=100.0,
        stop_loss_pct=5.0,
    )

    assert result.should_sell is False


# --- トレーリングストップ -----------------------------------------------------


def test_trailing_stop_triggers_after_pullback_from_high() -> None:
    # 100->130まで上昇後、130から3%以上下落（ただしまだ含み益あり）
    result = detect_exit_signal(
        "TEST",
        entry_price=100.0,
        current_price=125.0,
        highest_price_since_entry=130.0,
        take_profit_pct=50.0,  # 利確は発火させない
        stop_loss_pct=50.0,  # 損切りも発火させない
        trailing_stop_pct=3.0,
    )

    assert result.pnl_pct == pytest.approx(25.0)
    assert result.should_sell is True
    assert result.reason == REASON_TRAILING_STOP


def test_trailing_stop_does_not_trigger_when_pnl_not_positive() -> None:
    # 高値からの下落率は大きいが、建値割れ(pnl<=0)の場合はトレーリングでなく
    # 損切りロジックの管轄とし、トレーリングストップ単独では発火しない。
    result = detect_exit_signal(
        "TEST",
        entry_price=100.0,
        current_price=99.0,
        highest_price_since_entry=130.0,
        take_profit_pct=50.0,
        stop_loss_pct=50.0,  # 損切りも発火させない
        trailing_stop_pct=3.0,
    )

    assert result.should_sell is False
    assert result.reason == REASON_NONE


def test_highest_price_is_corrected_when_stale_argument_is_lower_than_current() -> None:
    # 呼び出し側が highest_price_since_entry を更新し忘れて current_price より
    # 低い値を渡しても、current_price 自体が新高値として補正されるため
    # 誤ってトレーリングストップが発火しない。
    result = detect_exit_signal(
        "TEST",
        entry_price=100.0,
        current_price=120.0,
        highest_price_since_entry=110.0,  # current_price より低い(古い)値
        trailing_stop_pct=3.0,
        take_profit_pct=50.0,
        stop_loss_pct=50.0,
    )

    assert result.highest_price == pytest.approx(120.0)
    assert result.should_sell is False
    assert result.reason == REASON_NONE


# --- 優先順位 -------------------------------------------------------------------


def test_take_profit_takes_priority_over_trailing_stop() -> None:
    # entry=100, high=130, current=115: pnl=15%(利確条件も満たす)かつ
    # 高値からの下落率も -11.5%(トレーリング条件も満たす) -> 利確が優先されるべき
    result = detect_exit_signal(
        "TEST",
        entry_price=100.0,
        current_price=115.0,
        highest_price_since_entry=130.0,
        take_profit_pct=10.0,
        stop_loss_pct=5.0,
        trailing_stop_pct=3.0,
    )

    assert result.should_sell is True
    assert result.reason == REASON_TAKE_PROFIT


# --- 戻り値のフィールド ---------------------------------------------------------


def test_returns_exit_signal_result_with_expected_fields() -> None:
    result = detect_exit_signal(
        "AAPL",
        entry_price=100.0,
        current_price=90.0,
        highest_price_since_entry=105.0,
        stop_loss_pct=5.0,
    )

    assert isinstance(result, ExitSignalResult)
    assert result.symbol == "AAPL"
    assert result.entry_price == 100.0
    assert result.current_price == 90.0
    assert result.highest_price == pytest.approx(105.0)
    assert result.pnl_pct == pytest.approx(-10.0)
    assert result.drawdown_from_high_pct == pytest.approx((90.0 - 105.0) / 105.0 * 100.0)


# --- ブローカー側に置く待機注文 -------------------------------------------------------


def test_resolve_order_prices_from_entry_and_percentages() -> None:
    assert resolve_stop_price(100.0, 5.0) == pytest.approx(95.0)
    assert resolve_take_profit_price(100.0, 10.0) == pytest.approx(110.0)


@pytest.mark.parametrize("entry,pct", [(0.0, 5.0), (-1.0, 5.0), (100.0, 0.0), (100.0, -5.0)])
def test_resolve_order_prices_reject_invalid_inputs(entry: float, pct: float) -> None:
    with pytest.raises(ValueError):
        resolve_stop_price(entry, pct)
    with pytest.raises(ValueError):
        resolve_take_profit_price(entry, pct)


def test_no_resting_order_exit_when_price_stays_between_the_orders() -> None:
    assert detect_resting_order_exit(
        stop_price=95.0, take_profit_price=110.0, bar_low=97.0, bar_high=105.0,
    ) is None


def test_stop_fills_at_the_stop_price_when_touched_without_a_gap() -> None:
    result = detect_resting_order_exit(
        stop_price=95.0, take_profit_price=110.0,
        bar_low=94.0, bar_high=101.0, bar_open=100.0,
    )

    assert result is not None
    assert result.reason == REASON_STOP_LOSS
    assert result.fill_price == pytest.approx(95.0)


def test_stop_fills_at_the_open_when_the_market_gaps_below_it() -> None:
    """窓を開けて下落した場合、逆指値より不利な始値で約定すること。

    逆指値はトリガー後に成行注文になるため、値段は保証されない。
    ここをモデル化しないと、1トレードのリスクを口座の1%に収める前提が
    崩れる場面（ギャップ）で損失を過小評価する。
    """
    result = detect_resting_order_exit(
        stop_price=95.0, take_profit_price=110.0,
        bar_low=88.0, bar_high=92.0, bar_open=90.0,
    )

    assert result.reason == REASON_STOP_LOSS
    assert result.fill_price == pytest.approx(90.0)


def test_take_profit_fills_at_the_limit_price_when_touched_without_a_gap() -> None:
    result = detect_resting_order_exit(
        stop_price=95.0, take_profit_price=110.0,
        bar_low=99.0, bar_high=112.0, bar_open=100.0,
    )

    assert result.reason == REASON_TAKE_PROFIT
    assert result.fill_price == pytest.approx(110.0)


def test_take_profit_fills_at_the_open_when_the_market_gaps_above_it() -> None:
    """指値は値段より不利には約定しないため、窓を開けた分は有利に働く。"""
    result = detect_resting_order_exit(
        stop_price=95.0, take_profit_price=110.0,
        bar_low=114.0, bar_high=120.0, bar_open=115.0,
    )

    assert result.reason == REASON_TAKE_PROFIT
    assert result.fill_price == pytest.approx(115.0)


def test_stop_takes_precedence_when_both_orders_are_reachable() -> None:
    """どちらが先か判別できないバーでは、保守的に損切りを優先すること。"""
    result = detect_resting_order_exit(
        stop_price=95.0, take_profit_price=110.0,
        bar_low=90.0, bar_high=115.0, bar_open=100.0,
    )

    assert result.reason == REASON_STOP_LOSS


def test_live_polling_uses_the_observed_price_for_both_high_and_low() -> None:
    """ライブのポーリングでは、観測した現在値だけで判定する（バー内は不明）。"""
    assert detect_resting_order_exit(
        stop_price=95.0, take_profit_price=110.0, bar_low=96.0, bar_high=96.0,
    ) is None

    hit = detect_resting_order_exit(
        stop_price=95.0, take_profit_price=110.0, bar_low=94.0, bar_high=94.0,
    )
    assert hit.reason == REASON_STOP_LOSS
    # 始値が無いのでギャップの有無を判別できない。逆指値(95.0)どおりに約定したと
    # 仮定するのは楽観的すぎるため、観測できた価格(94.0)を採る。
    assert hit.fill_price == pytest.approx(94.0)


def test_unknown_gap_is_resolved_against_the_position_on_both_sides() -> None:
    """始値が分からない場合は、損切りも利確も不利な側に倒すこと。

    分からないものを有利側に倒すと、バックテストが実運用より良く見える。
    """
    stop = detect_resting_order_exit(
        stop_price=95.0, take_profit_price=110.0, bar_low=80.0, bar_high=80.0,
    )
    take_profit = detect_resting_order_exit(
        stop_price=95.0, take_profit_price=110.0, bar_low=130.0, bar_high=130.0,
    )

    # 損切りは観測値まで滑ったものとして扱う。
    assert stop.fill_price == pytest.approx(80.0)
    # 利確は指値どおり。観測値(130.0)まで伸びた分は取れたことにしない。
    assert take_profit.fill_price == pytest.approx(110.0)


@pytest.mark.parametrize("stop,take_profit", [(0.0, 110.0), (95.0, 0.0), (-1.0, 110.0)])
def test_detect_resting_order_exit_rejects_invalid_prices(stop: float, take_profit: float) -> None:
    with pytest.raises(ValueError):
        detect_resting_order_exit(
            stop_price=stop, take_profit_price=take_profit, bar_low=100.0, bar_high=100.0,
        )
