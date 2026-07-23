"""strategy/exit_signal.py の単体テスト。"""

import pytest

from strategy.exit_signal import (
    REASON_NONE,
    REASON_STOP_LOSS,
    REASON_TAKE_PROFIT,
    REASON_TRAILING_STOP,
    ExitSignalResult,
    detect_exit_signal,
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
