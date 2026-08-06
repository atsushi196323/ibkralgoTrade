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
from typing import Dict, List, Optional

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
ENABLE_REAL_ORDERS: bool = True

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

# 待機注文がブローカー側で生きていると見なせる状態。
# PreSubmitted は親の約定待ちで保留されている子注文（whyHeld='child'）の状態で、
# 親が約定すれば Submitted へ移る。どちらも「置かれている」side である。
_LIVE_ORDER_STATUSES = frozenset({"PreSubmitted", "Submitted", _STATUS_FILLED})

# 米国株の呼値。**丸めずに送ると注文が拒否される。**
# 2026-08-05のペーパー検証で、損切り 66.50×0.95 = 63.1750 がそのまま送られ、
# IBKRが `Warning 110（指定価格がこのコントラクトの呼値と一致しません）` を返して
# 逆指値だけが不成立になった。利確(73.15)はたまたま2桁だったため通り、
# **損切りの無い建玉が残った**。ib_insyncは110を警告としてしか通知せず、
# 子注文には状態変化すら来ないため、丸めを欠くと静かに防御だけが消える。
#
# 1ドル未満の銘柄は呼値が $0.0001 になるが、監視できる株価帯の下限
# （main.resolve_min_tradeable_price）は資金$1,220でも$6.10であり該当しない。
MIN_PRICE_INCREMENT_USD: float = 0.01

# 待機注文の有効期間。**DAYにしてはならない。**
# スイングは建玉を持ち越すため、DAYだと引けで待機注文が失効し、
# 翌日の寄り付きまで損切りの無い時間ができる（ブラケットをブローカー側に
# 置いている意味そのものが消える）。明示しないとIB Gateway側の
# Order Preset が DAY を上書き適用する（Error 10349 として現れる）。
_RESTING_ORDER_TIF: str = "GTC"

# Order Preset による上書きを警告済みの銘柄。
# 突き合わせは毎サイクル(300秒)走るため、素朴に出すと1建玉あたり1日78行になり、
# 「読むべき1行」が埋もれる（「3. 実行環境と設定」のログ方針）。銘柄ごとに
# 初回だけ出し、上書きが解消した時点で落として次の建玉でまた出せるようにする。
_TIF_DOWNGRADE_WARNED: set = set()

# 親の約定後、子注文が生きた状態へ移るまで待つ上限（秒）。
# 送信直後は PendingSubmit で、ブローカーが受理して初めて PreSubmitted/Submitted
# になる。短すぎると正常な注文を不成立と誤判定して建玉を決済してしまう。
_CHILD_ORDER_LIVE_TIMEOUT_SECONDS: float = 10.0

# ブラケットの子として置く決済注文の種類（損切りの逆指値・利確の指値）。
# 待機注文の取り消しと約定検知は、この2種類の売り注文だけを対象にする。
_RESTING_EXIT_ORDER_TYPES = frozenset({"STP", "LMT"})

# 取り消し要求がまだブローカー側で終わっていない状態。
# `_LIVE_ORDER_STATUSES` と別に持つのは、あちらが Filled を「置かれている側」
# として含んでいるため。取り消しの完了待ちで Filled を待ち続けると、
# 待機注文が約定して建玉が消えた場面でタイムアウトまで止まる。
_PENDING_CANCEL_STATUSES = frozenset(
    {"ApiPending", "PendingSubmit", "PreSubmitted", "Submitted", "PendingCancel"}
)

# 取り消しがブローカー側で確定するまで待つ上限（秒）。
# 実測では cancelOrder から Cancelled まで約0.6秒かかっており、
# 10秒はその十数倍にあたる。
_ORDER_CANCEL_TIMEOUT_SECONDS: float = 10.0


class RestingOrdersNotLiveError(RuntimeError):
    """親は約定したが、ブラケットの子注文がブローカー側で生きていない。

    `OrderNotFilledError` と同じく、**呼び出し側にローカル記録を作らせない**
    ために例外にしている。こちらは建玉ができた後に判明するため、
    投げる前に建玉を成行で決済してから投げる（`_ensure_children_are_live_async`）。
    """


class RestingOrderCancelTimeoutError(RuntimeError):
    """待機注文の取り消しがブローカー側で確定しなかった。

    **投げた側は成行決済へ進んではならない。** 建玉と同数の売り注文が
    生きたまま成行の売りを重ねると、IBKRは超過分を空売りと見なして拒否する
    （2026-08-05に `Error 201` として実測。この口座は評価額が証拠金取引の
    最低額 200,000 JPY を下回るため即座に弾かれた）。

    取り消せていないということは待機注文がまだ建玉を守っているということでも
    あるので、次のサイクルへ持ち越すのが安全側である。
    """


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


def round_to_tick(price: float) -> float:
    """待機注文の値段を呼値（MIN_PRICE_INCREMENT_USD）へ切り上げる。

    切り上げに倒すのは、損切りが「予定より広くならない」側だからである。
    切り下げると逆指値が1呼値ぶん遠くなり、1トレードのリスクが
    設計値(1%)をわずかに超える。利確側は切り上げでも「予定より早く利確しない」
    側なので、両方を同じ向きに倒せる。

    除算の誤差を先に丸めてから切り上げる。63.175 / 0.01 は二進浮動小数点では
    6317.499999... になり、そのまま ceil すると 63.18 ではなく 63.18 を経ずに
    1呼値ずれる（63.175 のように呼値のちょうど半分の値は実際に出る）。
    """
    ticks = math.ceil(round(price / MIN_PRICE_INCREMENT_USD, 6))
    return round(ticks * MIN_PRICE_INCREMENT_USD, 2)


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

    # 呼値へ丸めてから組み立てる。丸めないとIBKRが Warning 110 を返して
    # その注文だけが不成立になる（MIN_PRICE_INCREMENT_USD の説明を参照）。
    stop_price = round_to_tick(stop_price)
    take_profit_price = round_to_tick(take_profit_price)

    parent = MarketOrder(ACTION_BUY, quantity)
    parent.transmit = False

    oca_group = f"{_OCA_GROUP_PREFIX}_{symbol}_{id(parent)}"

    stop_loss = StopOrder(ACTION_SELL, quantity, stop_price)
    stop_loss.ocaGroup = oca_group
    stop_loss.ocaType = _OCA_TYPE_REDUCE_WITH_OVERFILL_PROTECTION
    stop_loss.tif = _RESTING_ORDER_TIF
    stop_loss.transmit = False

    take_profit = LimitOrder(ACTION_SELL, quantity, take_profit_price)
    take_profit.ocaGroup = oca_group
    take_profit.ocaType = _OCA_TYPE_REDUCE_WITH_OVERFILL_PROTECTION
    take_profit.tif = _RESTING_ORDER_TIF
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
    # 実約定価格へ移すための値幅（比率）は、呼値へ丸める前の値段から取る。
    stop_ratio = stop_price / reference_price
    take_profit_ratio = take_profit_price / reference_price
    # 呼値へ丸めた後の値段を以降の記録に使う。引数の値のまま返すと、
    # positions.json に「ブローカーに置いていない値段」が残る。
    stop_price = orders.stop_loss.auxPrice
    take_profit_price = orders.take_profit.lmtPrice

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

    # 子注文の値段は、参照価格ではなく**実約定価格**を基準に置き直す。
    stop_price, take_profit_price = _reprice_children_to_fill(
        ib, contract, orders, reference_price, fill_price, stop_ratio, take_profit_ratio,
    )

    # 親が約定した = 建玉ができた時点で、子注文が本当にブローカー側で生きているかを
    # 確かめる。送信が受理されたことと、注文が板に置かれたことは別である
    # （呼値違反・値幅制限・プリセットによる拒否は、ib_insyncからは警告としてしか
    # 見えず、子注文の状態には何も来ないことがある）。
    await _ensure_children_are_live_async(
        ib, contract, child_trades, quantity, orders.oca_group,
    )
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


def _reprice_children_to_fill(
    ib: IB,
    contract: Stock,
    orders: BracketOrders,
    reference_price: float,
    fill_price: Optional[float],
    stop_ratio: float,
    take_profit_ratio: float,
) -> tuple:
    """子注文の値段を、参照価格ではなく実約定価格を基準に置き直す。

    **参照価格は遅延データ由来で、実約定と数%ずれる。** 2026-08-05の
    ペーパー検証では参照価格66.50に対し実約定が67.44（+1.4%）で、
    意図した -5%/+10% のはずの待機注文が、実際の建値から見ると
    **-6.3%/+8.5%** の位置に置かれていた。損切りが遠い側にずれるため、
    1トレードのリスクが設計値(1%)を超える。

    そのうえ、この日はBot側のポーリング判定（建値-5%）の方が先に発動した。
    ブローカー側に待機注文を置く意味は「プロセスが落ちていても効く」ことに
    あるので、その注文が実勢とずれた位置にあると防御の主役がBot側へ移り、
    設計の前提が崩れる。

    値幅は参照価格に対する比率として保つ（呼び出し側の -5%/+10% という意図を
    そのまま実約定価格へ移す）。**比率は呼値へ丸める前の値段から取る。**
    丸めた後の値段から取ると、丸めが二重に効いて1呼値ぶんずれる。

    **修正時は transmit=True にすること。** グループは既に送信済みなので、
    Falseのままだと修正が市場へ届かない。
    """
    stop_price = orders.stop_loss.auxPrice
    take_profit_price = orders.take_profit.lmtPrice
    if fill_price is None or fill_price <= 0 or reference_price <= 0:
        return stop_price, take_profit_price

    new_stop = round_to_tick(fill_price * stop_ratio)
    new_take_profit = round_to_tick(fill_price * take_profit_ratio)
    if new_stop == stop_price and new_take_profit == take_profit_price:
        return stop_price, take_profit_price

    orders.stop_loss.auxPrice = new_stop
    orders.take_profit.lmtPrice = new_take_profit
    for child in (orders.stop_loss, orders.take_profit):
        child.transmit = True
        ib.placeOrder(contract, child)

    logger.info(
        "[%s] 実約定(%.2f)に合わせて待機注文を置き直しました: "
        "損切り %.2f -> %.2f / 利確 %.2f -> %.2f（参照価格は %.2f）",
        contract.symbol, fill_price,
        stop_price, new_stop, take_profit_price, new_take_profit, reference_price,
    )
    return new_stop, new_take_profit


async def place_resting_exit_orders_async(
    ib: IB,
    contract: Stock,
    quantity: int,
    stop_price: float,
    take_profit_price: float,
    reference_price: float,
) -> str:
    """既にある建玉に対して、待機決済注文（損切りSTP＋利確LMT）を置き直す。

    親の成行買いを伴わない点だけがブラケットと違う。使うのは次の2つの場面で、
    どちらも**建玉があるのに待機注文が無い**状態を解消するためのものである。

    - ボット側の成行決済が失敗したとき（取り消し済みの待機注文を戻す）
    - 起動時・サイクル開始時に、建玉に対応する待機注文が見つからないとき
      （待機注文はDAYだと引けで失効し、IBKR側の都合で取り消されることもある）

    戻り値はOCAグループ名。**IBKRはブラケットの子のocaGroupを親のpermIdへ
    書き換える**ため、この名前で後から照合してはならない（`_is_resting_exit_order`）。
    """
    if quantity <= 0:
        raise ValueError("数量は正の整数である必要があります。")

    # 数量のクランプは掛けない。決済(SELL)に適用すると、ブローカー側に建玉が
    # 残ったままローカルの追跡だけが消える（「9. 開発時の禁止事項」）。
    orders = build_bracket_orders(
        symbol=contract.symbol, quantity=quantity,
        stop_price=stop_price, take_profit_price=take_profit_price,
        reference_price=reference_price,
    )
    stop_loss, take_profit = orders.stop_loss, orders.take_profit
    # 親が無いので、最初の1件も含めて独立した注文として送る。
    stop_loss.transmit = True
    stop_loss.parentId = 0
    take_profit.parentId = 0

    if not ENABLE_REAL_ORDERS:
        logger.info(
            "[DRY-RUN] 待機注文の再設置シミュレーション: symbol=%s qty=%s "
            "損切り=STP@%.2f 利確=LMT@%.2f (placeOrderは呼び出していません)",
            contract.symbol, quantity, stop_loss.auxPrice, take_profit.lmtPrice,
        )
        return orders.oca_group

    child_trades = [ib.placeOrder(contract, order) for order in (stop_loss, take_profit)]
    await _ensure_children_are_live_async(
        ib, contract, child_trades, quantity, orders.oca_group, flatten_on_failure=False,
    )
    logger.info(
        "[%s] 待機注文を置き直しました: qty=%s 損切り=STP@%.2f 利確=LMT@%.2f",
        contract.symbol, quantity, stop_loss.auxPrice, take_profit.lmtPrice,
    )
    return orders.oca_group


async def _ensure_children_are_live_async(
    ib: IB,
    contract: Stock,
    child_trades: List,
    quantity: int,
    oca_group: str,
    flatten_on_failure: bool = True,
) -> None:
    """子注文（損切り・利確）がブローカー側で生きているか確かめる。

    生きていなければ **建玉をその場で成行決済し**、`RestingOrdersNotLiveError` を
    投げる。呼び出し側はローカルにポジションを記録しないので、ブローカーにも
    ローカルにも建玉が残らない状態へ揃う。

    `flatten_on_failure=False` は、既にある建玉へ待機注文を置き直す場面
    （`place_resting_exit_orders_async`）で使う。そちらは決済に失敗した直後に
    呼ばれうるので、ここで再び成行決済を試みると同じ失敗を繰り返すだけになる。
    決済せずに例外を投げ、無防備であることを呼び出し側に知らせる。

    残す選択肢を採らないのは、損切りの無い建玉を持つことがこのプロジェクトで
    最も避けたい状態だからである（「決済の置き場所」）。片方だけ生きている場合も
    同様に扱う: 利確だけが残った建玉は、下方向に無防備なまま持ち越される。

    2026-08-05のペーパー検証で、呼値に合わない逆指値(63.175)がIBKR側で不成立に
    なり、利確だけが生きた建玉が実際に発生した。当時この検証は無く、
    `positions.json` には存在しない損切り値段が記録されていた。
    """
    deadline = asyncio.get_event_loop().time() + _CHILD_ORDER_LIVE_TIMEOUT_SECONDS
    while True:
        dead = [t for t in child_trades if t.orderStatus.status not in _LIVE_ORDER_STATUSES]
        if not dead:
            return
        if asyncio.get_event_loop().time() >= deadline:
            break
        await asyncio.sleep(_ORDER_STATUS_POLL_INTERVAL_SECONDS)

    detail = ", ".join(
        f"{t.order.orderType}@{t.order.auxPrice or t.order.lmtPrice}(status={t.orderStatus.status})"
        for t in dead
    )
    if not flatten_on_failure:
        logger.error(
            "[%s] 待機注文を置き直せませんでした(oca=%s): %s。"
            "**建玉が損切りの無い状態で残っています。**",
            contract.symbol, oca_group, detail,
        )
        raise RestingOrdersNotLiveError(
            f"[{contract.symbol}] 待機注文が成立しませんでした（{detail}）。"
            "建玉は無防備なまま残っています。"
        )

    logger.error(
        "[%s] 待機注文がブローカー側で生きていません(oca=%s): %s。"
        "損切りの無い建玉を残さないため、建玉を成行で決済します。",
        contract.symbol, oca_group, detail,
    )
    for child_trade in child_trades:
        ib.cancelOrder(child_trade.order)
    await place_market_order_async(ib, contract, ACTION_SELL, quantity)
    raise RestingOrdersNotLiveError(
        f"[{contract.symbol}] ブラケットの子注文が成立しませんでした（{detail}）。"
        "建玉は成行で決済済みです。"
    )


@dataclass(frozen=True)
class RestingOrderFill:
    """ブローカー側の待機注文が約定していたことの記録。"""

    order_type: str
    fill_price: float
    commission: float


def _is_resting_exit_order(trade, symbol: str) -> bool:
    """その注文が、この銘柄の建玉に対する待機決済注文か。

    **OCAグループ名で突き合わせてはならない。** ブラケットの子注文の
    `ocaGroup` は、こちらが付けた名前(`BRACKET_AAPL_...`)のままではなく
    **IBKR側で親のpermId(例 `1171471109`)へ書き換えられる**（2026-08-05に実測）。
    名前で照合すると一致が0件になり、取り消しも約定検知も静かに不発になる。

    銘柄で突き合わせるのは、`PositionManager` が1銘柄1建玉しか持たないため
    曖昧さが無いからである。新規建ての親(BUY)を拾わないよう、売りの
    待機注文（逆指値・指値）に限る。
    """
    order = trade.order
    contract = getattr(trade, "contract", None)
    if getattr(contract, "symbol", None) != symbol:
        return False
    if getattr(order, "action", None) != ACTION_SELL:
        return False
    return getattr(order, "orderType", None) in _RESTING_EXIT_ORDER_TYPES


@dataclass(frozen=True)
class RestingExitProtection:
    """ある銘柄の建玉が、ブローカー側の待機注文でどこまで守られているか。"""

    live_order_types: frozenset
    # 待機注文の**約定**が観測できたか。約定していれば建玉はもう閉じており、
    # OCAの相方もIBKR側が取り消す。置き直しの対象にしてはならない。
    has_filled_exit: bool

    @property
    def is_complete(self) -> bool:
        """損切りと利確の両方が置かれているか（＝置き直しが不要か）。

        **片方だけでは守られていない。** 2026-08-05の実測では、呼値違反で
        逆指値だけが不成立になり、利確だけが生きた建玉が残った。片方でも
        生きていれば「保護あり」と数えると、この**下方向に無防備な建玉**を
        毎サイクル見逃し続ける。
        """
        return self.has_filled_exit or _RESTING_EXIT_ORDER_TYPES <= self.live_order_types


def _warn_about_tif_downgrades(downgraded: Dict[str, set]) -> None:
    """待機注文の有効期間が `GTC` 以外へ書き換えられていたら記録する。

    **コードで `tif='GTC'` を明示しても、IB Gateway の Order Preset がそれを
    上書きする**（`Error 10349`。2026-08-06のログでは板に置かれた子注文が
    実際に `tif='DAY'` だった）。上書きは注文を拒否しないので発注は成功し、
    こちらの `Order` オブジェクトは送信時の `GTC` を保持したままになる。
    **つまりブローカーから読み直さない限り、この縮退はどこにも現れない。**

    DAY のまま持ち越すと待機注文が引けで失効し、翌日の寄り付きまで損切りの
    無い時間ができる。毎サイクルの突き合わせが翌日には置き直すが、
    **夜間の穴は埋まらない**ため、防御ではなく検知としてここに置く。

    直せるのは Gateway の Global Configuration → Presets だけなので、
    案内はその1点に絞る。
    """
    for symbol in sorted(set(_TIF_DOWNGRADE_WARNED) - set(downgraded)):
        _TIF_DOWNGRADE_WARNED.discard(symbol)

    for symbol, tifs in sorted(downgraded.items()):
        if symbol in _TIF_DOWNGRADE_WARNED:
            continue
        _TIF_DOWNGRADE_WARNED.add(symbol)
        logger.warning(
            "[%s] 待機注文の有効期間が %s になっています（%s で発注したはずのもの）。"
            "IB Gateway の Order Preset による上書きで、引けで失効するため"
            "翌朝まで損切りの無い時間ができます。"
            "Global Configuration → Presets → Stocks の Time in Force を GTC にしてください。",
            symbol, "/".join(sorted(tifs)), _RESTING_ORDER_TIF,
        )


async def find_resting_exit_protection_async(ib: IB) -> Dict[str, RestingExitProtection]:
    """銘柄ごとに、待機決済注文がブローカー側でどう置かれているかを返す。

    **`ib.openTrades()` ではなく `reqAllOpenOrdersAsync()` を使う。** 前者は
    このクライアントIDが出した注文しか含まないため、他のクライアント（手動の
    修復・別プロセス）が置いた注文を「無い」と誤判定し、二重に置いてしまう。
    建玉3株に対して売り注文が6株ぶん並ぶと、IBKRは超過分を空売りと見なして
    注文を拒否する（2026-08-05に `Error 201` として実測）。
    """
    trades = await ib.reqAllOpenOrdersAsync()

    live: Dict[str, set] = {}
    filled: set = set()
    downgraded: Dict[str, set] = {}
    for trade in trades:
        symbol = getattr(trade.contract, "symbol", "")
        if not _is_resting_exit_order(trade, symbol):
            continue
        status = trade.orderStatus.status
        if status == _STATUS_FILLED:
            filled.add(symbol)
        elif status in _LIVE_ORDER_STATUSES:
            live.setdefault(symbol, set()).add(trade.order.orderType)
            tif = getattr(trade.order, "tif", "") or ""
            if tif and tif != _RESTING_ORDER_TIF:
                downgraded.setdefault(symbol, set()).add(tif)

    _warn_about_tif_downgrades(downgraded)

    return {
        symbol: RestingExitProtection(
            live_order_types=frozenset(live.get(symbol, ())),
            has_filled_exit=symbol in filled,
        )
        for symbol in set(live) | filled
    }


def find_filled_resting_exit(ib: IB, symbol: str) -> Optional[RestingOrderFill]:
    """その銘柄の待機注文が約定していればその内容を返す。

    実発注時の決済検知はこちらを使う。ドライラン中は観測した現在値から
    推定するしかないが（`strategy.exit_signal.detect_resting_order_exit`）、
    それは300秒ごとの1点しか見ないため、バーの中で逆指値に触れて戻した
    動きを取りこぼす。実際の約定が取れるならその推定は要らない。
    """
    if not symbol:
        return None

    for trade in ib.trades():
        order = trade.order
        if not _is_resting_exit_order(trade, symbol):
            continue
        if trade.orderStatus.status != _STATUS_FILLED:
            continue

        fill_price = _fill_price_of(trade)
        if fill_price is None:
            # 約定はしているのに値段が読めない。ここで推定を入れると損益が
            # 静かにずれるため、次のサイクルで読めるまで判断を持ち越す。
            logger.warning(
                "[%s] 待機注文が約定していますが約定価格が読めません: type=%s",
                symbol, getattr(order, "orderType", None),
            )
            return None
        return RestingOrderFill(
            order_type=str(getattr(order, "orderType", "")),
            fill_price=fill_price,
            commission=_commission_of(trade),
        )

    return None


async def cancel_bracket_orders_async(ib: IB, symbol: str) -> None:
    """その銘柄の待機注文を取り消す。

    トレーリングストップや大引け前の強制決済のように、ボット側の判断で
    成行決済した場合は、必ずこれを呼んで待機注文を消すこと。残したままだと
    建玉が無いのに売り注文だけが生き続け、次にその銘柄を建てた瞬間に
    意図しない決済が起きる。

    突き合わせを銘柄で行う理由は `_is_resting_exit_order` を参照。
    ブローカー同期で取り込んだ建玉のように、こちらがOCAグループ名を
    知らない場合でも取り消せる。

    **取り消しの完了を待ってから返す。** `cancelOrder` は要求を投げるだけで、
    ブローカー側が `Cancelled` にするまでの間、注文はまだ板に生きている。
    2026-08-05の実測では、取り消し要求の1ミリ秒後に出した成行売りが
    「建玉3株 + 生きている売りLMT 3株 + 売り成行3株」＝売り超過と見なされ、
    `Error 201` で拒否された（取り消しが確定したのはその0.4秒後）。

    Raises:
        RestingOrderCancelTimeoutError: 取り消しが確定しなかった場合。
    """
    if not symbol:
        return

    if not ENABLE_REAL_ORDERS:
        logger.info(
            "[DRY-RUN] 待機注文の取り消しシミュレーション: symbol=%s "
            "(cancelOrderは呼び出していません)",
            symbol,
        )
        return

    targets = await _find_cancellable_resting_orders_async(ib, symbol)
    for trade in targets:
        ib.cancelOrder(trade.order)

    logger.info("[%s] 待機注文の取り消しを要求しました: 件数=%d", symbol, len(targets))
    if not targets:
        return

    await _await_cancellation_async(ib, symbol)
    logger.info("[%s] 待機注文の取り消しが確定しました。", symbol)


async def _find_cancellable_resting_orders_async(ib: IB, symbol: str) -> List:
    """その銘柄の、まだ取り消しが終わっていない待機注文を返す。

    **`ib.openTrades()` ではなく `reqAllOpenOrdersAsync()` を使う。** 前者は
    このクライアントIDが出した注文しか含まないため、他のクライアント（手動の
    修復・別プロセス）が置いた注文を取り消し損ねる。そのまま成行を重ねると
    売り超過になり `Error 201` で拒否される（`find_symbols_with_live_resting_exits_async`
    と同じ理由）。
    """
    trades = await ib.reqAllOpenOrdersAsync()
    return [
        trade
        for trade in trades
        if _is_resting_exit_order(trade, symbol)
        and trade.orderStatus.status in _PENDING_CANCEL_STATUSES
    ]


async def _await_cancellation_async(ib: IB, symbol: str) -> None:
    """待機注文がブローカー側から消えるまで待つ。

    ブローカーへ問い合わせ直すのは、他のクライアントが出した注文の状態が
    こちらの `Trade` オブジェクトへ必ず反映されるとは限らないため。
    ヒストリカルデータのリクエストではないのでペーシング枠（「6.1」）は
    消費しない。
    """
    waited = 0.0
    while waited < _ORDER_CANCEL_TIMEOUT_SECONDS:
        await asyncio.sleep(_ORDER_STATUS_POLL_INTERVAL_SECONDS)
        waited += _ORDER_STATUS_POLL_INTERVAL_SECONDS
        if not await _find_cancellable_resting_orders_async(ib, symbol):
            return

    raise RestingOrderCancelTimeoutError(
        f"[{symbol}] 待機注文の取り消しが {_ORDER_CANCEL_TIMEOUT_SECONDS:.0f} 秒以内に"
        "確定しませんでした。生きている売り注文へ成行の売りを重ねると"
        "売り超過として拒否されるため、決済を次のサイクルへ持ち越します。"
    )
