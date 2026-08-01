"""IBKRへの重複リクエストを避けるためのキャッシュ。

ペーシング制限(core/pacing.py)への対策の中心。制限を守るために待たされるより、
そもそも同じデータを取り直さない方が良い。

キャッシュ対象は「サイクルごとに変化しないもの」に限る:

- 日足バー: 1取引日に1本しか増えないため、同じ取引日に取り直す意味がない。
  ポーリング間隔(数分)ごとに再取得すると、それだけでペーシング制限を食い潰す。
  ただし取引時間中の日足には**確定していない当日のバー**が並び、これは現在値と
  一緒に動く。キャッシュの前提が崩れるため取得時に落としている
  (market_data.drop_unconfirmed_today_bars)。
- コントラクト(qualifyContractsAsyncの結果): 銘柄のconId・上場取引所は
  日中に変わらない。

日中足(5分足等)はデイトレードのシグナル判定そのものなので、キャッシュしない。
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, Optional

import pandas as pd
from ib_insync import IB, Contract, Stock

from core.market_hours import US_EASTERN
from data.market_data import (
    drop_unconfirmed_today_bars,
    get_historical_bars_async,
    qualify_stock_async,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _DailyBarEntry:
    bars: pd.DataFrame
    trading_date: date


class DailyBarCache:
    """日足バーを米国東部時間の取引日単位でキャッシュする。

    取得に失敗した場合（空のDataFrame）はキャッシュしない。一時的な切断や
    ペーシング違反で空が返ったとき、その日いっぱい空を返し続けてしまうため。
    """

    # 既定の取得期間。暦日90日はおよそ62営業日で、スイング判定の移動平均
    # (main.SWING_MA_WINDOW = 30本) に対して倍程度の余裕がある。
    # 本数が足りないとmain側の `len(daily_df) >= SWING_MA_WINDOW` で
    # 日足分岐が例外も警告も出さずにスキップされるため、余裕を持たせている。
    # 取得は取引日ごとに銘柄あたり1回だけなので、期間を延ばしても
    # ペーシング制限(§6.1)上のリクエスト数は増えない。
    def __init__(self, duration: str = "90 D") -> None:
        self.duration = duration
        self._entries: Dict[str, _DailyBarEntry] = {}

    def _current_trading_date(self, now: Optional[datetime] = None) -> date:
        reference = now if now is not None else datetime.now(US_EASTERN)
        return reference.astimezone(US_EASTERN).date()

    async def get_async(
        self, ib: IB, contract: Contract, now: Optional[datetime] = None,
    ) -> pd.DataFrame:
        symbol = contract.symbol
        trading_date = self._current_trading_date(now)

        entry = self._entries.get(symbol)
        if entry is not None and entry.trading_date == trading_date:
            logger.debug("[%s] 日足バーをキャッシュから返します(%s)。", symbol, trading_date)
            return entry.bars

        fetched = await get_historical_bars_async(
            ib, contract, duration=self.duration, bar_size="1 day",
        )
        # 取引時間中の日足には確定していない当日のバーが並ぶ。これを残すと
        # 「その日最初のサイクルで取得した中途半端な終値」が確定値として
        # 1日中キャッシュされ、判定がその時刻の値に固定される。
        bars = drop_unconfirmed_today_bars(fetched, now=now)
        if not bars.empty:
            self._entries[symbol] = _DailyBarEntry(bars=bars, trading_date=trading_date)
        return bars

    def clear(self) -> None:
        self._entries.clear()


class ContractCache:
    """qualifyContractsAsyncの結果をシンボル単位でキャッシュする。

    コントラクトの特定はサイクルごとに毎回行う必要がなく、銘柄数×サイクル数の
    往復をそのまま削減できる。
    """

    def __init__(self) -> None:
        self._contracts: Dict[str, Stock] = {}

    async def get_async(self, ib: IB, symbol: str) -> Stock:
        cached = self._contracts.get(symbol)
        if cached is not None:
            return cached

        contract = await qualify_stock_async(ib, symbol)
        self._contracts[symbol] = contract
        return contract

    def clear(self) -> None:
        self._contracts.clear()
