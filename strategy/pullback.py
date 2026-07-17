"""市場のパニック（プルバック）を検知する売買シグナル判定ロジック。

直近終値が短期移動平均から一定割合以上下方乖離した場合に「買いシグナル」を出す。
"""

import logging
from dataclasses import dataclass

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class SignalResult:
    symbol: str
    should_buy: bool
    latest_close: float
    moving_average: float
    deviation_pct: float


def detect_pullback_signal(
    symbol: str,
    df: pd.DataFrame,
    ma_window: int = 20,
    threshold_pct: float = 5.0,
) -> SignalResult:
    if len(df) < ma_window:
        raise ValueError(
            f"移動平均ウィンドウ({ma_window})に対してデータ点数が不足しています: {len(df)}"
        )

    working_df = df.copy()
    working_df["ma"] = working_df["close"].rolling(window=ma_window).mean()

    latest_close: float = float(working_df["close"].iloc[-1])
    moving_average: float = float(working_df["ma"].iloc[-1])
    deviation_pct: float = (latest_close - moving_average) / moving_average * 100.0

    should_buy: bool = deviation_pct <= -threshold_pct

    logger.info(
        "[%s] 終値=%.2f MA(%d)=%.2f 乖離率=%.2f%% シグナル=%s",
        symbol, latest_close, ma_window, moving_average, deviation_pct,
        "BUY" if should_buy else "NONE",
    )

    return SignalResult(
        symbol=symbol,
        should_buy=should_buy,
        latest_close=latest_close,
        moving_average=moving_average,
        deviation_pct=deviation_pct,
    )
