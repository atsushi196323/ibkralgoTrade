"""scripts/check_screener.py の単体テスト（IB呼び出しはすべてモック化）。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from main import SCREENER_MAX_PE_RATIO
from scripts.check_screener import (
    PeRatioSample,
    ScreenerDiagnosis,
    _check_pe_ratios,
    _check_scanner,
)


def _sample(symbol: str, pe_ratio) -> PeRatioSample:
    return PeRatioSample(symbol=symbol, pe_ratio=pe_ratio)


# --- PeRatioSample ----------------------------------------------------------------


def test_sample_distinguishes_fetch_failure_from_filter_rejection() -> None:
    """「取得できなかった」と「取得できたが条件で落ちた」を区別すること。

    購読権限の有無を判定する材料は前者だけ。割高・赤字による除外を
    権限の問題と混同すると、診断が意味をなさない。
    """
    failed = _sample("AAA", None)
    too_expensive = _sample("BBB", SCREENER_MAX_PE_RATIO + 10.0)
    passing = _sample("CCC", SCREENER_MAX_PE_RATIO - 1.0)

    assert failed.fetched is False
    assert too_expensive.fetched is True
    assert too_expensive.passes_filter is False
    assert passing.fetched is True
    assert passing.passes_filter is True


def test_negative_pe_is_fetched_but_rejected() -> None:
    """赤字銘柄(PERが負)はデータとしては取得できている。"""
    loss_making = _sample("AAA", -5.0)

    assert loss_making.fetched is True
    assert loss_making.passes_filter is False


# --- ScreenerDiagnosis ------------------------------------------------------------


def test_scanner_unavailable_when_no_hits() -> None:
    diagnosis = ScreenerDiagnosis(scan_hits=0, pe_samples=[])

    assert diagnosis.scanner_available is False
    assert diagnosis.falls_back_to_fixed_watchlist is True


def test_fundamentals_available_when_at_least_one_pe_is_fetched() -> None:
    """1銘柄でも取れれば権限はあると判断する。

    個別銘柄がたまたまPERを持たないケースと、購読権限の欠如を
    区別するため「全滅かどうか」で判定している。
    """
    diagnosis = ScreenerDiagnosis(
        scan_hits=50,
        pe_samples=[_sample("AAA", None), _sample("BBB", None), _sample("CCC", 12.0)],
    )

    assert diagnosis.fundamentals_available is True
    assert diagnosis.num_pe_fetched == 1
    assert diagnosis.falls_back_to_fixed_watchlist is False


def test_fundamentals_unavailable_when_every_pe_fetch_fails() -> None:
    diagnosis = ScreenerDiagnosis(
        scan_hits=50,
        pe_samples=[_sample("AAA", None), _sample("BBB", None)],
    )

    assert diagnosis.fundamentals_available is False
    assert diagnosis.falls_back_to_fixed_watchlist is True


def test_healthy_diagnosis_does_not_fall_back() -> None:
    diagnosis = ScreenerDiagnosis(
        scan_hits=50,
        pe_samples=[_sample("AAA", 12.0), _sample("BBB", 30.0)],
    )

    assert diagnosis.scanner_available is True
    assert diagnosis.fundamentals_available is True
    assert diagnosis.num_passing_filter == 1
    assert diagnosis.falls_back_to_fixed_watchlist is False


# --- _check_scanner ---------------------------------------------------------------


def test_check_scanner_returns_candidates() -> None:
    ib = MagicMock()
    candidates = [MagicMock(symbol="AAA"), MagicMock(symbol="BBB")]

    with patch(
        "scripts.check_screener.run_market_cap_scan_async",
        new=AsyncMock(return_value=candidates),
    ):
        result = asyncio.run(_check_scanner(ib, number_of_rows=50))

    assert result == candidates


def test_check_scanner_swallows_exceptions() -> None:
    """スキャンが例外で落ちても診断を続けられること（後段の判定も出したい）。"""
    ib = MagicMock()

    with patch(
        "scripts.check_screener.run_market_cap_scan_async",
        new=AsyncMock(side_effect=RuntimeError("no permission")),
    ):
        result = asyncio.run(_check_scanner(ib, number_of_rows=50))

    assert result == []


# --- _check_pe_ratios -------------------------------------------------------------


def test_check_pe_ratios_samples_only_the_requested_number() -> None:
    """権限の切り分けが目的なので、候補全件は叩かないこと。"""
    ib = MagicMock()
    candidates = [MagicMock(symbol=f"SYM{i}") for i in range(20)]
    mock_pe = AsyncMock(return_value=12.0)

    with patch("scripts.check_screener.get_pe_ratio_async", new=mock_pe):
        samples = asyncio.run(_check_pe_ratios(ib, candidates, num_samples=3, interval_seconds=0.0))

    assert len(samples) == 3
    assert mock_pe.await_count == 3


def test_check_pe_ratios_records_failures_without_stopping() -> None:
    ib = MagicMock()
    candidates = [MagicMock(symbol="AAA"), MagicMock(symbol="BBB"), MagicMock(symbol="CCC")]

    with patch(
        "scripts.check_screener.get_pe_ratio_async",
        new=AsyncMock(side_effect=[None, RuntimeError("boom"), 12.0]),
    ):
        samples = asyncio.run(_check_pe_ratios(ib, candidates, num_samples=3, interval_seconds=0.0))

    assert [s.fetched for s in samples] == [False, False, True]
    assert samples[2].pe_ratio == pytest.approx(12.0)


def test_check_pe_ratios_returns_empty_when_there_are_no_candidates() -> None:
    ib = MagicMock()

    with patch("scripts.check_screener.get_pe_ratio_async", new=AsyncMock()) as mock_pe:
        samples = asyncio.run(_check_pe_ratios(ib, [], num_samples=5, interval_seconds=0.0))

    assert samples == []
    mock_pe.assert_not_awaited()


def test_check_pe_ratios_paces_requests_between_symbols() -> None:
    """本番と同じ間隔を空けること（連続発行はペーシング制限に触れうる）。

    実時間のsleepをテストに持ち込まないよう、asyncio.sleepをモックして
    呼び出し回数だけを検証する。
    """
    ib = MagicMock()
    candidates = [MagicMock(symbol="AAA"), MagicMock(symbol="BBB"), MagicMock(symbol="CCC")]

    with patch("scripts.check_screener.get_pe_ratio_async", new=AsyncMock(return_value=12.0)), \
        patch("scripts.check_screener.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        asyncio.run(_check_pe_ratios(ib, candidates, num_samples=3, interval_seconds=1.0))

    # 1銘柄目の前では待たないため、3銘柄なら2回。
    assert mock_sleep.await_count == 2
    mock_sleep.assert_awaited_with(1.0)
