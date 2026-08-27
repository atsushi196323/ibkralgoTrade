"""約定記録（想定価格と実約定価格の乖離）の単体テスト。"""

import json
import os
from datetime import datetime, timezone

import pytest

from execution.fill_log import (
    EVENT_ENTRY,
    EVENT_EXIT,
    FillLog,
    adverse_usd,
    deviation_pct,
)


@pytest.fixture
def log(tmp_path) -> FillLog:
    return FillLog(str(tmp_path / "fills.jsonl"))


# --- 乖離の符号 -----------------------------------------------------------------


def test_the_adverse_side_is_normalised_so_a_round_trip_does_not_cancel_out() -> None:
    """買いの不利と売りの不利が、合計したときに打ち消し合わないこと。

    生の差額(実約定 - 想定)をそのまま持つと、高く買って安く売った往復——
    どちらも不利な方向——が符号違いになり、合計すると乖離が無かったように
    見える。不利な側を正に揃えることでそのまま合計できる。
    """
    # 想定100で101で買った（1ドル高く買った＝不利）。
    bought = adverse_usd("BUY", intended_price=100.0, fill_price=101.0, quantity=10)
    # 想定100で99で売った（1ドル安く売った＝不利）。
    sold = adverse_usd("SELL", intended_price=100.0, fill_price=99.0, quantity=10)

    assert bought == pytest.approx(10.0)
    assert sold == pytest.approx(10.0)
    assert bought + sold == pytest.approx(20.0)


def test_a_favourable_fill_is_recorded_as_a_negative_adverse_amount() -> None:
    """有利に約定した分はマイナスで残ること（不利側だけを見て悲観しないため）。"""
    assert adverse_usd("BUY", 100.0, 99.0, 10) == pytest.approx(-10.0)
    assert adverse_usd("SELL", 100.0, 101.0, 10) == pytest.approx(-10.0)


def test_the_raw_deviation_keeps_its_sign() -> None:
    """乖離率は向きを保つこと。参照価格が実勢より高いのか低いのかが分かる。"""
    assert deviation_pct(66.50, 67.44) == pytest.approx(1.4135, abs=1e-3)
    assert deviation_pct(67.44, 66.50) == pytest.approx(-1.3938, abs=1e-3)


def test_an_unknown_intended_price_is_not_treated_as_zero() -> None:
    """想定価格が無い建玉で、約定代金そのものを乖離として数えないこと。

    ブローカー同期で取り込んだ建玉は待機注文の値段を持たない(0)。0を基準に
    差を取ると「100ドルの株を1株、100ドル不利に約定した」という記録になり、
    不利側の合計が実態と桁で違う値になる。
    """
    assert adverse_usd("SELL", intended_price=0.0, fill_price=100.0, quantity=1) is None
    assert deviation_pct(0.0, 100.0) is None


# --- 待機注文が実際に置かれた位置 ---------------------------------------------


def test_the_resting_orders_are_measured_from_the_fill_not_the_reference(log) -> None:
    """待機注文の位置を、参照価格ではなく実約定価格から測ること。

    参照価格を基準にすると、まさに検出したいずれ（参照価格が実勢と
    ずれていること）が定義上ゼロになって消える。

    値は2026-08-05のAMBQの実測: 参照66.50に対し実約定67.44で、待機注文は
    参照ベースの 63.17 / 73.15 に置かれた。建値から見ると -6.3% / +8.5% で、
    意図した -5% / +10% から離れている。
    """
    record = log.record_entry(
        symbol="AMBQ", quantity=3,
        intended_price=66.50, fill_price=67.44,
        stop_price=63.17, take_profit_price=73.15,
        designed_stop_pct=5.0, designed_take_profit_pct=10.0,
        account_equity=1220.0, dry_run=False,
    )

    assert record.deviation_pct == pytest.approx(1.41, abs=0.01)
    assert record.effective_stop_pct == pytest.approx(-6.33, abs=0.01)
    assert record.effective_take_profit_pct == pytest.approx(8.47, abs=0.01)
    # 設計値は「損切り-5% / 利確+10%」として、比較できる符号で残す。
    assert record.designed_stop_pct == pytest.approx(-5.0)
    assert record.designed_take_profit_pct == pytest.approx(10.0)


def test_the_risk_is_measured_from_the_stop_that_was_actually_placed(log) -> None:
    """1トレードのリスクを、実際に置かれた逆指値から測ること。

    設計値(1%)を超えていたかは、発注前の理論値からは分からない。2026-08-10の
    UPSは実約定104.06に対し逆指値が98.69(-5.16%)に残り、資金の1.16%だった。
    """
    record = log.record_entry(
        symbol="UPS", quantity=2,
        intended_price=103.50, fill_price=104.06,
        stop_price=98.69, designed_stop_pct=5.0,
        account_equity=925.0, dry_run=False,
    )

    # (104.06 - 98.69) * 2 / 925 * 100 = 1.161%
    assert record.risk_pct_of_equity == pytest.approx(1.161, abs=0.01)
    assert record.effective_stop_pct == pytest.approx(-5.16, abs=0.01)


def test_a_dry_run_entry_is_recorded_without_inventing_a_fill_price(log) -> None:
    """ドライランでは実約定が無いので、乖離を0として記録しないこと。

    0として残すと平均乖離がドライランの行で薄まり、実発注のずれが見えなくなる。
    """
    record = log.record_entry(
        symbol="AAPL", quantity=1, intended_price=80.0, fill_price=None,
        stop_price=76.0, designed_stop_pct=5.0, account_equity=1220.0,
    )

    assert record.fill_price is None
    assert record.deviation_pct is None
    assert record.adverse_usd is None
    # 実約定が無くても、待機注文がどこに置かれたかは参照価格から測れる。
    assert record.effective_stop_pct == pytest.approx(-5.0)


# --- 書き込みと読み戻し ---------------------------------------------------------


def test_records_are_appended_one_json_object_per_line(log) -> None:
    log.record_entry("AAPL", 1, 80.0, 80.5, dry_run=False)
    log.record_exit("AAPL", 1, "STP", 76.0, 75.8, dry_run=False)

    with open(log.file_path, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]

    assert [row["event"] for row in rows] == [EVENT_ENTRY, EVENT_EXIT]
    assert [row["action"] for row in rows] == ["BUY", "SELL"]
    assert log.load()[1].order_type == "STP"


def test_a_broken_line_does_not_hide_the_rest(log) -> None:
    """1行壊れても残りを読めること（JSONLを選んだ理由）。

    書き込み中に停止した場合に壊れうるのは最後の1行だけで、その1行のために
    その日の記録を全部捨てる理由は無い。
    """
    log.record_entry("AAPL", 1, 80.0, 80.5, dry_run=False)
    with open(log.file_path, "a", encoding="utf-8") as f:
        f.write('{"symbol": "BROK\n')
    log.record_exit("AAPL", 1, "LMT", 88.0, 88.2, dry_run=False)

    records = log.load()
    assert [r.symbol for r in records] == ["AAPL", "AAPL"]
    assert [r.event for r in records] == [EVENT_ENTRY, EVENT_EXIT]


def test_unknown_columns_in_older_rows_are_ignored(log) -> None:
    """後から列が増えても古い行を読めること。"""
    with open(log.file_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "recorded_at": "2026-08-05T00:00:00+00:00",
            "trading_day": "2026-08-04",
            "symbol": "AMBQ", "event": EVENT_ENTRY, "action": "BUY",
            "order_type": "MKT", "quantity": 3,
            "intended_price": 66.5, "fill_price": 67.44,
            "deviation_pct": 1.41, "adverse_usd": 2.82,
            "a_column_that_does_not_exist_yet": 1,
        }) + "\n")

    record = log.load()[0]
    assert record.symbol == "AMBQ"
    assert record.commission == 0.0


def test_a_write_failure_does_not_stop_trading(tmp_path, caplog) -> None:
    """記録に失敗しても例外を上げないこと。

    これは観測専用の記録であり、書けなかったことで発注や決済を止めては
    ならない（止めると、観測を足したせいで建玉が無防備になる）。
    """
    # ディレクトリと同じ名前のファイルは作れない。
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    log = FillLog(str(blocked))

    assert log.record_entry("AAPL", 1, 80.0, 80.5) is None
    assert "約定記録の書き込みに失敗" in caplog.text


def test_the_trading_day_is_stamped_in_us_eastern(log) -> None:
    """取引日の区切りを市場時間・クールダウン・控えと揃えること。

    日本時間で採ると、東部時間のザラ場の途中（日本時間の0時）で日付が変わり、
    同じセッションの約定が2日に割れる。
    """
    # 2026-08-19 01:30 JST = 2026-08-18 12:30 ET（同じ取引日のザラ場中）。
    moment = datetime(2026, 8, 18, 16, 30, tzinfo=timezone.utc)
    record = log.record_entry("INTC", 2, 102.0, 102.4, now=moment)

    assert record.trading_day == "2026-08-18"


def test_no_file_is_created_until_something_is_recorded(tmp_path) -> None:
    """インスタンス生成だけでファイルもディレクトリも作らないこと。

    import しただけで作業ディレクトリに logs/ ができると、テストが実際の
    運用記録のあるディレクトリを触りうる（`core.logging_setup` と同じ理由）。
    """
    path = tmp_path / "nested" / "fills.jsonl"
    FillLog(str(path))

    assert not os.path.exists(path.parent)
