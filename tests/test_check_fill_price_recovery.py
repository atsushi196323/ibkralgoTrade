"""再接続後の約定価格の読み取り確認（照会CLI）のテスト。

このCLIは、直した経路（`avgFillPrice` が空なら Fill から読む）が実データで
働くかを確かめる唯一の手段である。**通常の稼働ではこの経路を一度も通らない**
ため、判定の中身が壊れても稼働ログからは気付けない。
"""

import json
from unittest.mock import MagicMock

from scripts.check_fill_price_recovery import _read_fills, _verdict, _append_record


def _trade(symbol="INTC", action="SELL", order_type="STP", status="Filled",
           avg_fill_price=0.0, fills=((97.33, 2.0),)):
    trade = MagicMock()
    trade.orderStatus.status = status
    trade.orderStatus.avgFillPrice = avg_fill_price
    trade.contract.symbol = symbol
    trade.order.action = action
    trade.order.orderType = order_type
    trade.order.permId = 470578602
    made = []
    for price, shares in fills:
        fill = MagicMock()
        fill.execution.price = price
        fill.execution.shares = shares
        made.append(fill)
    trade.fills = made
    return trade


def _ib(*trades):
    ib = MagicMock()
    ib.trades = MagicMock(return_value=list(trades))
    return ib


def test_unfilled_orders_are_not_read() -> None:
    """約定していない注文は判定材料にならない（板に生きているだけ）。"""
    assert _read_fills(_ib(_trade(status="Submitted"))) == []


def test_a_reconnected_fill_is_read_from_its_fills() -> None:
    """再接続で取り込んだ注文＝avgFillPriceが空でも値段が読めること。"""
    readings = _read_fills(_ib(_trade(avg_fill_price=0.0)))

    assert (readings[0].price, readings[0].source) == (97.33, "fills")


def test_a_recovered_fill_is_reported_as_the_fix_working() -> None:
    readings = _read_fills(_ib(_trade(avg_fill_price=0.0)))

    exit_code, verdict = _verdict(readings)

    assert exit_code == 0
    assert "修正が効いています" in verdict


def test_an_unreadable_fill_fails() -> None:
    """値段がどこからも読めない＝直した不具合がそのまま残っている。

    ここだけが終了コード1になる。判定材料が無い日を失敗にすると、毎晩自動で
    走るぶんだけ失敗が積み上がり、本当の失敗が埋もれる。
    """
    exit_code, verdict = _verdict(_read_fills(_ib(_trade(avg_fill_price=0.0, fills=()))))

    assert exit_code == 1
    assert "読めません" in verdict


def test_a_day_without_fills_is_not_a_failure() -> None:
    exit_code, verdict = _verdict([])

    assert exit_code == 0
    assert "判定材料がありません" in verdict


def test_fills_read_while_running_are_not_a_verdict() -> None:
    """avgFillPriceが読めた日は、再接続の状況を再現できていない。

    「読めた」を「修正が効いた」と報告すると、確かめていないものを確かめたことに
    してしまう。
    """
    exit_code, verdict = _verdict(_read_fills(_ib(_trade(avg_fill_price=97.33))))

    assert exit_code == 0
    assert "判定にはなりません" in verdict


def test_records_are_appended_not_overwritten(tmp_path) -> None:
    """記録は追記専用。上書きすると前夜までの判定が消える。"""
    out = tmp_path / "checks" / "fill_price_recovery.jsonl"

    _append_record(str(out), {"checked_at": "1", "verdict": "a"})
    _append_record(str(out), {"checked_at": "2", "verdict": "b"})

    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert [row["checked_at"] for row in rows] == ["1", "2"]


def test_a_day_with_recorded_exits_but_no_readable_fills_is_not_called_empty(tmp_path) -> None:
    """決済を記録した日に約定が1件も読めなければ、それは「約定が無かった日」ではない。

    IBKRが返す約定は Gateway のタイムゾーン(Asia/Tokyo)の当日ぶんに限られ、
    米国のザラ場は日本時間の日付をまたぐ。2026-08-24 のINTC(STP 87.06)は
    22:30 JST に約定したが、06:05 JST の確認からは読めず「今日の約定はなし」と
    記録されていた。**確かめられなかったことを、確かめる材料が無かったことと
    同じに扱ってはならない。**
    """
    exit_code, verdict = _verdict([], recent_journal_exits=1)

    assert exit_code == 0
    assert "判定できません" in verdict
    assert "判定材料がありません" not in verdict


def _write_journal(path, closed_at: str) -> None:
    path.write_text(
        "symbol,entry_price,exit_price,quantity,reason,pnl,pnl_pct,r_multiple,"
        "closed_at,commission,usd_jpy_rate,entry_date\n"
        f"INTC,91.61,87.06,2,STOP_LOSS,-9.1,-4.97,-0.99,{closed_at},2.0,159.1,"
        "2026-08-20T13:31:17+00:00\n",
        encoding="utf-8",
    )


def test_recent_exits_are_counted_from_the_journal(tmp_path) -> None:
    from datetime import datetime, timedelta, timezone

    from scripts.check_fill_price_recovery import count_recent_journal_exits

    now = datetime(2026, 8, 24, 21, 5, tzinfo=timezone.utc)
    journal = tmp_path / "trade_journal.csv"
    _write_journal(journal, (now - timedelta(hours=8)).isoformat())

    assert count_recent_journal_exits(str(journal), now=now) == 1


def test_older_exits_do_not_make_the_check_look_blind(tmp_path) -> None:
    """前のセッションの決済まで数えると、毎晩「判定できません」になる。"""
    from datetime import datetime, timedelta, timezone

    from scripts.check_fill_price_recovery import count_recent_journal_exits

    now = datetime(2026, 8, 24, 21, 5, tzinfo=timezone.utc)
    journal = tmp_path / "trade_journal.csv"
    _write_journal(journal, (now - timedelta(days=4)).isoformat())

    assert count_recent_journal_exits(str(journal), now=now) == 0


def test_a_missing_journal_is_not_an_error(tmp_path) -> None:
    from scripts.check_fill_price_recovery import count_recent_journal_exits

    assert count_recent_journal_exits(str(tmp_path / "nope.csv")) == 0
