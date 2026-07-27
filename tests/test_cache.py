"""data/cache.py の単体テスト（IB呼び出しはすべてモック化）。"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd

from core.market_hours import US_EASTERN
from data.cache import ContractCache, DailyBarCache


def _make_bars(closes: list) -> pd.DataFrame:
    return pd.DataFrame({"close": closes})


def _et(year: int, month: int, day: int, hour: int = 10) -> datetime:
    return datetime(year, month, day, hour, tzinfo=US_EASTERN)


# --- DailyBarCache --------------------------------------------------------------


def test_daily_bars_are_fetched_once_per_trading_day() -> None:
    ib = MagicMock()
    contract = MagicMock(symbol="AAPL")
    bars = _make_bars([1.0, 2.0])
    cache = DailyBarCache()
    now = _et(2026, 7, 27)

    with patch(
        "data.cache.get_historical_bars_async", new=AsyncMock(return_value=bars)
    ) as mock_fetch:
        async def run():
            first = await cache.get_async(ib, contract, now=now)
            second = await cache.get_async(ib, contract, now=now)
            return first, second

        first, second = asyncio.run(run())

    # 日足は1取引日に1本しか増えないため、同じ日に取り直さない
    mock_fetch.assert_awaited_once()
    assert first is second


def test_daily_bars_are_refetched_on_the_next_trading_day() -> None:
    ib = MagicMock()
    contract = MagicMock(symbol="AAPL")
    cache = DailyBarCache()

    with patch(
        "data.cache.get_historical_bars_async",
        new=AsyncMock(side_effect=[_make_bars([1.0]), _make_bars([1.0, 2.0])]),
    ) as mock_fetch:
        async def run():
            await cache.get_async(ib, contract, now=_et(2026, 7, 27))
            return await cache.get_async(ib, contract, now=_et(2026, 7, 28))

        result = asyncio.run(run())

    assert mock_fetch.await_count == 2
    assert len(result) == 2


def test_daily_bars_are_cached_per_symbol() -> None:
    ib = MagicMock()
    cache = DailyBarCache()
    now = _et(2026, 7, 27)

    with patch(
        "data.cache.get_historical_bars_async", new=AsyncMock(return_value=_make_bars([1.0]))
    ) as mock_fetch:
        async def run():
            await cache.get_async(ib, MagicMock(symbol="AAPL"), now=now)
            await cache.get_async(ib, MagicMock(symbol="MSFT"), now=now)
            await cache.get_async(ib, MagicMock(symbol="AAPL"), now=now)

        asyncio.run(run())

    assert mock_fetch.await_count == 2


def test_empty_daily_bars_are_not_cached() -> None:
    """取得失敗(空)をキャッシュすると、その日いっぱい空を返し続けてしまう。"""
    ib = MagicMock()
    contract = MagicMock(symbol="AAPL")
    cache = DailyBarCache()
    now = _et(2026, 7, 27)

    with patch(
        "data.cache.get_historical_bars_async",
        new=AsyncMock(side_effect=[pd.DataFrame(), _make_bars([1.0])]),
    ) as mock_fetch:
        async def run():
            first = await cache.get_async(ib, contract, now=now)
            second = await cache.get_async(ib, contract, now=now)
            return first, second

        first, second = asyncio.run(run())

    assert mock_fetch.await_count == 2
    assert first.empty
    assert not second.empty


def test_daily_bar_cache_uses_configured_duration() -> None:
    ib = MagicMock()
    contract = MagicMock(symbol="AAPL")
    cache = DailyBarCache(duration="300 D")

    with patch(
        "data.cache.get_historical_bars_async", new=AsyncMock(return_value=_make_bars([1.0]))
    ) as mock_fetch:
        asyncio.run(cache.get_async(ib, contract, now=_et(2026, 7, 27)))

    assert mock_fetch.await_args.kwargs["duration"] == "300 D"
    assert mock_fetch.await_args.kwargs["bar_size"] == "1 day"


def test_daily_bar_cache_clear_forces_refetch() -> None:
    ib = MagicMock()
    contract = MagicMock(symbol="AAPL")
    cache = DailyBarCache()
    now = _et(2026, 7, 27)

    with patch(
        "data.cache.get_historical_bars_async", new=AsyncMock(return_value=_make_bars([1.0]))
    ) as mock_fetch:
        async def run():
            await cache.get_async(ib, contract, now=now)
            cache.clear()
            await cache.get_async(ib, contract, now=now)

        asyncio.run(run())

    assert mock_fetch.await_count == 2


# --- ContractCache --------------------------------------------------------------


def test_contract_is_qualified_only_once_per_symbol() -> None:
    ib = MagicMock()
    contract = MagicMock(symbol="AAPL")
    cache = ContractCache()

    with patch(
        "data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)
    ) as mock_qualify:
        async def run():
            first = await cache.get_async(ib, "AAPL")
            second = await cache.get_async(ib, "AAPL")
            return first, second

        first, second = asyncio.run(run())

    mock_qualify.assert_awaited_once()
    assert first is second is contract


def test_contract_cache_keeps_symbols_separate() -> None:
    ib = MagicMock()
    cache = ContractCache()

    with patch(
        "data.cache.qualify_stock_async",
        new=AsyncMock(side_effect=[MagicMock(symbol="AAPL"), MagicMock(symbol="MSFT")]),
    ) as mock_qualify:
        async def run():
            aapl = await cache.get_async(ib, "AAPL")
            msft = await cache.get_async(ib, "MSFT")
            return aapl, msft

        aapl, msft = asyncio.run(run())

    assert mock_qualify.await_count == 2
    assert aapl.symbol == "AAPL"
    assert msft.symbol == "MSFT"


def test_contract_qualification_failure_is_not_cached() -> None:
    ib = MagicMock()
    contract = MagicMock(symbol="AAPL")
    cache = ContractCache()

    with patch(
        "data.cache.qualify_stock_async",
        new=AsyncMock(side_effect=[ValueError("特定に失敗"), contract]),
    ):
        async def run():
            try:
                await cache.get_async(ib, "AAPL")
            except ValueError:
                pass
            return await cache.get_async(ib, "AAPL")

        result = asyncio.run(run())

    assert result is contract
