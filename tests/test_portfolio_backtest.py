"""backtest/portfolio.py の単体テスト。

このモジュールが答えるのは「口座で何が起きるか」なので、**銘柄独立の集計では
現れない制約**——同時保有数・枠の取り合い・日次サーキットブレーカー——が
実際に効いていることを固定する。効いていなければ、測っているものは
`backtest/engine.py` を並べたのと変わらない。
"""

import pandas as pd
import pytest

from backtest.engine import BacktestConfig, run_backtest
from backtest.portfolio import PortfolioConfig, run_portfolio_backtest


def _df(closes, start: str = "2020-01-01") -> pd.DataFrame:
    dates = pd.date_range(start, periods=len(closes), freq="D")
    return pd.DataFrame({"date": dates, "close": closes})


def _sawtooth(cycles: int, ma_window: int = 5) -> list:
    """押し目シグナルが繰り返し立つ形（上げてから急落）。"""
    out: list = []
    for _ in range(cycles):
        out.extend([100.0] * ma_window)
        out.append(80.0)
        out.extend([110.0] * ma_window)
    return out


_UNCONSTRAINED = dict(
    max_watchlist_size=None, require_long_term_uptrend=False, min_history_bars=5,
    max_position_size=10**9, max_order_notional_usd=10**12,
    daily_loss_limit_pct=10**9, max_daily_entry_orders=10**9,
)


def test_the_portfolio_engine_matches_the_single_symbol_engine() -> None:
    """制約を外した1銘柄では、既存エンジンと1円まで一致すること。

    **これがこのモジュールの土台である。** ポートフォリオ側だけで決済や
    手数料を書き直しているので、ずれていれば「資金を共有した結果」ではなく
    実装の差を測ってしまう。
    """
    df = _df(_sawtooth(12))
    common = dict(
        ma_window=5, threshold_pct=5.0, take_profit_pct=10.0, stop_loss_pct=5.0,
        trailing_stop_pct=5.0, risk_per_trade_pct=1.0, initial_equity=100_000.0,
    )

    single = run_backtest("AAA", df, BacktestConfig(**common))
    portfolio = run_portfolio_backtest(
        {"AAA": df}, ["AAA"],
        PortfolioConfig(**common, max_concurrent_positions=1, **_UNCONSTRAINED),
    )

    assert len(portfolio.trades) == len(single.trades) > 0
    assert portfolio.final_equity == pytest.approx(single.final_equity)
    for a, b in zip(single.trades, portfolio.trades, strict=True):
        assert b.pnl == pytest.approx(a.pnl)


def test_the_concurrent_limit_caps_the_number_of_open_positions() -> None:
    """同時に建つのは上限までであること。

    銘柄独立の集計では全銘柄が同時に建つ。押し目は市場全体の下げで一斉に
    出るため、この上限が無いと**実際には起こり得ない同時建玉**を前提に
    成績を測ることになる。
    """
    closes = _sawtooth(12)
    bars = {name: _df(closes) for name in ("AAA", "BBB", "CCC", "DDD")}
    config = PortfolioConfig(
        ma_window=5, threshold_pct=5.0, initial_equity=100_000.0,
        max_concurrent_positions=2, **_UNCONSTRAINED,
    )

    result = run_portfolio_backtest(bars, sorted(bars), config)

    # 同じ日に建った建玉の数を、エントリー日ごとに数える。
    per_day: dict = {}
    for trade in result.trades:
        per_day.setdefault(trade.entry_date, []).append(trade.symbol)
    assert per_day
    assert max(len(v) for v in per_day.values()) <= 2


def test_slots_are_taken_in_listed_order() -> None:
    """枠は記載順に埋まること（ライブと同じ）。

    ライブは `run_watchlist_cycle_async` がウォッチリストを順に処理し、
    枠が埋まった時点で以降の銘柄は判定に入らない。乖離の大きい順ではない。
    """
    closes = _sawtooth(12)
    bars = {name: _df(closes) for name in ("AAA", "BBB", "CCC")}
    config = PortfolioConfig(
        ma_window=5, threshold_pct=5.0, initial_equity=100_000.0,
        max_concurrent_positions=1, **_UNCONSTRAINED,
    )

    result = run_portfolio_backtest(bars, ["CCC", "BBB", "AAA"], config)

    # 全銘柄が同じ値動きなので、空いている枠は必ず先頭の銘柄が取る。
    # （決済した当日は同じ銘柄が建てられないため、その日だけ次点へ回る。）
    counts = {name: sum(1 for t in result.trades if t.symbol == name) for name in bars}
    assert result.trades[0].symbol == "CCC"
    assert counts["CCC"] > counts["BBB"] >= counts["AAA"]


def test_the_daily_circuit_breaker_stops_new_entries() -> None:
    """当日の実現損失が上限に達したら、その日は新規建てしないこと。"""
    closes = _sawtooth(12)
    bars = {name: _df(closes) for name in ("AAA", "BBB")}
    base = dict(
        ma_window=5, threshold_pct=5.0, initial_equity=100_000.0,
        max_concurrent_positions=2, max_watchlist_size=None,
        require_long_term_uptrend=False, min_history_bars=5,
        max_position_size=10**9, max_order_notional_usd=10**12,
        max_daily_entry_orders=10**9,
    )

    loose = run_portfolio_backtest(bars, sorted(bars), PortfolioConfig(daily_loss_limit_pct=10**9, **base))
    # 1トレードのリスクが1%なので、0.01%で頭打ちにすれば損切りの当日に必ず発動する。
    tight = run_portfolio_backtest(bars, sorted(bars), PortfolioConfig(daily_loss_limit_pct=0.01, **base))

    assert loose.circuit_breaker_days == 0
    assert tight.circuit_breaker_days > 0
    # 発動した日に建った建玉が1つも無いこと（日数だけ数えても意味が無い）。
    tripped = set(tight.circuit_breaker_dates)
    assert not [t for t in tight.trades if t.entry_date in tripped]


def test_the_watchlist_cap_truncates_in_listed_order() -> None:
    """監視上限を超える銘柄は、記載順で切り詰められること。"""
    closes = _sawtooth(12)
    bars = {name: _df(closes) for name in ("AAA", "BBB", "CCC")}
    config = PortfolioConfig(
        ma_window=5, threshold_pct=5.0, initial_equity=100_000.0,
        max_concurrent_positions=3, require_long_term_uptrend=False,
        min_history_bars=5, max_position_size=10**9,
        max_order_notional_usd=10**12, daily_loss_limit_pct=10**9,
        max_daily_entry_orders=10**9, max_watchlist_size=2,
    )

    result = run_portfolio_backtest(bars, ["AAA", "BBB", "CCC"], config)

    assert {t.symbol for t in result.trades} == {"AAA", "BBB"}


def test_the_exposure_and_drawdown_are_reported() -> None:
    """稼働率と最大DDが出ること（この2つがポートフォリオ検証の主目的）。"""
    closes = _sawtooth(12)
    result = run_portfolio_backtest(
        {"AAA": _df(closes)}, ["AAA"],
        PortfolioConfig(ma_window=5, threshold_pct=5.0, initial_equity=100_000.0,
                        max_concurrent_positions=1, **_UNCONSTRAINED),
    )

    assert 0.0 < result.average_exposure_pct < 100.0
    assert result.max_drawdown_pct() >= 0.0
    assert len(result.equity_curve) == len(_df(closes))
