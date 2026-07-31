"""execution/account.py の単体テスト。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from execution.account import get_account_equity_async, get_settled_cash_async


def _make_account_value(tag: str, value: str):
    return MagicMock(tag=tag, value=value)


def test_returns_net_liquidation_by_default() -> None:
    ib = MagicMock()
    ib.accountSummaryAsync = AsyncMock(
        return_value=[
            _make_account_value("AvailableFunds", "50000.0"),
            _make_account_value("NetLiquidation", "123456.78"),
        ]
    )

    equity = asyncio.run(get_account_equity_async(ib))

    assert equity == pytest.approx(123456.78)


def test_returns_requested_tag() -> None:
    ib = MagicMock()
    ib.accountSummaryAsync = AsyncMock(
        return_value=[
            _make_account_value("NetLiquidation", "123456.78"),
            _make_account_value("AvailableFunds", "50000.0"),
        ]
    )

    equity = asyncio.run(get_account_equity_async(ib, tag="AvailableFunds"))

    assert equity == pytest.approx(50000.0)


def test_raises_when_tag_not_found() -> None:
    ib = MagicMock()
    ib.accountSummaryAsync = AsyncMock(return_value=[_make_account_value("AvailableFunds", "50000.0")])

    with pytest.raises(ValueError):
        asyncio.run(get_account_equity_async(ib))


# --- 決済済み現金（GFV回避の資金判定に使う） -------------------------------------


def test_returns_settled_cash() -> None:
    ib = MagicMock()
    ib.accountSummaryAsync = AsyncMock(
        return_value=[
            _make_account_value("NetLiquidation", "123456.78"),
            _make_account_value("SettledCash", "1234.56"),
        ]
    )

    assert asyncio.run(get_settled_cash_async(ib)) == pytest.approx(1234.56)


def test_settled_cash_returns_none_when_tag_is_missing() -> None:
    """例外ではなくNoneを返すこと。

    「取得できなかった」と「0ドルしかない」を呼び出し側が区別できる必要がある。
    0として扱うと、資金があるのに永久に建てられなくなる。
    """
    ib = MagicMock()
    ib.accountSummaryAsync = AsyncMock(
        return_value=[_make_account_value("NetLiquidation", "123456.78")]
    )

    assert asyncio.run(get_settled_cash_async(ib)) is None


def test_settled_cash_returns_none_when_value_is_not_numeric() -> None:
    ib = MagicMock()
    ib.accountSummaryAsync = AsyncMock(
        return_value=[_make_account_value("SettledCash", "")]
    )

    assert asyncio.run(get_settled_cash_async(ib)) is None


def test_settled_cash_can_be_zero() -> None:
    """0ドルは正常な観測値であり、Noneと区別されること。"""
    ib = MagicMock()
    ib.accountSummaryAsync = AsyncMock(
        return_value=[_make_account_value("SettledCash", "0.0")]
    )

    assert asyncio.run(get_settled_cash_async(ib)) == pytest.approx(0.0)
