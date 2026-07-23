"""時価総額・PERによる割安株スクリーニング。

IBKRスキャナーで時価総額により母集団を絞り込み、各候補のPERを取得して
さらにフィルタする2段階構成。先に時価総額で母集団を絞ってからPERを取得
するのは、reqFundamentalDataAsyncを候補全銘柄へ個別リクエストする
コスト（レート制限・往復時間）を抑えるため。

このスクリーニングは過去時点のPER（point-in-timeデータ）をIBKR経由で
遡って取得できないため、backtest/ パッケージでは検証できない。ライブの
ドライラン運用で結果を確認しながら閾値を調整すること。
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import List

from ib_insync import IB

from data.fundamentals import get_pe_ratio_async, run_market_cap_scan_async

logger = logging.getLogger(__name__)


@dataclass
class ScreenerConfig:
    market_cap_above: float = 2_000_000_000.0
    market_cap_below: float = 200_000_000_000.0
    max_pe_ratio: float = 15.0
    scan_code: str = "MOST_ACTIVE"
    number_of_rows: int = 50
    # 最大50銘柄分のreqFundamentalDataAsyncを間隔調整なしで連続発行すると
    # IBKR側のペーシング制限に抵触しうるため、リクエスト間に挟む待機秒数。
    pe_request_interval_seconds: float = 1.0


async def screen_value_stocks_async(ib: IB, config: ScreenerConfig) -> List[str]:
    if config.max_pe_ratio <= 0:
        raise ValueError("max_pe_ratio は正の値である必要があります。")

    candidates = await run_market_cap_scan_async(
        ib,
        market_cap_above=config.market_cap_above,
        market_cap_below=config.market_cap_below,
        scan_code=config.scan_code,
        number_of_rows=config.number_of_rows,
    )

    selected: List[str] = []
    for index, contract in enumerate(candidates):
        if index > 0 and config.pe_request_interval_seconds > 0:
            await asyncio.sleep(config.pe_request_interval_seconds)

        pe_ratio = await get_pe_ratio_async(ib, contract)
        # PERが取得できない・赤字（PER<=0）の銘柄は「割安」の判定対象外とする
        if pe_ratio is not None and 0 < pe_ratio <= config.max_pe_ratio:
            selected.append(contract.symbol)

    logger.info(
        "割安株スクリーニング完了: 候補=%d件 -> PER<=%.1fで%d件選定 %s",
        len(candidates), config.max_pe_ratio, len(selected), selected,
    )
    return selected
