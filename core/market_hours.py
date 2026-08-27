"""米国株式市場（NYSE/Nasdaq）・日本株式市場（東証）のレギュラーセッション判定。

祝日判定は `holidays` パッケージ（financial_holidays("NYSE") / Japan()）に委譲する。
振替休日や秋分の日・春分の日のような移動祝日、土曜開催の祝日を金曜/月曜に
振り替える観測日調整を自前で計算するのは誤りやすく、実績のあるパッケージ側の
実装に任せる方が安全なため。
"""

from datetime import date, timedelta, datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

import holidays

US_EASTERN = ZoneInfo("America/New_York")
JST = ZoneInfo("Asia/Tokyo")

MARKET_OPEN: time = time(9, 30)
MARKET_CLOSE: time = time(16, 0)
# デイトレード想定のポジションを大引け前に強制決済する基準時刻。
# オーバーナイト持ち越しによる想定外のギャップリスクを避けるため、
# 大引けそのものより手前に設定し、決済シミュレーション実行の猶予を持たせる。
DAY_TRADE_FLATTEN_TIME: time = time(15, 55)

# 東証の売買立会時間（前場・後場、間に昼休みを挟む）。
# 2024年11月5日の取引時間延長後の後場終了時刻(15:30)を反映している。
JP_MORNING_SESSION_OPEN: time = time(9, 0)
JP_MORNING_SESSION_CLOSE: time = time(11, 30)
JP_AFTERNOON_SESSION_OPEN: time = time(12, 30)
JP_AFTERNOON_SESSION_CLOSE: time = time(15, 30)

# 年ごとのインスタンスを毎回作り直す必要はなく、`in`演算子で問い合わせた年を
# 遅延的に内部キャッシュへ追加していく仕様のため、モジュールレベルで使い回す。
_US_MARKET_HOLIDAYS = holidays.financial_holidays("NYSE")
_JP_NATIONAL_HOLIDAYS = holidays.Japan()
# 東証の年末年始休場日(12/31, 1/2, 1/3)。1/1は国民の祝日として
# _JP_NATIONAL_HOLIDAYSに含まれるが、これらは祝日ではなく取引所独自の
# 休場日のため別途判定する。
_JP_EXCHANGE_YEAR_END_CLOSURE_MONTH_DAYS = frozenset({(12, 31), (1, 2), (1, 3)})


def _to_eastern(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(US_EASTERN)
    if now.tzinfo is None:
        return now.replace(tzinfo=US_EASTERN)
    return now.astimezone(US_EASTERN)


def _to_tokyo(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(JST)
    if now.tzinfo is None:
        return now.replace(tzinfo=JST)
    return now.astimezone(JST)


def is_us_market_holiday(check_date: Optional[date] = None) -> bool:
    """NYSE/Nasdaqの休場日（祝日）か判定する。"""
    target = check_date if check_date is not None else datetime.now(US_EASTERN).date()
    return target in _US_MARKET_HOLIDAYS


def is_us_trading_day(check_date: Optional[date] = None) -> bool:
    """その日にNYSE/Nasdaqのレギュラーセッションがあるか（土日・祝日でないか）。

    `is_regular_trading_hours` と違い**時刻を見ない**。launchdは日付でしか
    ジョブを絞れず、祝日を除外するキーも持たないため、起動の可否は
    「その日が取引日か」だけで判定する必要がある（`scripts/is_us_trading_day.py`）。
    """
    target = check_date if check_date is not None else datetime.now(US_EASTERN).date()
    if target.weekday() >= 5:
        return False
    return not is_us_market_holiday(target)


def count_trading_days_between(start: date, end: date) -> int:
    """start(除く)からend(含む)までの米国の取引日数。

    **暦日ではなく営業日で数える。** 保有期間を暦日で数えると、週末と祝日の
    ぶんだけ早く満期になり、営業日で測ったバックテストと前提がずれる
    （60営業日は暦でおよそ84日）。

    `end` が `start` 以前なら0。祝日の判定は `holidays` パッケージへ委譲する
    （移動祝日と振替休日を自前計算しない）。
    """
    if end <= start:
        return 0
    days = 0
    current = start + timedelta(days=1)
    while current <= end:
        if is_us_trading_day(current):
            days += 1
        current += timedelta(days=1)
    return days


def is_japan_market_holiday(check_date: Optional[date] = None) -> bool:
    """東証の休場日（祝日・年末年始休場）か判定する。"""
    target = check_date if check_date is not None else datetime.now(JST).date()
    if (target.month, target.day) in _JP_EXCHANGE_YEAR_END_CLOSURE_MONTH_DAYS:
        return True
    return target in _JP_NATIONAL_HOLIDAYS


def is_regular_trading_hours(now: Optional[datetime] = None) -> bool:
    reference = _to_eastern(now)

    if reference.weekday() >= 5:  # 土曜(5)・日曜(6)
        return False
    if is_us_market_holiday(reference.date()):
        return False

    return MARKET_OPEN <= reference.time() < MARKET_CLOSE


def is_japan_regular_trading_hours(now: Optional[datetime] = None) -> bool:
    """東証の売買立会時間内（前場・後場、昼休みを除く）か判定する。"""
    reference = _to_tokyo(now)

    if reference.weekday() >= 5:  # 土曜(5)・日曜(6)
        return False
    if is_japan_market_holiday(reference.date()):
        return False

    current_time = reference.time()
    in_morning_session = JP_MORNING_SESSION_OPEN <= current_time < JP_MORNING_SESSION_CLOSE
    in_afternoon_session = JP_AFTERNOON_SESSION_OPEN <= current_time < JP_AFTERNOON_SESSION_CLOSE
    return in_morning_session or in_afternoon_session


def is_day_trade_flatten_time(now: Optional[datetime] = None) -> bool:
    """デイトレードポジションを強制決済すべき時刻(DAY_TRADE_FLATTEN_TIME以降)か判定する。"""
    return _to_eastern(now).time() >= DAY_TRADE_FLATTEN_TIME
