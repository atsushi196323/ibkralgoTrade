"""execution/trade_journal.py の単体テスト。"""

import math
import os
from datetime import date

import pytest

from execution.trade_journal import TradeJournal, TradeRecord, summarize_trade_records


@pytest.fixture
def journal_path(tmp_path) -> str:
    return str(tmp_path / "trades.csv")


# --- record_trade / load_trades --------------------------------------------------


def test_record_trade_creates_file_with_header(journal_path) -> None:
    journal = TradeJournal(journal_path)

    journal.record_trade(
        symbol="AAPL", entry_price=100.0, exit_price=110.0, quantity=10,
        reason="TAKE_PROFIT", pnl=100.0, pnl_pct=10.0, r_multiple=2.0,
    )

    assert os.path.exists(journal_path)
    trades = journal.load_trades()
    assert len(trades) == 1
    assert trades[0].symbol == "AAPL"
    assert trades[0].pnl == pytest.approx(100.0)
    assert trades[0].r_multiple == pytest.approx(2.0)


def test_record_trade_appends_without_overwriting(journal_path) -> None:
    journal = TradeJournal(journal_path)

    journal.record_trade(
        symbol="AAPL", entry_price=100.0, exit_price=110.0, quantity=10,
        reason="TAKE_PROFIT", pnl=100.0, pnl_pct=10.0, r_multiple=2.0,
    )
    journal.record_trade(
        symbol="MSFT", entry_price=200.0, exit_price=190.0, quantity=5,
        reason="STOP_LOSS", pnl=-50.0, pnl_pct=-5.0, r_multiple=-1.0,
    )

    trades = journal.load_trades()
    assert len(trades) == 2
    assert [t.symbol for t in trades] == ["AAPL", "MSFT"]


def test_record_trade_persists_none_r_multiple(journal_path) -> None:
    journal = TradeJournal(journal_path)

    journal.record_trade(
        symbol="AAPL", entry_price=100.0, exit_price=110.0, quantity=10,
        reason="TAKE_PROFIT", pnl=100.0, pnl_pct=10.0, r_multiple=None,
    )

    trades = journal.load_trades()
    assert trades[0].r_multiple is None


def test_record_trade_persists_commission_and_usd_jpy_rate(journal_path) -> None:
    journal = TradeJournal(journal_path)

    journal.record_trade(
        symbol="AAPL", entry_price=100.0, exit_price=110.0, quantity=10,
        reason="TAKE_PROFIT", pnl=100.0, pnl_pct=10.0, r_multiple=2.0,
        commission=1.5, usd_jpy_rate=150.0,
    )

    trade = journal.load_trades()[0]
    assert trade.commission == pytest.approx(1.5)
    assert trade.usd_jpy_rate == pytest.approx(150.0)
    # net_pnl_usd = pnl(100.0) - commission(1.5)
    assert trade.net_pnl_usd == pytest.approx(98.5)
    # net_pnl_jpy = net_pnl_usd(98.5) * usd_jpy_rate(150.0)
    assert trade.net_pnl_jpy == pytest.approx(98.5 * 150.0)


def test_record_trade_persists_entry_date(journal_path) -> None:
    journal = TradeJournal(journal_path)

    journal.record_trade(
        symbol="AAPL", entry_price=100.0, exit_price=110.0, quantity=10,
        reason="TAKE_PROFIT", pnl=100.0, pnl_pct=10.0, r_multiple=2.0,
        entry_date="2026-01-05T00:00:00+00:00",
    )

    trade = journal.load_trades()[0]
    assert trade.entry_date == "2026-01-05T00:00:00+00:00"


def test_record_trade_defaults_commission_and_rate_when_omitted(journal_path) -> None:
    journal = TradeJournal(journal_path)

    journal.record_trade(
        symbol="AAPL", entry_price=100.0, exit_price=110.0, quantity=10,
        reason="TAKE_PROFIT", pnl=100.0, pnl_pct=10.0, r_multiple=2.0,
    )

    trade = journal.load_trades()[0]
    assert trade.commission == pytest.approx(0.0)
    assert trade.usd_jpy_rate is None
    assert trade.net_pnl_jpy is None


def test_load_trades_falls_back_when_reading_legacy_csv_without_new_columns(journal_path) -> None:
    # commission/usd_jpy_rate列を持たない旧フォーマットのCSVでも読めること
    with open(journal_path, "w", newline="", encoding="utf-8") as f:
        f.write("symbol,entry_price,exit_price,quantity,reason,pnl,pnl_pct,r_multiple,closed_at\n")
        f.write("AAPL,100.0,110.0,10,TAKE_PROFIT,100.0,10.0,2.0,2026-01-01T00:00:00+00:00\n")

    journal = TradeJournal(journal_path)
    trade = journal.load_trades()[0]

    assert trade.commission == pytest.approx(0.0)
    assert trade.usd_jpy_rate is None


def test_load_trades_returns_empty_list_when_file_missing(journal_path) -> None:
    journal = TradeJournal(journal_path)

    assert journal.load_trades() == []


def test_record_trade_creates_parent_directory(tmp_path) -> None:
    nested_path = str(tmp_path / "nested" / "dir" / "trades.csv")
    journal = TradeJournal(nested_path)

    journal.record_trade(
        symbol="AAPL", entry_price=100.0, exit_price=110.0, quantity=10,
        reason="TAKE_PROFIT", pnl=100.0, pnl_pct=10.0, r_multiple=2.0,
    )

    assert os.path.exists(nested_path)


# --- compute_stats / summarize_trade_records --------------------------------------


def test_compute_stats_no_trades(journal_path) -> None:
    journal = TradeJournal(journal_path)

    stats = journal.compute_stats()

    assert stats.num_trades == 0
    assert stats.win_rate_pct == 0.0
    assert stats.total_pnl == 0.0
    assert stats.profit_factor == 0.0
    assert stats.avg_r_multiple is None


def test_compute_stats_mixed_wins_and_losses(journal_path) -> None:
    journal = TradeJournal(journal_path)
    journal.record_trade(
        symbol="AAPL", entry_price=100.0, exit_price=110.0, quantity=10,
        reason="TAKE_PROFIT", pnl=100.0, pnl_pct=10.0, r_multiple=2.0,
    )
    journal.record_trade(
        symbol="MSFT", entry_price=200.0, exit_price=190.0, quantity=5,
        reason="STOP_LOSS", pnl=-50.0, pnl_pct=-5.0, r_multiple=-1.0,
    )

    stats = journal.compute_stats()

    assert stats.num_trades == 2
    assert stats.win_rate_pct == pytest.approx(50.0)
    assert stats.total_pnl == pytest.approx(50.0)
    assert stats.profit_factor == pytest.approx(100.0 / 50.0)
    assert stats.avg_r_multiple == pytest.approx((2.0 + (-1.0)) / 2)


def test_compute_stats_total_pnl_jpy_none_when_no_rate_recorded(journal_path) -> None:
    journal = TradeJournal(journal_path)
    journal.record_trade(
        symbol="AAPL", entry_price=100.0, exit_price=110.0, quantity=10,
        reason="TAKE_PROFIT", pnl=100.0, pnl_pct=10.0, r_multiple=2.0,
    )

    stats = journal.compute_stats()

    assert stats.total_pnl_jpy is None


def test_compute_stats_total_pnl_jpy_sums_only_trades_with_rate(journal_path) -> None:
    journal = TradeJournal(journal_path)
    journal.record_trade(
        symbol="AAPL", entry_price=100.0, exit_price=110.0, quantity=10,
        reason="TAKE_PROFIT", pnl=100.0, pnl_pct=10.0, r_multiple=2.0,
        commission=0.0, usd_jpy_rate=150.0,
    )
    # usd_jpy_rate未記録(旧レコード相当)は円換算合計から除外される
    journal.record_trade(
        symbol="MSFT", entry_price=200.0, exit_price=190.0, quantity=5,
        reason="STOP_LOSS", pnl=-50.0, pnl_pct=-5.0, r_multiple=-1.0,
    )
    journal.record_trade(
        symbol="GOOG", entry_price=100.0, exit_price=105.0, quantity=2,
        reason="TAKE_PROFIT", pnl=10.0, pnl_pct=5.0, r_multiple=1.0,
        commission=1.0, usd_jpy_rate=145.0,
    )

    stats = journal.compute_stats()

    # (100.0 - 0.0) * 150.0 + (10.0 - 1.0) * 145.0
    assert stats.total_pnl_jpy == pytest.approx(100.0 * 150.0 + 9.0 * 145.0)


def test_summarize_trade_records_all_wins_infinite_profit_factor() -> None:
    from execution.trade_journal import TradeRecord

    trades = [
        TradeRecord(
            symbol="AAPL", entry_price=100.0, exit_price=110.0, quantity=10,
            reason="TAKE_PROFIT", pnl=100.0, pnl_pct=10.0, r_multiple=2.0, closed_at="2026-01-01",
        )
    ]

    stats = summarize_trade_records(trades)

    assert stats.profit_factor == math.inf


def test_summarize_trade_records_ignores_none_r_multiple_in_average() -> None:
    from execution.trade_journal import TradeRecord

    trades = [
        TradeRecord(
            symbol="AAPL", entry_price=100.0, exit_price=110.0, quantity=10,
            reason="TAKE_PROFIT", pnl=100.0, pnl_pct=10.0, r_multiple=2.0, closed_at="2026-01-01",
        ),
        TradeRecord(
            symbol="MSFT", entry_price=200.0, exit_price=210.0, quantity=1,
            reason="TAKE_PROFIT", pnl=10.0, pnl_pct=5.0, r_multiple=None, closed_at="2026-01-02",
        ),
    ]

    stats = summarize_trade_records(trades)

    assert stats.avg_r_multiple == pytest.approx(2.0)


def test_summarize_trade_records_is_net_of_commission() -> None:
    """集計は手数料控除後で行うこと。

    グロスでプラス・ネットでマイナスのトレードを勝ちとして数えると、勝率も
    プロフィットファクターも同時に楽観へ寄る。往復手数料が約定代金の1%を
    占める小口座では、利幅の薄いトレードは符号ごと変わる（2026-08-05の実測:
    AMBQ 3株・約定代金$203でグロス -13.18 / 手数料 2.00 / ネット -15.19）。

    `backtest/` の成績はコスト込みで出しているので、ライブ側をグロスで
    集計すると、実運用と検証を比べる指標そのものが別物になる。
    """
    from execution.trade_journal import TradeRecord

    trades = [
        # グロス +0.50 だが往復手数料 0.70 でネットは -0.20（＝負け）。
        TradeRecord(
            symbol="AAPL", entry_price=100.0, exit_price=100.5, quantity=1,
            reason="TAKE_PROFIT", pnl=0.5, pnl_pct=0.5, r_multiple=0.1,
            closed_at="2026-01-01", commission=0.7,
        ),
        TradeRecord(
            symbol="AMBQ", entry_price=67.44, exit_price=63.38, quantity=3,
            reason="STOP_LOSS", pnl=-12.18, pnl_pct=-6.02, r_multiple=-1.2,
            closed_at="2026-01-02", commission=2.0,
        ),
    ]

    stats = summarize_trade_records(trades)

    assert stats.win_rate_pct == 0.0
    assert stats.total_pnl == pytest.approx(-0.2 - 14.18)
    # 勝ちが1件も無いのでプロフィットファクターは0（infではない）。
    assert stats.profit_factor == 0.0


# --- compute_daily_pnl (サーキットブレーカーの判定基準) ----------------------------


def test_compute_daily_pnl_sums_only_trades_closed_on_target_date(journal_path) -> None:
    journal = TradeJournal(journal_path)
    # 米国東部時間で同日となる2件(UTC 15:00, 18:00 は共にET午前〜午後で07-22)
    journal._append_to_file(
        TradeRecord(
            symbol="AAPL", entry_price=100.0, exit_price=110.0, quantity=1,
            reason="TAKE_PROFIT", pnl=10.0, pnl_pct=10.0, r_multiple=2.0,
            closed_at="2026-07-22T15:00:00+00:00",
        )
    )
    journal._append_to_file(
        TradeRecord(
            symbol="MSFT", entry_price=100.0, exit_price=90.0, quantity=1,
            reason="STOP_LOSS", pnl=-4.0, pnl_pct=-4.0, r_multiple=-1.0,
            closed_at="2026-07-22T18:00:00+00:00",
        )
    )
    # 前日分は集計対象外
    journal._append_to_file(
        TradeRecord(
            symbol="GOOG", entry_price=100.0, exit_price=105.0, quantity=1,
            reason="TAKE_PROFIT", pnl=5.0, pnl_pct=5.0, r_multiple=1.0,
            closed_at="2026-07-21T15:00:00+00:00",
        )
    )

    daily_pnl = journal.compute_daily_pnl(reference_date=date(2026, 7, 22))

    assert daily_pnl == pytest.approx(6.0)


def test_compute_daily_pnl_zero_when_no_matching_trades(journal_path) -> None:
    journal = TradeJournal(journal_path)
    journal._append_to_file(
        TradeRecord(
            symbol="GOOG", entry_price=100.0, exit_price=105.0, quantity=1,
            reason="TAKE_PROFIT", pnl=5.0, pnl_pct=5.0, r_multiple=1.0,
            closed_at="2026-07-21T15:00:00+00:00",
        )
    )

    assert journal.compute_daily_pnl(reference_date=date(2026, 7, 22)) == 0.0


def test_compute_daily_pnl_zero_when_journal_empty(journal_path) -> None:
    journal = TradeJournal(journal_path)

    assert journal.compute_daily_pnl(reference_date=date(2026, 7, 22)) == 0.0


def test_compute_daily_pnl_defaults_to_today() -> None:
    # reference_date省略時は例外にならず動作すること（値そのものは現在日時依存のため検証しない）
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        journal = TradeJournal(os.path.join(tmp_dir, "trades.csv"))
        assert journal.compute_daily_pnl() == 0.0


def test_compute_daily_pnl_deducts_commission(journal_path) -> None:
    """サーキットブレーカーはグロスではなく手数料控除後で判定する。

    グロスで判定すると往復手数料の分だけ資金の減りを過小評価し、
    実際には上限を超えてから発動することになる。
    """
    journal = TradeJournal(journal_path)
    journal._append_to_file(
        TradeRecord(
            symbol="AAPL", entry_price=100.0, exit_price=110.0, quantity=1,
            reason="TAKE_PROFIT", pnl=10.0, pnl_pct=10.0, r_multiple=2.0,
            closed_at="2026-07-22T15:00:00+00:00", commission=2.0,
        )
    )
    journal._append_to_file(
        TradeRecord(
            symbol="MSFT", entry_price=100.0, exit_price=90.0, quantity=1,
            reason="STOP_LOSS", pnl=-10.0, pnl_pct=-10.0, r_multiple=-1.0,
            closed_at="2026-07-22T18:00:00+00:00", commission=3.0,
        )
    )

    # グロスなら 10 - 10 = 0 だが、手数料5.0を引いて -5.0
    assert journal.compute_daily_pnl(reference_date=date(2026, 7, 22)) == pytest.approx(-5.0)


def test_compute_daily_pnl_matches_gross_when_no_commission(journal_path) -> None:
    # ドライラン中は実約定が無く手数料0.0固定のため、従来と同じ値になること
    journal = TradeJournal(journal_path)
    journal._append_to_file(
        TradeRecord(
            symbol="AAPL", entry_price=100.0, exit_price=110.0, quantity=1,
            reason="TAKE_PROFIT", pnl=10.0, pnl_pct=10.0, r_multiple=2.0,
            closed_at="2026-07-22T15:00:00+00:00",
        )
    )

    assert journal.compute_daily_pnl(reference_date=date(2026, 7, 22)) == pytest.approx(10.0)
