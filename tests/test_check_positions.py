"""scripts/check_positions.py の単体テスト（IBKRへの実接続は行わない）。"""

from types import SimpleNamespace

from execution.order_manager import RestingExitProtection
from scripts.check_positions import _broker_positions, _format_protection


def _broker_item(symbol: str, quantity: float, sec_type: str = "STK", currency: str = "USD"):
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=symbol, secType=sec_type, currency=currency),
        position=quantity,
    )


# --- ブローカー建玉の絞り込み -------------------------------------------------


def test_only_us_stock_long_positions_are_compared() -> None:
    """`reqPositions` は全口座・全アセットクラスを返す。

    シンボル文字列だけで突き合わせると、オプションや他国上場の同名株を
    現物ポジションと取り違える（`PositionManager._is_tracked_position` と同じ絞り込み）。
    """
    raw = [
        _broker_item("UPS", 2),
        _broker_item("AAPL", 1, sec_type="OPT"),
        _broker_item("SAP", 10, currency="EUR"),
        _broker_item("TSLA", -5),
    ]

    assert _broker_positions(raw) == {"UPS": 2.0}


def test_positions_without_a_contract_are_skipped() -> None:
    assert _broker_positions([SimpleNamespace(contract=None, position=3)]) == {}


# --- 待機注文の表示 -----------------------------------------------------------


def test_a_position_without_resting_orders_is_called_out_as_unprotected() -> None:
    assert "無防備" in _format_protection(None)


def test_a_half_live_bracket_is_called_out_as_unprotected() -> None:
    """**片方だけでは守られていない。**

    2026-08-05の実測では、呼値違反で逆指値だけが不成立になり、
    利確だけが生きた建玉が残った（下方向に無防備）。
    """
    protection = RestingExitProtection(
        live_order_types=frozenset({"LMT"}), has_filled_exit=False, take_profit_price=114.27,
    )

    text = _format_protection(protection)

    assert "無防備" in text
    assert "114.27" in text


def test_a_complete_bracket_reports_both_book_prices() -> None:
    protection = RestingExitProtection(
        live_order_types=frozenset({"STP", "LMT"}),
        has_filled_exit=False,
        stop_price=98.69,
        take_profit_price=114.27,
    )

    text = _format_protection(protection)

    assert "生存" in text and "無防備" not in text
    assert "98.69" in text and "114.27" in text


def test_a_filled_resting_order_means_the_position_is_already_closed() -> None:
    """約定が観測できた場合、建玉はもう閉じている（置き直しの対象ではない）。"""
    protection = RestingExitProtection(
        live_order_types=frozenset(), has_filled_exit=True,
    )

    assert "閉じている" in _format_protection(protection)
