"""core/market_hours.py の単体テスト。"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from core.market_hours import is_day_trade_flatten_time, is_regular_trading_hours

ET = ZoneInfo("America/New_York")


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
