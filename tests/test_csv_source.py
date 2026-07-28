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
    rows = "".join(f"{d.date()},{c}\n" for d, c in zip(dates, closes))
    path = _write_csv(tmp_path, "SYNTH.csv", "Date,Close\n" + rows)

    df = load_bars_from_csv(path)
    result = run_backtest("SYNTH", df, BacktestConfig(ma_window=5))

    assert len(result.trades) == 1
    assert result.trades[0].entry_date == pd.Timestamp("2026-01-10")
