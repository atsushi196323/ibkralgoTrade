"""注文の組み立て・発注（ドライラン仕様）。

検証が完了するまで実発注(placeOrder)は行わず、注文内容をログ出力するのみ。
"""

import logging
from dataclasses import dataclass

from ib_insync import IB, MarketOrder, Stock

logger = logging.getLogger(__name__)

# ロジック検証完了まではハードコードで最大ロット数を制限する。
# 制限をかけるのは新規建て(BUY)のみ。決済(SELL)には適用しない理由は
# place_dry_run_order_asyncのdocstringを参照。
MAX_POSITION_SIZE: int = 10

ACTION_BUY: str = "BUY"
ACTION_SELL: str = "SELL"
_VALID_ACTIONS = frozenset({ACTION_BUY, ACTION_SELL})


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
    """注文をシミュレートする（placeOrderは呼ばない）。

    MAX_POSITION_SIZEによる数量制限は新規建て(BUY)にのみ適用する。
    決済(SELL)に適用してはならない: 呼び出し側は決済成立を前提に
    ローカルのポジションを閉じるため、SELLの数量を丸めるとブローカー側に
    建玉が残ったままローカルの追跡だけが消え、損切りもトレーリングストップも
    効かない未追跡ポジションが生まれる。ブローカー同期(sync_with_broker_async)で
    取り込んだMAX_POSITION_SIZEより大きい既存ポジションで実際に起きる。
    """
    if action not in _VALID_ACTIONS:
        raise ValueError(f"action は {sorted(_VALID_ACTIONS)} のいずれかである必要があります: {action}")
    if quantity <= 0:
        raise ValueError("数量は正の整数である必要があります。")

    if action == ACTION_BUY and quantity > MAX_POSITION_SIZE:
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
