"""execution/order_manager.py の単体テスト（IB呼び出しはすべてモック化）。"""

import asyncio
import logging
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from execution import order_manager
from execution.order_manager import (
    MAX_ORDER_NOTIONAL_USD,
    MAX_ORDER_PRICE_DEVIATION_PCT,
    MAX_POSITION_SIZE,
    OrderNotFilledError,
    RestingOrderCancelTimeoutError,
    RestingOrdersNotLiveError,
    build_bracket_orders,
    cancel_bracket_orders_async,
    ensure_orders_are_paper_only,
    find_filled_resting_exit,
    find_resting_exit_protection_async,
    place_bracket_order_async,
    place_market_order_async,
    place_resting_exit_orders_async,
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

    asyncio.run(cancel_bracket_orders_async(ib, "AAPL"))
    asyncio.run(cancel_bracket_orders_async(ib, ""))

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
                order_id: int = 42, order_type: str = "MKT", oca_group=None,
                symbol: str = "AAPL", action: str = "SELL", tif: str = "GTC"):
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
    trade.order.action = action
    trade.order.tif = tif
    trade.contract.symbol = symbol
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
    ib.placeOrder = MagicMock(side_effect=[
        _make_trade(avg_fill_price=101.25, commission=0.35),
        _make_trade(), _make_trade(),
        # 実約定に合わせた置き直しのぶん。
        _make_trade(), _make_trade(),
    ])

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
    """待機注文の約定は、銘柄で突き合わせて拾うこと。

    OCAグループ名では突き合わせられない。ブラケットの子注文の ocaGroup は
    IBKR側で親のpermIdへ書き換えられるため（2026-08-05に実測）、こちらが
    付けた名前と一致しない。
    """
    ib = MagicMock()
    other = _make_trade(order_type="STP", symbol="MSFT")
    mine = _make_trade(order_type="LMT", avg_fill_price=110.0, commission=0.35)
    ib.trades = MagicMock(return_value=[other, mine])

    fill = find_filled_resting_exit(ib, "AAPL")

    assert (fill.order_type, fill.fill_price, fill.commission) == ("LMT", 110.0, 0.35)


def test_find_filled_resting_exit_ignores_unfilled_and_other_symbols() -> None:
    ib = MagicMock()
    ib.trades = MagicMock(return_value=[_make_trade(status="Submitted")])

    assert find_filled_resting_exit(ib, "AAPL") is None
    assert find_filled_resting_exit(ib, "") is None


def test_find_filled_resting_exit_ignores_the_entry_order() -> None:
    """新規建ての親(BUY成行)を決済の約定として拾ってはならない。"""
    ib = MagicMock()
    ib.trades = MagicMock(return_value=[_make_trade(order_type="MKT", action="BUY")])

    assert find_filled_resting_exit(ib, "AAPL") is None


def test_filled_resting_exit_without_a_readable_price_is_not_reported() -> None:
    """約定価格が読めないうちは決済扱いにしないこと。

    IBKRは未受信のフィールドをNaNや0で埋める。推定で埋めると損益が静かにずれる。
    """
    ib = MagicMock()
    ib.trades = MagicMock(return_value=[_make_trade(avg_fill_price=0.0, order_type="STP")])

    assert find_filled_resting_exit(ib, "AAPL") is None


def test_real_cancel_cancels_only_the_resting_orders_of_that_symbol() -> None:
    """OCAグループ名が書き換えられていても取り消せること（銘柄で突き合わせる）。

    照会は reqAllOpenOrders で行う。openTrades() は自分のクライアントIDの注文
    しか含まず、他クライアントが置いた注文を取り消し損ねる。
    """
    ib = MagicMock()
    mine = _make_trade(status="Submitted", order_type="STP", oca_group="1171471109")
    other_symbol = _make_trade(status="Submitted", order_type="STP", symbol="MSFT")
    entry_order = _make_trade(status="Submitted", order_type="MKT", action="BUY")
    # 1回目は取り消し対象を返し、2回目（確認）は消えている。
    ib.reqAllOpenOrdersAsync = AsyncMock(
        side_effect=[[mine, other_symbol, entry_order], [other_symbol, entry_order]]
    )

    with _real_orders_enabled():
        asyncio.run(cancel_bracket_orders_async(ib, "AAPL"))

    ib.cancelOrder.assert_called_once_with(mine.order)
    ib.openTrades.assert_not_called()


def test_real_cancel_waits_until_the_cancellation_is_confirmed() -> None:
    """取り消しが確定するまで返らないこと。

    `cancelOrder` は要求を投げるだけで、ブローカーが `Cancelled` にするまで
    注文は板に生きている。2026-08-05の実測では、要求の1ミリ秒後に出した
    成行売りが「建玉3株 + 生きている売りLMT 3株 + 売り成行3株」＝売り超過と
    見なされ `Error 201` で拒否された（取り消しの確定はその0.4秒後）。
    """
    ib = MagicMock()
    pending = _make_trade(status="PendingCancel", order_type="LMT")
    ib.reqAllOpenOrdersAsync = AsyncMock(
        side_effect=[[pending], [pending], [pending], []]
    )

    with _real_orders_enabled():
        asyncio.run(cancel_bracket_orders_async(ib, "AAPL"))

    # 消えるまで問い合わせ直している（初回の照会 + 確認3回）。
    assert ib.reqAllOpenOrdersAsync.await_count == 4


def test_real_cancel_raises_when_the_cancellation_never_confirms() -> None:
    """確定しないまま時間切れになったら例外にすること。

    握り潰して成行へ進むと、生きている売り注文へ売りを重ねて売り超過になる。
    取り消せていない＝待機注文がまだ建玉を守っている、でもあるので、
    決済を次のサイクルへ持ち越す方が安全である。
    """
    ib = MagicMock()
    stuck = _make_trade(status="PendingCancel", order_type="STP")
    ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[stuck])

    with _real_orders_enabled(), pytest.raises(RestingOrderCancelTimeoutError):
        asyncio.run(cancel_bracket_orders_async(ib, "AAPL"))


def test_real_cancel_does_not_wait_when_there_is_nothing_to_cancel() -> None:
    """既に消えている待機注文を待ち続けないこと。

    ブローカー側で失効・約定済みの銘柄でここに滞留すると、決済判定が
    そのぶん遅れる。
    """
    ib = MagicMock()
    filled = _make_trade(status="Filled", order_type="STP")
    ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[filled])

    with _real_orders_enabled():
        asyncio.run(cancel_bracket_orders_async(ib, "AAPL"))

    ib.cancelOrder.assert_not_called()
    assert ib.reqAllOpenOrdersAsync.await_count == 1


# --- 待機注文の値段の丸めと生存確認 -------------------------------------------------


def test_resting_order_prices_are_rounded_to_the_tick() -> None:
    """呼値($0.01)に合わない値段のまま送ってはならない。

    2026-08-05のペーパー検証で、損切り 63.175 がそのまま送られてIBKRが
    Warning 110 を返し、**逆指値だけが不成立になった**（利確はたまたま2桁で通り、
    損切りの無い建玉が残った）。ib_insyncは110を警告としてしか通知せず、
    子注文の状態にも何も来ないため、丸めを欠くと静かに防御だけが消える。
    """
    orders = build_bracket_orders(
        symbol="AAPL", quantity=3,
        stop_price=63.175, take_profit_price=73.1549, reference_price=66.5,
    )

    assert orders.stop_loss.auxPrice == pytest.approx(63.18)
    assert orders.take_profit.lmtPrice == pytest.approx(73.16)


def test_tick_rounding_never_widens_the_stop() -> None:
    """丸めは切り上げ側へ倒すこと（損切りが予定より遠くならない側）。

    切り下げると逆指値が1呼値ぶん遠くなり、1トレードのリスクが設計値(1%)を
    わずかに超える。
    """
    orders = build_bracket_orders(
        symbol="AAPL", quantity=1,
        stop_price=94.991, take_profit_price=110.001, reference_price=100.0,
    )

    assert orders.stop_loss.auxPrice >= 94.991
    assert orders.take_profit.lmtPrice >= 110.001


def test_resting_children_are_good_till_cancelled() -> None:
    """子注文の有効期間はGTCであること。

    DAYだと引けで待機注文が失効し、持ち越すスイングの建玉が翌日の寄り付きまで
    無防備になる。明示しないとIB Gateway側のOrder PresetがDAYを上書きする
    （実測: Error 10349）。
    """
    orders = build_bracket_orders(
        symbol="AAPL", quantity=1,
        stop_price=95.0, take_profit_price=110.0, reference_price=100.0,
    )

    assert orders.stop_loss.tif == "GTC"
    assert orders.take_profit.tif == "GTC"


def test_bracket_result_reports_the_prices_actually_placed() -> None:
    """戻り値の値段は丸めた後のものであること。

    呼び出し側(main)はこれをそのまま positions.json へ記録するため、丸める前の
    値を返すと「ブローカーに置いていない値段」で決済判定・R倍率を計算する。
    """
    ib = MagicMock()
    # 参照価格どおりに約定した場合（置き直しは起きない）。
    ib.placeOrder = MagicMock(side_effect=[
        _make_trade(avg_fill_price=66.5), _make_trade(), _make_trade(),
    ])

    with _real_orders_enabled():
        result = asyncio.run(place_bracket_order_async(
            ib, MagicMock(symbol="AAPL"), quantity=3,
            stop_price=63.175, take_profit_price=73.1549, reference_price=66.5,
        ))

    assert result.stop_price == pytest.approx(63.18)
    assert result.take_profit_price == pytest.approx(73.16)


def test_position_is_flattened_when_a_child_order_is_not_live() -> None:
    """子注文がブローカー側で生きていなければ、建玉を成行で決済して例外にすること。

    送信が受理されたことと、注文が板に置かれたことは別である。片方だけ生きた
    状態（例: 利確だけ）で残すと、下方向に無防備な建玉を持ち越すことになる。
    例外にするのは、呼び出し側にローカル記録を作らせないため。
    """
    ib = MagicMock()
    parent = _make_trade(order_id=99)
    dead_stop = _make_trade(status="Inactive")
    live_take_profit = _make_trade(status="PreSubmitted")
    exit_trade = _make_trade()
    ib.placeOrder = MagicMock(side_effect=[parent, dead_stop, live_take_profit, exit_trade])

    with _real_orders_enabled(), \
        patch("execution.order_manager._CHILD_ORDER_LIVE_TIMEOUT_SECONDS", 0.0), \
        pytest.raises(RestingOrdersNotLiveError):
        asyncio.run(place_bracket_order_async(
            ib, MagicMock(symbol="AAPL"), quantity=3,
            stop_price=95.0, take_profit_price=110.0, reference_price=100.0,
        ))

    # 生き残っている子注文も取り消す（建玉が無いのに売り注文だけが残るため）。
    cancelled = [call.args[0] for call in ib.cancelOrder.call_args_list]
    assert dead_stop.order in cancelled and live_take_profit.order in cancelled
    # 建玉は成行で手仕舞う。
    exit_order = ib.placeOrder.call_args_list[3].args[1]
    assert exit_order.action == "SELL"
    assert exit_order.totalQuantity == 3


def test_live_children_do_not_trigger_a_flatten() -> None:
    """子注文が生きていれば決済しないこと（誤検知で建玉を失わない）。"""
    ib = MagicMock()
    ib.placeOrder = MagicMock(side_effect=[
        _make_trade(order_id=99),
        _make_trade(status="PreSubmitted"),
        _make_trade(status="Submitted"),
    ])

    with _real_orders_enabled():
        result = asyncio.run(place_bracket_order_async(
            ib, MagicMock(symbol="AAPL"), quantity=3,
            stop_price=95.0, take_profit_price=110.0, reference_price=100.0,
        ))

    assert result.dry_run is False
    ib.cancelOrder.assert_not_called()
    assert ib.placeOrder.call_count == 3


# --- 実約定価格に合わせた置き直し ---------------------------------------------------


def test_children_are_repriced_from_the_actual_fill() -> None:
    """待機注文は参照価格ではなく実約定価格を基準に置き直すこと。

    2026-08-05のペーパー検証では、参照価格66.50（遅延データ）に対し実約定が
    67.44だった。置き直さないと、意図した -5%/+10% の待機注文が実際の建値から
    見て -6.3%/+8.5% の位置に残り、1トレードのリスクが設計値を超える。
    """
    ib = MagicMock()
    ib.placeOrder = MagicMock(side_effect=[
        _make_trade(avg_fill_price=67.44), _make_trade(), _make_trade(),
        _make_trade(), _make_trade(),
    ])

    with _real_orders_enabled():
        result = asyncio.run(place_bracket_order_async(
            ib, MagicMock(symbol="AMBQ"), quantity=3,
            stop_price=66.5 * 0.95, take_profit_price=66.5 * 1.10, reference_price=66.5,
        ))

    # 実約定の -5% / +10%（呼値へ切り上げ）になっていること。
    assert result.stop_price == pytest.approx(64.07, abs=0.01)
    assert result.take_profit_price == pytest.approx(74.19, abs=0.01)

    # 置き直しは修正として送る。グループは送信済みなので transmit=True が要る。
    repriced = [call.args[1] for call in ib.placeOrder.call_args_list[3:]]
    assert [order.transmit for order in repriced] == [True, True]


def test_children_are_not_repriced_when_the_fill_matches_the_reference() -> None:
    """値段が変わらないなら注文を触らないこと（無駄な修正要求を出さない）。"""
    ib = MagicMock()
    ib.placeOrder = MagicMock(side_effect=[
        _make_trade(avg_fill_price=100.0), _make_trade(), _make_trade(),
    ])

    with _real_orders_enabled():
        asyncio.run(place_bracket_order_async(
            ib, MagicMock(symbol="AAPL"), quantity=3,
            stop_price=95.0, take_profit_price=110.0, reference_price=100.0,
        ))

    assert ib.placeOrder.call_count == 3


# --- 待機注文の置き直し -------------------------------------------------------------


def test_resting_exit_orders_can_be_placed_without_a_parent() -> None:
    """既にある建玉へ、損切り・利確をOCAで置き直せること。"""
    ib = MagicMock()
    ib.placeOrder = MagicMock(side_effect=[
        _make_trade(status="Submitted"), _make_trade(status="Submitted"),
    ])

    with _real_orders_enabled():
        asyncio.run(place_resting_exit_orders_async(
            ib, MagicMock(symbol="AAPL"), quantity=3,
            stop_price=95.0, take_profit_price=110.0, reference_price=100.0,
        ))

    sent = [call.args[1] for call in ib.placeOrder.call_args_list]
    assert [order.orderType for order in sent] == ["STP", "LMT"]
    # 親が無いので、どちらも独立した注文として送信される。
    assert [order.transmit for order in sent] == [True, True]
    assert [order.parentId for order in sent] == [0, 0]
    assert sent[0].ocaGroup == sent[1].ocaGroup


def test_failed_restore_does_not_flatten_the_position() -> None:
    """置き直しに失敗しても成行決済しないこと。

    この経路は決済が失敗した直後に呼ばれうるので、ここで再び成行を試みても
    同じ失敗を繰り返すだけになる。無防備であることを例外で知らせる。
    """
    ib = MagicMock()
    ib.placeOrder = MagicMock(side_effect=[
        _make_trade(status="Inactive"), _make_trade(status="Submitted"),
    ])

    with _real_orders_enabled(), \
        patch("execution.order_manager._CHILD_ORDER_LIVE_TIMEOUT_SECONDS", 0.0), \
        pytest.raises(RestingOrdersNotLiveError):
        asyncio.run(place_resting_exit_orders_async(
            ib, MagicMock(symbol="AAPL"), quantity=3,
            stop_price=95.0, take_profit_price=110.0, reference_price=100.0,
        ))

    # 成行売り(MKT)は出していない。
    assert [call.args[1].orderType for call in ib.placeOrder.call_args_list] == ["STP", "LMT"]


def test_live_resting_exits_are_looked_up_across_all_clients() -> None:
    """待機注文の有無は reqAllOpenOrders で見ること。

    openTrades() は自分のクライアントIDの注文しか含まないため、他クライアントが
    置いた注文を「無い」と誤判定して二重に置く。建玉を超える売り注文はIBKRが
    空売りと見なして拒否する（実測 Error 201）。
    """
    ib = MagicMock()
    stop = _make_trade(status="PreSubmitted", order_type="STP")
    take_profit = _make_trade(status="Submitted", order_type="LMT")
    filled = _make_trade(status="Filled", order_type="STP", symbol="MSFT")
    ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[stop, take_profit, filled])

    protection = asyncio.run(find_resting_exit_protection_async(ib))

    assert protection["AAPL"].is_complete is True
    assert protection["MSFT"].is_complete is True
    ib.openTrades.assert_not_called()


def test_a_position_with_only_one_live_child_is_not_protected() -> None:
    """片方だけ生きている建玉を「保護あり」と数えないこと。

    2026-08-05の実測では、呼値違反で逆指値だけが不成立になり、利確だけが生きた
    建玉が残った。片方でもあれば保護ありとすると、この下方向に無防備な状態を
    毎サイクル見逃し続ける。
    """
    ib = MagicMock()
    ib.reqAllOpenOrdersAsync = AsyncMock(
        return_value=[_make_trade(status="Submitted", order_type="LMT")]
    )

    protection = asyncio.run(find_resting_exit_protection_async(ib))

    assert protection["AAPL"].is_complete is False
    assert protection["AAPL"].live_order_types == frozenset({"LMT"})


def test_a_filled_resting_exit_counts_as_complete() -> None:
    """待機注文が約定していれば置き直しの対象にしないこと。

    約定していれば建玉はもう閉じており、OCAの相方もIBKR側が取り消す。
    ここで置き直すと、建玉が無いのに売り注文だけが並ぶ。
    """
    ib = MagicMock()
    ib.reqAllOpenOrdersAsync = AsyncMock(
        return_value=[_make_trade(status="Filled", order_type="STP")]
    )

    assert asyncio.run(find_resting_exit_protection_async(ib))["AAPL"].is_complete is True


def test_a_resting_order_downgraded_to_day_is_reported(caplog) -> None:
    """有効期間がGTC以外へ書き換えられていたら記録すること。

    Order Preset による上書きは注文を拒否しないため発注は成功し、こちら側の
    Order は送信時のGTCを保持したままになる。ブローカーから読み直したこの経路
    以外に気付く手段が無い（2026-08-06のログで tif='DAY' として実測）。
    """
    ib = MagicMock()
    ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[
        _make_trade(status="Submitted", order_type="STP", tif="DAY"),
        _make_trade(status="Submitted", order_type="LMT", tif="DAY"),
    ])

    order_manager._TIF_DOWNGRADE_WARNED.clear()
    with caplog.at_level(logging.WARNING):
        protection = asyncio.run(find_resting_exit_protection_async(ib))

    # 上書きされていても、注文自体は板に置かれている（保護そのものは成立する）。
    assert protection["AAPL"].is_complete is True
    assert "DAY" in caplog.text and "Presets" in caplog.text


def test_the_tif_downgrade_is_reported_once_per_position(caplog) -> None:
    """毎サイクル出さないこと。

    突き合わせは300秒ごとに走るため、素朴に出すと1建玉あたり1日78行になり、
    切り分けに使う行が埋もれる。解消したら落として次の建玉でまた出せること。
    """
    ib = MagicMock()
    ib.reqAllOpenOrdersAsync = AsyncMock(
        return_value=[_make_trade(status="Submitted", order_type="STP", tif="DAY")]
    )

    order_manager._TIF_DOWNGRADE_WARNED.clear()
    with caplog.at_level(logging.WARNING):
        asyncio.run(find_resting_exit_protection_async(ib))
        first = caplog.text.count("Presets")
        asyncio.run(find_resting_exit_protection_async(ib))
        assert caplog.text.count("Presets") == first == 1

        # GTCへ直ったサイクルを挟むと、次の上書きはまた記録される。
        ib.reqAllOpenOrdersAsync = AsyncMock(
            return_value=[_make_trade(status="Submitted", order_type="STP", tif="GTC")]
        )
        asyncio.run(find_resting_exit_protection_async(ib))
        assert caplog.text.count("Presets") == 1

        ib.reqAllOpenOrdersAsync = AsyncMock(
            return_value=[_make_trade(status="Submitted", order_type="STP", tif="DAY")]
        )
        asyncio.run(find_resting_exit_protection_async(ib))
        assert caplog.text.count("Presets") == 2
