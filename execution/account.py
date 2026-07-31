"""IBKRアカウントの資金状況取得。"""

import logging
from typing import Optional

from ib_insync import IB

logger = logging.getLogger(__name__)

# 決済済み現金（受渡しが完了し、次の建玉に自由に使える現金）のタグ。
# NetLiquidationは未受渡しの売却代金も含んだ評価額なので、キャッシュ口座では
# 「いま実際に支払える額」と一致しない。Good Faith Violationは未受渡しの代金で
# 買った建玉を受渡し前に売ると発生するため、新規建ての資金判定にはこちらを使う。
SETTLED_CASH_TAG: str = "SettledCash"


async def get_account_equity_async(ib: IB, tag: str = "NetLiquidation") -> float:
    summary = await ib.accountSummaryAsync()
    for item in summary:
        if item.tag == tag:
            equity = float(item.value)
            logger.info("口座資金(%s)を取得しました: %.2f", tag, equity)
            return equity

    raise ValueError(f"アカウントサマリーに {tag} が見つかりませんでした。")


async def get_settled_cash_async(ib: IB) -> Optional[float]:
    """決済済み現金を返す。取得できなければNoneを返す。

    例外ではなくNoneを返すのは、呼び出し側にGFV回避の判断を委ねるため。
    「取得できなかった」と「0ドルしかない」は意味が違い、前者を0として
    扱うと資金があるのに永久に建てられなくなる。

    タグが見つからない場合は、サマリーに実在したタグ名をログに出す。
    口座種別によってタグの有無が変わりうるため、接続先で何が返っているかを
    ログだけで切り分けられるようにしておく（この関数が黙ってNoneを返すと、
    新規エントリーが止まった理由に気付けない）。
    """
    summary = await ib.accountSummaryAsync()
    for item in summary:
        if item.tag == SETTLED_CASH_TAG:
            try:
                settled_cash = float(item.value)
            except (TypeError, ValueError):
                logger.warning(
                    "%s の値が数値として解釈できませんでした: %r",
                    SETTLED_CASH_TAG, item.value,
                )
                return None
            logger.info("決済済み現金(%s)を取得しました: %.2f", SETTLED_CASH_TAG, settled_cash)
            return settled_cash

    logger.warning(
        "アカウントサマリーに %s が見つかりませんでした。存在したタグ: %s",
        SETTLED_CASH_TAG, sorted({item.tag for item in summary}),
    )
    return None
