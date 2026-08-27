"""data/market_data.py の単体テスト（IB呼び出しはすべてモック化）。"""

import asyncio
import logging
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from core.market_hours import US_EASTERN
from data import market_data
from data.market_data import (
    DEFAULT_HISTORICAL_TIMEOUT_SECONDS,
    PRICE_SOURCE_HISTORICAL,
    PRICE_SOURCE_SNAPSHOT,
    PRICE_SOURCE_STREAMING,
    get_current_price_async,
    get_current_price_quote_async,
    get_historical_bars_async,
    get_historical_pacer,
    get_intraday_bars_async,
    get_usd_jpy_rate_async,
)


def _make_ticker(market_price, close):
    ticker = MagicMock()
    ticker.marketPrice = MagicMock(return_value=market_price)
    ticker.close = close
    return ticker


def _make_ib(snapshot_ticker=None, streaming_ticker=None) -> MagicMock:
    """現在価格の取得経路ごとに返す値を仕込んだIBモックを作る。

    Noneを渡した経路は「価格が取れない」状態（NaNのティッカー）にする。
    """
    ib = MagicMock()
    ib.reqTickersAsync = AsyncMock(
        return_value=[snapshot_ticker if snapshot_ticker is not None else _make_nan_ticker()]
    )
    ib.reqMktData = MagicMock(
        return_value=streaming_ticker if streaming_ticker is not None else _make_nan_ticker()
    )
    return ib


def _make_nan_ticker():
    return _make_ticker(market_price=float("nan"), close=float("nan"))


# 各テストで待機時間を消費しないよう、ストリーミングは即時判定のみにする。
_NO_WAIT = {"streaming_timeout_seconds": 0.0, "allow_historical_fallback": False}


# --- 現在価格: 取得経路のフォールバック連鎖 -------------------------------------


def test_get_current_price_prefers_streaming_over_snapshot() -> None:
    contract = MagicMock(symbol="AAPL")
    ib = _make_ib(
        streaming_ticker=_make_ticker(market_price=151.0, close=149.5),
        snapshot_ticker=_make_ticker(market_price=150.0, close=149.5),
    )

    price = asyncio.run(get_current_price_async(ib, contract, **_NO_WAIT))

    assert price == 151.0
    # ストリーミングで取れたならスナップショットは発行しない
    ib.reqTickersAsync.assert_not_awaited()


def test_get_current_price_cancels_streaming_subscription() -> None:
    contract = MagicMock(symbol="AAPL")
    ib = _make_ib(streaming_ticker=_make_ticker(market_price=151.0, close=149.5))

    asyncio.run(get_current_price_async(ib, contract, **_NO_WAIT))

    # 購読を張りっぱなしにするとIBKRの同時購読数上限を食い潰す
    ib.cancelMktData.assert_called_once_with(contract)


def test_get_current_price_falls_back_to_snapshot_when_streaming_has_no_data() -> None:
    contract = MagicMock(symbol="AAPL")
    ib = _make_ib(snapshot_ticker=_make_ticker(market_price=150.0, close=149.5))

    price = asyncio.run(get_current_price_async(ib, contract, **_NO_WAIT))

    assert price == 150.0


def test_get_current_price_falls_back_to_snapshot_when_streaming_raises() -> None:
    contract = MagicMock(symbol="AAPL")
    ib = _make_ib(snapshot_ticker=_make_ticker(market_price=150.0, close=149.5))
    ib.reqMktData = MagicMock(side_effect=RuntimeError("購読に失敗"))

    price = asyncio.run(get_current_price_async(ib, contract, **_NO_WAIT))

    assert price == 150.0


def test_get_current_price_falls_back_to_historical_close_when_realtime_unavailable() -> None:
    contract = MagicMock(symbol="AAPL")
    ib = _make_ib()
    bars = pd.DataFrame({"close": [148.0, 149.0, 152.5]})

    with patch(
        "data.market_data.get_historical_bars_async", new=AsyncMock(return_value=bars)
    ) as mock_get_bars:
        price = asyncio.run(
            get_current_price_async(ib, contract, streaming_timeout_seconds=0.0)
        )

    assert price == 152.5
    assert mock_get_bars.await_args.kwargs["what_to_show"] == "TRADES"


def test_get_current_price_uses_midpoint_bars_for_forex_fallback() -> None:
    # 為替には出来高を伴う取引が無いため、TRADESではバーが返らない
    contract = MagicMock(symbol="USD", secType="CASH")
    ib = _make_ib()

    with patch(
        "data.market_data.get_historical_bars_async",
        new=AsyncMock(return_value=pd.DataFrame({"close": [155.2]})),
    ) as mock_get_bars:
        price = asyncio.run(
            get_current_price_async(ib, contract, streaming_timeout_seconds=0.0)
        )

    assert price == 155.2
    assert mock_get_bars.await_args.kwargs["what_to_show"] == "MIDPOINT"


def test_get_current_price_skips_historical_fallback_when_disabled() -> None:
    contract = MagicMock(symbol="AAPL")
    ib = _make_ib()

    with patch(
        "data.market_data.get_historical_bars_async", new=AsyncMock()
    ) as mock_get_bars:
        price = asyncio.run(get_current_price_async(ib, contract, **_NO_WAIT))

    assert price is None
    mock_get_bars.assert_not_awaited()


def test_get_current_price_returns_none_when_every_source_fails() -> None:
    contract = MagicMock(symbol="AAPL")
    ib = _make_ib()

    with patch(
        "data.market_data.get_historical_bars_async",
        new=AsyncMock(return_value=pd.DataFrame()),
    ):
        price = asyncio.run(
            get_current_price_async(ib, contract, streaming_timeout_seconds=0.0)
        )

    assert price is None


# --- 現在価格: 個々のフィールドの扱い -------------------------------------------


def test_get_current_price_falls_back_to_close_when_market_price_is_nan() -> None:
    contract = MagicMock(symbol="AAPL")
    ib = _make_ib(snapshot_ticker=_make_ticker(market_price=float("nan"), close=149.5))

    price = asyncio.run(get_current_price_async(ib, contract, **_NO_WAIT))

    assert price == 149.5


def test_get_current_price_returns_none_when_market_price_and_close_are_both_nan() -> None:
    contract = MagicMock(symbol="AAPL")
    ib = _make_ib(snapshot_ticker=_make_nan_ticker())

    price = asyncio.run(get_current_price_async(ib, contract, **_NO_WAIT))

    assert price is None


def test_get_current_price_returns_none_when_market_price_and_close_are_both_none() -> None:
    contract = MagicMock(symbol="AAPL")
    ib = _make_ib(snapshot_ticker=_make_ticker(market_price=None, close=None))

    price = asyncio.run(get_current_price_async(ib, contract, **_NO_WAIT))

    assert price is None


@pytest.mark.parametrize("bad_price", [0.0, -1.0])
def test_get_current_price_rejects_non_positive_prices(bad_price) -> None:
    # IBKRはデータ未受信のフィールドを0で埋めてくることがあり、
    # そのまま採用すると建値0のポジションやゼロ除算を招く
    contract = MagicMock(symbol="AAPL")
    ib = _make_ib(snapshot_ticker=_make_ticker(market_price=bad_price, close=bad_price))

    price = asyncio.run(get_current_price_async(ib, contract, **_NO_WAIT))

    assert price is None


def test_get_current_price_returns_none_when_snapshot_returns_no_tickers() -> None:
    contract = MagicMock(symbol="AAPL")
    ib = _make_ib()
    ib.reqTickersAsync = AsyncMock(return_value=[])

    price = asyncio.run(get_current_price_async(ib, contract, **_NO_WAIT))

    assert price is None


def test_get_current_price_waits_for_streaming_tick_before_giving_up() -> None:
    """初回ポーリング時点で未受信でも、待機後に届いたティックを拾えること。"""
    contract = MagicMock(symbol="AAPL")
    ticker = MagicMock()
    ticker.close = float("nan")
    # 1回目はNaN、2回目に価格が届く
    ticker.marketPrice = MagicMock(side_effect=[float("nan"), 150.0])

    ib = _make_ib(streaming_ticker=ticker)

    with patch("data.market_data.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        price = asyncio.run(
            get_current_price_async(
                ib, contract, streaming_timeout_seconds=4.0, allow_historical_fallback=False,
            )
        )

    assert price == 150.0
    mock_sleep.assert_awaited_once()
    ib.reqTickersAsync.assert_not_awaited()


# --- 現在価格: 使い回しTickerによる価格の凍結 -----------------------------------
#
# ib_asyncのreqMktDataは同じコントラクトに対して同一のTickerを返し、
# cancelMktData後も前回の値を保持する。購読直後にそれを読むと、市場が動いても
# 永久に同じ価格を返し続ける（実測でボット側の決済判定が凍結した）。


def _make_timed_ticker(market_price, close, update_time):
    ticker = _make_ticker(market_price=market_price, close=close)
    ticker.time = update_time
    return ticker


def test_streaming_price_is_rejected_when_the_ticker_is_not_updated() -> None:
    """使い回しのTickerに新しいティックが来なければ、その値を採用しないこと。"""
    contract = MagicMock(symbol="AAPL")
    ticker = _make_timed_ticker(
        market_price=151.0, close=149.5, update_time=datetime(2026, 7, 31, 13, 50),
    )
    ib = _make_ib(streaming_ticker=ticker, snapshot_ticker=_make_ticker(150.0, 149.5))

    price = asyncio.run(get_current_price_async(ib, contract, **_NO_WAIT))

    # ストリーミングは見送り、下位の経路（スナップショット）の値になる。
    assert price == 150.0


def test_streaming_price_is_used_when_a_new_tick_arrives() -> None:
    """購読後に更新時刻が進んだら、その価格を採用すること。"""
    contract = MagicMock(symbol="AAPL")
    ticker = _make_timed_ticker(
        market_price=151.0, close=149.5, update_time=datetime(2026, 7, 31, 13, 50),
    )
    ib = _make_ib(streaming_ticker=ticker)

    async def _deliver_tick(_seconds: float) -> None:
        ticker.time = datetime(2026, 7, 31, 13, 53)

    with patch("data.market_data.asyncio.sleep", new=AsyncMock(side_effect=_deliver_tick)):
        price = asyncio.run(
            get_current_price_async(
                ib, contract, streaming_timeout_seconds=4.0, allow_historical_fallback=False,
            )
        )

    assert price == 151.0


def test_streaming_price_is_used_when_the_ticker_has_no_update_time() -> None:
    """更新時刻が読めない場合は鮮度を判定せず、従来どおり値を採用すること。"""
    contract = MagicMock(symbol="AAPL")
    # _make_tickerのtimeはMagicMockでdatetimeではない＝「判定できない」状態。
    ib = _make_ib(streaming_ticker=_make_ticker(market_price=151.0, close=149.5))

    assert asyncio.run(get_current_price_async(ib, contract, **_NO_WAIT)) == 151.0


# --- USD/JPY -------------------------------------------------------------------


def test_get_usd_jpy_rate_returns_market_price() -> None:
    ib = _make_ib(snapshot_ticker=_make_ticker(market_price=150.25, close=150.0))

    rate = asyncio.run(get_usd_jpy_rate_async(ib, **_NO_WAIT))

    assert rate == 150.25
    # Forex("USDJPY")コントラクトでreqTickersAsyncが呼ばれること
    called_contract = ib.reqTickersAsync.call_args.args[0]
    assert called_contract.symbol == "USD"
    assert called_contract.currency == "JPY"


def test_get_usd_jpy_rate_returns_none_when_unavailable() -> None:
    ib = _make_ib(snapshot_ticker=_make_ticker(market_price=None, close=None))

    rate = asyncio.run(get_usd_jpy_rate_async(ib, **_NO_WAIT))

    assert rate is None


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
        timeout=DEFAULT_HISTORICAL_TIMEOUT_SECONDS,
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
        timeout=DEFAULT_HISTORICAL_TIMEOUT_SECONDS,
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


# --- ペーシング制限 --------------------------------------------------------------


def test_historical_request_consumes_a_pacing_slot() -> None:
    """IBKRの「10分あたり60件」制限を超えると空のバー列が返り、違反を検知できない。

    そのため発行前に必ず枠を確保していること。
    """
    ib = MagicMock()
    ib.reqHistoricalDataAsync = AsyncMock(return_value=[])
    contract = MagicMock(symbol="AAPL")
    pacer = get_historical_pacer()

    assert pacer.used_in_window() == 0

    asyncio.run(get_historical_bars_async(ib, contract))

    assert pacer.used_in_window() == 1


def test_every_historical_caller_shares_one_pacer() -> None:
    # IBKRの制限はクライアント単位で課されるため、呼び出し箇所ごとに
    # 別のペーサーを持っては意味がない
    ib = MagicMock()
    ib.reqHistoricalDataAsync = AsyncMock(return_value=[])
    pacer = get_historical_pacer()

    async def run():
        await get_historical_bars_async(ib, MagicMock(symbol="AAPL"))
        await get_intraday_bars_async(ib, MagicMock(symbol="MSFT"))

    asyncio.run(run())

    assert pacer.used_in_window() == 2


# --- 価格の鮮度 -------------------------------------------------------------------
#
# フォールバック連鎖の下位（ティッカーのclose・ヒストリカル最終終値）は前営業日の
# 終値を返しうる。価格だけを返していると呼び出し側から区別できず、その値を参照価格に
# したブラケットが実勢からずれた値段で並ぶ。PriceQuote.is_stale がその唯一の手がかり。


def _eastern(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=US_EASTERN)


def test_streaming_price_is_marked_fresh() -> None:
    contract = MagicMock(symbol="AAPL")
    ib = _make_ib(streaming_ticker=_make_ticker(market_price=151.0, close=149.5))

    quote = asyncio.run(
        get_current_price_quote_async(ib, contract, streaming_timeout_seconds=0.0)
    )

    assert quote.price == 151.0
    assert quote.source == PRICE_SOURCE_STREAMING
    assert quote.is_stale is False


def test_ticker_close_fallback_is_marked_stale() -> None:
    """marketPrice()が取れずcloseへ落ちた時点で、値は前営業日の終値である。"""
    contract = MagicMock(symbol="AAPL")
    ib = _make_ib(snapshot_ticker=_make_ticker(market_price=float("nan"), close=149.5))

    quote = asyncio.run(
        get_current_price_quote_async(ib, contract, streaming_timeout_seconds=0.0)
    )

    assert quote.price == 149.5
    assert quote.source == PRICE_SOURCE_SNAPSHOT
    assert quote.is_stale is True


def test_historical_fallback_is_fresh_when_the_last_bar_is_today() -> None:
    contract = MagicMock(symbol="AAPL")
    ib = _make_ib()
    bars = pd.DataFrame({
        "date": [date(2026, 7, 30), date(2026, 7, 31)],
        "close": [149.0, 152.5],
    })

    with patch("data.market_data.get_historical_bars_async", new=AsyncMock(return_value=bars)):
        quote = asyncio.run(
            get_current_price_quote_async(
                ib, contract, streaming_timeout_seconds=0.0, now=_eastern(2026, 7, 31),
            )
        )

    assert quote.price == 152.5
    assert quote.source == PRICE_SOURCE_HISTORICAL
    assert quote.is_stale is False


def test_historical_fallback_is_stale_when_the_last_bar_is_an_earlier_day() -> None:
    """休場明けやギャップ後に前営業日の終値を掴んだケース。"""
    contract = MagicMock(symbol="AAPL")
    ib = _make_ib()
    bars = pd.DataFrame({
        "date": [date(2026, 7, 29), date(2026, 7, 30)],
        "close": [149.0, 152.5],
    })

    with patch("data.market_data.get_historical_bars_async", new=AsyncMock(return_value=bars)):
        quote = asyncio.run(
            get_current_price_quote_async(
                ib, contract, streaming_timeout_seconds=0.0, now=_eastern(2026, 7, 31),
            )
        )

    assert quote.price == 152.5
    assert quote.is_stale is True


def test_historical_fallback_is_stale_when_the_bar_date_is_unreadable() -> None:
    """日付が読めないときは古い側に倒すこと。

    新しい側に倒すと、鮮度検証が黙って素通しになったことに気付けない。
    """
    contract = MagicMock(symbol="AAPL")
    ib = _make_ib()

    with patch(
        "data.market_data.get_historical_bars_async",
        new=AsyncMock(return_value=pd.DataFrame({"close": [152.5]})),
    ):
        quote = asyncio.run(
            get_current_price_quote_async(
                ib, contract, streaming_timeout_seconds=0.0, now=_eastern(2026, 7, 31),
            )
        )

    assert quote.price == 152.5
    assert quote.is_stale is True


def test_bar_date_is_normalized_from_timestamps_and_strings() -> None:
    """ib_asyncはbarSize次第でdate/datetime/文字列のいずれも返す。"""
    contract = MagicMock(symbol="AAPL")
    ib = _make_ib()

    for value in (
        pd.Timestamp("2026-07-31"),
        datetime(2026, 7, 31, 16, 0),
        "2026-07-31",
    ):
        bars = pd.DataFrame({"date": [value], "close": [152.5]})
        with patch("data.market_data.get_historical_bars_async", new=AsyncMock(return_value=bars)):
            quote = asyncio.run(
                get_current_price_quote_async(
                    ib, contract, streaming_timeout_seconds=0.0, now=_eastern(2026, 7, 31),
                )
            )
        assert quote.is_stale is False, f"{value!r} を当日として解釈できていない"


def test_get_current_price_async_still_returns_a_plain_price() -> None:
    """既存の呼び出し側（決済判定・為替レート）の戻り値を変えていないこと。"""
    contract = MagicMock(symbol="AAPL")
    ib = _make_ib(streaming_ticker=_make_ticker(market_price=151.0, close=149.5))

    price = asyncio.run(get_current_price_async(ib, contract, streaming_timeout_seconds=0.0))

    assert price == 151.0


def test_quote_is_none_when_every_route_fails() -> None:
    contract = MagicMock(symbol="AAPL")
    ib = _make_ib()

    with patch(
        "data.market_data.get_historical_bars_async",
        new=AsyncMock(return_value=pd.DataFrame()),
    ):
        quote = asyncio.run(
            get_current_price_quote_async(ib, contract, streaming_timeout_seconds=0.0)
        )

    assert quote is None


def test_historical_bars_pass_the_timeout_through() -> None:
    """重い取得のためにタイムアウトを延ばせること。

    **タイムアウトは例外ではなく空のバー列として返る**（ib_asyncが要求を
    取り消し、IBKRが Error 162 を返す）。ペーシング違反と区別がつかないため、
    1年ぶんの5分足のような重い取得では呼び出し側が延ばせる必要がある
    （2026-08-13に42銘柄中41銘柄が既定の60秒で空を返した）。
    """
    ib = MagicMock()
    ib.reqHistoricalDataAsync = AsyncMock(return_value=[])

    asyncio.run(get_historical_bars_async(
        ib, MagicMock(symbol="AAPL"), duration="1 Y", bar_size="5 mins", timeout=300.0,
    ))

    assert ib.reqHistoricalDataAsync.await_args.kwargs["timeout"] == 300.0


def test_a_failed_subscription_cancel_is_reported(caplog) -> None:
    """購読の解除に失敗したらWARNINGで残すこと。

    **DEBUGだと `bot.log`（INFO以上）に1行も残らない。** 解除に失敗した購読は
    張りっぱなしになり、積み上がるとIBKRの同時購読数の上限を食い潰す（「6.4」）。
    そうなったときの症状は「価格が取れない銘柄が増える」で、原因がここだと
    分かる手掛かりが無くなる。
    """
    ib = MagicMock()
    ib.reqMktData = MagicMock(return_value=MagicMock(marketPrice=lambda: float("nan"), close=None))
    ib.cancelMktData = MagicMock(side_effect=RuntimeError("boom"))
    contract = MagicMock(symbol="AAPL")

    with caplog.at_level(logging.WARNING):
        asyncio.run(market_data._get_streaming_price_async(ib, contract, timeout_seconds=0.01))

    assert "解除できませんでした" in caplog.text
