"""プルバック戦略のヒストリカルバックテストエンジン。

main.py（ライブ実行）と同じシグナル判定・ポジションサイジング関数
（strategy/pullback.py, strategy/exit_signal.py, execution/position_sizing.py）
をそのまま再利用してバー単位でシミュレーションする。ライブ用ロジックと
バックテスト用ロジックが乖離する（ロジックドリフト）ことを防ぐための設計。
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd

from execution.position_sizing import calculate_position_size
from strategy.exit_signal import detect_exit_signal
from strategy.pullback import detect_pullback_signal

logger = logging.getLogger(__name__)


@dataclass
class BacktestConfig:
    ma_window: int = 20
    threshold_pct: float = 5.0
    take_profit_pct: float = 10.0
    stop_loss_pct: float = 5.0
    trailing_stop_pct: float = 5.0
    risk_per_trade_pct: float = 1.0
    initial_equity: float = 100_000.0


@dataclass
class Trade:
    symbol: str
    entry_date: object
    entry_price: float
    exit_date: object
    exit_price: float
    quantity: int
    reason: str
    pnl: float
    pnl_pct: float


@dataclass
class BacktestResult:
    symbol: str
    config: BacktestConfig
    initial_equity: float
    final_equity: float
    trades: List[Trade] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=pd.Series)


def run_backtest(symbol: str, df: pd.DataFrame, config: Optional[BacktestConfig] = None) -> BacktestResult:
    config = config or BacktestConfig()

    if df.empty:
        raise ValueError("df が空です。")
    if "close" not in df.columns:
        raise ValueError("df には 'close' 列が必要です。")

    dates = df["date"] if "date" in df.columns else pd.Series(range(len(df)))

    equity: float = config.initial_equity
    position_qty: int = 0
    entry_price: float = 0.0
    entry_date = None
    highest_price: float = 0.0

    trades: List[Trade] = []
    equity_curve_values: List[float] = []

    for i in range(len(df)):
        close_price = float(df["close"].iloc[i])
        is_last_bar = i == len(df) - 1

        if position_qty == 0:
            if i + 1 >= config.ma_window:
                window_df = df.iloc[max(0, i + 1 - config.ma_window): i + 1]
                signal = detect_pullback_signal(
                    symbol, window_df, ma_window=config.ma_window, threshold_pct=config.threshold_pct,
                )
                if signal.should_buy:
                    quantity = calculate_position_size(
                        account_equity=equity,
                        entry_price=close_price,
                        stop_loss_pct=config.stop_loss_pct,
                        risk_per_trade_pct=config.risk_per_trade_pct,
                    )
                    if quantity > 0:
                        position_qty = quantity
                        entry_price = close_price
                        entry_date = dates.iloc[i]
                        highest_price = close_price

            equity_curve_values.append(equity)
        else:
            highest_price = max(highest_price, close_price)
            result = detect_exit_signal(
                symbol,
                entry_price=entry_price,
                current_price=close_price,
                highest_price_since_entry=highest_price,
                take_profit_pct=config.take_profit_pct,
                stop_loss_pct=config.stop_loss_pct,
                trailing_stop_pct=config.trailing_stop_pct,
            )

            if result.should_sell or is_last_bar:
                reason = result.reason if result.should_sell else "END_OF_DATA"
                pnl = (close_price - entry_price) * position_qty
                pnl_pct = (close_price - entry_price) / entry_price * 100.0
                equity += pnl
                trades.append(
                    Trade(
                        symbol=symbol,
                        entry_date=entry_date,
                        entry_price=entry_price,
                        exit_date=dates.iloc[i],
                        exit_price=close_price,
                        quantity=position_qty,
                        reason=reason,
                        pnl=pnl,
                        pnl_pct=pnl_pct,
                    )
                )
                position_qty = 0
                entry_price = 0.0
                entry_date = None
                highest_price = 0.0
                equity_curve_values.append(equity)
            else:
                unrealized_pnl = (close_price - entry_price) * position_qty
                equity_curve_values.append(equity + unrealized_pnl)

    equity_curve = pd.Series(equity_curve_values, index=dates.iloc[: len(equity_curve_values)].values)

    logger.info(
        "[%s] バックテスト完了: trades=%d final_equity=%.2f (初期%.2f)",
        symbol, len(trades), equity, config.initial_equity,
    )

    return BacktestResult(
        symbol=symbol,
        config=config,
        initial_equity=config.initial_equity,
        final_equity=equity,
        trades=trades,
        equity_curve=equity_curve,
    )
