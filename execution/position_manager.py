"""保有ポジションの状態管理。

銘柄ごとの建値・保有数量・エントリー後の高値を追跡し、
strategy/exit_signal.py の判定に必要な状態を提供する。

`highest_price`（トレーリングストップ判定用）はIBKR側では保持されない値のため
ローカルで追跡するが、保有銘柄・数量・建値そのものは `ib.reqPositionsAsync()` で
取得できるブローカー側の実ポジションと定期的に同期し、重複エントリーの防止と
決済判断の基準として利用する。
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from ib_insync import IB

logger = logging.getLogger(__name__)

# エントリーのトリガーとなったシグナルの種別。将来的に決済ルールを
# 種別ごとに出し分けたり、デイトレード分のみ引け前に強制決済したりする
# 判定に使う。
STRATEGY_TYPE_SWING: str = "swing"
STRATEGY_TYPE_DAY: str = "day"
# ブローカー側で発見した未追跡ポジションなど、どちらのシグナルで
# 建てられたか不明な場合に使う。
STRATEGY_TYPE_UNKNOWN: str = "unknown"


@dataclass
class Position:
    symbol: str
    entry_price: float
    quantity: int
    highest_price: float
    # 1株あたりの想定リスク額（entry_price * stop_loss_pct/100）。決済時のR倍率算出に使う。
    # ブローカー側で発見した未追跡ポジション（このBotが建てたものではない）は
    # 想定リスクが不明なため0.0のままとし、R倍率は算出しない。
    risk_per_share: float = 0.0
    # エントリーのトリガーとなったシグナル種別("swing"/"day"/"unknown")
    strategy_type: str = STRATEGY_TYPE_UNKNOWN


class PositionManager:
    def __init__(self) -> None:
        self._positions: Dict[str, Position] = {}

    def has_position(self, symbol: str) -> bool:
        return symbol in self._positions

    def get_position(self, symbol: str) -> Optional[Position]:
        return self._positions.get(symbol)

    def count_open_positions(self) -> int:
        return len(self._positions)

    def open_symbols(self) -> List[str]:
        """現在保有中の全銘柄を返す。

        日次のウォッチリスト入れ替え（スクリーニング結果での置き換え）で
        銘柄がリストから外れても、保有中ポジションの決済判定（利確・損切り・
        トレーリングストップ等）を継続するために、呼び出し側で監視対象に
        含めるべき銘柄を把握する用途に使う。
        """
        return list(self._positions.keys())

    def open_position(
        self, symbol: str, entry_price: float, quantity: int, risk_per_share: float = 0.0,
        strategy_type: str = STRATEGY_TYPE_UNKNOWN,
    ) -> Position:
        if entry_price <= 0:
            raise ValueError("entry_price は正の値である必要があります。")
        if quantity <= 0:
            raise ValueError("quantity は正の整数である必要があります。")
        if risk_per_share < 0:
            raise ValueError("risk_per_share は0以上である必要があります。")
        if not strategy_type:
            raise ValueError("strategy_type は空でない文字列である必要があります。")
        if symbol in self._positions:
            raise ValueError(f"{symbol} のポジションは既に存在します。")

        position = Position(
            symbol=symbol,
            entry_price=entry_price,
            quantity=quantity,
            highest_price=entry_price,
            risk_per_share=risk_per_share,
            strategy_type=strategy_type,
        )
        self._positions[symbol] = position
        logger.info(
            "[%s] ポジションを新規建てしました: entry=%.2f qty=%s strategy=%s",
            symbol, entry_price, quantity, strategy_type,
        )
        return position

    def update_highest_price(self, symbol: str, current_price: float) -> Position:
        position = self._positions.get(symbol)
        if position is None:
            raise KeyError(f"{symbol} のポジションが存在しません。")

        if current_price > position.highest_price:
            position.highest_price = current_price
        return position

    def close_position(self, symbol: str) -> Position:
        position = self._positions.pop(symbol, None)
        if position is None:
            raise KeyError(f"{symbol} のポジションが存在しません。")

        logger.info("[%s] ポジションを決済しました。", symbol)
        return position

    async def sync_with_broker_async(self, ib: IB) -> None:
        """ブローカー側の実ポジションと同期する。

        `ib.reqPositionsAsync()` で取得できる保有銘柄・数量・建値(avgCost)を
        正として反映する。ローカルで未追跡だったブローカー側ポジションは
        新規に取り込み（＝重複エントリー防止の基準となる）、既に追跡中の
        銘柄はブローカー側の数量・建値で上書きする（部分約定等でavgCostが
        変動するケースに対応）。

        ブローカー側に存在しないローカルポジション（このBotがドライラン注文で
        建てたが未約定の想定ポジションなど）はそのまま保持し、削除しない。
        """
        broker_positions = await ib.reqPositionsAsync()

        for broker_position in broker_positions:
            if broker_position.position == 0:
                continue

            symbol = broker_position.contract.symbol
            entry_price = float(broker_position.avgCost)
            quantity = int(broker_position.position)

            existing = self._positions.get(symbol)
            if existing is None:
                logger.info(
                    "[%s] ブローカー側の既存ポジションを検出し、追跡を開始します: entry=%.2f qty=%s",
                    symbol, entry_price, quantity,
                )
                self._positions[symbol] = Position(
                    symbol=symbol,
                    entry_price=entry_price,
                    quantity=quantity,
                    highest_price=entry_price,
                )
            else:
                existing.entry_price = entry_price
                existing.quantity = quantity
                existing.highest_price = max(existing.highest_price, entry_price)
