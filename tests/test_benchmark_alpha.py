"""ベンチマーク超過リターンの集計。

**この集計が守っている不変条件は「分からないものを有利側に倒さない」である。**
ベンチマークに突き合わないトレードを 0% として扱うと、上げ相場では
その分だけ超過リターンが良く出て、しかも黙って起きる。
"""

from datetime import date, timedelta

import pandas as pd
import pytest

from backtest.benchmark import compute_benchmark_alpha
from backtest.engine import Trade


def _benchmark(prices: dict) -> pd.DataFrame:
    days = sorted(prices)
    return pd.DataFrame({"date": days, "close": [prices[d] for d in days]})


def _trade(entry: date, exit_: date, pnl_pct: float) -> Trade:
    return Trade(
        symbol="TEST", entry_date=entry, entry_price=100.0, exit_date=exit_,
        exit_price=100.0 * (1 + pnl_pct / 100.0), quantity=1, reason="TAKE_PROFIT",
        pnl=pnl_pct, pnl_pct=pnl_pct,
    )


def test_a_trade_that_only_matched_the_market_shows_no_alpha() -> None:
    d0, d1 = date(2026, 1, 5), date(2026, 1, 9)
    bars = _benchmark({d0: 100.0, d1: 105.0})

    alpha = compute_benchmark_alpha([_trade(d0, d1, 5.0)], bars)

    assert alpha.n == 1
    assert alpha.mean_trade_pct == 5.0
    assert alpha.mean_benchmark_pct == pytest.approx(5.0)
    assert alpha.mean_excess_pct == pytest.approx(0.0)


def test_a_winning_trade_in_a_stronger_market_shows_negative_alpha() -> None:
    d0, d1 = date(2026, 1, 5), date(2026, 1, 9)
    bars = _benchmark({d0: 100.0, d1: 110.0})

    alpha = compute_benchmark_alpha([_trade(d0, d1, 4.0)], bars)

    assert alpha.mean_excess_pct == pytest.approx(-6.0)


def test_trades_the_benchmark_cannot_price_are_excluded_not_zeroed() -> None:
    """突き合わない日を 0% として数えると、超過リターンが有利側へ寄る。"""
    d0, d1 = date(2026, 1, 5), date(2026, 1, 9)
    bars = _benchmark({d0: 100.0, d1: 110.0})
    missing = _trade(date(2026, 3, 2), date(2026, 3, 6), 8.0)

    alpha = compute_benchmark_alpha([_trade(d0, d1, 4.0), missing], bars)

    assert alpha.n == 1
    assert alpha.unmatched == 1
    assert alpha.mean_excess_pct == pytest.approx(-6.0)


def test_overlapping_trades_discount_the_t_statistic() -> None:
    """同じ市場の動きを何度も数えている分、t値は割り引かれなければならない。"""
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(40)]
    bars = _benchmark({d: 100.0 for d in days})
    overlapping = [_trade(days[0], days[20], 1.0 + i * 0.1) for i in range(8)]

    alpha = compute_benchmark_alpha(overlapping, bars)

    assert alpha.avg_concurrent_trades == 8.0
    assert alpha.effective_n == 1.0
    assert abs(alpha.effective_t_stat) < abs(alpha.t_stat)


def test_an_empty_trade_list_is_not_an_error() -> None:
    alpha = compute_benchmark_alpha([], _benchmark({date(2026, 1, 5): 100.0}))

    assert alpha.n == 0
    assert alpha.t_stat == 0.0
