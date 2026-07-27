"""scripts/export_tax_report.py の単体テスト。"""

import csv
import os
from datetime import datetime
from unittest.mock import patch

import pytest

from core.market_hours import JST
from execution.trade_journal import TradeJournal, TradeRecord
from scripts.export_tax_report import _default_tax_year, main


def _make_trade(symbol: str, closed_at: str, **overrides) -> TradeRecord:
    defaults = dict(
        symbol=symbol, entry_price=100.0, exit_price=110.0, quantity=10,
        reason="TAKE_PROFIT", pnl=100.0, pnl_pct=10.0, r_multiple=2.0,
        closed_at=closed_at, commission=1.0, usd_jpy_rate=150.0,
        entry_date="2026-03-01T14:30:00+00:00",
    )
    defaults.update(overrides)
    return TradeRecord(**defaults)


@pytest.fixture
def journal_with_trades(tmp_path):
    path = str(tmp_path / "trade_journal.csv")
    journal = TradeJournal(path)
    journal._append_to_file(_make_trade("AAPL", "2026-05-20T15:00:00+00:00"))
    journal._append_to_file(_make_trade("MSFT", "2025-11-10T15:00:00+00:00"))
    return path


def _read_rows(path: str) -> list:
    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _run(args: list) -> int:
    with patch("sys.argv", ["export_tax_report", *args]):
        return main()


# --- 既定の対象年 ---------------------------------------------------------------


def test_default_tax_year_is_previous_year() -> None:
    # 確定申告は前年分を申告する
    assert _default_tax_year(datetime(2026, 3, 15, tzinfo=JST)) == 2025
    assert _default_tax_year(datetime(2026, 12, 31, tzinfo=JST)) == 2025


# --- 出力 -----------------------------------------------------------------------


def test_exports_only_the_requested_year(journal_with_trades, tmp_path) -> None:
    output = str(tmp_path / "out.csv")

    exit_code = _run(["--journal", journal_with_trades, "--year", "2026", "--output", output])

    assert exit_code == 0
    rows = _read_rows(output)
    assert [row["symbol"] for row in rows] == ["AAPL"]


def test_all_years_exports_every_trade(journal_with_trades, tmp_path) -> None:
    output = str(tmp_path / "out.csv")

    exit_code = _run(["--journal", journal_with_trades, "--all-years", "--output", output])

    assert exit_code == 0
    rows = _read_rows(output)
    assert {row["symbol"] for row in rows} == {"AAPL", "MSFT"}


def test_output_contains_yen_converted_pnl(journal_with_trades, tmp_path) -> None:
    output = str(tmp_path / "out.csv")

    _run(["--journal", journal_with_trades, "--year", "2026", "--output", output])

    row = _read_rows(output)[0]
    # 手数料控除後 (100.0 - 1.0) を決済時レート150.0で円換算
    assert float(row["pnl_usd"]) == pytest.approx(99.0)
    assert float(row["pnl_jpy"]) == pytest.approx(14850.0)


def test_default_output_path_is_derived_from_year(journal_with_trades, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    exit_code = _run(["--journal", journal_with_trades, "--year", "2026"])

    assert exit_code == 0
    assert os.path.exists(tmp_path / "logs" / "tax_report_2026.csv")


def test_creates_output_directory_when_missing(journal_with_trades, tmp_path) -> None:
    output = str(tmp_path / "nested" / "dir" / "out.csv")

    exit_code = _run(["--journal", journal_with_trades, "--year", "2026", "--output", output])

    assert exit_code == 0
    assert os.path.exists(output)


# --- 異常系 ---------------------------------------------------------------------


def test_missing_journal_returns_error(tmp_path) -> None:
    exit_code = _run(["--journal", str(tmp_path / "missing.csv"), "--year", "2026"])

    assert exit_code == 1


def test_empty_journal_returns_error(tmp_path) -> None:
    path = str(tmp_path / "empty.csv")
    with open(path, "w", encoding="utf-8") as f:
        f.write("")

    exit_code = _run(["--journal", path, "--year", "2026"])

    assert exit_code == 1


def test_year_and_all_years_are_mutually_exclusive(journal_with_trades) -> None:
    exit_code = _run(["--journal", journal_with_trades, "--year", "2026", "--all-years"])

    assert exit_code == 2


def test_year_without_matching_trades_still_succeeds(journal_with_trades, tmp_path) -> None:
    output = str(tmp_path / "out.csv")

    exit_code = _run(["--journal", journal_with_trades, "--year", "2020", "--output", output])

    # ヘッダーのみのCSVを出力して正常終了する（税理士に「該当なし」を示せる）
    assert exit_code == 0
    assert _read_rows(output) == []


def test_warns_when_entry_date_is_unknown(journal_with_trades, tmp_path, caplog) -> None:
    # ブローカー同期で発見した未追跡ポジション由来の決済は取得日が不明
    journal = TradeJournal(journal_with_trades)
    journal._append_to_file(
        _make_trade("GOOG", "2026-06-01T15:00:00+00:00", entry_date=None)
    )
    output = str(tmp_path / "out.csv")

    with caplog.at_level("WARNING"):
        _run(["--journal", journal_with_trades, "--year", "2026", "--output", output])

    assert "取得年月日" in caplog.text
