"""株価データ・オプションチェーン情報の取得と前処理。"""

import asyncio
import logging
import math
from typing import Optional

import pandas as pd
from ib_insync import IB, Contract, Forex, Stock
from ib_insync import util as ib_util

from core.pacing import RequestPacer

logger = logging.getLogger(__name__)

# ヒストリカルデータのリクエストは、このモジュールを経由するものすべてが
# 同じペーサーを共有する。IBKRの制限はクライアント単位で課されるため、
# 呼び出し箇所(メインループ・スクリーナー・バックテスト)ごとに分けては意味がない。
_historical_pacer = RequestPacer()


def get_historical_pacer() -> RequestPacer:
    """ヒストリカルデータ用の共有ペーサーを返す（テスト・監視用）。"""
    return _historical_pacer

# 現在価格を取得する際、ストリーミング購読(reqMktData)を待つ最大秒数と
# その間のポーリング間隔。リアルタイムデータが配信されていれば通常1秒未満で
# ティックが届くため、この上限まで待つのは「データが来ない」ケースのみ。
STREAMING_TIMEOUT_SECONDS: float = 4.0
STREAMING_POLL_INTERVAL_SECONDS: float = 0.5

# ヒストリカルバーで現在価格を代替する際のリクエストパラメータ。
# 直近の営業日の終値が取れれば十分なので短い期間で足りる。
_FALLBACK_BAR_DURATION: str = "5 D"
_FALLBACK_BAR_SIZE: str = "1 day"

# 為替(Forex)コントラクトには出来高を伴う「取引」が無いため、
# reqHistoricalDataのwhatToShowにTRADESを指定するとバーが返らない。
_FOREX_SEC_TYPE: str = "CASH"
_FOREX_WHAT_TO_SHOW: str = "MIDPOINT"


async def qualify_stock_async(
    ib: IB,
    symbol: str,
    exchange: str = "SMART",
    currency: str = "USD",
) -> Stock:
    contract = Stock(symbol, exchange, currency)
    qualified = await ib.qualifyContractsAsync(contract)
    if not qualified:
        raise ValueError(f"シンボル {symbol} のコントラクト特定に失敗しました。")
    logger.info("コントラクトを特定しました: %s", qualified[0])
    return qualified[0]


def _to_usable_price(value: object) -> Optional[float]:
    """ティッカーの各フィールドを、価格として採用できる場合のみfloatで返す。

    IBKRは未取得のフィールドをNaNで埋めてくるため、NaN・None・非正数は
    すべて「欠測」として扱う。数値型以外（モックオブジェクト等）も弾く。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    price = float(value)
    if math.isnan(price) or price <= 0:
        return None
    return price


def _extract_ticker_price(ticker: object) -> Optional[float]:
    """Tickerから現在価格として使える値を取り出す（marketPrice優先、無ければclose）。

    ib_insyncのmarketPrice()は「bid/askの範囲内にあるlast、無ければ仲値」を返すが、
    どちらも未取得ならNaNになるため、その場合は前日終値(close)にフォールバックする。
    """
    try:
        price = _to_usable_price(ticker.marketPrice())
    except Exception:  # marketPrice()自体が失敗するケース（データ未受信時等）
        price = None
    if price is not None:
        return price
    return _to_usable_price(getattr(ticker, "close", None))


async def _get_streaming_price_async(
    ib: IB, contract: Contract, timeout_seconds: float,
) -> Optional[float]:
    """reqMktData(snapshot=False)のストリーミング購読から現在価格を取得する。

    スナップショット要求(reqTickersAsync)と違い、遅延データ
    (reqMarketDataType(3))でも配信されるため、リアルタイムデータの購読契約が
    無いペーパー口座ではこちらが本命の経路になる。
    """
    try:
        ticker = ib.reqMktData(contract, "", False, False)
    except Exception:
        logger.exception("%s のストリーミング購読の開始に失敗しました。", contract.symbol)
        return None

    try:
        # ティックが既に届いていることもあるため、待機の前に1度確認する。
        # timeout_seconds=0 なら待機せず即時判定のみ行う。
        polls = max(int(timeout_seconds / STREAMING_POLL_INTERVAL_SECONDS), 0) + 1
        for attempt in range(polls):
            if attempt > 0:
                await asyncio.sleep(STREAMING_POLL_INTERVAL_SECONDS)
            price = _extract_ticker_price(ticker)
            if price is not None:
                return price
        return None
    finally:
        try:
            ib.cancelMktData(contract)
        except Exception:
            logger.debug("%s のストリーミング購読の解除に失敗しました。", contract.symbol)


async def _get_snapshot_price_async(ib: IB, contract: Contract) -> Optional[float]:
    """reqTickersAsync（内部でsnapshot=Trueを使う）から現在価格を取得する。

    IBKRはスナップショット要求に遅延データを適用しないため、リアルタイム
    データの購読契約が無い口座では失敗する。契約がある場合は1往復で済み
    ストリーミングより速いため、フォールバック連鎖には残してある。
    """
    try:
        tickers = await ib.reqTickersAsync(contract)
    except Exception:
        logger.exception("%s のスナップショット取得に失敗しました。", contract.symbol)
        return None

    if not tickers:
        return None
    return _extract_ticker_price(tickers[0])


async def _get_last_close_price_async(ib: IB, contract: Contract) -> Optional[float]:
    """ヒストリカルバーの最終終値を現在価格の代わりに使う。

    マーケットデータの購読権限が全く無い口座でも、ヒストリカルデータだけは
    取得できることが多いため、最後の砦として用意している。ただし値は
    リアルタイムではない（直近の営業日の終値）点に注意。
    """
    what_to_show = (
        _FOREX_WHAT_TO_SHOW if getattr(contract, "secType", None) == _FOREX_SEC_TYPE else "TRADES"
    )
    try:
        df = await get_historical_bars_async(
            ib, contract,
            duration=_FALLBACK_BAR_DURATION,
            bar_size=_FALLBACK_BAR_SIZE,
            what_to_show=what_to_show,
        )
    except Exception:
        logger.exception("%s のヒストリカルバーによる価格取得に失敗しました。", contract.symbol)
        return None

    if df.empty or "close" not in df.columns:
        return None
    return _to_usable_price(df["close"].iloc[-1])


async def get_current_price_async(
    ib: IB,
    contract: Contract,
    streaming_timeout_seconds: float = STREAMING_TIMEOUT_SECONDS,
    allow_historical_fallback: bool = True,
) -> Optional[float]:
    """現在価格を取得する。取得経路を順に試し、最初に成功したものを返す。

    口座のマーケットデータ購読状況によって使える経路が変わるため、
    単一の経路に依存せずフォールバックさせる:

        1. ストリーミング (reqMktData)   … 遅延データでも配信される
        2. スナップショット (reqTickers) … リアルタイム購読契約が要る
        3. ヒストリカル終値              … 購読権限が無くても取れることが多い

    Args:
        streaming_timeout_seconds: 1のストリーミングでティックを待つ秒数。
            0を指定すると待機せず即時判定のみ行う（テスト用）。
        allow_historical_fallback: 3を試すか。ヒストリカルデータのリクエストは
            IBKRのペーシング制限（10分あたり60件）を消費するため、多数の銘柄を
            高頻度でポーリングする場合はFalseにして呼び出し側で制御すること。

    Returns:
        取得できた価格。すべての経路が失敗した場合はNone。
    """
    price = await _get_streaming_price_async(ib, contract, streaming_timeout_seconds)
    if price is not None:
        logger.info("%s の現在価格: %s (ストリーミング)", contract.symbol, price)
        return price

    price = await _get_snapshot_price_async(ib, contract)
    if price is not None:
        logger.info("%s の現在価格: %s (スナップショット)", contract.symbol, price)
        return price

    if allow_historical_fallback:
        price = await _get_last_close_price_async(ib, contract)
        if price is not None:
            logger.info("%s の現在価格: %s (ヒストリカル最終終値)", contract.symbol, price)
            return price

    logger.warning(
        "%s の現在価格をどの経路でも取得できませんでした"
        "（ストリーミング/スナップショット/ヒストリカル）。",
        contract.symbol,
    )
    return None


async def get_usd_jpy_rate_async(
    ib: IB,
    streaming_timeout_seconds: float = STREAMING_TIMEOUT_SECONDS,
    allow_historical_fallback: bool = True,
) -> Optional[float]:
    """USD/JPYの現在レートを取得する。

    決済損益を確定申告向けに円換算する際にtrade_journal.TradeRecord.usd_jpy_rate
    として記録するためのもの。ForexコントラクトはStockと異なり曖昧さがないため
    qualifyContractsAsyncは不要。
    """
    return await get_current_price_async(
        ib, Forex("USDJPY"),
        streaming_timeout_seconds=streaming_timeout_seconds,
        allow_historical_fallback=allow_historical_fallback,
    )


async def get_historical_bars_async(
    ib: IB,
    contract: Contract,
    duration: str = "60 D",
    bar_size: str = "1 day",
    what_to_show: str = "TRADES",
) -> pd.DataFrame:
    # IBKRの「10分あたり60件」制限を超えると、例外ではなく空のバー列が返るため
    # 呼び出し側から違反を検知できない。発行前に必ず枠を確保する。
    await _historical_pacer.acquire()

    bars = await ib.reqHistoricalDataAsync(
        contract,
        endDateTime="",
        durationStr=duration,
        barSizeSetting=bar_size,
        whatToShow=what_to_show,
        useRTH=True,
        formatDate=1,
    )
    df: pd.DataFrame = ib_util.df(bars)
    if df is None:
        return pd.DataFrame()
    logger.info("%s のヒストリカルバーを%d件取得しました。", contract.symbol, len(df))
    return df


# IBKRのreqHistoricalDataが受け付ける日中足のbarSizeSetting一覧。
# デイトレードのシグナル判定はこのいずれかの短期足を使うこと（日足は不可）。
INTRADAY_BAR_SIZES: frozenset = frozenset(
    {
        "1 secs", "5 secs", "10 secs", "15 secs", "30 secs",
        "1 min", "2 mins", "3 mins", "5 mins", "10 mins", "15 mins", "20 mins", "30 mins",
        "1 hour", "2 hours", "3 hours", "4 hours", "8 hours",
    }
)


async def get_intraday_bars_async(
    ib: IB,
    contract: Stock,
    duration: str = "2 D",
    bar_size: str = "5 mins",
    what_to_show: str = "TRADES",
) -> pd.DataFrame:
    """デイトレード向けの短期足（分足・秒足・時間足）を取得する。

    IBKRは日中足に対してリクエストできる期間(duration)に制限があるため、
    swing向けの `get_historical_bars_async` のデフォルト(60 D/1 day)とは
    別関数として分離している。
    """
    if bar_size not in INTRADAY_BAR_SIZES:
        raise ValueError(
            f"bar_size は日中足である必要があります（指定値: {bar_size}）。"
            f"利用可能な値: {sorted(INTRADAY_BAR_SIZES)}"
        )

    return await get_historical_bars_async(
        ib, contract, duration=duration, bar_size=bar_size, what_to_show=what_to_show,
    )
