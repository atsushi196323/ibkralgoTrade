"""注文の組み立て・発注。

`ENABLE_REAL_ORDERS` が False（既定）の間は実発注(placeOrder)を行わず、
注文内容をログ出力するのみ。True にするとペーパー口座へ実際に発注する。

新規建てはブラケット注文（親の成行買い＋子の損切り逆指値・利確指値）で組む。
子注文をブローカー側に置いておくことが重要で、ボットのプロセスが落ちていても、
TWSとの接続が切れていても、市場が動けば約定する。ポーリング間隔
（main.POLL_INTERVAL_SECONDS）による決済の遅れも受けない。
子注文どうしはOCA(One-Cancels-All)グループで結び、片方が約定したら
もう片方が自動的に取り消されるようにする（残さないと、決済済みの銘柄に
売り注文だけが残り、次に建てた瞬間に意図せず売られる）。
"""

import asyncio
import logging
import math
from dataclasses import dataclass, field
from typing import List, Optional

from ib_insync import IB, LimitOrder, MarketOrder, Order, Stock, StopOrder

logger = logging.getLogger(__name__)

# 実際にブローカーへ注文を送るか。**既定は無効（ドライラン）。**
#
# 有効にしてよいのはペーパー口座だけである（`ensure_orders_are_paper_only` が
# 起動時に強制する）。ドライランのままでは、ブラケットのtransmit順序・OCAの
# 連動・実約定価格・手数料・注文拒否時の挙動が一切検証できない。これらは
# 「9. 開発時の禁止事項」が実発注の前提として挙げている項目そのものであり、
# ペーパー口座はそれを安全に潰すための環境である。
#
# 有効にしても株数(MAX_POSITION_SIZE)と金額(MAX_ORDER_NOTIONAL_USD)の
# クランプは外さない。
ENABLE_REAL_ORDERS: bool = False

# 実発注を許可するポート（ペーパー取引）。TWS=7497 / IB Gateway=4002。
# **許可リストで判定する。** 本番ポート(7496/4001)の拒否リストにすると、
# .env の打ち間違い（7495等）が素通りする。
PAPER_TRADING_PORTS = frozenset({7497, 4002})

# 発注後、約定または拒否が確定するまで待つ上限（秒）。
# 成行注文は取引時間中なら即座に約定するため、これを超えるのは異常
# （時間外・板が無い・IBKR側の滞留）であり、待ち続けても意味が無い。
ORDER_FILL_TIMEOUT_SECONDS: float = 60.0
_ORDER_STATUS_POLL_INTERVAL_SECONDS: float = 1.0

_STATUS_FILLED: str = "Filled"
# 約定せずに終わった状態。拒否(Inactive)もここに含まれる。
_TERMINAL_UNFILLED_STATUSES = frozenset({"Cancelled", "ApiCancelled", "Inactive"})


class OrderNotFilledError(RuntimeError):
    """注文が約定しないまま終了した（拒否・取消・タイムアウト）。

    **例外にしているのは、呼び出し側にローカル記録を作らせないためである。**
    資金不足などでIBKRが注文を拒否した場合に、約定した前提でポジションを
    記録すると、実体の無い建玉を追跡し、存在しない建玉へ決済のSELLを出す
    （「9. 開発時の禁止事項」）。決済側で投げた場合はポジションが開いたまま
    残るが、これも「売れていないのに閉じる」より安全な側である。
    """


def ensure_orders_are_paper_only(port: int) -> None:
    """実発注が有効なら、接続先がペーパーのポートであることを強制する。

    起動時に1度だけ呼ぶ。ドライランのまま本番ポートへつなぐのは（データ取得だけ
    なので）従来どおり許すが、実発注が有効な状態での本番ポートは止める。
    """
    if not ENABLE_REAL_ORDERS:
        return
    if port not in PAPER_TRADING_PORTS:
        raise RuntimeError(
            f"実発注(ENABLE_REAL_ORDERS=True)が有効ですが、接続先ポート {port} は"
            f"ペーパー取引のポート {sorted(PAPER_TRADING_PORTS)} ではありません。"
            "検証中の実発注はペーパー口座に限ります。"
        )

# ロジック検証完了まではハードコードで最大ロット数を制限する。
# 制限をかけるのは新規建て(BUY)のみ。決済(SELL)には適用しない理由は
# place_market_order_asyncのdocstringを参照。
#
# **2026-08-04に 10 -> 40 へ引き上げた。** 10株では株価$24.40未満の銘柄
# （運用者が監視を指示した RIVN $16.48 / JOBY $7.37 を含む）でクランプが掛かり、
# リスクベースのサイジングが効かなくなっていた。CLAUDE.md「検証時の初期資金」の
# 実測どおり、JOBYでは本来33株($243)が10株($74)に縮み、1トレードのリスクが
# 1.00% -> 0.30%、往復手数料の約定代金比が 0.29% -> 0.97% になる。
# **バックテストは MAX_POSITION_SIZE を適用しないので、クランプが掛かる限り
# 実運用の条件が検証の条件と一致しない。** 引き上げはその不一致を無くすもので、
# 安全弁を骨抜きにするための緩和ではない。
#
# 40という値は「資金$1,220でクランプが binding になる株価」を$6.10まで下げる
# ための数字である（数量 = floor(資金 × リスク% ÷ (株価 × 損切り%)) が40を
# 超えるのは株価 < $6.10 のとき）。JOBYの$7.37に対して2割の余裕がある。
# **資金額を変えたらこの前提も変わる**（下限株価 = 上限株価 ÷ MAX_POSITION_SIZE）。
#
# 絶対額の歯止めは下の MAX_ORDER_NOTIONAL_USD が持つ。40株でクランプが
# 効く価格帯($6.10未満)では建玉は$244以下にしかならないため、株数の上限を
# 上げても晒す金額が増えるわけではない。
MAX_POSITION_SIZE: int = 40

# 1注文あたりの名目金額の上限(USD)。MAX_POSITION_SIZEは「株数」の上限なので、
# 株価によって実際に晒す金額が2桁変わる（40株でも株価5ドルなら200ドル、
# 株価800ドルなら32,000ドル）。ドライラン検証中の安全弁としては、株数と金額の
# 両方に蓋をしておかないと意味が薄い。MAX_POSITION_SIZE と同様、
# 適用するのは新規建て(BUY)のみ。
#
# 資金$1,220での正常な建玉は株価によらず$244（= 資金 × リスク% ÷ 損切り%）
# なので、$5,000は20倍の余裕がある。**これを口座資金に合わせて絞りたく
# なるが、絞りすぎてはならない。** この上限は注文を拒否するのではなく数量を
# 切り下げるため、増資して正常な建玉が上限を超えた瞬間に、静かに小さい建玉を
# 作り続けることになる（＝いま MAX_POSITION_SIZE で起きていた問題と同じ）。
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
class OrderResult:
    symbol: str
    action: str
    quantity: int
    order_type: str
    dry_run: bool = True
    # 実発注時のみ埋まる。ドライラン中は約定が無いため呼び出し側が
    # 観測した価格で代用する。
    fill_price: Optional[float] = None
    commission: float = 0.0


def _fill_price_of(trade) -> Optional[float]:
    """約定価格を返す。取れなければNone。

    IBKRは未受信のフィールドをNaNや0で埋めてくるため、値として採用する前に
    「NaNでない、かつ正の数」を確認する（「6.4」）。
    """
    price = getattr(trade.orderStatus, "avgFillPrice", None)
    if price is None:
        return None
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    if math.isnan(price) or price <= 0:
        return None
    return price


def _commission_of(trade) -> float:
    """約定に紐づく手数料の合計を返す。

    部分約定では Fill が複数に分かれるため合算する。commissionReport は
    約定直後にはまだ届いていないことがあり、その場合は0として扱う
    （取れなかった手数料を推定で埋めると、損益が静かにずれる）。
    """
    total = 0.0
    for fill in getattr(trade, "fills", []) or []:
        report = getattr(fill, "commissionReport", None)
        commission = getattr(report, "commission", None)
        if commission is None:
            continue
        try:
            commission = float(commission)
        except (TypeError, ValueError):
            continue
        if math.isnan(commission):
            continue
        total += commission
    return total


async def _await_fill_async(ib: IB, trade, symbol: str, label: str) -> None:
    """約定が確定するまで待つ。約定しなければ OrderNotFilledError を投げる。"""
    waited = 0.0
    while not trade.isDone() and waited < ORDER_FILL_TIMEOUT_SECONDS:
        await asyncio.sleep(_ORDER_STATUS_POLL_INTERVAL_SECONDS)
        waited += _ORDER_STATUS_POLL_INTERVAL_SECONDS

    status = trade.orderStatus.status
    if status == _STATUS_FILLED:
        return

    if status in _TERMINAL_UNFILLED_STATUSES:
        raise OrderNotFilledError(
            f"[{symbol}] {label}が約定せずに終了しました: status={status}。"
            "資金不足などでIBKRが拒否した可能性があります。"
        )
    # タイムアウト。板が無い・時間外など、待ち続けても約定しない状況を想定する。
    # 宙に浮いたままにすると建玉の有無が分からなくなるため、取り消してから投げる。
    ib.cancelOrder(trade.order)
    raise OrderNotFilledError(
        f"[{symbol}] {label}が {ORDER_FILL_TIMEOUT_SECONDS:.0f} 秒以内に約定しませんでした"
        f"（status={status}）。注文を取り消しました。"
    )


async def place_market_order_async(
    ib: IB,
    contract: Stock,
    action: str,
    quantity: int,
    order_type: str = "MKT",
    reference_price: Optional[float] = None,
) -> OrderResult:
    """成行注文を出す（`ENABLE_REAL_ORDERS` が False ならシミュレートするだけ）。

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

    if not ENABLE_REAL_ORDERS:
        logger.info(
            "[DRY-RUN] 注文シミュレーション: symbol=%s action=%s qty=%s type=%s "
            "(placeOrderは呼び出していません)",
            contract.symbol, action, quantity, order_type,
        )
        logger.debug("[DRY-RUN] 構築されたOrderオブジェクト: %s", order)
        return OrderResult(
            symbol=contract.symbol,
            action=action,
            quantity=quantity,
            order_type=order_type,
        )

    trade = ib.placeOrder(contract, order)
    await _await_fill_async(ib, trade, contract.symbol, f"{action}の成行注文")

    fill_price = _fill_price_of(trade)
    commission = _commission_of(trade)
    logger.info(
        "[%s] %s %s株を約定しました: fill=%s commission=%.2f",
        contract.symbol, action, quantity,
        f"{fill_price:.2f}" if fill_price is not None else "不明", commission,
    )
    return OrderResult(
        symbol=contract.symbol,
        action=action,
        quantity=quantity,
        order_type=order_type,
        dry_run=False,
        fill_price=fill_price,
        commission=commission,
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
class BracketResult:
    symbol: str
    quantity: int
    stop_price: float
    take_profit_price: float
    oca_group: str
    dry_run: bool = True
    # 実発注時のみ埋まる親注文の約定価格。**ローカルの建値にはこちらを使う。**
    # 参照価格（発注時の現在値）で記録すると、実際の約定とずれた建値で
    # 損益・R倍率・トレーリングの基準を計算することになる。
    fill_price: Optional[float] = None
    commission: float = 0.0
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
    placeOrder 実行後にしか決まらないため、実発注時は親を place した直後に
    `place_bracket_order_async` が子へ代入する。
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


async def place_bracket_order_async(
    ib: IB,
    contract: Stock,
    quantity: int,
    stop_price: float,
    take_profit_price: float,
    reference_price: float,
) -> BracketResult:
    """新規建てのブラケット注文を出す（`ENABLE_REAL_ORDERS` が False ならシミュレート）。

    数量制限(MAX_POSITION_SIZE / MAX_ORDER_NOTIONAL_USD)は新規建てなので適用する。
    丸めた数量は子注文にもそのまま反映する必要がある（親より子が多いと、決済後に
    余った売り注文が残る）ため、丸めてから組み立てる。

    実発注時の送信順は「親 → 子(parentId代入) → 最後の子で transmit」。
    親が約定しなかった場合は **送信済みの子を取り消してから** 例外を投げる。
    残すと建玉が無いのに売り注文だけが生き、次にその銘柄を建てた瞬間に
    意図しない決済が起きる。
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

    if not ENABLE_REAL_ORDERS:
        logger.info(
            "[DRY-RUN] ブラケット注文シミュレーション: symbol=%s qty=%s "
            "親=成行買い 損切り=STP@%.2f 利確=LMT@%.2f oca=%s "
            "(placeOrderは呼び出していません)",
            contract.symbol, quantity, stop_price, take_profit_price, orders.oca_group,
        )
        logger.debug("[DRY-RUN] 構築されたOrderオブジェクト: %s", orders.as_list())
        return BracketResult(
            symbol=contract.symbol,
            quantity=quantity,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            oca_group=orders.oca_group,
            orders=orders,
        )

    parent_trade = ib.placeOrder(contract, orders.parent)
    child_trades = []
    try:
        for child in (orders.stop_loss, orders.take_profit):
            # parentId は親の orderId が採番された後にしか代入できない。
            child.parentId = parent_trade.order.orderId
            child_trades.append(ib.placeOrder(contract, child))
        await _await_fill_async(ib, parent_trade, contract.symbol, "新規建ての親注文")
    except Exception:
        for child_trade in child_trades:
            ib.cancelOrder(child_trade.order)
        if child_trades:
            logger.warning(
                "[%s] 親注文が約定しなかったため、送信済みの子注文(oca=%s)を取り消しました。",
                contract.symbol, orders.oca_group,
            )
        raise

    fill_price = _fill_price_of(parent_trade)
    commission = _commission_of(parent_trade)
    logger.info(
        "[%s] ブラケットの親注文が約定しました: qty=%s fill=%s commission=%.2f "
        "損切り=STP@%.2f 利確=LMT@%.2f oca=%s",
        contract.symbol, quantity,
        f"{fill_price:.2f}" if fill_price is not None else "不明", commission,
        stop_price, take_profit_price, orders.oca_group,
    )

    return BracketResult(
        symbol=contract.symbol,
        quantity=quantity,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
        oca_group=orders.oca_group,
        dry_run=False,
        fill_price=fill_price,
        commission=commission,
        orders=orders,
    )


@dataclass(frozen=True)
class RestingOrderFill:
    """ブローカー側の待機注文が約定していたことの記録。"""

    order_type: str
    fill_price: float
    commission: float


def find_filled_resting_exit(ib: IB, oca_group: Optional[str]) -> Optional[RestingOrderFill]:
    """OCAグループの待機注文が約定していればその内容を返す。

    実発注時の決済検知はこちらを使う。ドライラン中は観測した現在値から
    推定するしかないが（`strategy.exit_signal.detect_resting_order_exit`）、
    それは180秒ごとの1点しか見ないため、バーの中で逆指値に触れて戻した
    動きを取りこぼす。実際の約定が取れるならその推定は要らない。
    """
    if not oca_group:
        return None

    for trade in ib.trades():
        order = trade.order
        if getattr(order, "ocaGroup", None) != oca_group:
            continue
        if trade.orderStatus.status != _STATUS_FILLED:
            continue

        fill_price = _fill_price_of(trade)
        if fill_price is None:
            # 約定はしているのに値段が読めない。ここで推定を入れると損益が
            # 静かにずれるため、次のサイクルで読めるまで判断を持ち越す。
            logger.warning(
                "待機注文が約定していますが約定価格が読めません: oca=%s type=%s",
                oca_group, getattr(order, "orderType", None),
            )
            return None
        return RestingOrderFill(
            order_type=str(getattr(order, "orderType", "")),
            fill_price=fill_price,
            commission=_commission_of(trade),
        )

    return None


async def cancel_bracket_orders_async(ib: IB, symbol: str, oca_group: Optional[str]) -> None:
    """ブローカー側に残っている待機注文を取り消す。

    トレーリングストップや大引け前の強制決済のように、ボット側の判断で
    成行決済した場合は、必ずこれを呼んで待機注文を消すこと。残したままだと
    建玉が無いのに売り注文だけが生き続け、次にその銘柄を建てた瞬間に
    意図しない決済が起きる。
    """
    if not oca_group:
        return

    if not ENABLE_REAL_ORDERS:
        logger.info(
            "[DRY-RUN] 待機注文の取り消しシミュレーション: symbol=%s oca=%s "
            "(cancelOrderは呼び出していません)",
            symbol, oca_group,
        )
        return

    cancelled = 0
    for trade in ib.openTrades():
        if getattr(trade.order, "ocaGroup", None) != oca_group:
            continue
        ib.cancelOrder(trade.order)
        cancelled += 1

    logger.info("[%s] 待機注文を取り消しました: oca=%s 件数=%d", symbol, oca_group, cancelled)
