"""execution/position_sizing.py の単体テスト。"""

import pytest

from execution.position_sizing import calculate_position_size


def test_basic_risk_based_sizing() -> None:
    # equity=100,000 の1%=1,000をリスク許容額とし、
    # entry=50, stop_loss=5%(=2.5/株)のとき 1000/2.5=400株
    quantity = calculate_position_size(
        account_equity=100_000.0,
        entry_price=50.0,
        stop_loss_pct=5.0,
        risk_per_trade_pct=1.0,
    )

    assert quantity == 400


def test_floors_fractional_share_count() -> None:
    # risk_amount=1,000, per_share_risk=100*0.05=5 -> 200 (端数なし確認用に別ケース)
    quantity = calculate_position_size(
        account_equity=33_333.0,
        entry_price=50.0,
        stop_loss_pct=5.0,
        risk_per_trade_pct=1.0,
    )

    # risk_amount=333.33, per_share_risk=2.5 -> 133.33... を切り捨てて133
    assert quantity == 133


def test_returns_zero_when_risk_amount_smaller_than_one_share_risk() -> None:
    quantity = calculate_position_size(
        account_equity=100.0,
        entry_price=1000.0,
        stop_loss_pct=5.0,
        risk_per_trade_pct=1.0,
    )

    assert quantity == 0


def test_higher_risk_tolerance_increases_quantity() -> None:
    low_risk = calculate_position_size(
        account_equity=100_000.0, entry_price=50.0, stop_loss_pct=5.0, risk_per_trade_pct=1.0,
    )
    high_risk = calculate_position_size(
        account_equity=100_000.0, entry_price=50.0, stop_loss_pct=5.0, risk_per_trade_pct=2.0,
    )

    assert high_risk == low_risk * 2


def test_wider_stop_loss_decreases_quantity() -> None:
    tight_stop = calculate_position_size(
        account_equity=100_000.0, entry_price=50.0, stop_loss_pct=5.0, risk_per_trade_pct=1.0,
    )
    wide_stop = calculate_position_size(
        account_equity=100_000.0, entry_price=50.0, stop_loss_pct=10.0, risk_per_trade_pct=1.0,
    )

    assert wide_stop < tight_stop


@pytest.mark.parametrize(
    "account_equity,entry_price,stop_loss_pct,risk_per_trade_pct",
    [
        (0.0, 50.0, 5.0, 1.0),
        (-100.0, 50.0, 5.0, 1.0),
        (100_000.0, 0.0, 5.0, 1.0),
        (100_000.0, -50.0, 5.0, 1.0),
        (100_000.0, 50.0, 0.0, 1.0),
        (100_000.0, 50.0, -5.0, 1.0),
        (100_000.0, 50.0, 5.0, 0.0),
        (100_000.0, 50.0, 5.0, -1.0),
    ],
)
def test_raises_on_non_positive_args(account_equity, entry_price, stop_loss_pct, risk_per_trade_pct) -> None:
    with pytest.raises(ValueError):
        calculate_position_size(
            account_equity=account_equity,
            entry_price=entry_price,
            stop_loss_pct=stop_loss_pct,
            risk_per_trade_pct=risk_per_trade_pct,
        )
