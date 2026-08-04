"""引け後サマリ(scripts/daily_report.py)のテスト。

ログの解析は「取引日の切り方」と「静かな縮退を拾えるか」の2点が要で、
どちらも壊れても例外にならない（黙って0件のサマリが出るだけ）ため、
実際のログ行と同じ書式の文字列で押さえる。
"""

from datetime import date, timedelta, timezone

from execution.trade_journal import TradeRecord
from scripts.daily_report import (
    build_day_report,
    format_report,
    latest_trading_day,
    parse_log_lines,
)

JST = timezone(timedelta(hours=9))


def _lines(*rows: str):
    return list(parse_log_lines(rows, local_tz=JST))


def test_japan_evening_and_next_morning_belong_to_the_same_trading_day():
    """日本時間の22:30と翌05:00は、米国東部時間では同じ取引日である。

    ローカル日付で切ると寄り付きと引けが別の日の集計に入り、
    「その日いちばん押した乖離率」も分断される。
    """
    lines = _lines(
        "2026-08-03 22:31:00,000 [INFO] strategy.pullback: [KO] 終値=87.59 MA(30)=83.07 乖離率=5.44% シグナル=NONE",
        "2026-08-04 04:57:00,000 [INFO] strategy.pullback: [KO] 終値=86.00 MA(30)=83.07 乖離率=3.53% シグナル=NONE",
    )

    assert [line.trading_day for line in lines] == [date(2026, 8, 3), date(2026, 8, 3)]


def test_lowest_deviation_keeps_the_deepest_pullback_of_the_day():
    lines = _lines(
        "2026-08-03 22:31:00,000 [INFO] strategy.pullback: [KO] 終値=87.59 MA(30)=83.07 乖離率=-1.20% シグナル=NONE",
        "2026-08-03 23:31:00,000 [INFO] strategy.pullback: [KO] 終値=85.00 MA(30)=83.07 乖離率=-4.80% シグナル=NONE",
        "2026-08-04 00:31:00,000 [INFO] strategy.pullback: [KO] 終値=86.00 MA(30)=83.07 乖離率=-2.10% シグナル=NONE",
    )

    report = build_day_report(lines, [], date(2026, 8, 3))

    assert report.lowest_deviation == {"KO": -4.80}
    assert report.signal_evaluations["KO"] == 3


def test_entries_and_exits_are_extracted():
    lines = _lines(
        "2026-08-03 23:00:00,000 [INFO] execution.position_manager: [JOBY] ポジションを新規建てしました: "
        "entry=7.05 qty=34 strategy=swing 待機注文 STP=6.70 LMT=7.76",
        "2026-08-04 01:00:00,000 [INFO] execution.position_manager: [JOBY] ポジションを決済しました。本日中は再エントリーしません。",
    )

    report = build_day_report(lines, [], date(2026, 8, 3))

    assert report.entries == [("JOBY", 7.05)]
    assert report.exits == ["JOBY"]


def test_silent_degradations_are_surfaced():
    """スクリーニングの空応答と株価帯の除外は、例外にならない縮退である。

    この2つが拾えなくなると「なぜ1件も建たなかったのか」が
    サマリから消える（CLAUDE.md「3. 実行環境と設定」）。
    """
    lines = _lines(
        "2026-08-03 22:30:10,000 [WARNING] data.fundamentals: 時価総額スキャンの結果が0件でした: "
        "scan_code=MOST_ACTIVE cap=[2000000000, 200000000000]。マーケットスキャナーの購読権限が無い可能性があります。",
        "2026-08-03 22:30:15,000 [WARNING] __main__: [MSFT] 株価(464.72 USD)が上限(244.00 USD)を超えるため"
        "監視対象から外します（現在の口座資金では数量が0株になる）。",
        "2026-08-03 22:30:16,000 [WARNING] __main__: [JOBY] 株価(5.15 USD)が下限(6.10 USD)を下回るため"
        "監視対象から外します（株数クランプでリスクベースのサイジングが効かない）。",
    )

    report = build_day_report(lines, [], date(2026, 8, 3))

    assert len(report.screening_degraded) == 1
    assert report.excluded_symbols["MSFT"].startswith("上限外")
    assert report.excluded_symbols["JOBY"].startswith("下限外")


def test_entry_skip_reasons_are_counted():
    lines = _lines(
        "2026-08-03 23:00:00,000 [INFO] __main__: [KO] 本日すでに決済済みのため、"
        "新規エントリーをスキップします（当日中の再エントリー禁止）。",
        "2026-08-03 23:05:00,000 [INFO] __main__: [XOM] 同時保有ポジション数の上限(2)に達しているため"
        "新規エントリーをスキップします。",
        "2026-08-03 23:10:00,000 [INFO] __main__: [XOM] 同時保有ポジション数の上限(2)に達しているため"
        "新規エントリーをスキップします。",
    )

    report = build_day_report(lines, [], date(2026, 8, 3))

    assert report.skip_reasons["当日中の再エントリー禁止"] == 1
    assert report.skip_reasons["同時保有数の上限"] == 2


def test_manual_login_hint_and_connection_rounds_are_reported():
    lines = _lines(
        "2026-08-03 23:00:00,000 [ERROR] __main__: TWSへの再接続に失敗しました。300秒後に再試行します。",
        "2026-08-03 23:10:00,000 [ERROR] __main__: TWSへの再接続に失敗しました。300秒後に再試行します。",
        "2026-08-03 23:20:00,000 [ERROR] __main__: 3 ラウンド連続で接続できません。"
        "**IB Gatewayへの再ログインが必要な可能性があります**（2要素認証の期限切れ）。",
    )

    report = build_day_report(lines, [], date(2026, 8, 3))

    assert report.connection_failure_rounds == 2
    assert report.manual_login_hint is True


def test_trades_are_selected_by_eastern_trading_day():
    """ジャーナルの closed_at はUTC。ETへ直してから取引日で選ぶ。"""
    same_day = TradeRecord(
        symbol="KO", entry_price=80.0, exit_price=88.0, quantity=3, reason="TAKE_PROFIT",
        pnl=24.0, pnl_pct=10.0, r_multiple=2.0,
        closed_at="2026-08-04T01:30:00+00:00",  # = 2026-08-03 21:30 ET
        commission=0.7,
    )
    next_day = TradeRecord(
        symbol="XOM", entry_price=150.0, exit_price=142.5, quantity=1, reason="STOP_LOSS",
        pnl=-7.5, pnl_pct=-5.0, r_multiple=-1.0,
        closed_at="2026-08-04T18:00:00+00:00",  # = 2026-08-04 14:00 ET
        commission=0.7,
    )

    report = build_day_report([], [same_day, next_day], date(2026, 8, 3))

    assert [trade.symbol for trade in report.trades] == ["KO"]


def test_zero_commission_trades_are_flagged_as_dry_run():
    """手数料0の記録を実発注の成績として読ませない（CLAUDE.md「9. 禁止事項」）。"""
    dry_run_trade = TradeRecord(
        symbol="KO", entry_price=80.0, exit_price=88.0, quantity=3, reason="TAKE_PROFIT",
        pnl=24.0, pnl_pct=10.0, r_multiple=2.0,
        closed_at="2026-08-04T01:30:00+00:00", commission=0.0,
    )

    report = build_day_report([], [dry_run_trade], date(2026, 8, 3))
    text = format_report(report)

    assert "ドライラン" in text


def test_malformed_lines_are_skipped_without_raising():
    """例外のスタックトレースなど、書式に合わない行が混ざっても落ちない。"""
    lines = _lines(
        "Traceback (most recent call last):",
        '  File "/Users/x/main.py", line 884, in main',
        "2026-08-03 22:31:00,000 [INFO] strategy.pullback: [KO] 終値=87.59 MA(30)=83.07 乖離率=-1.20% シグナル=NONE",
    )

    assert len(lines) == 1
    assert latest_trading_day(lines) == date(2026, 8, 3)


def test_latest_trading_day_is_none_for_an_empty_log():
    assert latest_trading_day([]) is None
