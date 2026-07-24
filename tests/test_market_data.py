"""data/market_data.py の単体テスト（IB呼び出しはすべてモック化）。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from data.market_data import get_current_price_async, get_intraday_bars_async


def _make_ticker(market_price, close):
    ticker = MagicMock()
    ticker.marketPrice = MagicMock(return_value=market_price)
    ticker.close = close
    return ticker


def test_get_current_price_returns_market_price_when_available() -> None:
    ib = MagicMock()
    contract = MagicMock(symbol="AAPL")
    ticker = _make_ticker(market_price=150.0, close=149.5)
    ib.reqTickersAsync = AsyncMock(return_value=[ticker])

    price = asyncio.run(get_current_price_async(ib, contract))

    assert price == 150.0


def test_get_current_price_falls_back_to_close_when_market_price_is_nan() -> None:
    ib = MagicMock()
    contract = MagicMock(symbol="AAPL")
    ticker = _make_ticker(market_price=float("nan"), close=149.5)
    ib.reqTickersAsync = AsyncMock(return_value=[ticker])

    price = asyncio.run(get_current_price_async(ib, contract))

    assert price == 149.5


def test_get_current_price_returns_none_when_market_price_and_close_are_both_nan() -> None:
    ib = MagicMock()
    contract = MagicMock(symbol="AAPL")
    ticker = _make_ticker(market_price=float("nan"), close=float("nan"))
    ib.reqTickersAsync = AsyncMock(return_value=[ticker])

    price = asyncio.run(get_current_price_async(ib, contract))

    assert price is None


def test_get_current_price_returns_none_when_market_price_and_close_are_both_none() -> None:
    ib = MagicMock()
    contract = MagicMock(symbol="AAPL")
    ticker = _make_ticker(market_price=None, close=None)
    ib.reqTickersAsync = AsyncMock(return_value=[ticker])

    price = asyncio.run(get_current_price_async(ib, contract))

    assert price is None


def test_get_intraday_bars_delegates_to_historical_bars_with_given_params() -> None:
    ib = MagicMock()
    contract = MagicMock(symbol="AAPL")
    expected_df = pd.DataFrame({"close": [1.0, 2.0]})

    with patch(
        "data.market_data.get_historical_bars_async", new=AsyncMock(return_value=expected_df)
    ) as mock_get_bars:
        result = asyncio.run(
            get_intraday_bars_async(ib, contract, duration="2 D", bar_size="5 mins")
        )

    mock_get_bars.assert_awaited_once_with(
        ib, contract, duration="2 D", bar_size="5 mins", what_to_show="TRADES",
    )
    assert result is expected_df


def test_get_intraday_bars_uses_day_trade_defaults() -> None:
    ib = MagicMock()
    contract = MagicMock(symbol="AAPL")

    with patch(
        "data.market_data.get_historical_bars_async", new=AsyncMock(return_value=pd.DataFrame())
    ) as mock_get_bars:
        asyncio.run(get_intraday_bars_async(ib, contract))

    mock_get_bars.assert_awaited_once_with(
        ib, contract, duration="2 D", bar_size="5 mins", what_to_show="TRADES",
    )


@pytest.mark.parametrize("bar_size", ["1 day", "1 week", "not a real size"])
def test_get_intraday_bars_rejects_non_intraday_bar_sizes(bar_size) -> None:
    ib = MagicMock()
    contract = MagicMock(symbol="AAPL")

    with pytest.raises(ValueError):
        asyncio.run(get_intraday_bars_async(ib, contract, bar_size=bar_size))


@pytest.mark.parametrize("bar_size", ["1 min", "5 mins", "15 mins", "1 hour"])
def test_get_intraday_bars_accepts_known_intraday_bar_sizes(bar_size) -> None:
    ib = MagicMock()
    contract = MagicMock(symbol="AAPL")

    with patch(
        "data.market_data.get_historical_bars_async", new=AsyncMock(return_value=pd.DataFrame())
    ):
        asyncio.run(get_intraday_bars_async(ib, contract, bar_size=bar_size))
