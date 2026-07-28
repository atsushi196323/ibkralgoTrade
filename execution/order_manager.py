"""注文の組み立て・発注（ドライラン仕様）。

検証が完了するまで実発注(placeOrder)は行わず、注文内容をログ出力するのみ。

新規建てはブラケット注文（親の成行買い＋子の損切り逆指値・利確指値）で組む。
子注文をブローカー側に置いておくことが重要で、ボットのプロセスが落ちていても、
TWSとの接続が切れていても、市場が動けば約定する。ポーリング間隔
（main.POLL_INTERVAL_SECONDS）による決済の遅れも受けない。
子注文どうしはOCA(One-Cancels-All)グループで結び、片方が約定したら
もう片方が自動的に取り消されるようにする（残さないと、決済済みの銘柄に
売り注文だけが残り、次に建てた瞬間に意図せず売られる）。
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from ib_insync import IB, LimitOrder, MarketOrder, Order, Stock, StopOrder

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

# --- ブラケット注文 -----------------------------------------------------------------

# 親注文と子注文を結ぶOCAグループ名の接頭辞。銘柄ごとに一意にする。
_OCA_GROUP_PREFIX: str = "BRACKET"
# OCAグループの取り消し方式。2 = 「約定した分だけ残りを減らす（オーバーフィル抑制あり）」。
# 1(即時取り消し)より約定の重複が起きにくいIBKRの推奨値。
_OCA_TYPE_REDUCE_WITH_OVERFILL_PROTECTION: int = 2


@dataclass
class BracketOrders:
    """新規建て1回分のブラケット注文一式。"""

    parent: Order
    stop_loss: Order
    take_profit: Order
    oca_group: str

    def as_list(self) -> List[Order]:
        """発注順に並べたリスト。親→子の順で送ること。"""
        return [self.parent, self.stop_loss, self.take_profit]


@dataclass
class DryRunBracketResult:
    symbol: str
    quantity: int
    stop_price: float
    take_profit_price: float
    oca_group: str
    dry_run: bool = True
    orders: Optional[BracketOrders] = field(default=None, repr=False)


def build_bracket_orders(
    symbol: str,
    quantity: int,
    stop_price: float,
    take_profit_price: float,
) -> BracketOrders:
    """成行買いの親注文と、損切り・利確の子注文を組み立てる。

    `transmit` の扱い: 親と最初の子は False にし、最後の子だけ True にする。
    IBKRは transmit=True を受け取った時点でその注文グループを市場へ送るため、
    親を先に送信してしまうと、子注文（損切り）が届く前に建玉ができる。
    その僅かな隙間が、まさに防ぎたい「損切りの無い裸のポジション」である。

    `parentId` はここでは設定できない。IBKRが親注文へ採番する orderId が
    placeOrder 実行後にしか決まらないため、実発注を有効化する際に
    親を place した直後、子へ `order.parentId = parent_trade.order.orderId`
    を代入してから子を place すること。
    """
    if quantity <= 0:
        raise ValueError("数量は正の整数である必要があります。")
    if stop_price <= 0 or take_profit_price <= 0:
        raise ValueError("stop_price, take_profit_price は正の値である必要があります。")
    if stop_price >= take_profit_price:
        raise ValueError(
            f"stop_price({stop_price}) は take_profit_price({take_profit_price}) より"
            "小さい必要があります。"
        )

    parent = MarketOrder(ACTION_BUY, quantity)
    parent.transmit = False

    oca_group = f"{_OCA_GROUP_PREFIX}_{symbol}_{id(parent)}"

    stop_loss = StopOrder(ACTION_SELL, quantity, stop_price)
    stop_loss.ocaGroup = oca_group
    stop_loss.ocaType = _OCA_TYPE_REDUCE_WITH_OVERFILL_PROTECTION
    stop_loss.transmit = False

    take_profit = LimitOrder(ACTION_SELL, quantity, take_profit_price)
    take_profit.ocaGroup = oca_group
    take_profit.ocaType = _OCA_TYPE_REDUCE_WITH_OVERFILL_PROTECTION
    # 最後の1件だけ transmit=True。ここで初めてグループ全体が市場へ送られる。
    take_profit.transmit = True

    return BracketOrders(
        parent=parent, stop_loss=stop_loss, take_profit=take_profit, oca_group=oca_group,
    )


async def place_dry_run_bracket_order_async(
    ib: IB,
    contract: Stock,
    quantity: int,
    stop_price: float,
    take_profit_price: float,
) -> DryRunBracketResult:
    """新規建てのブラケット注文をシミュレートする（placeOrderは呼ばない）。

    数量制限(MAX_POSITION_SIZE)は新規建てなので適用する。丸めた数量は
    子注文にもそのまま反映する必要がある（親より子が多いと、決済後に
    余った売り注文が残る）ため、丸めてから組み立てる。
    """
    if quantity <= 0:
        raise ValueError("数量は正の整数である必要があります。")

    if quantity > MAX_POSITION_SIZE:
        logger.warning(
            "要求数量 %s が最大ロット数制限 (%s) を超えたため、制限値に丸めます。",
            quantity, MAX_POSITION_SIZE,
        )
        quantity = MAX_POSITION_SIZE

    orders = build_bracket_orders(
        symbol=contract.symbol,
        quantity=quantity,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
    )

    logger.info(
        "[DRY-RUN] ブラケット注文シミュレーション: symbol=%s qty=%s "
        "親=成行買い 損切り=STP@%.2f 利確=LMT@%.2f oca=%s "
        "(placeOrderは呼び出していません)",
        contract.symbol, quantity, stop_price, take_profit_price, orders.oca_group,
    )
    logger.debug("[DRY-RUN] 構築されたOrderオブジェクト: %s", orders.as_list())

    return DryRunBracketResult(
        symbol=contract.symbol,
        quantity=quantity,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
        oca_group=orders.oca_group,
        orders=orders,
    )


async def cancel_dry_run_bracket_orders_async(ib: IB, symbol: str, oca_group: Optional[str]) -> None:
    """ブローカー側に残っている待機注文の取り消しをシミュレートする。

    トレーリングストップや大引け前の強制決済のように、ボット側の判断で
    成行決済した場合は、必ずこれを呼んで待機注文を消すこと。残したままだと
    建玉が無いのに売り注文だけが生き続け、次にその銘柄を建てた瞬間に
    意図しない決済が起きる（ドライラン中は実注文が無いためログのみ）。
    """
    if not oca_group:
        return

    logger.info(
        "[DRY-RUN] 待機注文の取り消しシミュレーション: symbol=%s oca=%s "
        "(cancelOrderは呼び出していません)",
        symbol, oca_group,
    )
