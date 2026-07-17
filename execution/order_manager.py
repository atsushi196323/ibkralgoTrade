"""注文の組み立て・発注（ドライラン仕様）。

検証が完了するまで実発注(placeOrder)は行わず、注文内容をログ出力するのみ。
"""

import logging
from dataclasses import dataclass

from ib_insync import IB, MarketOrder, Stock

logger = logging.getLogger(__name__)

# ロジック検証完了まではハードコードで最大ロット数を制限する
MAX_POSITION_SIZE: int = 10


@dataclass
class DryRunOrderResult:
    symbol: str
    action: str
    quantity: int
    order_type: str
    dry_run: bool = True


async def place_dry_run_order_async(
    ib: IB,
    contract: Stock,
    action: str,
    quantity: int,
    order_type: str = "MKT",
) -> DryRunOrderResult:
    if quantity <= 0:
        raise ValueError("数量は正の整数である必要があります。")

    if quantity > MAX_POSITION_SIZE:
        logger.warning(
            "要求数量 %s が最大ロット数制限 (%s) を超えたため、制限値に丸めます。",
            quantity, MAX_POSITION_SIZE,
        )
        quantity = MAX_POSITION_SIZE

    order = MarketOrder(action, quantity)

    logger.info(
        "[DRY-RUN] 注文シミュレーション: symbol=%s action=%s qty=%s type=%s "
        "(placeOrderは呼び出していません)",
        contract.symbol, action, quantity, order_type,
    )
    logger.debug("[DRY-RUN] 構築されたOrderオブジェクト: %s", order)

    return DryRunOrderResult(
        symbol=contract.symbol,
        action=action,
        quantity=quantity,
        order_type=order_type,
    )
