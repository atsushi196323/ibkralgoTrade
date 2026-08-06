"""core/market_hours.py の単体テスト。"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from core.market_hours import (
    is_day_trade_flatten_time,
    is_japan_market_holiday,
    is_japan_regular_trading_hours,
    is_regular_trading_hours,
    is_us_market_holiday,
    is_us_trading_day,
)

ET = ZoneInfo("America/New_York")
JST = ZoneInfo("Asia/Tokyo")


@pytest.mark.parametrize(
    "when",
    [
        datetime(2026, 7, 22, 9, 30),  # 寄り付き（境界値、水曜）
        datetime(2026, 7, 22, 12, 0),  # 昼
        datetime(2026, 7, 22, 15, 59),  # 引け直前
    ],
)
def test_true_during_regular_session(when) -> None:
    assert is_regular_trading_hours(when) is True


@pytest.mark.parametrize(
    "when",
    [
        datetime(2026, 7, 22, 9, 29),  # 寄り付き前
        datetime(2026, 7, 22, 16, 0),  # 引け（境界値、当日はクローズ扱い）
        datetime(2026, 7, 22, 20, 0),  # 夜間
        datetime(2026, 7, 22, 4, 0),  # 早朝
    ],
)
def test_false_outside_regular_session(when) -> None:
    assert is_regular_trading_hours(when) is False


@pytest.mark.parametrize(
    "when",
    [
        datetime(2026, 7, 25, 12, 0),  # 土曜
        datetime(2026, 7, 26, 12, 0),  # 日曜
    ],
)
def test_false_on_weekend(when) -> None:
    assert is_regular_trading_hours(when) is False


def test_naive_datetime_is_treated_as_eastern_time() -> None:
    naive = datetime(2026, 7, 22, 10, 0)

    assert is_regular_trading_hours(naive) is True


def test_timezone_aware_datetime_is_converted_to_eastern() -> None:
    # UTC 14:00 は夏時間(EDT, UTC-4)のET 10:00に相当し、レギュラーセッション内。
    utc_time = datetime(2026, 7, 22, 14, 0, tzinfo=ZoneInfo("UTC"))

    assert is_regular_trading_hours(utc_time) is True


# --- 祝日判定 (NYSE) ----------------------------------------------------------


@pytest.mark.parametrize(
    "when",
    [
        date(2026, 1, 1),  # New Year's Day
        date(2026, 1, 19),  # Martin Luther King Jr. Day
        date(2026, 7, 3),  # Independence Day(観測日、7/4が土曜のため前日振替)
        date(2026, 12, 25),  # Christmas Day
    ],
)
def test_is_us_market_holiday_true_on_known_holidays(when) -> None:
    assert is_us_market_holiday(when) is True


def test_is_us_market_holiday_false_on_regular_weekday() -> None:
    assert is_us_market_holiday(date(2026, 7, 22)) is False


def test_regular_trading_hours_false_on_us_holiday_even_during_session_time() -> None:
    # 2026-01-01は木曜(平日)だが、NYSEの休場日のため取引時間内でもFalse。
    holiday_during_session = datetime(2026, 1, 1, 10, 0, tzinfo=ET)

    assert is_regular_trading_hours(holiday_during_session) is False


# --- 祝日判定 (東証) ----------------------------------------------------------


@pytest.mark.parametrize(
    "when",
    [
        date(2026, 1, 1),  # 元日(国民の祝日)
        date(2026, 1, 12),  # 成人の日
        date(2026, 9, 22),  # 秋分の日の前日にあたる国民の休日
        date(2026, 5, 6),  # こどもの日の振替休日
    ],
)
def test_is_japan_market_holiday_true_on_national_holidays(when) -> None:
    assert is_japan_market_holiday(when) is True


@pytest.mark.parametrize(
    "when",
    [
        date(2025, 12, 31),  # 大納会前日/年末休場(祝日ではないが取引所独自休場)
        date(2026, 1, 2),  # 年始休場(祝日ではないが取引所独自休場)
        date(2026, 1, 3),  # 年始休場(祝日ではないが取引所独自休場)
    ],
)
def test_is_japan_market_holiday_true_on_exchange_year_end_closures(when) -> None:
    assert is_japan_market_holiday(when) is True


def test_is_japan_market_holiday_false_on_regular_weekday() -> None:
    assert is_japan_market_holiday(date(2026, 7, 22)) is False


# --- 東証の売買立会時間 ---------------------------------------------------------


@pytest.mark.parametrize(
    "when",
    [
        datetime(2026, 7, 22, 9, 0, tzinfo=JST),  # 前場寄り付き（境界値、水曜）
        datetime(2026, 7, 22, 11, 0, tzinfo=JST),  # 前場中
        datetime(2026, 7, 22, 13, 0, tzinfo=JST),  # 後場中
        datetime(2026, 7, 22, 15, 29, tzinfo=JST),  # 後場引け直前
    ],
)
def test_is_japan_regular_trading_hours_true_during_session(when) -> None:
    assert is_japan_regular_trading_hours(when) is True


@pytest.mark.parametrize(
    "when",
    [
        datetime(2026, 7, 22, 8, 59, tzinfo=JST),  # 前場寄り付き前
        datetime(2026, 7, 22, 12, 0, tzinfo=JST),  # 昼休み
        datetime(2026, 7, 22, 15, 30, tzinfo=JST),  # 後場引け（境界値、当日はクローズ扱い）
        datetime(2026, 7, 22, 20, 0, tzinfo=JST),  # 夜間
    ],
)
def test_is_japan_regular_trading_hours_false_outside_session(when) -> None:
    assert is_japan_regular_trading_hours(when) is False


def test_is_japan_regular_trading_hours_false_on_holiday_even_during_session_time() -> None:
    # 2026-01-01は木曜(平日)だが、元日のためFalse。
    holiday_during_session = datetime(2026, 1, 1, 10, 0, tzinfo=JST)

    assert is_japan_regular_trading_hours(holiday_during_session) is False


def test_is_japan_regular_trading_hours_false_on_weekend() -> None:
    saturday = datetime(2026, 7, 25, 10, 0, tzinfo=JST)

    assert is_japan_regular_trading_hours(saturday) is False


def test_default_uses_current_time(monkeypatch) -> None:
    fixed_now = datetime(2026, 7, 22, 10, 0, tzinfo=ET)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr("core.market_hours.datetime", _FixedDatetime)

    assert is_regular_trading_hours() is True


# --- is_day_trade_flatten_time ----------------------------------------------


@pytest.mark.parametrize(
    "when",
    [
        datetime(2026, 7, 22, 15, 55),  # 基準時刻ちょうど（境界値）
        datetime(2026, 7, 22, 15, 59),
        datetime(2026, 7, 22, 16, 0),
    ],
)
def test_is_day_trade_flatten_time_true_at_or_after_threshold(when) -> None:
    assert is_day_trade_flatten_time(when) is True


@pytest.mark.parametrize(
    "when",
    [
        datetime(2026, 7, 22, 15, 54),
        datetime(2026, 7, 22, 9, 30),
        datetime(2026, 7, 22, 0, 0),
    ],
)
def test_is_day_trade_flatten_time_false_before_threshold(when) -> None:
    assert is_day_trade_flatten_time(when) is False


def test_is_day_trade_flatten_time_treats_naive_datetime_as_eastern() -> None:
    naive = datetime(2026, 7, 22, 15, 55)

    assert is_day_trade_flatten_time(naive) is True


def test_is_day_trade_flatten_time_converts_timezone_aware_datetime_to_eastern() -> None:
    # UTC 19:55 は夏時間(EDT, UTC-4)のET 15:55に相当し、基準時刻ちょうど。
    utc_time = datetime(2026, 7, 22, 19, 55, tzinfo=ZoneInfo("UTC"))

    assert is_day_trade_flatten_time(utc_time) is True


@pytest.mark.parametrize(
    "trading_day",
    [
        date(2026, 8, 6),   # 木曜
        date(2026, 8, 7),   # 金曜
        date(2026, 8, 10),  # 月曜
    ],
)
def test_is_us_trading_day_accepts_ordinary_weekdays(trading_day) -> None:
    assert is_us_trading_day(trading_day) is True


@pytest.mark.parametrize(
    "closed_day",
    [
        date(2026, 8, 8),   # 土曜
        date(2026, 8, 9),   # 日曜
        date(2026, 9, 7),   # レイバーデー（月曜の祝日）
        date(2026, 11, 26),  # サンクスギビング（木曜の祝日）
        date(2026, 7, 3),   # 独立記念日(7/4 土)の振替休場（金曜）
    ],
)
def test_is_us_trading_day_rejects_weekends_and_holidays(closed_day) -> None:
    """祝日はlaunchdでは表現できないため、この判定が起動の可否そのものになる。

    振替休場(7/3)を含めているのは、移動祝日の計算を自前でやらず
    `holidays` パッケージへ委譲していることの確認でもある。
    """
    assert is_us_trading_day(closed_day) is False
