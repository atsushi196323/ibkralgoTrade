"""IBKRアカウントの資金状況取得。"""

import logging

from ib_insync import IB

logger = logging.getLogger(__name__)


async def get_account_equity_async(ib: IB, tag: str = "NetLiquidation") -> float:
    summary = await ib.accountSummaryAsync()
    for item in summary:
        if item.tag == tag:
            equity = float(item.value)
            logger.info("口座資金(%s)を取得しました: %.2f", tag, equity)
            return equity

    raise ValueError(f"アカウントサマリーに {tag} が見つかりませんでした。")
