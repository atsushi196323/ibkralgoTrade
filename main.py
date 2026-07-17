"""エントリーポイント: 非同期イベントループの起動と全体のオーケストレーション。"""

import asyncio
import logging
from typing import List

from ib_insync import IB

from core.connection import IBKRConnection
from data.market_data import (
    get_current_price_async,
    get_historical_bars_async,
    qualify_stock_async,
)
from execution.order_manager import place_dry_run_order_async
from strategy.pullback import detect_pullback_signal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# 検証用ハイグロース銘柄ウォッチリスト（Rivian, Joby Aviation）
WATCHLIST: List[str] = ["RIVN", "JOBY"]


async def process_symbol_async(ib: IB, symbol: str) -> None:
    contract = await qualify_stock_async(ib, symbol)
    price = await get_current_price_async(ib, contract)
    logger.info("%s 現在価格: %s", symbol, price)

    df = await get_historical_bars_async(ib, contract)
    if df.empty:
        logger.warning("%s のヒストリカルデータが取得できなかったためスキップします。", symbol)
        return

    signal = detect_pullback_signal(symbol, df)
    if signal.should_buy:
        await place_dry_run_order_async(ib, contract, action="BUY", quantity=1)


async def main() -> None:
    connection = IBKRConnection()
    try:
        ib = await connection.connect_async()
        for symbol in WATCHLIST:
            try:
                await process_symbol_async(ib, symbol)
            except Exception:
                logger.exception("%s の処理中にエラーが発生しました。", symbol)
    finally:
        await connection.disconnect_async()


if __name__ == "__main__":
    asyncio.run(main())
