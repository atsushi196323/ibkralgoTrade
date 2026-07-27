"""execution/tax_export.py の単体テスト。"""

import csv

import pytest

from execution.tax_export import build_tax_export_rows, export_tax_report_csv
from execution.trade_journal import TradeRecord


def _trade(
    symbol: str,
    entry_price: float,
    exit_price: float,
    quantity: int,
    closed_at: str,
    entry_date=None,
    commission: float = 0.0,
    usd_jpy_rate=None,
) -> TradeRecord:
    return TradeRecord(
        symbol=symbol,
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=quantity,
        reason="TAKE_PROFIT",
        pnl=(exit_price - entry_price) * quantity,
        pnl_pct=(exit_price - entry_price) / entry_price * 100.0,
        r_multiple=None,
        closed_at=closed_at,
        commission=commission,
        usd_jpy_rate=usd_jpy_rate,
        entry_date=entry_date,
    )


# --- build_tax_export_rows --------------------------------------------------


def test_build_rows_computes_cost_and_proceeds_in_usd() -> None:
    trades = [
        _trade(
            "AAPL", entry_price=100.0, exit_price=110.0, quantity=10,
            closed_at="2026-03-01T00:00:00+00:00",
            entry_date="2026-02-01T00:00:00+00:00",
            commission=1.0, usd_jpy_rate=150.0,
        )
    ]

    rows = build_tax_export_rows(trades)

    assert len(rows) == 1
    row = rows[0]
    assert row.symbol == "AAPL"
    assert row.entry_date == "2026-02-01T00:00:00+00:00"
    assert row.exit_date == "2026-03-01T00:00:00+00:00"
    assert row.cost_usd == pytest.approx(1000.0)
    assert row.proceeds_usd == pytest.approx(1100.0)
    assert row.commission_usd == pytest.approx(1.0)
    assert row.usd_jpy_rate == pytest.approx(150.0)
    # net_pnl_usd = pnl(100.0) - commission(1.0)
    assert row.pnl_usd == pytest.approx(99.0)
    # net_pnl_jpy = net_pnl_usd(99.0) * usd_jpy_rate(150.0)
    assert row.pnl_jpy == pytest.approx(99.0 * 150.0)


def test_build_rows_sorted_by_exit_date_ascending() -> None:
    trades = [
        _trade("MSFT", 200.0, 210.0, 5, closed_at="2026-05-01T00:00:00+00:00"),
        _trade("AAPL", 100.0, 110.0, 10, closed_at="2026-01-01T00:00:00+00:00"),
    ]

    rows = build_tax_export_rows(trades)

    assert [row.symbol for row in rows] == ["AAPL", "MSFT"]


def test_build_rows_filters_by_tax_year_using_jst() -> None:
    trades = [
        # UTC 2025-12-31 15:30 -> JST 2026-01-01 00:30 (JSTでは2026年扱い)
        _trade("AAPL", 100.0, 110.0, 10, closed_at="2025-12-31T15:30:00+00:00"),
        _trade("MSFT", 200.0, 210.0, 5, closed_at="2026-06-01T00:00:00+00:00"),
        _trade("GOOG", 100.0, 105.0, 1, closed_at="2027-01-01T00:00:00+00:00"),
    ]

    rows = build_tax_export_rows(trades, tax_year=2026)

    assert {row.symbol for row in rows} == {"AAPL", "MSFT"}


def test_build_rows_none_when_no_trades() -> None:
    assert build_tax_export_rows([]) == []


def test_build_rows_preserves_none_entry_date_and_usd_jpy_rate() -> None:
    # ブローカー同期で発見した未追跡ポジション由来の決済など、記録が無いケース
    trades = [_trade("AAPL", 100.0, 110.0, 10, closed_at="2026-01-01T00:00:00+00:00")]

    rows = build_tax_export_rows(trades)

    assert rows[0].entry_date is None
    assert rows[0].usd_jpy_rate is None
    assert rows[0].pnl_jpy is None


# --- export_tax_report_csv ---------------------------------------------------


def test_export_writes_expected_header_and_row(tmp_path) -> None:
    trades = [
        _trade(
            "AAPL", entry_price=100.0, exit_price=110.0, quantity=10,
            closed_at="2026-03-01T00:00:00+00:00",
            entry_date="2026-02-01T00:00:00+00:00",
            commission=1.0, usd_jpy_rate=150.0,
        )
    ]
    file_path = str(tmp_path / "tax_report.csv")

    count = export_tax_report_csv(trades, file_path, tax_year=2026)

    assert count == 1
    with open(file_path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    assert len(rows) == 1
    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["entry_date"] == "2026-02-01T00:00:00+00:00"
    assert float(rows[0]["pnl_jpy"]) == pytest.approx(99.0 * 150.0)


def test_export_excludes_trades_outside_requested_tax_year(tmp_path) -> None:
    trades = [
        _trade("AAPL", 100.0, 110.0, 10, closed_at="2025-06-01T00:00:00+00:00"),
        _trade("MSFT", 200.0, 210.0, 5, closed_at="2026-06-01T00:00:00+00:00"),
    ]
    file_path = str(tmp_path / "tax_report.csv")

    count = export_tax_report_csv(trades, file_path, tax_year=2026)

    assert count == 1
    with open(file_path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert rows[0]["symbol"] == "MSFT"


def test_export_returns_zero_and_writes_header_only_when_no_matching_trades(tmp_path) -> None:
    trades = [_trade("AAPL", 100.0, 110.0, 10, closed_at="2025-06-01T00:00:00+00:00")]
    file_path = str(tmp_path / "tax_report.csv")

    count = export_tax_report_csv(trades, file_path, tax_year=2026)

    assert count == 0
    with open(file_path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert rows == []


def test_money_columns_are_rounded_to_avoid_float_artifacts() -> None:
    """二進浮動小数の誤差がそのまま出ると税理士へ渡す書類として体裁が悪い。"""
    trades = [
        TradeRecord(
            symbol="AAPL", entry_price=180.0, exit_price=198.0, quantity=10,
            reason="TAKE_PROFIT", pnl=180.0, pnl_pct=10.0, r_multiple=2.0,
            closed_at="2026-05-20T15:00:00+00:00",
            commission=2.5, usd_jpy_rate=152.3, entry_date="2026-04-02T14:31:00+00:00",
        )
    ]

    row = build_tax_export_rows(trades)[0]

    # 丸めなければ 27033.250000000004 になる
    assert row.pnl_jpy == 27033.25
    assert row.pnl_usd == 177.5
