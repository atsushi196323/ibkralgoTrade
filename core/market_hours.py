"""米国株式市場（NYSE/Nasdaq）のレギュラーセッション判定。

祝日カレンダーは考慮しない（平日9:30-16:00 ETのみを判定する簡易実装）。
必要になった場合はNYSE休場日カレンダーとの突き合わせを別途追加すること。
"""

from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

US_EASTERN = ZoneInfo("America/New_York")
MARKET_OPEN: time = time(9, 30)
MARKET_CLOSE: time = time(16, 0)
# デイトレード想定のポジションを大引け前に強制決済する基準時刻。
# オーバーナイト持ち越しによる想定外のギャップリスクを避けるため、
# 大引けそのものより手前に設定し、決済シミュレーション実行の猶予を持たせる。
DAY_TRADE_FLATTEN_TIME: time = time(15, 55)


def _to_eastern(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(US_EASTERN)
    if now.tzinfo is None:
        return now.replace(tzinfo=US_EASTERN)
    return now.astimezone(US_EASTERN)


def is_regular_trading_hours(now: Optional[datetime] = None) -> bool:
    reference = _to_eastern(now)

    if reference.weekday() >= 5:  # 土曜(5)・日曜(6)
        return False

    return MARKET_OPEN <= reference.time() < MARKET_CLOSE


def is_day_trade_flatten_time(now: Optional[datetime] = None) -> bool:
    """デイトレードポジションを強制決済すべき時刻(DAY_TRADE_FLATTEN_TIME以降)か判定する。"""
    return _to_eastern(now).time() >= DAY_TRADE_FLATTEN_TIME
