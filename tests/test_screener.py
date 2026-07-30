"""strategy/screener.py の単体テスト（IB呼び出しはすべてモック化）。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from strategy.screener import ScreenerConfig, _is_in_long_term_uptrend, screen_value_stocks_async


def _make_contract(symbol: str) -> MagicMock:
    return MagicMock(symbol=symbol)


def _make_price_df(closes: list) -> pd.DataFrame:
    return pd.DataFrame({"close": closes})


def test_screen_value_stocks_filters_by_max_pe_ratio() -> None:
    ib = MagicMock()
    candidates = [_make_contract("CHEAP"), _make_contract("EXPENSIVE"), _make_contract("NODATA")]

    async def _fake_pe_ratio(_ib, contract):
        return {"CHEAP": 10.0, "EXPENSIVE": 30.0, "NODATA": None}[contract.symbol]

    with patch("strategy.screener.run_market_cap_scan_async", new=AsyncMock(return_value=candidates)), \
        patch("strategy.screener.get_pe_ratio_async", new=AsyncMock(side_effect=_fake_pe_ratio)), \
        patch("strategy.screener.asyncio.sleep", new=AsyncMock()):

        result = asyncio.run(
            screen_value_stocks_async(
                ib, ScreenerConfig(max_pe_ratio=15.0, enable_trend_filter=False)
            )
        )

    assert result == ["CHEAP"]


def test_screen_value_stocks_paces_pe_requests_to_avoid_ibkr_rate_limit() -> None:
    ib = MagicMock()
    candidates = [_make_contract("A"), _make_contract("B"), _make_contract("C")]

    with patch("strategy.screener.run_market_cap_scan_async", new=AsyncMock(return_value=candidates)), \
        patch("strategy.screener.get_pe_ratio_async", new=AsyncMock(return_value=10.0)), \
        patch("strategy.screener.asyncio.sleep", new=AsyncMock()) as mock_sleep:

        asyncio.run(
            screen_value_stocks_async(
                ib,
                ScreenerConfig(
                    max_pe_ratio=15.0, pe_request_interval_seconds=2.0, enable_trend_filter=False,
                ),
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
            screen_value_stocks_async(
                ib,
                ScreenerConfig(
                    max_pe_ratio=15.0, pe_request_interval_seconds=0.0, enable_trend_filter=False,
                ),
            )
        )

    mock_sleep.assert_not_awaited()


def test_screen_value_stocks_excludes_negative_pe_ratio() -> None:
    ib = MagicMock()
    candidates = [_make_contract("LOSSMAKER")]

    with patch("strategy.screener.run_market_cap_scan_async", new=AsyncMock(return_value=candidates)), \
        patch("strategy.screener.get_pe_ratio_async", new=AsyncMock(return_value=-5.0)):

        result = asyncio.run(screen_value_stocks_async(ib, ScreenerConfig(max_pe_ratio=15.0)))

    assert result == []


def test_is_in_long_term_uptrend_true_when_close_above_ma() -> None:
    df = _make_price_df([100.0] * 199 + [150.0])  # 直近急伸 -> MAを上回る

    assert _is_in_long_term_uptrend(df, ma_window=200) is True


def test_is_in_long_term_uptrend_false_when_close_below_ma() -> None:
    df = _make_price_df([100.0] * 199 + [50.0])  # 直近急落 -> MAを下回る

    assert _is_in_long_term_uptrend(df, ma_window=200) is False


def test_is_in_long_term_uptrend_none_when_insufficient_data() -> None:
    df = _make_price_df([100.0] * 50)

    assert _is_in_long_term_uptrend(df, ma_window=200) is None


def test_screen_value_stocks_excludes_candidate_below_long_term_trend() -> None:
    ib = MagicMock()
    candidates = [_make_contract("DOWNTREND"), _make_contract("UPTREND")]
    trend_dfs = {
        "DOWNTREND": _make_price_df([100.0] * 199 + [50.0]),
        "UPTREND": _make_price_df([100.0] * 199 + [150.0]),
    }

    async def _fake_historical_bars(_ib, contract, duration, bar_size, what_to_show="TRADES"):
        return trend_dfs[contract.symbol]

    with patch("strategy.screener.run_market_cap_scan_async", new=AsyncMock(return_value=candidates)), \
        patch("strategy.screener.get_pe_ratio_async", new=AsyncMock(return_value=10.0)), \
        patch("strategy.screener.get_historical_bars_async", new=AsyncMock(side_effect=_fake_historical_bars)), \
        patch("strategy.screener.asyncio.sleep", new=AsyncMock()):

        result = asyncio.run(
            screen_value_stocks_async(ib, ScreenerConfig(max_pe_ratio=15.0, enable_trend_filter=True))
        )

    assert result == ["UPTREND"]


def test_screen_value_stocks_keeps_candidate_when_trend_data_insufficient() -> None:
    ib = MagicMock()
    candidates = [_make_contract("NEWLISTING")]

    with patch("strategy.screener.run_market_cap_scan_async", new=AsyncMock(return_value=candidates)), \
        patch("strategy.screener.get_pe_ratio_async", new=AsyncMock(return_value=10.0)), \
        patch(
            "strategy.screener.get_historical_bars_async",
            new=AsyncMock(return_value=_make_price_df([100.0] * 50)),
        ), \
        patch("strategy.screener.asyncio.sleep", new=AsyncMock()):

        result = asyncio.run(
            screen_value_stocks_async(ib, ScreenerConfig(max_pe_ratio=15.0, enable_trend_filter=True))
        )

    assert result == ["NEWLISTING"]


def test_screen_value_stocks_skips_trend_filter_when_disabled() -> None:
    ib = MagicMock()
    candidates = [_make_contract("CHEAP")]

    with patch("strategy.screener.run_market_cap_scan_async", new=AsyncMock(return_value=candidates)), \
        patch("strategy.screener.get_pe_ratio_async", new=AsyncMock(return_value=10.0)), \
        patch("strategy.screener.get_historical_bars_async", new=AsyncMock()) as mock_hist, \
        patch("strategy.screener.asyncio.sleep", new=AsyncMock()):

        result = asyncio.run(
            screen_value_stocks_async(ib, ScreenerConfig(max_pe_ratio=15.0, enable_trend_filter=False))
        )

    assert result == ["CHEAP"]
    mock_hist.assert_not_awaited()


def test_screen_value_stocks_passes_trend_config_to_historical_bars_fetch() -> None:
    ib = MagicMock()
    candidates = [_make_contract("CHEAP")]

    with patch("strategy.screener.run_market_cap_scan_async", new=AsyncMock(return_value=candidates)), \
        patch("strategy.screener.get_pe_ratio_async", new=AsyncMock(return_value=10.0)), \
        patch(
            "strategy.screener.get_historical_bars_async",
            new=AsyncMock(return_value=_make_price_df([100.0] * 199 + [150.0])),
        ) as mock_hist, \
        patch("strategy.screener.asyncio.sleep", new=AsyncMock()):

        asyncio.run(
            screen_value_stocks_async(
                ib,
                ScreenerConfig(
                    max_pe_ratio=15.0, enable_trend_filter=True,
                    trend_ma_window=200, trend_lookback_duration="300 D",
                ),
            )
        )

    mock_hist.assert_awaited_once_with(ib, candidates[0], duration="300 D", bar_size="1 day")


def test_screen_value_stocks_paces_requests_across_pe_and_trend_lookups() -> None:
    ib = MagicMock()
    candidates = [_make_contract("A"), _make_contract("B")]

    with patch("strategy.screener.run_market_cap_scan_async", new=AsyncMock(return_value=candidates)), \
        patch("strategy.screener.get_pe_ratio_async", new=AsyncMock(return_value=10.0)), \
        patch(
            "strategy.screener.get_historical_bars_async",
            new=AsyncMock(return_value=_make_price_df([100.0] * 199 + [150.0])),
        ), \
        patch("strategy.screener.asyncio.sleep", new=AsyncMock()) as mock_sleep:

        asyncio.run(
            screen_value_stocks_async(
                ib,
                ScreenerConfig(
                    max_pe_ratio=15.0, pe_request_interval_seconds=1.5, enable_trend_filter=True,
                ),
            )
        )

    # 2銘柄 x (PER取得 + トレンド判定) = 4リクエスト -> 最初の1回を除き3回待機
    assert mock_sleep.await_count == 3


def test_screen_value_stocks_skips_candidate_when_pe_lookup_raises() -> None:
    ib = MagicMock()
    candidates = [_make_contract("BROKEN"), _make_contract("CHEAP")]

    async def _fake_pe_ratio(_ib, contract):
        if contract.symbol == "BROKEN":
            raise TimeoutError("IBKRペーシング制限違反")
        return 10.0

    with patch("strategy.screener.run_market_cap_scan_async", new=AsyncMock(return_value=candidates)), \
        patch("strategy.screener.get_pe_ratio_async", new=AsyncMock(side_effect=_fake_pe_ratio)), \
        patch("strategy.screener.asyncio.sleep", new=AsyncMock()):

        result = asyncio.run(
            screen_value_stocks_async(
                ib, ScreenerConfig(max_pe_ratio=15.0, enable_trend_filter=False)
            )
        )

    # BROKENの例外はこの銘柄をスキップするだけで、他の候補の選定は継続される。
    assert result == ["CHEAP"]


def test_screen_value_stocks_skips_candidate_when_trend_lookup_raises() -> None:
    ib = MagicMock()
    candidates = [_make_contract("BROKEN"), _make_contract("UPTREND")]

    async def _fake_historical_bars(_ib, contract, duration, bar_size, what_to_show="TRADES"):
        if contract.symbol == "BROKEN":
            raise ConnectionError("一時的な切断")
        return _make_price_df([100.0] * 199 + [150.0])

    with patch("strategy.screener.run_market_cap_scan_async", new=AsyncMock(return_value=candidates)), \
        patch("strategy.screener.get_pe_ratio_async", new=AsyncMock(return_value=10.0)), \
        patch("strategy.screener.get_historical_bars_async", new=AsyncMock(side_effect=_fake_historical_bars)), \
        patch("strategy.screener.asyncio.sleep", new=AsyncMock()):

        result = asyncio.run(
            screen_value_stocks_async(ib, ScreenerConfig(max_pe_ratio=15.0, enable_trend_filter=True))
        )

    assert result == ["UPTREND"]


def test_screen_value_stocks_aborts_after_consecutive_pe_failures(caplog) -> None:
    ib = MagicMock()
    # 候補10件のうち、購読権限が無い想定で全件PER取得不可(None)にする。
    candidates = [_make_contract(f"SYM{i}") for i in range(10)]

    with patch("strategy.screener.run_market_cap_scan_async", new=AsyncMock(return_value=candidates)), \
        patch("strategy.screener.get_pe_ratio_async", new=AsyncMock(return_value=None)) as mock_pe, \
        patch("strategy.screener.asyncio.sleep", new=AsyncMock()), \
        caplog.at_level("ERROR", logger="strategy.screener"):

        result = asyncio.run(
            screen_value_stocks_async(
                ib,
                ScreenerConfig(
                    max_pe_ratio=15.0, enable_trend_filter=False, max_consecutive_pe_failures=3,
                ),
            )
        )

    assert result == []
    # 3件連続失敗した時点で打ち切られ、残り7件は処理されない(10件全件は呼ばれない)。
    assert mock_pe.await_count == 3
    assert any("購読権限" in record.message for record in caplog.records)


def test_screen_value_stocks_resets_failure_streak_on_success() -> None:
    ib = MagicMock()
    candidates = [_make_contract(f"SYM{i}") for i in range(6)]

    async def _fake_pe_ratio(_ib, contract):
        # 1件おきに失敗させる(連続失敗にはならない)。
        index = int(contract.symbol.removeprefix("SYM"))
        return None if index % 2 == 0 else 10.0

    with patch("strategy.screener.run_market_cap_scan_async", new=AsyncMock(return_value=candidates)), \
        patch("strategy.screener.get_pe_ratio_async", new=AsyncMock(side_effect=_fake_pe_ratio)), \
        patch("strategy.screener.asyncio.sleep", new=AsyncMock()):

        result = asyncio.run(
            screen_value_stocks_async(
                ib,
                ScreenerConfig(
                    max_pe_ratio=15.0, enable_trend_filter=False, max_consecutive_pe_failures=2,
                ),
            )
        )

    # 連続失敗が閾値(2)に達しないため、全6件処理され、成功した3件が選定される。
    assert result == ["SYM1", "SYM3", "SYM5"]


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


# --- 株価上限フィルター -------------------------------------------------------------


def _run_with_bars(config: ScreenerConfig, candidates: list, bars: dict) -> list:
    ib = MagicMock()

    async def _fake_historical_bars(_ib, contract, duration, bar_size, what_to_show="TRADES"):
        return bars[contract.symbol]

    with patch("strategy.screener.run_market_cap_scan_async", new=AsyncMock(return_value=candidates)), \
        patch("strategy.screener.get_pe_ratio_async", new=AsyncMock(return_value=10.0)), \
        patch("strategy.screener.get_historical_bars_async", new=AsyncMock(side_effect=_fake_historical_bars)), \
        patch("strategy.screener.asyncio.sleep", new=AsyncMock()):
        return asyncio.run(screen_value_stocks_async(ib, config))


def test_screen_value_stocks_excludes_symbols_above_max_price() -> None:
    """買えない株価の銘柄を監視対象に残さないこと。

    残すと毎サイクルの日中足リクエスト（ペーシング枠）を消費したうえで、
    数量0株になって必ずスキップされる。監視枠は10件しかない。
    """
    candidates = [_make_contract("CHEAP"), _make_contract("PRICEY")]
    bars = {
        "CHEAP": _make_price_df([100.0] * 200),
        "PRICEY": _make_price_df([500.0] * 200),
    }

    result = _run_with_bars(
        ScreenerConfig(max_pe_ratio=15.0, enable_trend_filter=False, max_price=244.0),
        candidates, bars,
    )

    assert result == ["CHEAP"]


def test_screen_value_stocks_excludes_symbols_with_unknown_price() -> None:
    """株価が取れない銘柄は除外に倒すこと（素通しすると気付けない）。"""
    candidates = [_make_contract("CHEAP"), _make_contract("NOBARS")]
    bars = {
        "CHEAP": _make_price_df([100.0] * 200),
        "NOBARS": pd.DataFrame(),
    }

    result = _run_with_bars(
        ScreenerConfig(max_pe_ratio=15.0, enable_trend_filter=False, max_price=244.0),
        candidates, bars,
    )

    assert result == ["CHEAP"]


def test_max_price_and_trend_filter_share_one_bar_request() -> None:
    """両方有効でも日足の取得は1銘柄1回で済ませること（ペーシング制限対策）。"""
    ib = MagicMock()
    candidates = [_make_contract("CHEAP")]
    # 終値が移動平均を上回る形（＝トレンドフィルターも通る）にする。
    mock_bars = AsyncMock(return_value=_make_price_df([100.0] * 199 + [150.0]))

    with patch("strategy.screener.run_market_cap_scan_async", new=AsyncMock(return_value=candidates)), \
        patch("strategy.screener.get_pe_ratio_async", new=AsyncMock(return_value=10.0)), \
        patch("strategy.screener.get_historical_bars_async", new=mock_bars), \
        patch("strategy.screener.asyncio.sleep", new=AsyncMock()):
        result = asyncio.run(screen_value_stocks_async(
            ib, ScreenerConfig(max_pe_ratio=15.0, enable_trend_filter=True, max_price=244.0),
        ))

    assert result == ["CHEAP"]
    assert mock_bars.await_count == 1


def test_no_bars_are_fetched_when_both_bar_based_filters_are_off() -> None:
    """株価上限もトレンドも無効なら、日足を取得しないこと。"""
    ib = MagicMock()
    candidates = [_make_contract("CHEAP")]
    mock_bars = AsyncMock()

    with patch("strategy.screener.run_market_cap_scan_async", new=AsyncMock(return_value=candidates)), \
        patch("strategy.screener.get_pe_ratio_async", new=AsyncMock(return_value=10.0)), \
        patch("strategy.screener.get_historical_bars_async", new=mock_bars), \
        patch("strategy.screener.asyncio.sleep", new=AsyncMock()):
        result = asyncio.run(screen_value_stocks_async(
            ib, ScreenerConfig(max_pe_ratio=15.0, enable_trend_filter=False, max_price=None),
        ))

    assert result == ["CHEAP"]
    mock_bars.assert_not_awaited()
