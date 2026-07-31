"""市場全体（指数）の乖離率を、銘柄のバーへ日付で突き合わせて付与する。

`backtest/engine.py` はバーを位置インデックス（`iloc[i]`）で走査するため、
指数の系列を別DataFrameとして渡して同じ位置で参照すると、銘柄側に1行でも
欠損があった瞬間に日付がズレる。ズレは例外にならず「未来の指数値を見て
判定する」（＝ルックアヘッド）という形で成績を良く見せるだけなので、
**必ず日付でマージしてから走査する**。そのための前処理をここに閉じ込め、
エンジン側は付与済みの列を読むだけにしている。

指数の移動平均期間をパラメータグリッドの軸にしていないのは意図的である。
軸を1つ増やすと組合せ数が倍々に増えて実行時間を食ううえ、市場のレジームを
測る物差しまで学習期間に合わせて選ぶと過剰最適化の余地が広がるため。
"""

import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

MARKET_DEVIATION_COLUMN: str = "market_deviation_pct"
_DATE_COLUMN: str = "date"
_CLOSE_COLUMN: str = "close"

DEFAULT_MARKET_MA_WINDOW: int = 30


def compute_market_deviation(
    market_df: pd.DataFrame, ma_window: int = DEFAULT_MARKET_MA_WINDOW,
) -> pd.DataFrame:
    """指数のバーから「日付 -> 乖離率(%)」のDataFrameを作る。

    移動平均が確定しない先頭 ma_window-1 本はNaNになる（＝フィルター条件を
    満たさない扱い。`strategy.pullback.passes_market_filter` 参照）。
    """
    if ma_window <= 0:
        raise ValueError("ma_window は正の整数である必要があります。")
    for column in (_DATE_COLUMN, _CLOSE_COLUMN):
        if column not in market_df.columns:
            raise ValueError(
                f"指数のバーには '{column}' 列が必要です（日付で突き合わせるため）。"
            )

    closes = market_df[_CLOSE_COLUMN].astype(float)
    # ここは1回だけの前処理なので、素直にrolling()で全期間を計算してよい
    # （エンジンのホットループとは違い、実行時間に効かない）。
    moving_average = closes.rolling(ma_window).mean()
    deviation = (closes - moving_average) / moving_average * 100.0

    return pd.DataFrame({
        _DATE_COLUMN: pd.to_datetime(market_df[_DATE_COLUMN]),
        MARKET_DEVIATION_COLUMN: deviation,
    })


def attach_market_deviation(
    df: pd.DataFrame,
    market_df: pd.DataFrame,
    ma_window: int = DEFAULT_MARKET_MA_WINDOW,
    symbol: Optional[str] = None,
) -> pd.DataFrame:
    """銘柄のバーに指数の乖離率の列を日付で突き合わせて付与した複製を返す。"""
    if _DATE_COLUMN not in df.columns:
        raise ValueError(
            "市場フィルターを使うバーには 'date' 列が必要です。"
            "位置インデックスで突き合わせると日付がズレてもエラーにならず、"
            "未来の指数値を参照してしまうため。"
        )

    deviation_df = compute_market_deviation(market_df, ma_window)

    merged = df.copy()
    merged[_DATE_COLUMN] = pd.to_datetime(merged[_DATE_COLUMN])
    merged = merged.merge(deviation_df, on=_DATE_COLUMN, how="left")

    missing = int(merged[MARKET_DEVIATION_COLUMN].isna().sum())
    if missing:
        # 突き合わない日は「フィルター条件を満たさない」扱いになりエントリーが
        # 消える。取引所が同じなら先頭のma_window-1本だけのはずなので、
        # それを大きく超える場合はデータ源のズレ（期間・営業日）を疑うこと。
        logger.info(
            "[%s] 指数の乖離率が突き合わなかったバー: %d/%d本"
            "（先頭%d本は移動平均が確定しないため必ず含まれる）。",
            symbol or "-", missing, len(merged), ma_window - 1,
        )
    return merged
