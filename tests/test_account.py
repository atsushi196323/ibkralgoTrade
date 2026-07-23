"""execution/account.py の単体テスト。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from execution.account import get_account_equity_async


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
