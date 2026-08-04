"""execution/order_manager.py の単体テスト（IB呼び出しはすべてモック化）。"""

import asyncio
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from execution.order_manager import (
    MAX_ORDER_NOTIONAL_USD,
    MAX_ORDER_PRICE_DEVIATION_PCT,
    MAX_POSITION_SIZE,
    OrderNotFilledError,
    build_bracket_orders,
    cancel_bracket_orders_async,
    ensure_orders_are_paper_only,
    find_filled_resting_exit,
    place_bracket_order_async,
    place_market_order_async,
)


def _place(action: str, quantity: int):
    ib = MagicMock()
    contract = MagicMock(symbol="AAPL")
    return asyncio.run(place_market_order_async(ib, contract, action=action, quantity=quantity))


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

    asyncio.run(place_market_order_async(ib, contract, action="BUY", quantity=1))

    ib.placeOrder.assert_not_called()


# --- ブラケット注文 -----------------------------------------------------------------


def test_bracket_has_market_parent_and_stop_and_limit_children() -> None:
    orders = build_bracket_orders("AAPL", quantity=10, stop_price=95.0, take_profit_price=110.0,
                                  reference_price=100.0)

    assert orders.parent.orderType == "MKT"
    assert orders.parent.action == "BUY"
    assert orders.stop_loss.orderType == "STP"
    assert orders.stop_loss.action == "SELL"
    assert orders.stop_loss.auxPrice == pytest.approx(95.0)
    assert orders.take_profit.orderType == "LMT"
    assert orders.take_profit.action == "SELL"
    assert orders.take_profit.lmtPrice == pytest.approx(110.0)


def test_only_the_last_child_transmits() -> None:
    """親を先に送信すると、損切りが届く前に建玉ができてしまう。

    その僅かな隙間が「損切りの無い裸のポジション」であり、
    ブローカー側へ損切りを置く目的そのものを損なう。
    """
    orders = build_bracket_orders("AAPL", quantity=10, stop_price=95.0, take_profit_price=110.0,
                                  reference_price=100.0)

    assert orders.parent.transmit is False
    assert orders.stop_loss.transmit is False
    assert orders.take_profit.transmit is True
    # 送信順は親 -> 子。
    assert orders.as_list() == [orders.parent, orders.stop_loss, orders.take_profit]


def test_children_share_one_oca_group_so_one_fill_cancels_the_other() -> None:
    orders = build_bracket_orders("AAPL", quantity=10, stop_price=95.0, take_profit_price=110.0,
                                  reference_price=100.0)

    assert orders.stop_loss.ocaGroup == orders.oca_group
    assert orders.take_profit.ocaGroup == orders.oca_group
    assert orders.stop_loss.ocaType == orders.take_profit.ocaType == 2


def test_all_bracket_orders_share_the_same_quantity() -> None:
    """子の数量が親とずれると、決済後に売り注文が残る/建玉が残る。"""
    orders = build_bracket_orders("AAPL", quantity=7, stop_price=95.0, take_profit_price=110.0,
                                  reference_price=100.0)

    assert orders.parent.totalQuantity == 7
    assert orders.stop_loss.totalQuantity == 7
    assert orders.take_profit.totalQuantity == 7


@pytest.mark.parametrize(
    "quantity,stop,take_profit",
    [
        (0, 95.0, 110.0),
        (-1, 95.0, 110.0),
        (10, 0.0, 110.0),
        (10, 95.0, 0.0),
        (10, 110.0, 95.0),  # 損切りが利確より上
        (10, 100.0, 100.0),
    ],
)
def test_build_bracket_orders_rejects_invalid_inputs(
    quantity: int, stop: float, take_profit: float
) -> None:
    with pytest.raises(ValueError):
        build_bracket_orders(
            "AAPL", quantity=quantity, stop_price=stop, take_profit_price=take_profit,
            reference_price=100.0,
        )


def test_dry_run_bracket_clamps_quantity_including_children() -> None:
    """最大ロット数で丸めた数量が子注文にも反映されること。

    親だけ丸めて子を丸め忘れると、決済後に余った売り注文が残る。
    """
    contract = MagicMock(symbol="AAPL")

    result = asyncio.run(
        place_bracket_order_async(
            MagicMock(), contract, quantity=MAX_POSITION_SIZE + 500,
            stop_price=95.0, take_profit_price=110.0, reference_price=100.0,
        )
    )

    assert result.quantity == MAX_POSITION_SIZE
    assert result.orders.parent.totalQuantity == MAX_POSITION_SIZE
    assert result.orders.stop_loss.totalQuantity == MAX_POSITION_SIZE
    assert result.orders.take_profit.totalQuantity == MAX_POSITION_SIZE


def test_dry_run_bracket_does_not_call_place_order() -> None:
    ib = MagicMock()
    contract = MagicMock(symbol="AAPL")

    asyncio.run(
        place_bracket_order_async(
            ib, contract, quantity=5, stop_price=95.0, take_profit_price=110.0,
            reference_price=100.0,
        )
    )

    ib.placeOrder.assert_not_called()


# --- 1注文あたりの名目金額の上限 -------------------------------------------------


def test_buy_is_clamped_by_notional_when_share_price_is_high() -> None:
    """株数の上限内でも、金額の上限を超える数量は丸めること。

    MAX_POSITION_SIZEは株数の制限なので、高価格帯の銘柄では
    実際に晒す金額が桁違いになる。
    """
    # 1株800ドル: 上限5,000ドルなら6株まで（10株なら8,000ドルで超過）
    price = MAX_ORDER_NOTIONAL_USD / 6.25
    contract = MagicMock(symbol="AAPL")

    result = asyncio.run(
        place_market_order_async(
            MagicMock(), contract, action="BUY", quantity=MAX_POSITION_SIZE,
            reference_price=price,
        )
    )

    assert result.quantity == 6
    assert result.quantity * price <= MAX_ORDER_NOTIONAL_USD


def test_sell_is_not_clamped_by_notional() -> None:
    """決済(SELL)には金額上限も適用してはならない（株数上限と同じ理由）。"""
    contract = MagicMock(symbol="AAPL")

    result = asyncio.run(
        place_market_order_async(
            MagicMock(), contract, action="SELL", quantity=100,
            reference_price=MAX_ORDER_NOTIONAL_USD,  # 1株で上限に達する値段
        )
    )

    assert result.quantity == 100


def test_bracket_notional_clamp_applies_to_children_too() -> None:
    """金額上限で丸めた数量も、親子で一致していること。"""
    price = MAX_ORDER_NOTIONAL_USD / 6.25
    contract = MagicMock(symbol="AAPL")

    result = asyncio.run(
        place_bracket_order_async(
            MagicMock(), contract, quantity=MAX_POSITION_SIZE,
            stop_price=price * 0.95, take_profit_price=price * 1.10,
            reference_price=price,
        )
    )

    assert result.quantity == 6
    assert result.orders.parent.totalQuantity == 6
    assert result.orders.stop_loss.totalQuantity == 6
    assert result.orders.take_profit.totalQuantity == 6


def test_bracket_rejected_when_one_share_exceeds_notional_limit() -> None:
    """1株すら金額上限を超える銘柄は、丸めて0株にせず発注を拒否すること。

    0株の注文を作ると、呼び出し側は建玉ができた前提でローカルに
    ポジションを記録してしまう。
    """
    contract = MagicMock(symbol="BRK A")
    price = MAX_ORDER_NOTIONAL_USD * 100

    with pytest.raises(ValueError):
        asyncio.run(
            place_bracket_order_async(
                MagicMock(), contract, quantity=1,
                stop_price=price * 0.95, take_profit_price=price * 1.10,
                reference_price=price,
            )
        )


# --- 待機注文の値段の妥当性 -------------------------------------------------------


@pytest.mark.parametrize("stop_price,take_profit_price", [
    # 損切りが現在値から遠すぎる（パーセントと小数を取り違えた等）
    (10.0, 110.0),
    # 利確が現在値から遠すぎる
    (95.0, 1_000.0),
])
def test_bracket_rejects_prices_far_from_reference(stop_price, take_profit_price) -> None:
    """壊れた値段の注文をブローカーへ出す前に止めること。"""
    with pytest.raises(ValueError, match="乖離"):
        build_bracket_orders(
            "AAPL", quantity=10,
            stop_price=stop_price, take_profit_price=take_profit_price,
            reference_price=100.0,
        )


def test_bracket_accepts_prices_at_the_deviation_boundary() -> None:
    """許容範囲ちょうどの値段は通ること（正常系を巻き込んで弾かない）。"""
    reference_price = 100.0
    edge = reference_price * MAX_ORDER_PRICE_DEVIATION_PCT / 100.0

    orders = build_bracket_orders(
        "AAPL", quantity=10,
        stop_price=reference_price - edge, take_profit_price=reference_price + edge,
        reference_price=reference_price,
    )

    assert orders.parent.totalQuantity == 10


@pytest.mark.parametrize("reference_price", [0.0, -1.0])
def test_bracket_rejects_non_positive_reference_price(reference_price) -> None:
    with pytest.raises(ValueError):
        build_bracket_orders(
            "AAPL", quantity=10, stop_price=95.0, take_profit_price=110.0,
            reference_price=reference_price,
        )


def test_dry_run_cancel_does_not_call_cancel_order() -> None:
    ib = MagicMock()

    asyncio.run(cancel_bracket_orders_async(ib, "AAPL", "OCA_1"))
    asyncio.run(cancel_bracket_orders_async(ib, "AAPL", None))

    ib.cancelOrder.assert_not_called()


# --- 実発注（ペーパー口座） --------------------------------------------------------

# ドライランのままでは、ブラケットのtransmit順序・OCAの連動・実約定価格・手数料・
# 注文拒否時の挙動が一切検証できない。ペーパー口座はそれを安全に潰すための環境で、
# ここはその経路の不変条件を固定する。


@contextmanager
def _real_orders_enabled():
    """実発注を有効にし、約定待ちのsleepを実時間から切り離す。"""
    async def _no_sleep(_seconds):
        return None

    with patch("execution.order_manager.ENABLE_REAL_ORDERS", True), \
        patch("execution.order_manager.asyncio.sleep", new=_no_sleep):
        yield


def _make_trade(status: str = "Filled", avg_fill_price: float = 100.0, commission: float = 0.35,
                order_id: int = 42, order_type: str = "MKT", oca_group=None):
    trade = MagicMock()
    trade.isDone = MagicMock(return_value=True)
    trade.orderStatus.status = status
    trade.orderStatus.avgFillPrice = avg_fill_price
    fill = MagicMock()
    fill.commissionReport.commission = commission
    trade.fills = [fill]
    trade.order.orderId = order_id
    trade.order.orderType = order_type
    trade.order.ocaGroup = oca_group
    return trade


def test_paper_port_guard_allows_paper_and_blocks_anything_else() -> None:
    """実発注が有効なら、接続先はペーパーのポートに限ること。

    許可リストで判定するのは、本番ポートの拒否リストだと .env の打ち間違い
    （7495等）が素通りするため。
    """
    with patch("execution.order_manager.ENABLE_REAL_ORDERS", True):
        for paper_port in (7497, 4002):
            ensure_orders_are_paper_only(paper_port)
        for other_port in (7496, 4001, 7495):
            with pytest.raises(RuntimeError):
                ensure_orders_are_paper_only(other_port)


def test_guard_is_inert_while_dry_running() -> None:
    """ドライランならデータ取得だけなので、どのポートでも止めない。"""
    for port in (7496, 4001, 7497):
        ensure_orders_are_paper_only(port)


def test_real_bracket_sends_parent_first_then_children_with_parent_id() -> None:
    """親→子の順で送り、子には親のorderIdを代入すること。

    parentId は placeOrder 後にしか決まらない。代入を落とすと、IBKR側で
    親子関係が成立せず、子が独立した売り注文として生きる。
    """
    ib = MagicMock()
    contract = MagicMock(symbol="AAPL")
    parent_trade = _make_trade(order_id=99)
    ib.placeOrder = MagicMock(side_effect=[parent_trade, _make_trade(), _make_trade()])

    with _real_orders_enabled():
        result = asyncio.run(place_bracket_order_async(
            ib, contract, quantity=3, stop_price=95.0, take_profit_price=110.0,
            reference_price=100.0,
        ))

    sent_orders = [call.args[1] for call in ib.placeOrder.call_args_list]
    assert sent_orders[0].orderType == "MKT"
    assert [order.parentId for order in sent_orders[1:]] == [99, 99]
    # transmit=True は最後の子だけ（親を先に送ると損切りの無い建玉が一瞬できる）。
    assert [order.transmit for order in sent_orders] == [False, False, True]
    assert result.dry_run is False


def test_real_bracket_reports_the_actual_fill_and_commission() -> None:
    """建値は参照価格ではなく実約定を返すこと。"""
    ib = MagicMock()
    ib.placeOrder = MagicMock(
        side_effect=[_make_trade(avg_fill_price=101.25, commission=0.35), _make_trade(), _make_trade()]
    )

    with _real_orders_enabled():
        result = asyncio.run(place_bracket_order_async(
            ib, MagicMock(symbol="AAPL"), quantity=3, stop_price=95.0, take_profit_price=110.0,
            reference_price=100.0,
        ))

    assert result.fill_price == 101.25
    assert result.commission == 0.35


def test_rejected_parent_cancels_the_children_and_raises() -> None:
    """親が拒否されたら、送信済みの子を取り消して例外にすること。

    子を残すと建玉が無いのに売り注文だけが生き、次にその銘柄を建てた瞬間に
    意図しない決済が起きる。例外にするのは、呼び出し側に実体の無い建玉を
    記録させないため。
    """
    ib = MagicMock()
    rejected = _make_trade(status="Inactive")
    child_a, child_b = _make_trade(), _make_trade()
    ib.placeOrder = MagicMock(side_effect=[rejected, child_a, child_b])

    with _real_orders_enabled(), pytest.raises(OrderNotFilledError):
        asyncio.run(place_bracket_order_async(
            ib, MagicMock(symbol="AAPL"), quantity=3, stop_price=95.0, take_profit_price=110.0,
            reference_price=100.0,
        ))

    cancelled = [call.args[0] for call in ib.cancelOrder.call_args_list]
    assert cancelled == [child_a.order, child_b.order]


def test_unfilled_market_order_is_cancelled_and_raises() -> None:
    """約定しないまま時間切れになったら、注文を取り消して例外にすること。"""
    ib = MagicMock()
    pending = _make_trade(status="Submitted")
    pending.isDone = MagicMock(return_value=False)
    ib.placeOrder = MagicMock(return_value=pending)

    with _real_orders_enabled(), pytest.raises(OrderNotFilledError):
        asyncio.run(place_market_order_async(
            ib, MagicMock(symbol="AAPL"), action="SELL", quantity=3,
        ))

    ib.cancelOrder.assert_called_once_with(pending.order)


def test_real_market_order_returns_the_fill() -> None:
    ib = MagicMock()
    ib.placeOrder = MagicMock(return_value=_make_trade(avg_fill_price=98.7, commission=0.35))

    with _real_orders_enabled():
        result = asyncio.run(place_market_order_async(
            ib, MagicMock(symbol="AAPL"), action="SELL", quantity=3,
        ))

    assert (result.fill_price, result.commission, result.dry_run) == (98.7, 0.35, False)


def test_find_filled_resting_exit_matches_the_oca_group() -> None:
    """待機注文の約定は、OCAグループで突き合わせて拾うこと。"""
    ib = MagicMock()
    other = _make_trade(order_type="STP", oca_group="OTHER")
    mine = _make_trade(order_type="LMT", oca_group="OCA_1", avg_fill_price=110.0, commission=0.35)
    ib.trades = MagicMock(return_value=[other, mine])

    fill = find_filled_resting_exit(ib, "OCA_1")

    assert (fill.order_type, fill.fill_price, fill.commission) == ("LMT", 110.0, 0.35)


def test_find_filled_resting_exit_ignores_unfilled_and_missing_groups() -> None:
    ib = MagicMock()
    ib.trades = MagicMock(return_value=[_make_trade(status="Submitted", oca_group="OCA_1")])

    assert find_filled_resting_exit(ib, "OCA_1") is None
    assert find_filled_resting_exit(ib, None) is None


def test_filled_resting_exit_without_a_readable_price_is_not_reported() -> None:
    """約定価格が読めないうちは決済扱いにしないこと。

    IBKRは未受信のフィールドをNaNや0で埋める。推定で埋めると損益が静かにずれる。
    """
    ib = MagicMock()
    ib.trades = MagicMock(return_value=[_make_trade(avg_fill_price=0.0, oca_group="OCA_1")])

    assert find_filled_resting_exit(ib, "OCA_1") is None


def test_real_cancel_cancels_only_the_matching_oca_group() -> None:
    ib = MagicMock()
    mine = _make_trade(oca_group="OCA_1")
    other = _make_trade(oca_group="OCA_2")
    ib.openTrades = MagicMock(return_value=[mine, other])

    with _real_orders_enabled():
        asyncio.run(cancel_bracket_orders_async(ib, "AAPL", "OCA_1"))

    ib.cancelOrder.assert_called_once_with(mine.order)
