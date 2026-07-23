"""data/fundamentals.py の単体テスト（IB呼び出しはすべてモック化）。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from ib_insync import ScannerSubscription

from data.fundamentals import get_pe_ratio_async, run_market_cap_scan_async

_SNAPSHOT_XML = """<ReportSnapshot>
  <Ratios>
    <Group ID="Valuation">
      <Ratio FieldName="MKTCAP" Type="N">150000</Ratio>
      <Ratio FieldName="PEEXCLXOR" Type="N">12.5</Ratio>
    </Group>
  </Ratios>
</ReportSnapshot>"""


def _make_scan_data(symbol: str) -> MagicMock:
    contract = MagicMock(symbol=symbol)
    contract_details = MagicMock(contract=contract)
    return MagicMock(contractDetails=contract_details)


# --- run_market_cap_scan_async ---------------------------------------------------


def test_run_market_cap_scan_returns_contracts_from_scan_results() -> None:
    ib = MagicMock()
    scan_results = [_make_scan_data("AAPL"), _make_scan_data("MSFT")]
    ib.reqScannerDataAsync = AsyncMock(return_value=scan_results)

    stocks = asyncio.run(
        run_market_cap_scan_async(ib, market_cap_above=1e9, market_cap_below=1e11)
    )

    assert [s.symbol for s in stocks] == ["AAPL", "MSFT"]


def test_run_market_cap_scan_builds_subscription_with_market_cap_filters() -> None:
    ib = MagicMock()
    ib.reqScannerDataAsync = AsyncMock(return_value=[])

    asyncio.run(
        run_market_cap_scan_async(
            ib, market_cap_above=2e9, market_cap_below=5e10, scan_code="TOP_PERC_GAIN", number_of_rows=25,
        )
    )

    ib.reqScannerDataAsync.assert_awaited_once()
    (subscription,), _ = ib.reqScannerDataAsync.call_args
    assert isinstance(subscription, ScannerSubscription)
    assert subscription.marketCapAbove == 2e9
    assert subscription.marketCapBelow == 5e10
    assert subscription.scanCode == "TOP_PERC_GAIN"
    assert subscription.numberOfRows == 25


@pytest.mark.parametrize(
    "market_cap_above,market_cap_below",
    [(-1.0, 1e9), (1e9, -1.0), (5e9, 1e9)],
)
def test_run_market_cap_scan_raises_on_invalid_range(market_cap_above, market_cap_below) -> None:
    ib = MagicMock()

    with pytest.raises(ValueError):
        asyncio.run(
            run_market_cap_scan_async(ib, market_cap_above=market_cap_above, market_cap_below=market_cap_below)
        )


# --- get_pe_ratio_async ------------------------------------------------------------


def test_get_pe_ratio_parses_valid_snapshot_xml() -> None:
    ib = MagicMock()
    ib.reqFundamentalDataAsync = AsyncMock(return_value=_SNAPSHOT_XML)
    contract = MagicMock(symbol="AAPL")

    pe_ratio = asyncio.run(get_pe_ratio_async(ib, contract))

    assert pe_ratio == pytest.approx(12.5)


def test_get_pe_ratio_returns_none_when_report_empty() -> None:
    ib = MagicMock()
    ib.reqFundamentalDataAsync = AsyncMock(return_value="")
    contract = MagicMock(symbol="AAPL")

    assert asyncio.run(get_pe_ratio_async(ib, contract)) is None


def test_get_pe_ratio_returns_none_when_field_missing() -> None:
    ib = MagicMock()
    ib.reqFundamentalDataAsync = AsyncMock(
        return_value="<ReportSnapshot><Ratios></Ratios></ReportSnapshot>"
    )
    contract = MagicMock(symbol="AAPL")

    assert asyncio.run(get_pe_ratio_async(ib, contract)) is None


def test_get_pe_ratio_returns_none_on_malformed_xml() -> None:
    ib = MagicMock()
    ib.reqFundamentalDataAsync = AsyncMock(return_value="<not><valid xml")
    contract = MagicMock(symbol="AAPL")

    assert asyncio.run(get_pe_ratio_async(ib, contract)) is None
