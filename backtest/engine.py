"""プルバック戦略のヒストリカルバックテストエンジン。

main.py（ライブ実行）と同じシグナル判定・ポジションサイジング関数
（strategy/pullback.py, strategy/exit_signal.py, execution/position_sizing.py）
をそのまま再利用してバー単位でシミュレーションする。ライブ用ロジックと
バックテスト用ロジックが乖離する（ロジックドリフト）ことを防ぐための設計。
"""

import logging
import math
from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd

from backtest.costs import CostModel
from execution.position_sizing import calculate_position_size
from strategy.exit_signal import (
    REASON_STOP_LOSS,
    detect_exit_signal,
    detect_resting_order_exit,
    resolve_stop_price,
    resolve_take_profit_price,
)
from backtest.market_reference import MARKET_DEVIATION_COLUMN
from strategy.pullback import MarketFilterConfig, detect_pullback_signal

logger = logging.getLogger(__name__)

# 大引け前の強制決済（デイトレード）。ライブでは米国東部時間15:55に出す成行で、
# バックテストではその取引日の最後のバーの終値で約定させる。
REASON_SESSION_END: str = "SESSION_END"


@dataclass
class BacktestConfig:
    ma_window: int = 20
    threshold_pct: float = 5.0
    take_profit_pct: float = 10.0
    stop_loss_pct: float = 5.0
    trailing_stop_pct: float = 5.0
    risk_per_trade_pct: float = 1.0
    initial_equity: float = 100_000.0
    # 手数料・スリッページ。既定でコストを織り込む（backtest/costs.py参照）。
    # ZERO_COSTへ差し替えられるのはシグナル判定の単体テストとコスト影響の比較のみ。
    costs: CostModel = field(default_factory=CostModel)
    # 市場全体（指数）によるエントリーの追加条件。既定はすべてNone（無効）で、
    # 有効化の可否はウォークフォワードに決めさせる（strategy/pullback.py参照）。
    # 使うには事前に backtest.market_reference.attach_market_deviation で
    # 指数の乖離率の列を付与しておく必要がある。
    market_min_deviation_pct: Optional[float] = None
    market_max_deviation_pct: Optional[float] = None
    relative_threshold_pct: Optional[float] = None
    # 取引日の最後のバーで建玉を手仕舞う（デイトレード検証用。既定False）。
    #
    # **日中足でこれを外すと、検証しているものがライブと別の戦略になる。**
    # ライブのデイトレード判定で建てた建玉は米国東部時間15:55に強制決済し
    # （`main.py` の大引け前決済。オーバーナイトのギャップリスクを避けるため）、
    # スイングだけが持ち越す。外したまま5分足を回すと建玉が翌日以降へ繋がり、
    # ギャップの分だけ損益が別物になる。
    #
    # 日足では1バー=1日なので毎バー決済することになり意味を成さない。
    # 既定をFalseにしているのはそのため（スイングの検証結果は変わらない）。
    close_at_session_end: bool = False
    # 決済した当日は同じ銘柄へ再エントリーしない（ライブ側と揃えるための既定True）。
    # これが無いと、下落トレンド中に「買う→損切り→また買う」を1日に何度も
    # 繰り返す。日足のように1バー=1日のデータでは元々起こらないため無影響だが、
    # 日中足の検証とライブ挙動の再現には必須。
    block_same_day_reentry: bool = True


@dataclass
class Trade:
    symbol: str
    entry_date: object
    # entry_price / exit_price はスリッページ込みの約定価格（バーの終値ではない）。
    entry_price: float
    exit_date: object
    exit_price: float
    quantity: int
    reason: str
    # pnl は手数料控除後（ネット）。プロフィットファクター等の指標が
    # コストを織り込んだ値になるよう、集計側ではなくここで差し引いている。
    pnl: float
    pnl_pct: float
    # 往復（買い＋売り）の手数料合計と、その控除前の損益。
    commission: float = 0.0
    gross_pnl: float = 0.0


@dataclass
class BacktestResult:
    symbol: str
    config: BacktestConfig
    initial_equity: float
    final_equity: float
    trades: List[Trade] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=pd.Series)


@dataclass(frozen=True)
class _Bar:
    """1バー分の値。OHLCが無いCSVでも動くよう、欠けている値は終値で代用する。"""

    high: float
    low: float
    close: float
    open: Optional[float] = None


def _bar_at(df: pd.DataFrame, index: int, close_price: float) -> _Bar:
    def _optional(column: str) -> Optional[float]:
        if column not in df.columns:
            return None
        value = float(df[column].iloc[index])
        return None if math.isnan(value) else value

    high = _optional("high")
    low = _optional("low")
    return _Bar(
        high=close_price if high is None else high,
        low=close_price if low is None else low,
        close=close_price,
        open=_optional("open"),
    )


def _trading_day_of(value: object) -> object:
    """バーの日付から「取引日」を取り出す（同日中の再エントリー禁止の判定単位）。

    日足のように1バー=1日なら実質何も起きないが、日中足では
    「損切りした当日は同じ銘柄へ再エントリーしない」というライブ側のルールを
    そのまま再現する必要がある。
    """
    date_attr = getattr(value, "date", None)
    return date_attr() if callable(date_attr) else value


def run_backtest(symbol: str, df: pd.DataFrame, config: Optional[BacktestConfig] = None) -> BacktestResult:
    config = config or BacktestConfig()

    if df.empty:
        raise ValueError("df が空です。")
    if "close" not in df.columns:
        raise ValueError("df には 'close' 列が必要です。")

    dates = df["date"] if "date" in df.columns else pd.Series(range(len(df)))

    market_filter = MarketFilterConfig(
        min_deviation_pct=config.market_min_deviation_pct,
        max_deviation_pct=config.market_max_deviation_pct,
        relative_threshold_pct=config.relative_threshold_pct,
    )
    if market_filter.is_enabled and MARKET_DEVIATION_COLUMN not in df.columns:
        # 黙って無効化するとフィルター無しの成績を「フィルター有り」と
        # 取り違えるため、ここで落とす。
        raise ValueError(
            f"市場フィルターを有効にするには '{MARKET_DEVIATION_COLUMN}' 列が必要です。"
            "backtest.market_reference.attach_market_deviation で付与してください。"
        )
    market_deviations = (
        df[MARKET_DEVIATION_COLUMN] if MARKET_DEVIATION_COLUMN in df.columns else None
    )

    costs = config.costs

    equity: float = config.initial_equity
    position_qty: int = 0
    entry_price: float = 0.0
    entry_date = None
    highest_price: float = 0.0
    entry_commission: float = 0.0
    stop_price: float = 0.0
    take_profit_price: float = 0.0
    last_exit_day: object = None

    trades: List[Trade] = []
    equity_curve_values: List[float] = []

    for i in range(len(df)):
        close_price = float(df["close"].iloc[i])
        bar = _bar_at(df, i, close_price)
        current_day = _trading_day_of(dates.iloc[i])
        is_last_bar = i == len(df) - 1
        # その取引日の最後のバーか（次のバーの取引日が変わるか）。
        is_session_end = config.close_at_session_end and (
            is_last_bar or _trading_day_of(dates.iloc[i + 1]) != current_day
        )

        if position_qty == 0:
            in_cooldown = config.block_same_day_reentry and current_day == last_exit_day
            # 大引けで手仕舞う設定のとき、その日の最後のバーでは建てない。
            # 建てても同じバーで決済することになり、往復の手数料とスリッページ
            # だけを負う建玉が生まれる（ライブにこの動きは無い）。
            if not in_cooldown and not is_session_end and i + 1 >= config.ma_window:
                window_df = df.iloc[max(0, i + 1 - config.ma_window): i + 1]
                market_deviation = None
                if market_deviations is not None:
                    value = float(market_deviations.iloc[i])
                    # 突き合わない日（NaN）はNoneとして渡し、フィルター側で
                    # 「条件を満たさない」扱いにする。
                    market_deviation = None if math.isnan(value) else value
                signal = detect_pullback_signal(
                    symbol, window_df, ma_window=config.ma_window, threshold_pct=config.threshold_pct,
                    market_deviation_pct=market_deviation, market_filter=market_filter,
                )
                if signal.should_buy:
                    # 判定はバーの終値で行い、約定はスリッページ分だけ不利な価格で行う。
                    # ライブ側もシグナル検知後に成行で発注するため、
                    # 以降の損益・決済判定の基準はこの約定価格に揃える。
                    fill_price = costs.buy_fill_price(close_price)
                    quantity = calculate_position_size(
                        account_equity=equity,
                        entry_price=fill_price,
                        stop_loss_pct=config.stop_loss_pct,
                        risk_per_trade_pct=config.risk_per_trade_pct,
                    )
                    if quantity > 0:
                        position_qty = quantity
                        entry_price = fill_price
                        entry_date = dates.iloc[i]
                        highest_price = fill_price
                        entry_commission = costs.commission_for(quantity, fill_price)
                        # 建てた直後にブローカーへ置く待機注文の値段。
                        stop_price = resolve_stop_price(fill_price, config.stop_loss_pct)
                        take_profit_price = resolve_take_profit_price(
                            fill_price, config.take_profit_pct
                        )

            equity_curve_values.append(equity)
        else:
            # 1. ブローカーに置いた待機注文（損切りの逆指値・利確の指値）。
            #    バーの中で約定するため、終値を待たずに決済される。
            resting_exit = detect_resting_order_exit(
                stop_price=stop_price,
                take_profit_price=take_profit_price,
                bar_low=bar.low,
                bar_high=bar.high,
                bar_open=bar.open,
            )

            # 2. ボット側で毎サイクル判定するもの（トレーリングストップ）。
            #    待機注文が約定していれば、そちらが先に決済しているので評価しない。
            highest_price = max(highest_price, bar.high)
            result = None
            if resting_exit is None:
                result = detect_exit_signal(
                    symbol,
                    entry_price=entry_price,
                    current_price=close_price,
                    highest_price_since_entry=highest_price,
                    take_profit_pct=config.take_profit_pct,
                    stop_loss_pct=config.stop_loss_pct,
                    trailing_stop_pct=config.trailing_stop_pct,
                )

            should_sell = resting_exit is not None or (result is not None and result.should_sell)

            if should_sell or is_last_bar or is_session_end:
                if resting_exit is not None:
                    reason = resting_exit.reason
                    # 逆指値はトリガー後に成行になるためスリッページを負う。
                    # 指値(利確)は値段どおりかそれより有利にしか約定しないので負わない。
                    exit_fill_price = (
                        costs.sell_fill_price(resting_exit.fill_price)
                        if reason == REASON_STOP_LOSS else resting_exit.fill_price
                    )
                else:
                    # トレーリング・大引け・期末の手仕舞いはボットが成行で出す。
                    if result is not None and result.should_sell:
                        reason = result.reason
                    elif is_session_end and not is_last_bar:
                        reason = REASON_SESSION_END
                    else:
                        reason = "END_OF_DATA"
                    exit_fill_price = costs.sell_fill_price(close_price)

                exit_commission = costs.commission_for(position_qty, exit_fill_price)
                commission = entry_commission + exit_commission

                gross_pnl = (exit_fill_price - entry_price) * position_qty
                pnl = gross_pnl - commission
                # 手数料込みの損益を、投下した約定代金に対する割合で表す。
                # ここをgross基準にすると「pnlは負なのにpnl_pctは正」という
                # 組み合わせが生じ、metrics側の勝敗判定(pnl基準)と食い違う。
                pnl_pct = pnl / (entry_price * position_qty) * 100.0
                equity += pnl
                trades.append(
                    Trade(
                        symbol=symbol,
                        entry_date=entry_date,
                        entry_price=entry_price,
                        exit_date=dates.iloc[i],
                        exit_price=exit_fill_price,
                        quantity=position_qty,
                        reason=reason,
                        pnl=pnl,
                        pnl_pct=pnl_pct,
                        commission=commission,
                        gross_pnl=gross_pnl,
                    )
                )
                position_qty = 0
                entry_price = 0.0
                entry_date = None
                highest_price = 0.0
                entry_commission = 0.0
                stop_price = 0.0
                take_profit_price = 0.0
                last_exit_day = current_day
                equity_curve_values.append(equity)
            else:
                # 含み損益は決済時のスリッページ・売り手数料を織り込めないが、
                # 支払い済みの買い手数料は差し引いて最大DDを過小評価しないようにする。
                unrealized_pnl = (close_price - entry_price) * position_qty
                equity_curve_values.append(equity + unrealized_pnl - entry_commission)

    equity_curve = pd.Series(equity_curve_values, index=dates.iloc[: len(equity_curve_values)].values)

    total_commission = sum(t.commission for t in trades)
    logger.info(
        "[%s] バックテスト完了: trades=%d final_equity=%.2f (初期%.2f) "
        "手数料合計=%.2f スリッページ=片道%.3f%%",
        symbol, len(trades), equity, config.initial_equity,
        total_commission, costs.slippage_pct,
    )

    return BacktestResult(
        symbol=symbol,
        config=config,
        initial_equity=config.initial_equity,
        final_equity=equity,
        trades=trades,
        equity_curve=equity_curve,
    )
