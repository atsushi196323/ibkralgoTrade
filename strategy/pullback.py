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

    closes = df["close"]
    latest_close: float = float(closes.iloc[-1])
    # 使うのは最新バー時点の移動平均だけなので、直近ma_window本の平均を直接取る。
    # DataFrameを複製して全期間のrolling().mean()を計算すると結果は同じだが、
    # バックテストはこの関数をバーごと・グリッドの組合せごとに呼ぶため
    # （42銘柄・10年で3000万回規模）、そこが実行時間の大半を占めてしまう。
    moving_average: float = float(closes.iloc[-ma_window:].mean())
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
