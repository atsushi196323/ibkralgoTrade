"""IBKRのリクエスト・ペーシング制限を守るためのレートリミッター。

IBKRはヒストリカルデータのリクエストに「10分あたり60件」という上限を課しており、
超えると pacing violation (エラー162) を返す。厄介なのは、ib_insyncの既定設定
(IB.RaiseRequestErrors=False) ではこれが例外にならず**空のバー列**として返る点で、
呼び出し側からは「データが無い銘柄」と区別がつかない。結果、ボットは何も
シグナルを出さないまま延々と動き続ける。

そのためリクエスト数を計算で調整するのではなく、発行側で構造的に制限する。
ウォッチリストの銘柄数やポーリング間隔を後から変えても破綻しないようにするのが
このモジュールの目的である。
"""

import asyncio
import logging
import time
from collections import deque
from typing import Awaitable, Callable, Deque, Optional

logger = logging.getLogger(__name__)

# IBKRのヒストリカルデータ制限: 10分(600秒)あたり60リクエスト。
HISTORICAL_MAX_REQUESTS: int = 60
HISTORICAL_WINDOW_SECONDS: float = 600.0

# 上限ちょうどを狙うと、IBKR側とこちらの計測開始点のズレで違反になりうるため
# 数件分の余裕を残して手前で止める。
HISTORICAL_SAFETY_MARGIN: int = 5


class RequestPacer:
    """直近window_seconds間の発行数をmax_requests件に抑えるスライディングウィンドウ制限。

    `await pacer.acquire()` は枠が空いていれば即座に返り、埋まっていれば
    最古のリクエストがウィンドウから外れるまで待ってから返る。
    """

    def __init__(
        self,
        max_requests: int = HISTORICAL_MAX_REQUESTS - HISTORICAL_SAFETY_MARGIN,
        window_seconds: float = HISTORICAL_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
    ) -> None:
        if max_requests <= 0:
            raise ValueError("max_requests は正の整数である必要があります。")
        if window_seconds <= 0:
            raise ValueError("window_seconds は正の値である必要があります。")

        self._max_requests = max_requests
        self._window_seconds = window_seconds
        # 実時間に依存しないテストのため、時計とsleepを差し替え可能にしている。
        self._clock = clock
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._timestamps: Deque[float] = deque()

    def _prune(self, now: float) -> None:
        while self._timestamps and now - self._timestamps[0] >= self._window_seconds:
            self._timestamps.popleft()

    def used_in_window(self) -> int:
        """現在のウィンドウ内で消費済みの枠数。"""
        self._prune(self._clock())
        return len(self._timestamps)

    def reset(self) -> None:
        self._timestamps.clear()

    async def acquire(self) -> None:
        """枠を1つ確保する。埋まっていれば空くまで待つ。"""
        while True:
            now = self._clock()
            self._prune(now)

            if len(self._timestamps) < self._max_requests:
                self._timestamps.append(now)
                return

            # 最古のリクエストがウィンドウから外れるまで待てば必ず枠が空く。
            wait_seconds = self._window_seconds - (now - self._timestamps[0])
            logger.warning(
                "IBKRのペーシング制限(%d件/%.0f秒)に達したため、%.1f秒待機します。"
                "ウォッチリストの銘柄数を減らすか、ポーリング間隔を延ばすことを検討してください。",
                self._max_requests, self._window_seconds, wait_seconds,
            )
            await self._sleep(max(wait_seconds, 0.0))
