"""リスクベースのポジションサイジング。

固定ロット数ではなく、1トレードあたり口座資金の何%をリスクに晒すか
（risk_per_trade_pct）を基準に、損切りライン（stop_loss_pct）までの
値幅から逆算して発注数量を決定する。

    risk_amount     = account_equity * risk_per_trade_pct / 100
    per_share_risk  = entry_price * stop_loss_pct / 100
    quantity        = floor(risk_amount / per_share_risk)

ここで算出した数量は、ロジック検証が完了するまでの安全弁である
execution.order_manager.MAX_POSITION_SIZE によって別途ハードクランプされる
（本関数はリスク計算のみを担い、最終上限はorder_manager側の責務とする）。
"""

import logging
import math

logger = logging.getLogger(__name__)


def calculate_position_size(
    account_equity: float,
    entry_price: float,
    stop_loss_pct: float,
    risk_per_trade_pct: float = 1.0,
) -> int:
    if account_equity <= 0:
        raise ValueError("account_equity は正の値である必要があります。")
    if entry_price <= 0:
        raise ValueError("entry_price は正の値である必要があります。")
    if stop_loss_pct <= 0:
        raise ValueError("stop_loss_pct は正の値である必要があります。")
    if risk_per_trade_pct <= 0:
        raise ValueError("risk_per_trade_pct は正の値である必要があります。")

    risk_amount: float = account_equity * (risk_per_trade_pct / 100.0)
    per_share_risk: float = entry_price * (stop_loss_pct / 100.0)

    quantity: int = max(math.floor(risk_amount / per_share_risk), 0)

    logger.info(
        "ポジションサイズ計算: equity=%.2f risk_pct=%.2f risk_amount=%.2f "
        "entry=%.2f stop_loss_pct=%.2f per_share_risk=%.4f -> qty=%s",
        account_equity, risk_per_trade_pct, risk_amount,
        entry_price, stop_loss_pct, per_share_risk, quantity,
    )

    return quantity
