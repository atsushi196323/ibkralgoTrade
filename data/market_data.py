"""株価データ・オプションチェーン情報の取得と前処理。"""

import logging
from typing import Optional

import pandas as pd
from ib_insync import IB, Stock
from ib_insync import util as ib_util

logger = logging.getLogger(__name__)


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


async def get_current_price_async(ib: IB, contract: Stock) -> Optional[float]:
    [ticker] = await ib.reqTickersAsync(contract)
    price = ticker.marketPrice()
    if price is None or price != price:  # NaNチェック
        price = ticker.close
    if price is None or price != price:  # フォールバック先(close)もNaN/Noneの場合、取得失敗として扱う
        logger.warning("%s の現在価格を取得できませんでした（marketPrice/closeともに欠測）。", contract.symbol)
        return None
    logger.info("%s の現在価格: %s", contract.symbol, price)
    return price


async def get_historical_bars_async(
    ib: IB,
    contract: Stock,
    duration: str = "60 D",
    bar_size: str = "1 day",
    what_to_show: str = "TRADES",
) -> pd.DataFrame:
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
