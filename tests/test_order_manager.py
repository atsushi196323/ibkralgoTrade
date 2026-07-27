"""execution/order_manager.py の単体テスト（IB呼び出しはすべてモック化）。"""

import asyncio
from unittest.mock import MagicMock

import pytest

from execution.order_manager import MAX_POSITION_SIZE, place_dry_run_order_async


def _place(action: str, quantity: int):
    ib = MagicMock()
    contract = MagicMock(symbol="AAPL")
    return asyncio.run(place_dry_run_order_async(ib, contract, action=action, quantity=quantity))


# --- 最大ロット数制限 -----------------------------------------------------------


def test_buy_within_limit_is_not_clamped() -> None:
    result = _place("BUY", MAX_POSITION_SIZE)

    assert result.quantity == MAX_POSITION_SIZE


def test_buy_above_limit_is_clamped() -> None:
    result = _place("BUY", MAX_POSITION_SIZE + 500)

    assert result.quantity == MAX_POSITION_SIZE


def test_sell_above_limit_is_not_clamped() -> None:
    """決済(SELL)に数量制限を適用してはならない。

    呼び出し側(main._process_exit_async)は決済成立を前提にローカルの
    ポジションを閉じるため、SELLの数量を丸めるとブローカー側に建玉が
    残ったままローカルの追跡だけが消え、損切りもトレーリングストップも
    効かない未追跡ポジションが生まれる。
    """
    result = _place("SELL", MAX_POSITION_SIZE + 500)

    assert result.quantity == MAX_POSITION_SIZE + 500


def test_broker_synced_position_can_be_fully_closed() -> None:
    # sync_with_broker_asyncが取り込んだ100株の既存ポジションを想定
    result = _place("SELL", 100)

    assert result.quantity == 100


# --- 入力値の検証 ---------------------------------------------------------------


@pytest.mark.parametrize("quantity", [0, -1])
def test_non_positive_quantity_raises(quantity) -> None:
    with pytest.raises(ValueError):
        _place("BUY", quantity)


@pytest.mark.parametrize("action", ["buy", "sell", "HOLD", ""])
def test_unknown_action_raises(action) -> None:
    with pytest.raises(ValueError):
        _place(action, 1)


# --- 戻り値 ---------------------------------------------------------------------


def test_result_carries_order_details() -> None:
    result = _place("BUY", 3)

    assert result.symbol == "AAPL"
    assert result.action == "BUY"
    assert result.order_type == "MKT"
    assert result.dry_run is True


def test_place_order_is_never_called() -> None:
    ib = MagicMock()
    contract = MagicMock(symbol="AAPL")

    asyncio.run(place_dry_run_order_async(ib, contract, action="BUY", quantity=1))

    ib.placeOrder.assert_not_called()
