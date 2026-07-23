"""保有ポジションの決済（Exit）判定ロジック。

利確(Take Profit)・損切り(Stop Loss)・トレーリングストップの3条件を評価し、
いずれかを満たした場合に決済シグナルを出す。
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

REASON_TAKE_PROFIT: str = "TAKE_PROFIT"
REASON_STOP_LOSS: str = "STOP_LOSS"
REASON_TRAILING_STOP: str = "TRAILING_STOP"
REASON_NONE: str = "NONE"
# main.py側の時刻ベースの強制決済（デイトレードポジションの大引け前フラット化）で使う理由コード。
# detect_exit_signal自体はこの理由を返さない（時刻を扱わない価格ベースの判定のため）。
REASON_EOD_FLATTEN: str = "EOD_FLATTEN"


@dataclass
class ExitSignalResult:
    symbol: str
    should_sell: bool
    reason: str
    entry_price: float
    current_price: float
    highest_price: float
    pnl_pct: float
    drawdown_from_high_pct: float


def detect_exit_signal(
    symbol: str,
    entry_price: float,
    current_price: float,
    highest_price_since_entry: float,
    take_profit_pct: float = 10.0,
    stop_loss_pct: float = 5.0,
    trailing_stop_pct: float = 3.0,
) -> ExitSignalResult:
    if entry_price <= 0:
        raise ValueError("entry_price は正の値である必要があります。")
    if current_price <= 0:
        raise ValueError("current_price は正の値である必要があります。")
    if highest_price_since_entry <= 0:
        raise ValueError("highest_price_since_entry は正の値である必要があります。")

    # 呼び出し側がエントリー後の高値更新を忘れていても、
    # entry_price/current_price を下回らないよう補正する。
    effective_highest: float = max(highest_price_since_entry, entry_price, current_price)

    pnl_pct: float = (current_price - entry_price) / entry_price * 100.0
    drawdown_from_high_pct: float = (
        (current_price - effective_highest) / effective_highest * 100.0
    )

    should_sell: bool = False
    reason: str = REASON_NONE

    if pnl_pct >= take_profit_pct:
        should_sell = True
        reason = REASON_TAKE_PROFIT
    elif pnl_pct <= -stop_loss_pct:
        should_sell = True
        reason = REASON_STOP_LOSS
    elif pnl_pct > 0 and drawdown_from_high_pct <= -trailing_stop_pct:
        should_sell = True
        reason = REASON_TRAILING_STOP

    logger.info(
        "[%s] entry=%.2f current=%.2f high=%.2f pnl=%.2f%% "
        "high比乖離=%.2f%% reason=%s",
        symbol, entry_price, current_price, effective_highest,
        pnl_pct, drawdown_from_high_pct, reason,
    )

    return ExitSignalResult(
        symbol=symbol,
        should_sell=should_sell,
        reason=reason,
        entry_price=entry_price,
        current_price=current_price,
        highest_price=effective_highest,
        pnl_pct=pnl_pct,
        drawdown_from_high_pct=drawdown_from_high_pct,
    )
