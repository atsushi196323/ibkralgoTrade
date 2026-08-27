"""保有ポジションの決済（Exit）判定ロジック。

利確(Take Profit)・損切り(Stop Loss)・トレーリングストップの3条件を評価し、
いずれかを満たした場合に決済シグナルを出す。

決済の責務は2つに分かれている:

1. **ブローカー側に置く待機注文**（利確の指値・損切りの逆指値）
   … `detect_resting_order_exit` / `resolve_stop_price` / `resolve_take_profit_price`
   建てた直後にブローカーへ送っておくため、ボットのプロセスが落ちていても、
   TWSとの接続が切れていても、市場が動けば約定する。オーバーナイトの
   ギャップや稼働中の切断に対する唯一の防御であり、ポーリング間隔
   （main.POLL_INTERVAL_SECONDS）による決済の遅れも受けない。

2. **ボット側で毎サイクル判定するもの**（トレーリングストップ・大引け前の強制決済）
   … `detect_exit_signal`
   トレーリングは「エントリー後の高値」という状態に依存し、大引け決済は
   時刻に依存するため、静的な待機注文としては表現できない。

1が発火した場合の約定価格は「注文の種類」で決まるため、注文の値段どおりに
約定するとは限らない。`detect_resting_order_exit` はそこまでモデル化する。
"""

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

REASON_TAKE_PROFIT: str = "TAKE_PROFIT"
REASON_STOP_LOSS: str = "STOP_LOSS"
REASON_TRAILING_STOP: str = "TRAILING_STOP"
REASON_NONE: str = "NONE"
# main.py側の時刻ベースの強制決済（デイトレードポジションの大引け前フラット化）で使う理由コード。
# detect_exit_signal自体はこの理由を返さない（時刻を扱わない価格ベースの判定のため）。
REASON_EOD_FLATTEN: str = "EOD_FLATTEN"

# 保有日数の満期による決済（横断モメンタム）。**利確・損切りと別の理由にする。**
# 同じ名前にすると、trade_journal の集計で「値幅で降りた」ものと
# 「時間で降りた」ものが混ざり、どちらの設計が効いたのか読めなくなる。
REASON_MOMENTUM_REBALANCE: str = "MOMENTUM_REBALANCE"


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


# --- ブローカー側に置く待機注文 -------------------------------------------------------


@dataclass
class RestingOrderExit:
    """ブローカー側の待機注文が約定したときの決済内容。"""

    reason: str
    # 実際の約定価格。注文に書いた値段とは限らない（下の約定モデル参照）。
    fill_price: float


def resolve_stop_price(entry_price: float, stop_loss_pct: float) -> float:
    """損切りの逆指値(STP)に設定する価格。"""
    if entry_price <= 0:
        raise ValueError("entry_price は正の値である必要があります。")
    if stop_loss_pct <= 0:
        raise ValueError("stop_loss_pct は正の値である必要があります。")
    return entry_price * (1.0 - stop_loss_pct / 100.0)


def resolve_take_profit_price(entry_price: float, take_profit_pct: float) -> float:
    """利確の指値(LMT)に設定する価格。"""
    if entry_price <= 0:
        raise ValueError("entry_price は正の値である必要があります。")
    if take_profit_pct <= 0:
        raise ValueError("take_profit_pct は正の値である必要があります。")
    return entry_price * (1.0 + take_profit_pct / 100.0)


def detect_resting_order_exit(
    stop_price: float,
    take_profit_price: float,
    bar_low: float,
    bar_high: float,
    bar_open: Optional[float] = None,
) -> Optional[RestingOrderExit]:
    """ブローカーに置いた待機注文が約定したかを判定し、約定価格を返す。

    約定モデル（注文の値段どおりに約定するとは限らない点が重要）:

    - **損切り(STP)** は逆指値なので、トリガーされた後は成行注文になる。
      窓を開けて下抜けした場合は逆指値より**不利な価格**で約定する。
      よって始値が逆指値を下回っていれば始値、そうでなければ逆指値で約定と見なす。
      この「ギャップ時に滑る」挙動こそが、1トレードのリスクを口座の1%に
      収める前提の主な崩れ方であり、モデル化しないと損失を過小評価する。
    - **利確(LMT)** は指値なので、指値より不利な価格では約定しない。
      窓を開けて上抜けした場合は始値（指値より有利）で約定する。

    同じバーの中で両方に到達した場合は、どちらが先かバーの情報からは
    判別できないため、**損切りを優先**する（保守的な側に倒す）。

    ライブのポーリングから呼ぶ場合は、観測した現在値を bar_low / bar_high の
    両方に渡す（bar_open は省略）。バー内の値動きが分からないので、
    「観測した瞬間の値でしか判定しない」という当然の挙動に縮退する。

    始値が渡されない場合（終値しか無いCSV、ライブのポーリング）はギャップの
    有無を判別できない。その場合は**どちらの注文も不利な側に倒して**扱う:

    - 損切りは、観測できた最安値が逆指値を下回っていればその価格で約定した
      ものとする（逆指値どおりに約定したと仮定するのは楽観的すぎる）
    - 利確は、観測値がいくら上でも指値どおりの約定とする（指値より有利な
      約定はギャップを確認できたときだけ認める）

    Args:
        bar_low: バーの安値（ライブでは観測した現在値）。
        bar_high: バーの高値（ライブでは観測した現在値）。
        bar_open: バーの始値。省略時はギャップを判定できない（上記参照）。

    Returns:
        約定した場合は RestingOrderExit。どちらも約定していなければ None。
    """
    if stop_price <= 0 or take_profit_price <= 0:
        raise ValueError("stop_price, take_profit_price は正の値である必要があります。")

    if bar_low <= stop_price:
        if bar_open is not None:
            # 始値が既に逆指値を割っていた（＝窓を開けて下落）場合は始値で約定する。
            # そうでなければバーの中で逆指値に触れただけなので、逆指値で約定する。
            fill_price = bar_open if bar_open < stop_price else stop_price
        else:
            fill_price = min(stop_price, bar_low)
        return RestingOrderExit(reason=REASON_STOP_LOSS, fill_price=fill_price)

    if bar_high >= take_profit_price:
        # 指値より有利な価格で寄り付いた場合のみ、その始値で約定する。
        gapped_above = bar_open is not None and bar_open > take_profit_price
        fill_price = bar_open if gapped_above else take_profit_price
        return RestingOrderExit(reason=REASON_TAKE_PROFIT, fill_price=fill_price)

    return None
