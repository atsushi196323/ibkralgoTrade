"""execution/account.py の単体テスト。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from execution.account import (
    get_account_equity_async,
    get_settled_cash_async,
    get_usd_to_base_rate_async,
)


def _make_account_value(tag: str, value: str, currency: str = "USD"):
    return MagicMock(tag=tag, value=value, currency=currency)


def _make_jpy_based_summary():
    """基準通貨が円の口座のアカウントサマリー（実測値に合わせた形）。

    NetLiquidationは円建ての行しか持たず、USD建ての額は
    NetLiquidationByCurrencyの方にある。
    """
    return [
        _make_account_value("NetLiquidation", "196059.62", currency="JPY"),
        _make_account_value("NetLiquidationByCurrency", "0.00", currency="JPY"),
        _make_account_value("NetLiquidationByCurrency", "1220.00", currency="USD"),
        _make_account_value("NetLiquidationByCurrency", "196059.6182", currency="BASE"),
        _make_account_value("AvailableFunds", "196059.62", currency="JPY"),
    ]


def test_returns_usd_equity_from_a_jpy_based_account() -> None:
    """基準通貨が円でも、USD建ての純資産を返すこと。

    円の数値をドルとして扱うと、資金を為替レート倍（約160倍）に見積もり、
    ポジションサイジングの数量が2桁狂う。
    """
    ib = MagicMock()
    ib.accountSummaryAsync = AsyncMock(return_value=_make_jpy_based_summary())

    equity = asyncio.run(get_account_equity_async(ib))

    assert equity == pytest.approx(1220.0)


def test_does_not_fall_back_to_the_base_currency_amount() -> None:
    """USD建ての行が無いときに基準通貨の額を返さないこと。

    黙ってフォールバックすると、通貨の取り違えが例外もログも出さずに
    数量だけを桁違いにする。
    """
    ib = MagicMock()
    ib.accountSummaryAsync = AsyncMock(
        return_value=[
            _make_account_value("NetLiquidation", "196059.62", currency="JPY"),
            _make_account_value("NetLiquidationByCurrency", "196059.6182", currency="BASE"),
        ]
    )

    with pytest.raises(ValueError):
        asyncio.run(get_account_equity_async(ib))


def test_returns_requested_tag_and_currency() -> None:
    ib = MagicMock()
    ib.accountSummaryAsync = AsyncMock(
        return_value=[
            _make_account_value("AvailableFunds", "196059.62", currency="JPY"),
            _make_account_value("AvailableFunds", "1220.0", currency="USD"),
        ]
    )

    equity = asyncio.run(get_account_equity_async(ib, tag="AvailableFunds"))

    assert equity == pytest.approx(1220.0)


def test_currency_can_be_ignored_for_tags_without_a_breakdown() -> None:
    """通貨別の内訳を持たないタグは currency=None で引けること。"""
    ib = MagicMock()
    ib.accountSummaryAsync = AsyncMock(
        return_value=[_make_account_value("NetLiquidation", "196059.62", currency="JPY")]
    )

    equity = asyncio.run(
        get_account_equity_async(ib, tag="NetLiquidation", currency=None)
    )

    assert equity == pytest.approx(196059.62)


def test_raises_when_tag_not_found() -> None:
    ib = MagicMock()
    ib.accountSummaryAsync = AsyncMock(return_value=[_make_account_value("AvailableFunds", "50000.0")])

    with pytest.raises(ValueError):
        asyncio.run(get_account_equity_async(ib))


# --- 決済済み現金（新規建ての資金の裏付け判定に使う） -----------------------------


def test_returns_settled_cash() -> None:
    ib = MagicMock()
    ib.accountSummaryAsync = AsyncMock(
        return_value=[
            _make_account_value("NetLiquidation", "123456.78"),
            _make_account_value("SettledCash", "1234.56"),
        ]
    )

    assert asyncio.run(get_settled_cash_async(ib)) == pytest.approx(1234.56)


def test_settled_cash_picks_the_usd_row_on_a_jpy_based_account() -> None:
    """基準通貨が円でも、USD建ての行を選ぶこと。

    呼び出し側は floor(決済済み現金 ÷ 株価USD) で株数を出すので、円建ての額を
    掴むとクランプが為替レート倍だけ緩くなる（EQUITY_TAG と同じ取り違え）。
    """
    ib = MagicMock()
    ib.accountSummaryAsync = AsyncMock(
        return_value=[
            _make_account_value("NetLiquidation", "196059.62", currency="JPY"),
            _make_account_value("SettledCash", "127772.18", currency="BASE"),
            _make_account_value("SettledCash", "0.00", currency="JPY"),
            _make_account_value("SettledCash", "803.71", currency="USD"),
        ]
    )

    assert asyncio.run(get_settled_cash_async(ib)) == pytest.approx(803.71)


def test_settled_cash_in_another_currency_is_not_used_as_usd() -> None:
    """USD建ての行が無ければNoneを返し、基準通貨の額へフォールバックしないこと。

    Noneならクランプを掛けずに通すだけ（注文が拒否されうるに留まる）だが、
    円建ての額をUSDとして使うと、有効にしたつもりのクランプが約160倍だけ
    緩くなり一度も効かない。
    """
    ib = MagicMock()
    ib.accountSummaryAsync = AsyncMock(
        return_value=[
            _make_account_value("NetLiquidation", "196059.62", currency="JPY"),
            _make_account_value("SettledCash", "127772.18", currency="JPY"),
        ]
    )

    assert asyncio.run(get_settled_cash_async(ib)) is None


def test_settled_cash_returns_none_when_tag_is_missing() -> None:
    """例外ではなくNoneを返すこと。

    「取得できなかった」と「0ドルしかない」を呼び出し側が区別できる必要がある。
    0として扱うと、資金があるのに永久に建てられなくなる。
    """
    ib = MagicMock()
    ib.accountSummaryAsync = AsyncMock(
        return_value=[_make_account_value("NetLiquidation", "123456.78")]
    )

    assert asyncio.run(get_settled_cash_async(ib)) is None


def test_settled_cash_returns_none_when_value_is_not_numeric() -> None:
    ib = MagicMock()
    ib.accountSummaryAsync = AsyncMock(
        return_value=[_make_account_value("SettledCash", "")]
    )

    assert asyncio.run(get_settled_cash_async(ib)) is None


def test_settled_cash_can_be_zero() -> None:
    """0ドルは正常な観測値であり、Noneと区別されること。"""
    ib = MagicMock()
    ib.accountSummaryAsync = AsyncMock(
        return_value=[_make_account_value("SettledCash", "0.0")]
    )

    assert asyncio.run(get_settled_cash_async(ib)) == pytest.approx(0.0)


def test_usd_to_base_rate_is_read_from_the_account_summary() -> None:
    """為替の購読が無くても円換算レートが取れること。

    IDEALPROのUSD.JPYはマーケットデータの追加購読が要り、この口座では
    3経路とも失敗する（2026-08-06に Error 10089 / 162 として実測。
    ジャーナルの usd_jpy_rate が空のまま記録されていた）。
    """
    ib = MagicMock()
    ib.accountSummaryAsync = AsyncMock(
        return_value=_make_jpy_based_summary()
        + [_make_account_value("ExchangeRate", "160.0533448", currency="USD")]
    )

    assert asyncio.run(get_usd_to_base_rate_async(ib)) == pytest.approx(160.0533448)


def test_usd_to_base_rate_is_not_reported_for_a_different_base_currency() -> None:
    """基準通貨が円でなければNoneを返すこと。

    基準通貨がドルの口座ではExchangeRateが1.0前後になり、それを円換算に
    使うと損益が約160分の1で記録される。通貨の取り違えは例外もログも出さずに
    桁だけを狂わせるため、分からない場合は記録しない側へ倒す。
    """
    ib = MagicMock()
    ib.accountSummaryAsync = AsyncMock(
        return_value=[
            _make_account_value("NetLiquidation", "1220.00", currency="USD"),
            _make_account_value("ExchangeRate", "1.0", currency="USD"),
        ]
    )

    assert asyncio.run(get_usd_to_base_rate_async(ib)) is None


def test_usd_to_base_rate_returns_none_when_the_tag_is_missing() -> None:
    ib = MagicMock()
    ib.accountSummaryAsync = AsyncMock(return_value=_make_jpy_based_summary())

    assert asyncio.run(get_usd_to_base_rate_async(ib)) is None


@pytest.mark.parametrize("value", ["", "0", "-160.0"])
def test_usd_to_base_rate_rejects_unusable_values(value) -> None:
    """解釈できない値・正でない値をレートとして採用しないこと。"""
    ib = MagicMock()
    ib.accountSummaryAsync = AsyncMock(
        return_value=_make_jpy_based_summary()
        + [_make_account_value("ExchangeRate", value, currency="USD")]
    )

    assert asyncio.run(get_usd_to_base_rate_async(ib)) is None
