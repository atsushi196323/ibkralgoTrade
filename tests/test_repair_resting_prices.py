"""scripts/repair_resting_prices.py の単体テスト（IBKRへの実接続は行わない）。"""

from types import SimpleNamespace

import main as bot
from scripts.repair_resting_prices import (
    _current_price,
    _intended_prices,
    _resting_exit_trades,
)


def _trade(symbol: str, action: str, order_type: str, price: float, status: str = "Submitted"):
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=symbol),
        order=SimpleNamespace(
            action=action,
            orderType=order_type,
            auxPrice=price if order_type == "STP" else 0.0,
            lmtPrice=price if order_type != "STP" else 0.0,
        ),
        orderStatus=SimpleNamespace(status=status),
    )


# --- 直すべき値段 -------------------------------------------------------------


def test_the_intended_prices_come_from_the_fill_not_the_reference_price() -> None:
    """基準は実約定価格である。

    参照価格（遅延データ）から決めた値段が板に残っているのがこのツールの
    直す対象なので、ここで再び参照価格を使うと直す意味が無くなる。
    2026-08-12のINTC（実約定 102.41）で置き直しが要求した値と一致すること。
    """
    assert _intended_prices(102.41) == (97.29, 112.66)


def test_the_intended_prices_are_rounded_to_the_tick() -> None:
    """呼値へ丸めずに送ると `Warning 110` で静かに不成立になる。

    104.06 × 0.95 = 98.857 は呼値($0.01)に乗らない。丸めは切り上げ側
    （損切りが予定より遠くならない側）。
    """
    stop, take_profit = _intended_prices(104.06)

    assert stop == 98.86
    assert take_profit == 114.47


def test_the_intended_prices_follow_the_swing_parameters() -> None:
    """決済幅の定数を変えたら、直す先も一緒に動くこと（値を二重に持たない）。"""
    stop, take_profit = _intended_prices(100.0)

    assert stop == round(100.0 * (1 - bot.SWING_STOP_LOSS_PCT / 100), 2)
    assert take_profit == round(100.0 * (1 + bot.SWING_TAKE_PROFIT_PCT / 100), 2)


# --- 直す対象の絞り込み -------------------------------------------------------


def test_only_the_sell_resting_orders_of_that_symbol_are_touched() -> None:
    """新規建ての親(BUY)と他銘柄を拾ってはならない。

    OCAグループ名では突き合わせない（IBKRが親のpermIdへ書き換えるため）。
    銘柄と「売りのSTP/LMT」で絞れば、1銘柄1建玉なので曖昧さは無い。
    """
    trades = [
        _trade("INTC", "SELL", "STP", 95.44),
        _trade("INTC", "SELL", "LMT", 110.51),
        _trade("INTC", "BUY", "MKT", 0.0),
        _trade("UPS", "SELL", "STP", 98.69),
    ]

    found = _resting_exit_trades(trades, "INTC")

    assert sorted(found) == ["LMT", "STP"]
    assert _current_price(found["STP"].order) == 95.44
    assert _current_price(found["LMT"].order) == 110.51


def test_orders_that_are_no_longer_live_are_not_modified() -> None:
    """約定済み・取消済みの注文へ修正を送っても意味が無く、誤って新規注文になりうる。

    2026-08-18の実測では、修正が拒否されると `Cancelled` の複製が残る。
    これを次回の対象に拾うと、建玉を超える売り注文を並べかねない。
    """
    trades = [
        _trade("INTC", "SELL", "STP", 95.44, status="Cancelled"),
        _trade("INTC", "SELL", "LMT", 110.51, status="Filled"),
    ]

    assert _resting_exit_trades(trades, "INTC") == {}
