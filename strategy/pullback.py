"""市場のパニック（プルバック）を検知する売買シグナル判定ロジック。

直近終値が短期移動平均から一定割合以上下方乖離した場合に「買いシグナル」を出す。

さらに、市場全体（指数）の乖離率を併せて渡すと、以下の追加条件を課せる。
いずれも既定は無効（None）で、有効化するかどうかはウォークフォワード検証で
決めること。有効化した設定をライブへ持ち込む前に、必ず検証を通す。

- レジームフィルター (`min_deviation_pct`): 指数が下げすぎているときは買わない
- パニックフィルター (`max_deviation_pct`): 指数が下げているときだけ買う（逆張り）
- 相対乖離 (`relative_threshold_pct`): 指数の下げでは説明できない、その銘柄
  固有の下げだけを買う

前2つは同じ軸の符号違いであり、どちらが正しいかを事前に決める必要はない。
グリッドに両方を入れてウォークフォワードに選ばせるのが本来の使い方である。
"""

import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketFilterConfig:
    """市場全体の状況によるエントリーの追加条件。

    すべてNoneなら何も課さない（＝従来どおりの絶対乖離だけで判定する）。
    """

    # 指数の乖離率がこの値以上のときだけ買う（例: -3.0 なら「指数が-3%より
    # 深く下げているときは買わない」）。下降レジームを避ける向きのフィルター。
    min_deviation_pct: Optional[float] = None
    # 指数の乖離率がこの値以下のときだけ買う（例: -1.0 なら「指数が-1%以上
    # 下げているときだけ買う」）。市場のパニックを待つ向きのフィルター。
    max_deviation_pct: Optional[float] = None
    # 個別銘柄の乖離率が指数の乖離率をこの幅以上下回っているときだけ買う
    # （例: 3.0 なら「指数より3%以上余計に売られている」）。市場全体の下げと
    # 銘柄固有の下げを分離するための条件で、上2つとは独立した軸。
    relative_threshold_pct: Optional[float] = None

    @property
    def is_enabled(self) -> bool:
        return any(
            value is not None
            for value in (
                self.min_deviation_pct, self.max_deviation_pct, self.relative_threshold_pct,
            )
        )


@dataclass
class SignalResult:
    symbol: str
    should_buy: bool
    latest_close: float
    moving_average: float
    deviation_pct: float
    # 指数の乖離率と、そこからの相対乖離（個別 - 指数）。
    # 市場フィルターを使わない場合はNone。
    market_deviation_pct: Optional[float] = None
    relative_deviation_pct: Optional[float] = None


def compute_deviation_pct(closes: pd.Series, ma_window: int) -> float:
    """直近終値の、直近ma_window本の移動平均からの乖離率(%)を返す。

    使うのは最新バー時点の移動平均だけなので、直近ma_window本の平均を直接取る。
    DataFrameを複製して全期間のrolling().mean()を計算しても結果は同じだが、
    バックテストはこの計算をバーごと・グリッドの組合せごとに行うため
    （42銘柄・10年で3000万回規模）、そこが実行時間の大半を占めてしまう。
    """
    latest_close: float = float(closes.iloc[-1])
    moving_average: float = float(closes.iloc[-ma_window:].mean())
    return (latest_close - moving_average) / moving_average * 100.0


def passes_market_filter(
    deviation_pct: float,
    market_deviation_pct: Optional[float],
    config: MarketFilterConfig,
) -> bool:
    """市場全体の状況による追加条件を満たすかを返す。

    指数の乖離率が取得できていない場合（market_deviation_pct=None）は
    条件を満たさないものとして扱う。分からないものを有利側に倒すと、
    フィルターが実質無効になっているのに気付けないため。
    """
    if not config.is_enabled:
        return True

    if market_deviation_pct is None:
        return False

    if config.min_deviation_pct is not None and market_deviation_pct < config.min_deviation_pct:
        return False

    if config.max_deviation_pct is not None and market_deviation_pct > config.max_deviation_pct:
        return False

    if config.relative_threshold_pct is not None:
        relative = deviation_pct - market_deviation_pct
        if relative > -config.relative_threshold_pct:
            return False

    return True


def detect_pullback_signal(
    symbol: str,
    df: pd.DataFrame,
    ma_window: int = 20,
    threshold_pct: float = 5.0,
    market_deviation_pct: Optional[float] = None,
    market_filter: Optional[MarketFilterConfig] = None,
) -> SignalResult:
    if len(df) < ma_window:
        raise ValueError(
            f"移動平均ウィンドウ({ma_window})に対してデータ点数が不足しています: {len(df)}"
        )

    closes = df["close"]
    latest_close: float = float(closes.iloc[-1])
    moving_average: float = float(closes.iloc[-ma_window:].mean())
    deviation_pct: float = (latest_close - moving_average) / moving_average * 100.0

    should_buy: bool = deviation_pct <= -threshold_pct

    relative_deviation_pct: Optional[float] = (
        None if market_deviation_pct is None else deviation_pct - market_deviation_pct
    )

    if should_buy and market_filter is not None and market_filter.is_enabled:
        if not passes_market_filter(deviation_pct, market_deviation_pct, market_filter):
            logger.info(
                "[%s] 乖離率=%.2f%%だが市場フィルターで見送りました"
                "（指数の乖離率=%s 相対乖離=%s）。",
                symbol, deviation_pct,
                "N/A" if market_deviation_pct is None else f"{market_deviation_pct:.2f}%",
                "N/A" if relative_deviation_pct is None else f"{relative_deviation_pct:.2f}%",
            )
            should_buy = False

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
        market_deviation_pct=market_deviation_pct,
        relative_deviation_pct=relative_deviation_pct,
    )
