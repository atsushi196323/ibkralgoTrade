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
from datetime import date, datetime, timezone
from typing import Dict, List, Optional

from ib_insync import IB

from core.market_hours import US_EASTERN

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


def _current_trading_day(now: Optional[datetime] = None) -> date:
    """取引日の区切りを米国東部時間で判定する。

    クールダウン（当日中の再エントリー禁止）の「当日」は、市場時間
    (core.market_hours)や日次サーキットブレーカー(TradeJournal.compute_daily_pnl)と
    同じ区切りでなければならない。ローカル時間で判定すると、日本時間の
    深夜0時をまたいだ瞬間に同じ取引日の中でクールダウンが解除される。
    """
    reference = now or datetime.now(US_EASTERN)
    return reference.astimezone(US_EASTERN).date()


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
    # ブローカー側に置いた待機注文の値段。ボットのプロセスが落ちていても
    # 効き続ける唯一の防御なので、再起動後も引き継げるよう永続化する。
    # ブローカー同期で取り込んだ未追跡ポジションは待機注文を持たないため0.0。
    stop_price: float = 0.0
    take_profit_price: float = 0.0
    # 待機注文を結んでいるOCAグループ名。ボット側の判断で成行決済した際に、
    # 残っている待機注文を取り消すために使う。
    oca_group: Optional[str] = None
    # 新規建て時に支払った手数料(USD)。決済時に往復ぶんをまとめて損益へ
    # 織り込むため、建玉と一緒に持ち越す（決済時には決済側の手数料しか
    # 分からない）。ドライラン中と、ブローカー同期で取り込んだ建玉は0.0。
    entry_commission: float = 0.0
    # entry_price が実約定価格か（＝手数料を含まない純粋な約定値か）。
    # **IBKRの avgCost は手数料込みである**（2026-08-05の実測: 実約定 67.44 に
    # 対し avgCost 67.77333635 = 67.44 + 手数料1.00/3株）。ブローカー同期が
    # これで entry_price を上書きすると、entry_commission と二重に手数料を
    # 引くことになる。このフラグが立っている建玉は同期で建値を上書きしない。
    entry_price_is_fill: bool = False


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
        # 銘柄 -> 最後に決済した取引日(米国東部時間, ISO形式の日付文字列)。
        # 「決済した当日は同じ銘柄へ再エントリーしない」判定に使う。
        self._last_exit_days: Dict[str, str] = {}
        # 当日の新規建て発注回数と、それを数えている取引日。
        # 想定外のシグナル連発（データ異常やロジックのバグ）で1日に何十回も
        # 建てにいくのを止めるための上限判定に使う。同時保有数の上限
        # (main.MAX_CONCURRENT_POSITIONS)は「同時に何銘柄持つか」の制限であって、
        # 建てては決済を繰り返す回数は抑えないため、別に数える必要がある。
        self._entry_order_day: Optional[str] = None
        self._entry_orders_today: int = 0
        # 直近の sync_with_broker_async でブローカー側にも実在が確認できた銘柄。
        # 永続化しない（再起動後は同期し直すまで「未確認」に戻すのが安全側）。
        self._broker_confirmed_symbols: set = set()
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
            # クールダウンと発注回数は後から追加した項目なので、無い状態ファイルも読める。
            last_exit_days = dict(payload.get("last_exit_days") or {})
            entry_order_day = payload.get("entry_order_day")
            entry_orders_today = int(payload.get("entry_orders_today") or 0)
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
        # 当日分だけ残す。過去日のクールダウンはもう効かないため、
        # 保持し続けると状態ファイルが際限なく膨らむ。
        today = _current_trading_day().isoformat()
        self._last_exit_days = {
            symbol: day for symbol, day in last_exit_days.items() if day == today
        }
        # 発注回数も同様に当日分のみ引き継ぐ。引き継がないと、再起動するたびに
        # カウンタが0に戻り、上限がいくらでも回避できてしまう。
        if entry_order_day == today:
            self._entry_order_day = entry_order_day
            self._entry_orders_today = entry_orders_today
            logger.info("本日の新規建て発注回数を復元しました: %d回", entry_orders_today)
        if self._positions:
            logger.info(
                "ポジション状態を復元しました: %d件 %s",
                len(self._positions), list(self._positions),
            )
        if self._last_exit_days:
            logger.info(
                "本日決済済みでクールダウン中の銘柄を復元しました: %s",
                list(self._last_exit_days),
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
            "last_exit_days": dict(self._last_exit_days),
            "entry_order_day": self._entry_order_day,
            "entry_orders_today": self._entry_orders_today,
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

    def is_in_cooldown(self, symbol: str, now: Optional[datetime] = None) -> bool:
        """その銘柄が「本日決済済み」でクールダウン中か判定する。

        決済した当日は同じ銘柄へ再エントリーしない。これが無いと、日足の
        乖離率はその日の間ほぼ変わらないため、損切りした直後のサイクルで
        同じ買いシグナルが再び成立し、「買う→損切り→また買う」を
        1日に何度も繰り返して損失を刻む。
        """
        last_exit_day = self._last_exit_days.get(symbol)
        if last_exit_day is None:
            return False
        return last_exit_day == _current_trading_day(now).isoformat()

    def count_entry_orders_today(self, now: Optional[datetime] = None) -> int:
        """当日（米国東部時間）にこのBotが出した新規建ての回数を返す。

        ブローカー同期(sync_with_broker_async)で取り込んだ既存ポジションは
        このBotが発注したものではないため数えない。
        """
        today = _current_trading_day(now).isoformat()
        if self._entry_order_day != today:
            return 0
        return self._entry_orders_today

    def open_position(
        self, symbol: str, entry_price: float, quantity: int, risk_per_share: float = 0.0,
        strategy_type: str = STRATEGY_TYPE_UNKNOWN,
        stop_price: float = 0.0, take_profit_price: float = 0.0,
        oca_group: Optional[str] = None,
        entry_commission: float = 0.0,
        entry_price_is_fill: bool = False,
        now: Optional[datetime] = None,
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
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            oca_group=oca_group,
            entry_commission=entry_commission,
            entry_price_is_fill=entry_price_is_fill,
        )
        self._positions[symbol] = position

        logger.info(
            "[%s] ポジションを新規建てしました: entry=%.2f qty=%s strategy=%s "
            "待機注文 STP=%.2f LMT=%.2f",
            symbol, entry_price, quantity, strategy_type, stop_price, take_profit_price,
        )
        self._save()
        return position

    def record_entry_order_attempt(self, now: Optional[datetime] = None) -> int:
        """新規建ての発注を1回数える。**発注する前に呼ぶこと。**

        数えるのは「約定した回数」ではなく「発注した回数」である。実発注では
        資金不足などで注文が拒否されうるが、拒否された注文も発注として
        ブローカーへ届いている。約定だけを数えると、全件拒否される状況で
        毎サイクル発注し続けても上限に掛からず、この上限が止めようとしている
        「有限回で打ち切る」が成立しない。
        """
        today = _current_trading_day(now).isoformat()
        if self._entry_order_day != today:
            self._entry_order_day = today
            self._entry_orders_today = 0
        self._entry_orders_today += 1
        self._save()
        return self._entry_orders_today

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

    def close_position(self, symbol: str, now: Optional[datetime] = None) -> Position:
        position = self._positions.pop(symbol, None)
        if position is None:
            raise KeyError(f"{symbol} のポジションが存在しません。")

        # 決済と同時にクールダウンを開始する（当日中の再エントリー禁止）。
        self._last_exit_days[symbol] = _current_trading_day(now).isoformat()

        logger.info(
            "[%s] ポジションを決済しました。本日中は再エントリーしません。", symbol,
        )
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
        ただし「ブローカーが実際に持っている銘柄」は記録しておく
        （`is_confirmed_by_broker`）。実発注時に、持っていない建玉へ成行の
        SELLを出すと売り建てになるため、呼び出し側がそれを防げるようにする。

        取り込むのは米国株の現物ロングのみ（TRACKED_SEC_TYPE / TRACKED_CURRENCY）。
        それ以外はこのBotの管理対象外として無視する。
        """
        broker_positions = await ib.reqPositionsAsync()
        changed = False
        confirmed: set = set()

        for broker_position in broker_positions:
            if not self._is_tracked_position(broker_position):
                continue

            changed = True
            symbol = broker_position.contract.symbol
            confirmed.add(symbol)
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
                existing.quantity = quantity
                if existing.entry_price_is_fill:
                    # 実約定価格を持っている建玉は上書きしない。avgCost は
                    # 手数料込みなので、上書きすると entry_commission と合わせて
                    # 手数料を二重に引くことになる（`Position.entry_price_is_fill`）。
                    continue
                existing.entry_price = entry_price
                existing.highest_price = max(existing.highest_price, entry_price)

        self._broker_confirmed_symbols = confirmed

        if changed:
            self._save()

    def is_confirmed_by_broker(self, symbol: str) -> bool:
        """直近の同期でブローカー側にも実在が確認できた建玉か。

        ドライランで建てた想定ポジションはブローカーに存在しないため常にFalse。
        同期をまだ一度も行っていない場合もFalseになる（実在すると分かるまでは
        「無い」側に倒す。実発注で持っていない株へSELLを出すと売り建てになる）。
        """
        return symbol in self._broker_confirmed_symbols
