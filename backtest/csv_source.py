"""CSVファイルからヒストリカルバーを読み込む（IBKR接続を必要としないデータ源）。

`backtest/run.py` の本来のデータ源はIBKRだが、IB Gatewayへログインできない
環境でも検証を進められるよう、外部データ（yfinance・Stooq等が出力するCSV）を
直接読み込む経路を用意する。`backtest.engine.run_backtest` が要求するのは
`close` 列（と任意の `date` 列）だけなので、CSVでもIBKRでも同じエンジンで
検証できる。

注意: IBKRのバーと外部データではバーの調整方法（配当調整の有無等）が異なり、
結果は完全には一致しない。エッジの有無を見る用途には十分だが、
実発注前の最終確認はIBKRのデータで行うこと。

想定するCSV（列名の大文字小文字・前後の空白は無視する）:
    Date,Open,High,Low,Close,Adj Close,Volume   # yfinance
    Date,Open,High,Low,Close,Volume             # Stooq
"""

import logging
import os
from typing import Optional

import pandas as pd

from core.market_hours import US_EASTERN

logger = logging.getLogger(__name__)

_CLOSE_COLUMN: str = "close"
# yfinanceの配当・分割調整済み終値。close列が無い場合の代替として使う。
_ADJ_CLOSE_COLUMN: str = "adj close"
_DATE_COLUMN: str = "date"


# 日中足のタイムスタンプはIBKRが取引所のローカル時刻＋オフセット付きで返すため、
# 1年ぶんのCSVには夏時間の境界をまたいで `-04:00` と `-05:00` が混在する。
_TZ_OFFSET_PATTERN: str = r"(?:[+-]\d{2}:?\d{2}|Z)$"


def _parse_dates(values: pd.Series) -> pd.Series:
    """日付列をパースする。タイムゾーン付きなら米国東部時間へ揃える。

    **混在オフセットの扱いはpandasのバージョンで変わる。** 2.2.2 は
    `object` 型のSeriesとして通し（FutureWarning）、3.0.5 は
    `ValueError: Mixed timezones detected` で落ちる。2026-08-13に、
    同じ日中足CSVがMac(2.2.2)では読めてVPS(3.0.5)では落ちる状態を実測した。
    **環境によって検証が通ったり落ちたりするのは、結果を信用できないのと同じ**
    なので、ここで明示的に揃える。

    東部時間へ変換するのは、`backtest.engine._trading_day_of` が
    「同日中の再エントリー禁止」と「大引け前の強制決済」の区切りに
    この値の日付を使うためである。取引日は東部時間で数える
    （ライブ側のクールダウン・日次サーキットブレーカーと同じ区切り）。

    **タイムゾーンを持たない日足CSVはそのまま扱う。** UTCとして解釈してから
    東部時間へ変換すると、日付が1日前へずれる（UTC 00:00 = 前日19:00 ET）。
    """
    if not values.astype(str).str.contains(_TZ_OFFSET_PATTERN, regex=True, na=False).any():
        return pd.to_datetime(values, errors="coerce")

    parsed = pd.to_datetime(values, errors="coerce", utc=True)
    return parsed.dt.tz_convert(US_EASTERN)


def load_bars_from_csv(path: str, price_column: Optional[str] = None) -> pd.DataFrame:
    """CSVを読み込み、バックテストエンジンが扱える形へ正規化して返す。

    Args:
        path: 読み込むCSVのパス。
        price_column: 終値として使う列名（大文字小文字は無視）。省略時は
            `close`、無ければ `adj close` を使う。

    Returns:
        `close` 列を持ち、`date` 列があれば日付昇順に並べ替えたDataFrame。

    Raises:
        FileNotFoundError: パスが存在しない場合。
        ValueError: 終値の列が見つからない、または有効な行が1件も無い場合。
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"CSVファイルが見つかりません: {path}")

    df = pd.read_csv(path)
    if df.empty:
        raise ValueError(f"CSVにデータ行がありません: {path}")

    df.columns = [str(col).strip().lower() for col in df.columns]

    resolved_column = _resolve_price_column(df, price_column, path)
    if resolved_column != _CLOSE_COLUMN:
        df[_CLOSE_COLUMN] = df[resolved_column]

    df[_CLOSE_COLUMN] = pd.to_numeric(df[_CLOSE_COLUMN], errors="coerce")

    if _DATE_COLUMN in df.columns:
        df[_DATE_COLUMN] = _parse_dates(df[_DATE_COLUMN])
        # 日付が壊れている行は捨てる。並べ替えの基準が欠けると
        # バーの前後関係が崩れ、シグナル判定そのものが無意味になるため。
        df = df.dropna(subset=[_DATE_COLUMN])
        df = df.sort_values(_DATE_COLUMN)

    # 終値が欠測の行（休場日のプレースホルダ等）は移動平均を汚すので除外する。
    df = df.dropna(subset=[_CLOSE_COLUMN])
    df = df[df[_CLOSE_COLUMN] > 0]
    df = df.reset_index(drop=True)

    if df.empty:
        raise ValueError(f"有効な終値を持つ行がCSVに1件もありません: {path}")

    logger.info(
        "CSVからバーを%d件読み込みました: %s (終値の列=%s)",
        len(df), path, resolved_column,
    )
    return df


def _resolve_price_column(df: pd.DataFrame, price_column: Optional[str], path: str) -> str:
    if price_column is not None:
        normalized = price_column.strip().lower()
        if normalized not in df.columns:
            raise ValueError(
                f"指定された列 '{price_column}' がCSVにありません: {path} "
                f"(利用可能な列: {list(df.columns)})"
            )
        return normalized

    if _CLOSE_COLUMN in df.columns:
        return _CLOSE_COLUMN
    if _ADJ_CLOSE_COLUMN in df.columns:
        return _ADJ_CLOSE_COLUMN

    raise ValueError(
        f"終値の列('{_CLOSE_COLUMN}' または '{_ADJ_CLOSE_COLUMN}')がCSVにありません: "
        f"{path} (利用可能な列: {list(df.columns)})"
    )
