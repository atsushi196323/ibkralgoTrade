"""保有ポジションの状態管理。

銘柄ごとの建値・保有数量・エントリー後の高値を追跡し、
strategy/exit_signal.py の判定に必要な状態を提供する。

`highest_price`（トレーリングストップ判定用）はIBKR側では保持されない値のため
ローカルで追跡するが、保有銘柄・数量・建値そのものは `ib.reqPositionsAsync()` で
取得できるブローカー側の実ポジションと定期的に同期し、重複エントリーの防止と
決済判断の基準として利用する。
"""

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ib_insync import IB

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH: str = "logs/positions.json"

# ブローカー同期で取り込む対象を、このBotが扱う建玉だけに絞り込む条件。
# `ib.reqPositionsAsync()` は全口座・全アセットクラスの建玉を返すため、
# シンボル文字列だけで突き合わせると、AAPLのオプションやカナダ上場の同名株を
# 「AAPLの現物ポジション」として取り込んでしまう。
TRACKED_SEC_TYPE: str = "STK"
TRACKED_CURRENCY: str = "USD"

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
    # 建玉日時(ISO8601, UTC)。確定申告の取得年月日として使う。
    # ブローカー側で発見した未追跡ポジション(このBotが建てたものではない)は
    # 実際の建玉日が分からないためNoneのままとする。
    entry_date: Optional[str] = None


class PositionManager:
    """保有ポジションを管理する。

    `state_path` を指定すると、状態変化のたびにJSONへ保存し、生成時に
    復元する。ドライラン中はブローカー側に建玉が存在しないため、永続化が
    無いとプロセスを再起動した時点で保有中の想定ポジションと
    `highest_price`（トレーリングストップの基準）が失われ、決済も
    ジャーナル記録もされないまま消滅する。

    `state_path` を省略した場合は完全にインメモリで動作する（単体テスト用）。
    """

    def __init__(self, state_path: Optional[str] = None) -> None:
        self._positions: Dict[str, Position] = {}
        self.state_path = state_path
        if state_path:
            self._load()

    # --- 永続化 ---------------------------------------------------------------

    def _load(self) -> None:
        if not self.state_path or not os.path.exists(self.state_path):
            return

        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            positions = [Position(**item) for item in payload["positions"]]
        except (OSError, ValueError, KeyError, TypeError):
            # 状態ファイルが壊れていてもボットの起動自体は止めない。
            # 破棄せずリネームして残し、後から中身を確認できるようにする。
            logger.exception(
                "ポジション状態ファイルの読み込みに失敗しました: %s。"
                "空の状態で開始し、ファイルは .corrupt を付けて退避します。",
                self.state_path,
            )
            self._quarantine_state_file()
            return

        self._positions = {position.symbol: position for position in positions}
        if self._positions:
            logger.info(
                "ポジション状態を復元しました: %d件 %s",
                len(self._positions), list(self._positions),
            )

    def _quarantine_state_file(self) -> None:
        try:
            os.replace(self.state_path, f"{self.state_path}.corrupt")
        except OSError:
            logger.exception("破損した状態ファイルの退避に失敗しました: %s", self.state_path)

    def _save(self) -> None:
        if not self.state_path:
            return

        directory = os.path.dirname(self.state_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        payload = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "positions": [asdict(position) for position in self._positions.values()],
        }

        # 書き込み中にプロセスが落ちても状態ファイルを壊さないよう、
        # 一時ファイルへ書いてから置換する。
        temp_path = f"{self.state_path}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.state_path)
        except OSError:
            # 保存に失敗してもインメモリの状態は正しいため、取引は継続する。
            logger.exception("ポジション状態の保存に失敗しました: %s", self.state_path)

    # --- 参照 -----------------------------------------------------------------

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
            entry_date=datetime.now(timezone.utc).isoformat(),
        )
        self._positions[symbol] = position
        logger.info(
            "[%s] ポジションを新規建てしました: entry=%.2f qty=%s strategy=%s",
            symbol, entry_price, quantity, strategy_type,
        )
        self._save()
        return position

    def update_highest_price(self, symbol: str, current_price: float) -> Position:
        position = self._positions.get(symbol)
        if position is None:
            raise KeyError(f"{symbol} のポジションが存在しません。")

        # 高値が更新された時だけ保存する。この関数はサイクルごとに
        # 全保有銘柄分呼ばれるため、毎回書き込むと無駄が大きい。
        if current_price > position.highest_price:
            position.highest_price = current_price
            self._save()
        return position

    def close_position(self, symbol: str) -> Position:
        position = self._positions.pop(symbol, None)
        if position is None:
            raise KeyError(f"{symbol} のポジションが存在しません。")

        logger.info("[%s] ポジションを決済しました。", symbol)
        self._save()
        return position

    def _is_tracked_position(self, broker_position: object) -> bool:
        """ブローカー側の建玉が、このBotの管理対象かを判定する。

        `ib.reqPositionsAsync()` は全口座・全アセットクラスの建玉を返すため、
        シンボル文字列だけで突き合わせると別物を取り込む危険がある。
        """
        if broker_position.position == 0:
            return False

        contract = broker_position.contract
        symbol = getattr(contract, "symbol", "?")

        sec_type = getattr(contract, "secType", None)
        if sec_type != TRACKED_SEC_TYPE:
            # 例: AAPLのコールオプションもcontract.symbolは"AAPL"になる
            logger.info(
                "[%s] 現物株(%s)ではないブローカー建玉のため、追跡対象外とします: secType=%s",
                symbol, TRACKED_SEC_TYPE, sec_type,
            )
            return False

        currency = getattr(contract, "currency", None)
        if currency != TRACKED_CURRENCY:
            # 例: トロント上場の同名株はcontract.symbolが衝突しうる
            logger.info(
                "[%s] %s建てではないブローカー建玉のため、追跡対象外とします: currency=%s",
                symbol, TRACKED_CURRENCY, currency,
            )
            return False

        if broker_position.position < 0:
            # 本Botは押し目買いのロングのみを扱う。ショートを取り込むと
            # 決済時にSELLの数量が負になり、発注処理が例外で落ちる。
            logger.warning(
                "[%s] ショートポジション(%s株)を検出しましたが、本Botはロング専用のため"
                "追跡対象外とします。手動で建てた建玉の場合は自分で管理すること。",
                symbol, broker_position.position,
            )
            return False

        return True

    async def sync_with_broker_async(self, ib: IB) -> None:
        """ブローカー側の実ポジションと同期する。

        `ib.reqPositionsAsync()` で取得できる保有銘柄・数量・建値(avgCost)を
        正として反映する。ローカルで未追跡だったブローカー側ポジションは
        新規に取り込み（＝重複エントリー防止の基準となる）、既に追跡中の
        銘柄はブローカー側の数量・建値で上書きする（部分約定等でavgCostが
        変動するケースに対応）。

        ブローカー側に存在しないローカルポジション（このBotがドライラン注文で
        建てたが未約定の想定ポジションなど）はそのまま保持し、削除しない。

        取り込むのは米国株の現物ロングのみ（TRACKED_SEC_TYPE / TRACKED_CURRENCY）。
        それ以外はこのBotの管理対象外として無視する。
        """
        broker_positions = await ib.reqPositionsAsync()
        changed = False

        for broker_position in broker_positions:
            if not self._is_tracked_position(broker_position):
                continue

            changed = True
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

        if changed:
            self._save()
