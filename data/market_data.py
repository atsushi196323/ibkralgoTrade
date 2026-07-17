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
