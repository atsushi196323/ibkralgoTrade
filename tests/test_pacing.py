"""core/pacing.py の単体テスト（実時間のsleepは発生させない）。"""

import asyncio

import pytest

from core.pacing import RequestPacer


class FakeClock:
    """テスト用の仮想時計。sleepが呼ばれた分だけ時刻が進む。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _make_pacer(max_requests: int = 3, window_seconds: float = 600.0):
    clock = FakeClock()
    pacer = RequestPacer(
        max_requests=max_requests,
        window_seconds=window_seconds,
        clock=clock.time,
        sleep=clock.sleep,
    )
    return pacer, clock


# --- 枠内では待たない -----------------------------------------------------------


def test_acquire_does_not_wait_while_under_limit() -> None:
    pacer, clock = _make_pacer(max_requests=3)

    async def run():
        for _ in range(3):
            await pacer.acquire()

    asyncio.run(run())

    assert clock.sleeps == []
    assert pacer.used_in_window() == 3


# --- 上限に達したら待つ ---------------------------------------------------------


def test_acquire_waits_until_oldest_request_leaves_the_window() -> None:
    pacer, clock = _make_pacer(max_requests=3, window_seconds=600.0)

    async def run():
        for _ in range(3):
            await pacer.acquire()
        # 4件目は最古(t=0)がウィンドウから外れるまで待たされる
        await pacer.acquire()

    asyncio.run(run())

    assert clock.sleeps == [600.0]
    assert clock.now == 600.0


def test_requests_spread_over_time_do_not_wait() -> None:
    pacer, clock = _make_pacer(max_requests=3, window_seconds=600.0)

    async def run():
        for _ in range(3):
            await pacer.acquire()
        # ウィンドウを跨いで時間が経てば枠は解放されている
        clock.now = 601.0
        await pacer.acquire()

    asyncio.run(run())

    assert clock.sleeps == []
    assert pacer.used_in_window() == 1


def test_window_slides_rather_than_resetting() -> None:
    """固定ウィンドウではなくスライディングウィンドウであること。"""
    pacer, clock = _make_pacer(max_requests=2, window_seconds=100.0)

    async def run():
        await pacer.acquire()          # t=0
        clock.now = 60.0
        await pacer.acquire()          # t=60
        # 枠は埋まっている。t=0の分が外れるのはt=100なので40秒待つ
        await pacer.acquire()

    asyncio.run(run())

    assert clock.sleeps == [40.0]


# --- 消費枠の観測 ---------------------------------------------------------------


def test_used_in_window_drops_old_requests() -> None:
    pacer, clock = _make_pacer(max_requests=5, window_seconds=100.0)

    async def run():
        await pacer.acquire()
        await pacer.acquire()

    asyncio.run(run())
    assert pacer.used_in_window() == 2

    clock.now = 100.0
    assert pacer.used_in_window() == 0


def test_reset_clears_consumed_slots() -> None:
    pacer, clock = _make_pacer(max_requests=2)

    async def run():
        await pacer.acquire()
        await pacer.acquire()

    asyncio.run(run())
    pacer.reset()

    assert pacer.used_in_window() == 0

    asyncio.run(run())
    assert clock.sleeps == []


# --- 既定値 ---------------------------------------------------------------------


def test_default_limit_stays_under_ibkr_cap() -> None:
    # IBKRの実際の上限は10分あたり60件。安全マージンを引いた値であること。
    pacer = RequestPacer()

    async def run():
        for _ in range(55):
            await pacer.acquire()

    asyncio.run(run())

    assert pacer.used_in_window() == 55


@pytest.mark.parametrize(
    "kwargs", [{"max_requests": 0}, {"max_requests": -1}, {"window_seconds": 0}],
)
def test_invalid_configuration_raises(kwargs) -> None:
    with pytest.raises(ValueError):
        RequestPacer(**kwargs)
