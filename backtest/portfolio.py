"""資金を共有するポートフォリオ水準のバックテスト。

`backtest/engine.py` は銘柄ごとに独立した資金でシミュレーションする。それは
「押し目買いにエッジがあるか」を測るには正しいが、**口座で何が起きるかは
測れない**——同時保有数の上限・日次サーキットブレーカー・枠の取り合い・
実際の最大ドローダウンは、資金を共有して初めて現れる。

このモジュールはライブ(`main.py`)の1サイクルをバー単位で再現する:

- 決済判定を先に回し、空いた枠に同じバーで新規建てを入れる
- 銘柄は**記載順**に処理し、枠が埋まった時点で以降の銘柄は判定に入らない
  （ライブの `run_watchlist_cycle_async` と同じ。乖離の大きい順ではない）
- 同日中の再エントリー禁止・1日の新規建て回数上限・日次サーキットブレーカー
- 新規建てのみ株数と金額のクランプを掛ける（決済には掛けない）
- エントリーには長期トレンドフィルターと日足の本数要件を課す

判定そのものは `strategy/` の関数をそのまま使う。ライブ・単一銘柄バックテスト・
ポートフォリオ検証の3者でロジックが割れないようにするためである。
"""

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

from backtest.costs import CostModel
from backtest.engine import Trade, _bar_at, _trading_day_of
from execution.position_sizing import calculate_position_size
from strategy.exit_signal import (
    REASON_STOP_LOSS,
    detect_exit_signal,
    detect_resting_order_exit,
    resolve_stop_price,
    resolve_take_profit_price,
)
from strategy.pullback import detect_pullback_signal
from strategy.screener import is_in_long_term_uptrend

logger = logging.getLogger(__name__)

REASON_END_OF_DATA: str = "END_OF_DATA"


@dataclass
class PortfolioConfig:
    """ライブの `main.py` の定数と1対1で対応させること。

    既定値はすべて2026-08-25時点のライブ設定である。**片方だけ変えると、
    検証しているものがライブと別の戦略になる。**
    """

    ma_window: int = 30
    threshold_pct: float = 5.0
    take_profit_pct: float = 10.0
    stop_loss_pct: float = 5.0
    trailing_stop_pct: float = 5.0
    risk_per_trade_pct: float = 1.0
    initial_equity: float = 1_220.0
    costs: CostModel = field(default_factory=CostModel)

    # ここから下がポートフォリオ水準でしか効かない制約。
    max_concurrent_positions: int = 2
    # 当日の実現損益（手数料控除後）がこの割合に達したら新規建てを止める。
    daily_loss_limit_pct: float = 3.0
    max_daily_entry_orders: int = 10
    block_same_day_reentry: bool = True

    # エントリーの前提（ライブの SWING_MIN_HISTORY_BARS / STRUGGLING_MA_WINDOW）。
    long_term_ma_window: int = 200
    min_history_bars: int = 200
    require_long_term_uptrend: bool = True

    # 新規建てのみに掛かる安全弁（execution/order_manager.py と同じ値）。
    max_position_size: int = 40
    max_order_notional_usd: float = 5_000.0
    # 監視銘柄数の上限。ライブは記載順に切り詰める。
    max_watchlist_size: Optional[int] = 24


@dataclass
class _OpenPosition:
    symbol: str
    quantity: int
    entry_price: float
    entry_date: object
    entry_commission: float
    stop_price: float
    take_profit_price: float
    highest_price: float


@dataclass
class PortfolioResult:
    config: PortfolioConfig
    initial_equity: float
    final_equity: float
    trades: List[Trade] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=pd.Series)
    # 資金のうち建玉に出ていた割合の平均。エッジがあっても稼働率が低ければ
    # 口座の年率は伸びない——同時保有枠を増やす議論はこの数字が起点になる。
    average_exposure_pct: float = 0.0
    # サーキットブレーカーで新規建てを止めた日。**日数だけでなく日付を持つのは、
    # 「発動したのに建っている」という取り違えを検証で潰せるようにするため。**
    circuit_breaker_dates: List[object] = field(default_factory=list)

    @property
    def circuit_breaker_days(self) -> int:
        return len(self.circuit_breaker_dates)

    @property
    def total_return_pct(self) -> float:
        return (self.final_equity / self.initial_equity - 1.0) * 100.0

    def cagr_pct(self, bars_per_year: int = 252) -> float:
        years = len(self.equity_curve) / bars_per_year
        if years <= 0 or self.initial_equity <= 0 or self.final_equity <= 0:
            return 0.0
        return ((self.final_equity / self.initial_equity) ** (1.0 / years) - 1.0) * 100.0

    def max_drawdown_pct(self) -> float:
        """終値ベースの最大ドローダウン。

        **これがポートフォリオ検証の主目的である。** 銘柄独立の集計では、
        同じ日に複数の建玉が同時に沈む効果が現れない（押し目は市場全体の
        下げで一斉に出るため、実測の日次リターン平均ペア相関は0.344）。
        """
        peak = float("-inf")
        worst = 0.0
        for value in self.equity_curve:
            peak = max(peak, float(value))
            if peak > 0:
                worst = max(worst, (peak - float(value)) / peak * 100.0)
        return worst


def _clamp_entry_quantity(quantity: int, price: float, config: PortfolioConfig) -> int:
    """新規建てにだけ掛かるクランプ（ライブの order_manager と同じ順序）。

    **決済に掛けてはならない**（「9. 開発時の禁止事項」）。ここは新規建て
    専用の経路なので、そのまま適用してよい。
    """
    quantity = min(quantity, config.max_position_size)
    if price > 0 and quantity * price > config.max_order_notional_usd:
        quantity = int(config.max_order_notional_usd // price)
    return max(0, quantity)


def run_portfolio_backtest(
    bars_by_symbol: Dict[str, pd.DataFrame],
    symbols: Optional[Sequence[str]] = None,
    config: Optional[PortfolioConfig] = None,
) -> PortfolioResult:
    """複数銘柄を1つの資金で回す。

    `symbols` は処理順（＝枠の取り合いの順序）。省略時は辞書の順序を使う。
    ライブは記載順に処理し、枠が埋まった時点で以降の銘柄を見ないため、
    **この順序は成績に影響する**。成績を見て決め直してはならない。
    """
    config = config or PortfolioConfig()
    order = list(symbols) if symbols is not None else list(bars_by_symbol)
    order = [s for s in order if s in bars_by_symbol and not bars_by_symbol[s].empty]
    if not order:
        raise ValueError("バーのある銘柄が1つもありません。")
    if config.max_watchlist_size is not None:
        order = order[: config.max_watchlist_size]

    # 日付 -> 行番号。銘柄ごとに上場日が違うため、共通の日付軸へ揃える。
    frames: Dict[str, pd.DataFrame] = {}
    index_of: Dict[str, Dict[object, int]] = {}
    all_days: List[object] = []
    for symbol in order:
        df = bars_by_symbol[symbol].reset_index(drop=True)
        dates = df["date"] if "date" in df.columns else pd.Series(range(len(df)))
        days = [_trading_day_of(d) for d in dates]
        frames[symbol] = df
        index_of[symbol] = {day: i for i, day in enumerate(days)}
        all_days.extend(days)
    timeline = sorted(set(all_days))

    costs = config.costs
    equity = config.initial_equity
    open_positions: Dict[str, _OpenPosition] = {}
    last_exit_day: Dict[str, object] = {}
    trades: List[Trade] = []
    equity_values: List[float] = []
    exposure_values: List[float] = []
    circuit_breaker_dates: List[object] = []

    for day_number, day in enumerate(timeline):
        is_last_day = day_number == len(timeline) - 1
        equity_at_day_start = equity
        realized_today = 0.0
        entries_today = 0

        # --- 1. 決済（ライブも決済判定を先に回す） -------------------------
        for symbol in list(open_positions):
            position = open_positions[symbol]
            row = index_of[symbol].get(day)
            if row is None:
                continue
            df = frames[symbol]
            close_price = float(df["close"].iloc[row])
            bar = _bar_at(df, row, close_price)

            resting_exit = detect_resting_order_exit(
                stop_price=position.stop_price,
                take_profit_price=position.take_profit_price,
                bar_low=bar.low, bar_high=bar.high, bar_open=bar.open,
            )
            position.highest_price = max(position.highest_price, bar.high)
            bot_exit = None
            if resting_exit is None:
                bot_exit = detect_exit_signal(
                    symbol,
                    entry_price=position.entry_price,
                    current_price=close_price,
                    highest_price_since_entry=position.highest_price,
                    take_profit_pct=config.take_profit_pct,
                    stop_loss_pct=config.stop_loss_pct,
                    trailing_stop_pct=config.trailing_stop_pct,
                )

            should_sell = resting_exit is not None or (bot_exit is not None and bot_exit.should_sell)
            if not (should_sell or is_last_day):
                continue

            if resting_exit is not None:
                reason = resting_exit.reason
                # 逆指値はトリガー後に成行になるためスリッページを負う。
                exit_fill = (
                    costs.sell_fill_price(resting_exit.fill_price)
                    if reason == REASON_STOP_LOSS else resting_exit.fill_price
                )
            elif bot_exit is not None and bot_exit.should_sell:
                reason = bot_exit.reason
                exit_fill = costs.sell_fill_price(close_price)
            else:
                reason = REASON_END_OF_DATA
                exit_fill = costs.sell_fill_price(close_price)

            exit_commission = costs.commission_for(position.quantity, exit_fill)
            commission = position.entry_commission + exit_commission
            gross = (exit_fill - position.entry_price) * position.quantity
            pnl = gross - commission
            equity += pnl
            realized_today += pnl
            trades.append(Trade(
                symbol=symbol, entry_date=position.entry_date,
                entry_price=position.entry_price, exit_date=day, exit_price=exit_fill,
                quantity=position.quantity, reason=reason, pnl=pnl,
                pnl_pct=pnl / (position.entry_price * position.quantity) * 100.0,
                commission=commission, gross_pnl=gross,
            ))
            del open_positions[symbol]
            last_exit_day[symbol] = day

        # --- 2. 新規建て --------------------------------------------------
        #
        # 日次サーキットブレーカーは**当日の実現損益**で判定する。含み損では
        # 発動しない（ライブと同じ。決済されるまで損益は確定しない）。
        breaker_tripped = (
            equity_at_day_start > 0
            and realized_today <= -equity_at_day_start * config.daily_loss_limit_pct / 100.0
        )
        if breaker_tripped:
            circuit_breaker_dates.append(day)

        if not breaker_tripped and not is_last_day:
            for symbol in order:
                if len(open_positions) >= config.max_concurrent_positions:
                    # 枠が埋まったら以降の銘柄は判定に入らない（ライブと同じ）。
                    break
                if entries_today >= config.max_daily_entry_orders:
                    break
                if symbol in open_positions:
                    continue
                if config.block_same_day_reentry and last_exit_day.get(symbol) == day:
                    continue
                row = index_of[symbol].get(day)
                if row is None or row + 1 < config.min_history_bars:
                    continue

                df = frames[symbol]
                close_price = float(df["close"].iloc[row])
                history = df.iloc[: row + 1]
                signal = detect_pullback_signal(
                    symbol, history.iloc[-config.ma_window:],
                    ma_window=config.ma_window, threshold_pct=config.threshold_pct,
                )
                if not signal.should_buy:
                    continue
                if config.require_long_term_uptrend and is_in_long_term_uptrend(
                    history, config.long_term_ma_window
                ) is not True:
                    continue

                fill_price = costs.buy_fill_price(close_price)
                quantity = calculate_position_size(
                    account_equity=equity, entry_price=fill_price,
                    stop_loss_pct=config.stop_loss_pct,
                    risk_per_trade_pct=config.risk_per_trade_pct,
                )
                quantity = _clamp_entry_quantity(quantity, fill_price, config)
                # 資金を共有しているので、建玉の合計が資金を超えないよう
                # 手元の現金でも頭打ちにする（キャッシュ口座の前提）。
                cash = equity - sum(p.entry_price * p.quantity for p in open_positions.values())
                if fill_price > 0:
                    quantity = min(quantity, int(cash // fill_price))
                if quantity <= 0:
                    continue

                entries_today += 1
                open_positions[symbol] = _OpenPosition(
                    symbol=symbol, quantity=quantity, entry_price=fill_price,
                    entry_date=day,
                    entry_commission=costs.commission_for(quantity, fill_price),
                    stop_price=resolve_stop_price(fill_price, config.stop_loss_pct),
                    take_profit_price=resolve_take_profit_price(
                        fill_price, config.take_profit_pct
                    ),
                    highest_price=fill_price,
                )

        # --- 3. 資産曲線 ---------------------------------------------------
        unrealized = 0.0
        deployed = 0.0
        for position in open_positions.values():
            row = index_of[position.symbol].get(day)
            if row is None:
                # その日のバーが無い銘柄は建値で据え置く（値が無いものを
                # 動かすと、休場や欠損がドローダウンとして現れる）。
                mark = position.entry_price
            else:
                mark = float(frames[position.symbol]["close"].iloc[row])
            unrealized += (mark - position.entry_price) * position.quantity - position.entry_commission
            deployed += mark * position.quantity
        equity_values.append(equity + unrealized)
        exposure_values.append(deployed / equity * 100.0 if equity > 0 else 0.0)

    equity_curve = pd.Series(equity_values, index=timeline)
    result = PortfolioResult(
        config=config, initial_equity=config.initial_equity, final_equity=equity,
        trades=trades, equity_curve=equity_curve,
        average_exposure_pct=(sum(exposure_values) / len(exposure_values)) if exposure_values else 0.0,
        circuit_breaker_dates=circuit_breaker_dates,
    )
    logger.info(
        "ポートフォリオ検証完了: 銘柄%d 枠%d trades=%d 最終資金%.2f (初期%.2f) "
        "CAGR=%.2f%% 最大DD=%.2f%% 平均稼働率=%.1f%% ブレーカー発動%d日",
        len(order), config.max_concurrent_positions, len(trades), equity,
        config.initial_equity, result.cagr_pct(), result.max_drawdown_pct(),
        result.average_exposure_pct, result.circuit_breaker_days,
    )
    return result
