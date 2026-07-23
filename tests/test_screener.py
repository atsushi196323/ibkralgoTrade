"""strategy/screener.py の単体テスト（IB呼び出しはすべてモック化）。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from strategy.screener import ScreenerConfig, screen_value_stocks_async


def _make_contract(symbol: str) -> MagicMock:
    return MagicMock(symbol=symbol)


def test_screen_value_stocks_filters_by_max_pe_ratio() -> None:
    ib = MagicMock()
    candidates = [_make_contract("CHEAP"), _make_contract("EXPENSIVE"), _make_contract("NODATA")]

    async def _fake_pe_ratio(_ib, contract):
        return {"CHEAP": 10.0, "EXPENSIVE": 30.0, "NODATA": None}[contract.symbol]

    with patch("strategy.screener.run_market_cap_scan_async", new=AsyncMock(return_value=candidates)), \
        patch("strategy.screener.get_pe_ratio_async", new=AsyncMock(side_effect=_fake_pe_ratio)), \
        patch("strategy.screener.asyncio.sleep", new=AsyncMock()):

        result = asyncio.run(screen_value_stocks_async(ib, ScreenerConfig(max_pe_ratio=15.0)))

    assert result == ["CHEAP"]


def test_screen_value_stocks_paces_pe_requests_to_avoid_ibkr_rate_limit() -> None:
    ib = MagicMock()
    candidates = [_make_contract("A"), _make_contract("B"), _make_contract("C")]

    with patch("strategy.screener.run_market_cap_scan_async", new=AsyncMock(return_value=candidates)), \
        patch("strategy.screener.get_pe_ratio_async", new=AsyncMock(return_value=10.0)), \
        patch("strategy.screener.asyncio.sleep", new=AsyncMock()) as mock_sleep:

        asyncio.run(
            screen_value_stocks_async(
                ib, ScreenerConfig(max_pe_ratio=15.0, pe_request_interval_seconds=2.0)
            )
        )

    # 候補間の待機のみ挿入される(3件->2回)。最初のリクエスト前や最後の後には不要。
    assert mock_sleep.await_count == 2
    mock_sleep.assert_awaited_with(2.0)


def test_screen_value_stocks_skips_sleep_when_interval_is_zero() -> None:
    ib = MagicMock()
    candidates = [_make_contract("A"), _make_contract("B")]

    with patch("strategy.screener.run_market_cap_scan_async", new=AsyncMock(return_value=candidates)), \
        patch("strategy.screener.get_pe_ratio_async", new=AsyncMock(return_value=10.0)), \
        patch("strategy.screener.asyncio.sleep", new=AsyncMock()) as mock_sleep:

        asyncio.run(
            screen_value_stocks_async(ib, ScreenerConfig(max_pe_ratio=15.0, pe_request_interval_seconds=0.0))
        )

    mock_sleep.assert_not_awaited()


def test_screen_value_stocks_excludes_negative_pe_ratio() -> None:
    ib = MagicMock()
    candidates = [_make_contract("LOSSMAKER")]

    with patch("strategy.screener.run_market_cap_scan_async", new=AsyncMock(return_value=candidates)), \
        patch("strategy.screener.get_pe_ratio_async", new=AsyncMock(return_value=-5.0)):

        result = asyncio.run(screen_value_stocks_async(ib, ScreenerConfig(max_pe_ratio=15.0)))

    assert result == []


def test_screen_value_stocks_returns_empty_when_no_candidates() -> None:
    ib = MagicMock()

    with patch("strategy.screener.run_market_cap_scan_async", new=AsyncMock(return_value=[])), \
        patch("strategy.screener.get_pe_ratio_async", new=AsyncMock()) as mock_pe:

        result = asyncio.run(screen_value_stocks_async(ib, ScreenerConfig()))

    assert result == []
    mock_pe.assert_not_awaited()


def test_screen_value_stocks_passes_config_to_scan() -> None:
    ib = MagicMock()
    config = ScreenerConfig(
        market_cap_above=3e9, market_cap_below=1e11, max_pe_ratio=20.0,
        scan_code="TOP_PERC_GAIN", number_of_rows=10,
    )

    with patch("strategy.screener.run_market_cap_scan_async", new=AsyncMock(return_value=[])) as mock_scan, \
        patch("strategy.screener.get_pe_ratio_async", new=AsyncMock()):

        asyncio.run(screen_value_stocks_async(ib, config))

    mock_scan.assert_awaited_once_with(
        ib, market_cap_above=3e9, market_cap_below=1e11, scan_code="TOP_PERC_GAIN", number_of_rows=10,
    )


def test_screen_value_stocks_raises_on_non_positive_max_pe_ratio() -> None:
    ib = MagicMock()

    with pytest.raises(ValueError):
        asyncio.run(screen_value_stocks_async(ib, ScreenerConfig(max_pe_ratio=0.0)))
