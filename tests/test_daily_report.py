"""引け後サマリ(scripts/daily_report.py)のテスト。

ログの解析は「取引日の切り方」と「静かな縮退を拾えるか」の2点が要で、
どちらも壊れても例外にならない（黙って0件のサマリが出るだけ）ため、
実際のログ行と同じ書式の文字列で押さえる。
"""

from datetime import date, datetime, timedelta, timezone

from execution.trade_journal import TradeRecord
from scripts.daily_report import (
    build_day_report,
    format_report,
    last_closed_trading_day,
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


def test_pending_symbols_show_how_far_they_are_from_returning():
    """監視から外れた銘柄の「復帰までの距離」を拾うこと。

    除外は永続化されず毎回やり直されるため（main()はスクリーニングが
    成功した日以外フォールバックのリストを入れ替えない）、これらは
    捨てた銘柄ではなく順番待ちの銘柄である。距離が出ていないと、
    待てば戻るのか当面戻らないのかを運用者が判断できない。

    文字列は main._drop_struggling_symbols_async が実際に出す書式。
    """
    lines = _lines(
        "2026-08-03 22:30:20,000 [INFO] __main__: [SPCX] 日足が36本しかなく長期トレンドを"
        "判定できないため、監視対象から外します（本数が揃うまでスイングの新規建てが"
        "できない）。再エントリーまで残り164営業日。",
        "2026-08-03 22:30:21,000 [INFO] __main__: [RIVN] 終値が200日移動平均を下回る"
        "下降トレンドのため、監視対象から外します。終値15.76 / MA200 16.10"
        "（あと+2.2%で復帰）。",
    )

    report = build_day_report(lines, [], date(2026, 8, 3))

    assert report.pending_symbols["SPCX"] == "本数不足: 日足 36本 → 残り 164営業日"
    assert report.pending_symbols["RIVN"] == (
        "下降トレンド: 終値 15.76 / MA 16.10 → あと +2.2% で復帰"
    )
    assert "再エントリー待ち" in format_report(report)


def test_entry_side_history_warning_is_not_counted_as_pending():
    """エントリー側の本数不足の警告を、再エントリー待ちとして数えないこと。

    似た書式の行が2箇所から出る。ウォッチリストの除外（＝順番待ち）だけを
    拾いたいので、区別できなくなると件数が二重になる。
    """
    lines = _lines(
        "2026-08-03 22:30:22,000 [WARNING] __main__: [SPCX] 日足が36本しかなく"
        "長期トレンド(200本)を判定できないため、スイングの新規建てを見送ります"
        "（上場から日が浅い銘柄では本数が揃うまでエントリーできません）。",
    )

    report = build_day_report(lines, [], date(2026, 8, 3))

    assert report.pending_symbols == {}


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


def test_a_full_position_book_is_not_reported_as_a_dead_monitoring_loop():
    """同時保有数の上限で埋まった日を「監視サイクルが回っていない」と書かないこと。

    エントリー判定は上限に達しているとその場で return するため、乖離率の行が
    1件も出ない。2026-08-14のVPSログ（UPS・INTCで枠が埋まった日）がこれで、
    決済判定は5分ごとに動いていたのにサマリは停止しているように読めた。
    サマリは「なぜ建たなかったのか」を切り分けるための道具なので、
    正常な稼働を故障として報告してはならない。
    """
    lines = _lines(
        "2026-08-14 23:47:19,000 [INFO] strategy.exit_signal: [INTC] entry=102.41 current=104.39 "
        "high=107.31 pnl=1.93% high比乖離=-2.72% reason=NONE",
        "2026-08-14 23:47:20,000 [INFO] __main__: [KO] 同時保有ポジション数の上限(2)に達しているため"
        "新規エントリーをスキップします。",
    )

    report = build_day_report(lines, [], date(2026, 8, 14))
    text = format_report(report)

    assert report.exit_evaluations["INTC"] == 1
    assert "監視サイクルが回っていない" not in text
    assert "決済判定サイクル" in text


def test_a_truly_idle_loop_is_still_reported():
    """判定も見送りも1件も無い日は、従来どおり「回っていない」と書くこと。"""
    lines = _lines(
        "2026-08-14 23:47:19,000 [INFO] __main__: 口座資金(USD)を取得しました: 1205.54",
    )

    text = format_report(build_day_report(lines, [], date(2026, 8, 14)))

    assert "監視サイクルが回っていない" in text


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


# --- 起動しなかった日を見逃さないための判定 ------------------------------------------

# `latest_trading_day` はログに書かれている最新の取引日しか返さないため、
# 起動しなかった日は前日のサマリがそのまま出て正常な日と区別できない。
# 2026-08-06に launchd の起動ジョブが disabled になっていたのを取りこぼした穴。

US_EASTERN_TZ = timezone(timedelta(hours=-4))  # 夏時間のET


def _et(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=US_EASTERN_TZ)


def test_last_closed_trading_day_waits_for_the_close():
    """引け前は当日を『引けた日』に数えないこと。

    数えると、まだ動いている最中のセッションを『ログが無い＝起動していない』と
    誤警告する。
    """
    # 2026-08-06(木) の 15:00 ET はまだ場中。
    assert last_closed_trading_day(_et(2026, 8, 6, 15)) == date(2026, 8, 5)
    assert last_closed_trading_day(_et(2026, 8, 6, 17)) == date(2026, 8, 6)


def test_last_closed_trading_day_skips_weekends():
    """土日は取引日として数えないこと（引け後ジョブは日本時間の朝に走る）。"""
    # 2026-08-08(土) / 09(日) から見た直近の引けた日は 07(金)。
    assert last_closed_trading_day(_et(2026, 8, 8, 17)) == date(2026, 8, 7)
    assert last_closed_trading_day(_et(2026, 8, 9, 17)) == date(2026, 8, 7)


def test_last_closed_trading_day_skips_holidays():
    """休場日を数えないこと。2026-01-01(元日)は休場。"""
    assert last_closed_trading_day(_et(2026, 1, 1, 17)) == date(2025, 12, 31)


def test_the_order_layer_section_reports_a_successful_bracket():
    """成功した注文層の経路もサマリに残すこと。

    ペーパーでの実発注検証は「注文層が正しく動いたか」を見るのが目的なので、
    WARNING/ERROR の集計だけでは何も記録されない日が「正常」と区別できない。
    """
    lines = _lines(
        "2026-08-06 22:31:00,000 [INFO] execution.order_manager: "
        "[INTC] ブラケットの親注文が約定しました: qty=2 fill=101.06 commission=1.00 "
        "損切り=STP@95.34 利確=LMT@111.05 oca=BRACKET_INTC_4535805584",
        "2026-08-06 22:31:01,000 [INFO] execution.order_manager: "
        "[INTC] 実約定(101.06)に合わせて待機注文を置き直しました: "
        "損切り 95.00 -> 96.01 / 利確 110.00 -> 111.17（参照価格は 100.00）",
    )

    report = build_day_report(lines, [], date(2026, 8, 6))

    assert report.bracket_fills[0]["symbol"] == "INTC"
    assert report.bracket_fills[0]["quantity"] == 2
    assert report.bracket_fills[0]["commission"] == 1.00
    assert report.repricings[0]["fill"] == 101.06
    assert report.repricings[0]["reference"] == 100.00

    text = format_report(report)
    assert "--- 注文層 ---" in text
    # 参照価格と実約定のずれ(+1.06%)が、待機注文の位置のずれそのものになる。
    assert "+1.06%" in text
    assert "有効期間の上書き: 検知なし" in text


def test_a_resting_exit_fill_is_reported_in_the_order_layer():
    """待機注文(子)の約定をサマリに出すこと。

    現フェーズの主目的はここで、これが出るまでOCAの取消連動はブローカー側の
    挙動として確認できない。決済の行だけではBot側の成行決済と区別がつかず、
    2026-08-18に初めて子注文が約定した日のサマリは「ブラケットの約定: なし」
    としか報告していなかった（親注文の約定しか数えていなかったため）。
    """
    lines = _lines(
        "2026-08-18 22:51:40,172 [INFO] __main__: "
        "[INTC] ブローカー側の待機注文が約定していました: "
        "reason=STOP_LOSS fill=97.33 commission=1.00",
    )

    report = build_day_report(lines, [], date(2026, 8, 18))

    assert report.resting_exit_fills[0]["symbol"] == "INTC"
    assert report.resting_exit_fills[0]["reason"] == "STOP_LOSS"
    assert report.resting_exit_fills[0]["fill"] == 97.33

    text = format_report(report)
    assert "待機注文(子)の約定: INTC reason=STOP_LOSS @ 97.33" in text


def test_a_fill_price_recovered_from_fills_is_called_out():
    """約定価格をFillから復元した日は、サマリにそう出すこと。

    再接続で取り込んだ注文は avgFillPrice が空で、Fillから復元できたかどうかは
    ログの source= にしか現れない。ここに出ないと、ボットが止まっている間の
    約定を拾えたことが1日の要約から消える。
    """
    lines = _lines(
        "2026-08-21 22:51:40,172 [INFO] __main__: "
        "[INTC] ブローカー側の待機注文が約定していました: "
        "reason=STOP_LOSS fill=97.33 commission=1.00 source=fills",
    )

    report = build_day_report(lines, [], date(2026, 8, 21))

    assert report.resting_exit_fills[0]["source"] == "fills"
    assert "約定価格は Fill から復元" in format_report(report)


def test_an_old_log_without_a_price_source_still_parses():
    """source= の無い過去のログも読めること（付ける前の記録が読めなくなる）。"""
    lines = _lines(
        "2026-08-18 22:51:40,172 [INFO] __main__: "
        "[INTC] ブローカー側の待機注文が約定していました: "
        "reason=STOP_LOSS fill=97.33 commission=1.00",
    )

    report = build_day_report(lines, [], date(2026, 8, 18))

    assert report.resting_exit_fills[0]["source"] is None
    assert "約定価格は Fill から復元" not in format_report(report)


def test_a_tif_downgrade_is_called_out_with_the_fix():
    """有効期間の上書きは、直し方(Presets)まで添えて出すこと。

    直せるのはGatewayのGUIだけなので、サマリに現象だけ書いても行動に移せない。
    """
    lines = _lines(
        "2026-08-06 22:31:00,000 [INFO] execution.order_manager: "
        "[INTC] ブラケットの親注文が約定しました: qty=2 fill=101.06 commission=1.00 "
        "損切り=STP@95.34 利確=LMT@111.05 oca=BRACKET_INTC_1",
        "2026-08-06 22:36:00,000 [WARNING] execution.order_manager: "
        "[INTC] 待機注文の有効期間が DAY になっています（GTC で発注したはずのもの）。"
        "IB Gateway の Order Preset による上書きで、引けで失効するため翌朝まで"
        "損切りの無い時間ができます。"
        "Global Configuration → Presets → Stocks の Time in Force を GTC にしてください。",
    )

    report = build_day_report(lines, [], date(2026, 8, 6))

    assert report.tif_downgrades == {"INTC": "DAY"}
    text = format_report(report)
    assert "INTC=DAY" in text and "Presets" in text
    assert "有効期間の上書き: 検知なし" not in text


def test_the_tif_warning_the_bot_emits_is_the_one_the_report_parses(caplog):
    """サマリの照合パターンが、実際に出る行と一致していること。

    書式を変えた瞬間に照合が0件になり、**上書きされていないのと区別がつかなくなる**。
    文字列をテスト側へ写すとその変更に追随できないので、本物のロガーが出した行を
    そのまま解析へ通す。
    """
    import logging as _logging

    from execution import order_manager

    order_manager._TIF_DOWNGRADE_WARNED.clear()
    with caplog.at_level(_logging.WARNING, logger="execution.order_manager"):
        order_manager._warn_about_tif_downgrades({"INTC": {"DAY"}})

    emitted = caplog.records[0].getMessage()
    lines = _lines(f"2026-08-06 22:36:00,000 [WARNING] execution.order_manager: {emitted}")

    assert build_day_report(lines, [], date(2026, 8, 6)).tif_downgrades == {"INTC": "DAY"}
