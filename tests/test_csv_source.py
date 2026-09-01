"""backtest/csv_source.py の単体テスト。"""

import pandas as pd
import pytest

from backtest.csv_source import load_bars_from_csv
from backtest.engine import BacktestConfig, run_backtest


def _write_csv(tmp_path, name: str, content: str) -> str:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_raises_when_file_missing(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        load_bars_from_csv(str(tmp_path / "missing.csv"))


def test_raises_when_csv_has_no_rows(tmp_path) -> None:
    path = _write_csv(tmp_path, "empty.csv", "Date,Close\n")

    with pytest.raises(ValueError):
        load_bars_from_csv(path)


def test_raises_when_close_column_missing(tmp_path) -> None:
    path = _write_csv(tmp_path, "no_close.csv", "Date,Open\n2026-01-05,100\n")

    with pytest.raises(ValueError):
        load_bars_from_csv(path)


def test_loads_yfinance_style_csv(tmp_path) -> None:
    path = _write_csv(
        tmp_path, "AAPL.csv",
        "Date,Open,High,Low,Close,Adj Close,Volume\n"
        "2026-01-05,100,101,99,100.5,100.5,1000\n"
        "2026-01-06,100,102,99,101.5,101.5,1200\n",
    )

    df = load_bars_from_csv(path)

    assert list(df["close"]) == [100.5, 101.5]
    assert pd.api.types.is_datetime64_any_dtype(df["date"])


def test_falls_back_to_adj_close_when_close_missing(tmp_path) -> None:
    path = _write_csv(
        tmp_path, "adj.csv",
        "Date,Adj Close\n2026-01-05,100.5\n2026-01-06,101.5\n",
    )

    df = load_bars_from_csv(path)

    assert list(df["close"]) == [100.5, 101.5]


def test_explicit_price_column_is_used(tmp_path) -> None:
    path = _write_csv(
        tmp_path, "pick.csv",
        "Date,Close,Adj Close\n2026-01-05,100.0,90.0\n2026-01-06,101.0,91.0\n",
    )

    df = load_bars_from_csv(path, price_column="Adj Close")

    assert list(df["close"]) == [90.0, 91.0]


def test_raises_when_explicit_price_column_missing(tmp_path) -> None:
    path = _write_csv(tmp_path, "pick.csv", "Date,Close\n2026-01-05,100.0\n")

    with pytest.raises(ValueError):
        load_bars_from_csv(path, price_column="VWAP")


def test_rows_are_sorted_by_date(tmp_path) -> None:
    path = _write_csv(
        tmp_path, "unsorted.csv",
        "Date,Close\n2026-01-07,102\n2026-01-05,100\n2026-01-06,101\n",
    )

    df = load_bars_from_csv(path)

    assert list(df["close"]) == [100.0, 101.0, 102.0]
    assert list(df.index) == [0, 1, 2]


def test_drops_rows_with_missing_or_non_positive_close(tmp_path) -> None:
    path = _write_csv(
        tmp_path, "gaps.csv",
        "Date,Close\n2026-01-05,100\n2026-01-06,\n2026-01-07,0\n2026-01-08,101\n",
    )

    df = load_bars_from_csv(path)

    assert list(df["close"]) == [100.0, 101.0]


def test_drops_rows_with_unparsable_date(tmp_path) -> None:
    path = _write_csv(
        tmp_path, "baddate.csv",
        "Date,Close\n2026-01-05,100\nN/A,999\n2026-01-06,101\n",
    )

    df = load_bars_from_csv(path)

    assert list(df["close"]) == [100.0, 101.0]


def test_raises_when_no_valid_rows_remain(tmp_path) -> None:
    path = _write_csv(tmp_path, "junk.csv", "Date,Close\n2026-01-05,\n2026-01-06,abc\n")

    with pytest.raises(ValueError):
        load_bars_from_csv(path)


def test_loaded_df_is_directly_usable_by_backtest_engine(tmp_path) -> None:
    """CSV経由でもIBKR経由と同じエンジンで検証できること（オフライン検証の要）。"""
    closes = [100.0] * 5 + [90.0, 100.0]
    dates = pd.date_range("2026-01-05", periods=len(closes), freq="D")
    rows = "".join(f"{d.date()},{c}\n" for d, c in zip(dates, closes, strict=True))
    path = _write_csv(tmp_path, "SYNTH.csv", "Date,Close\n" + rows)

    df = load_bars_from_csv(path)
    result = run_backtest("SYNTH", df, BacktestConfig(ma_window=5))

    assert len(result.trades) == 1
    assert result.trades[0].entry_date == pd.Timestamp("2026-01-10")


def test_intraday_timestamps_are_normalised_to_us_eastern(tmp_path) -> None:
    """夏時間の境界をまたぐ日中足を、環境によらず同じように読めること。

    IBKRの日中足は取引所のローカル時刻＋オフセットで返るため、1年ぶんの
    CSVには `-04:00`(EDT) と `-05:00`(EST) が混在する。**この扱いはpandasの
    バージョンで変わり**、2.2.2 は object 型で通し、3.0.5 は
    `ValueError: Mixed timezones detected` で落ちる（2026-08-13に、同じCSVが
    Macでは読めてVPSでは落ちる状態を実測）。環境で結果が変わる検証は
    信用できないため、ここで東部時間へ揃える。
    """
    path = tmp_path / "INTRADAY.csv"
    path.write_text(
        "date,close\n"
        "2025-11-01 09:30:00-04:00,100.0\n"   # 夏時間
        "2025-11-05 09:30:00-05:00,101.0\n"   # 冬時間
    )

    df = load_bars_from_csv(str(path))

    assert str(df["date"].dt.tz) == "America/New_York"
    # 取引日の区切り（同日中の再エントリー禁止・大引け決済）が東部時間で取れること。
    assert [d.date().isoformat() for d in df["date"]] == ["2025-11-01", "2025-11-05"]


def test_daily_dates_without_a_timezone_are_left_naive(tmp_path) -> None:
    """タイムゾーンを持たない日足は変換しないこと。

    UTCとして解釈してから東部時間へ変換すると、日付が1日前へずれる
    （UTC 00:00 = 前日19:00 ET）。
    """
    path = tmp_path / "DAILY.csv"
    path.write_text("date,close\n2026-01-05,100.0\n2026-01-06,101.0\n")

    df = load_bars_from_csv(str(path))

    assert df["date"].dt.tz is None
    assert [d.date().isoformat() for d in df["date"]] == ["2026-01-05", "2026-01-06"]
