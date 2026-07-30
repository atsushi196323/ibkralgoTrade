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
import math
from dataclasses import dataclass, field
from typing import List, Optional

from ib_insync import IB, LimitOrder, MarketOrder, Order, Stock, StopOrder

logger = logging.getLogger(__name__)

# ロジック検証完了まではハードコードで最大ロット数を制限する。
# 制限をかけるのは新規建て(BUY)のみ。決済(SELL)には適用しない理由は
# place_dry_run_order_asyncのdocstringを参照。
MAX_POSITION_SIZE: int = 10

# 1注文あたりの名目金額の上限(USD)。MAX_POSITION_SIZEは「株数」の上限なので、
# 株価によって実際に晒す金額が2桁変わる（10株でも株価5ドルなら50ドル、
# 株価800ドルなら8000ドル）。ドライラン検証中の安全弁としては、株数と金額の
# 両方に蓋をしておかないと意味が薄い。MAX_POSITION_SIZE と同様、
# 適用するのは新規建て(BUY)のみ。
MAX_ORDER_NOTIONAL_USD: float = 5_000.0

# 待機注文(損切りの逆指値・利確の指値)が参照価格から離れることを許す上限(%)。
# 正常系ではこの値には届かない（値段は main 側で参照価格の -5%/+10% 等として
# 算出されるため）。これはあくまで層の境界に置く不変条件で、パーセントと
# 小数の取り違え・決済パラメータの設定ミス・参照価格の桁違いといった
# 「値段が壊れた注文」がブローカーへ出て行くのを、注文組み立ての時点で止める。
# なお参照価格そのものが古い（ギャップ後に前日終値を掴む等）ケースは、
# 参照価格を基準に測っている以上ここでは検出できない。
MAX_ORDER_PRICE_DEVIATION_PCT: float = 25.0

ACTION_BUY: str = "BUY"
ACTION_SELL: str = "SELL"
_VALID_ACTIONS = frozenset({ACTION_BUY, ACTION_SELL})


def clamp_buy_quantity(symbol: str, quantity: int, reference_price: Optional[float] = None) -> int:
    """新規建て(BUY)の数量に、株数と名目金額の両方の上限を適用する。

    `reference_price` を省略した場合は株数の上限のみ適用する。
    金額上限によって0株になる場合（1株の値段が MAX_ORDER_NOTIONAL_USD を
    超える銘柄）は0を返す。発注できないことを呼び出し側が判断できるよう、
    ここでは例外にしない。
    """
    if quantity > MAX_POSITION_SIZE:
        logger.warning(
            "[%s] 要求数量 %s が最大ロット数制限 (%s) を超えたため、制限値に丸めます。",
            symbol, quantity, MAX_POSITION_SIZE,
        )
        quantity = MAX_POSITION_SIZE

    if reference_price is None:
        return quantity

    if reference_price <= 0:
        raise ValueError(f"reference_price は正の値である必要があります: {reference_price}")

    max_quantity_by_notional = math.floor(MAX_ORDER_NOTIONAL_USD / reference_price)
    if quantity > max_quantity_by_notional:
        logger.warning(
            "[%s] 数量 %s (@%.2f = %.2f USD) が1注文あたりの金額上限 (%.2f USD) を"
            "超えたため、%s株に丸めます。",
            symbol, quantity, reference_price, quantity * reference_price,
            MAX_ORDER_NOTIONAL_USD, max_quantity_by_notional,
        )
        quantity = max_quantity_by_notional

    return quantity


def validate_resting_order_prices(
    symbol: str, reference_price: float, stop_price: float, take_profit_price: float,
) -> None:
    """待機注文の値段が参照価格から乖離しすぎていないか検証する。

    超えていた場合は ValueError を投げる。呼び出し元（main の銘柄ループ）は
    銘柄単位の例外を握り潰して次の銘柄へ進むため、壊れた値段の注文が出るより
    その銘柄のエントリーを見送る方が安全である。
    """
    if reference_price <= 0:
        raise ValueError(f"reference_price は正の値である必要があります: {reference_price}")

    for label, price in (("stop_price", stop_price), ("take_profit_price", take_profit_price)):
        deviation_pct = abs(price - reference_price) / reference_price * 100.0
        if deviation_pct > MAX_ORDER_PRICE_DEVIATION_PCT:
            raise ValueError(
                f"[{symbol}] {label}({price:.2f}) が参照価格({reference_price:.2f})から"
                f"{deviation_pct:.1f}%乖離しており、許容範囲"
                f"({MAX_ORDER_PRICE_DEVIATION_PCT:.1f}%)を超えています。"
            )


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
    reference_price: Optional[float] = None,
) -> DryRunOrderResult:
    """注文をシミュレートする（placeOrderは呼ばない）。

    数量制限（MAX_POSITION_SIZE / MAX_ORDER_NOTIONAL_USD）は新規建て(BUY)にのみ
    適用する。決済(SELL)に適用してはならない: 呼び出し側は決済成立を前提に
    ローカルのポジションを閉じるため、SELLの数量を丸めるとブローカー側に
    建玉が残ったままローカルの追跡だけが消え、損切りもトレーリングストップも
    効かない未追跡ポジションが生まれる。ブローカー同期(sync_with_broker_async)で
    取り込んだMAX_POSITION_SIZEより大きい既存ポジションで実際に起きる。
    そのため `reference_price`（金額上限の判定に使う）もSELLでは参照しない。
    """
    if action not in _VALID_ACTIONS:
        raise ValueError(f"action は {sorted(_VALID_ACTIONS)} のいずれかである必要があります: {action}")
    if quantity <= 0:
        raise ValueError("数量は正の整数である必要があります。")

    if action == ACTION_BUY:
        quantity = clamp_buy_quantity(contract.symbol, quantity, reference_price)
        if quantity <= 0:
            raise ValueError(
                f"[{contract.symbol}] 1株の金額が1注文あたりの上限"
                f"({MAX_ORDER_NOTIONAL_USD:.2f} USD)を超えるため発注できません。"
            )

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
    reference_price: float,
) -> BracketOrders:
    """成行買いの親注文と、損切り・利確の子注文を組み立てる。

    `reference_price` は待機注文の値段の妥当性検証（参照価格からの乖離が
    MAX_ORDER_PRICE_DEVIATION_PCT 以内か）に使う。省略可能にしていないのは、
    省略できると呼び出し側が検証を素通りさせられてしまい、注文の値段を
    最後に検査する層が無くなるため。

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
    validate_resting_order_prices(symbol, reference_price, stop_price, take_profit_price)

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
    reference_price: float,
) -> DryRunBracketResult:
    """新規建てのブラケット注文をシミュレートする（placeOrderは呼ばない）。

    数量制限(MAX_POSITION_SIZE / MAX_ORDER_NOTIONAL_USD)は新規建てなので適用する。
    丸めた数量は子注文にもそのまま反映する必要がある（親より子が多いと、決済後に
    余った売り注文が残る）ため、丸めてから組み立てる。
    """
    if quantity <= 0:
        raise ValueError("数量は正の整数である必要があります。")

    quantity = clamp_buy_quantity(contract.symbol, quantity, reference_price)
    if quantity <= 0:
        raise ValueError(
            f"[{contract.symbol}] 1株の金額が1注文あたりの上限"
            f"({MAX_ORDER_NOTIONAL_USD:.2f} USD)を超えるため発注できません。"
        )

    orders = build_bracket_orders(
        symbol=contract.symbol,
        quantity=quantity,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
        reference_price=reference_price,
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
