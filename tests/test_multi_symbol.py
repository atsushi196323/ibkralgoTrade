"""backtest/multi_symbol.py の単体テスト。"""

import math

import pandas as pd
import pytest

from backtest.costs import ZERO_COST
from backtest.engine import Trade
from backtest.metrics import summarize_trades
from backtest.multi_symbol import (
    MultiSymbolReport,
    SymbolOutcome,
    format_report,
    run_multi_symbol_walk_forward,
)
from backtest.walk_forward import ParameterGrid


def _trade(pnl: float, symbol: str = "TEST") -> Trade:
    return Trade(
        symbol=symbol, entry_date=None, entry_price=100.0, exit_date=None,
        exit_price=100.0 + pnl, quantity=1, reason="TEST", pnl=pnl, pnl_pct=pnl,
    )


def _outcome(symbol: str, pnls: list) -> SymbolOutcome:
    trades = [_trade(pnl, symbol) for pnl in pnls]
    return SymbolOutcome(
        symbol=symbol, summary=summarize_trades(trades),
        num_windows=1, skipped_windows=0, trades=trades,
    )


def _report(outcomes: list) -> MultiSymbolReport:
    combined = summarize_trades([t for o in outcomes for t in o.trades])
    return MultiSymbolReport(outcomes=outcomes, combined=combined)


def _make_cyclical_df(num_cycles: int) -> pd.DataFrame:
    cycle = [100.0] * 5 + [90.0, 92.0, 95.0, 98.0, 106.0]
    return pd.DataFrame({"close": cycle * num_cycles})


# --- 集計 ---------------------------------------------------------------------------


def test_combined_summary_pools_trades_across_symbols() -> None:
    report = _report([_outcome("AAA", [10.0, -5.0]), _outcome("BBB", [20.0])])

    assert report.combined.num_trades == 3
    assert report.combined.total_pnl == pytest.approx(25.0)
    assert report.num_symbols == 2


def test_profitable_symbol_ratio_ignores_symbols_without_trades() -> None:
    """トレードが発生しなかった銘柄は分母から外すこと。

    含めると「勝てた銘柄の割合」が、単にシグナルが出なかっただけの
    銘柄で薄まってしまう。
    """
    report = _report([
        _outcome("AAA", [10.0]),      # プラス
        _outcome("BBB", [-10.0]),     # マイナス
        _outcome("CCC", []),          # トレードなし
    ])

    assert report.num_symbols == 3
    assert report.num_symbols_with_trades == 2
    assert report.num_profitable_symbols == 1
    assert report.profitable_symbol_ratio_pct == pytest.approx(50.0)


def test_profitable_symbol_ratio_is_zero_when_no_symbol_traded() -> None:
    report = _report([_outcome("AAA", []), _outcome("BBB", [])])

    assert report.profitable_symbol_ratio_pct == 0.0


def test_median_profit_factor_excludes_infinite_and_empty_symbols() -> None:
    """負け無しの銘柄(PF=inf)を含めると中央値がinfになりうるため除外する。"""
    report = _report([
        _outcome("AAA", [10.0, -10.0]),   # PF = 1.0
        _outcome("BBB", [30.0, -10.0]),   # PF = 3.0
        _outcome("CCC", [10.0]),          # 負け無し -> inf
        _outcome("DDD", []),              # トレードなし
    ])

    assert math.isinf(_outcome("CCC", [10.0]).summary.profit_factor)
    assert report.median_profit_factor == pytest.approx(2.0)


def test_median_profit_factor_is_none_when_nothing_is_measurable() -> None:
    report = _report([_outcome("AAA", []), _outcome("BBB", [10.0])])

    assert report.median_profit_factor is None


def test_symbol_is_profitable_only_when_total_pnl_is_positive() -> None:
    assert _outcome("AAA", [10.0, -5.0]).is_profitable is True
    assert _outcome("BBB", [5.0, -5.0]).is_profitable is False
    assert _outcome("CCC", []).is_profitable is False


# --- run_multi_symbol_walk_forward --------------------------------------------------


def _grid() -> ParameterGrid:
    return ParameterGrid(
        ma_window=(5,), threshold_pct=(5.0,), take_profit_pct=(10.0,),
        stop_loss_pct=(5.0,), trailing_stop_pct=(50.0,), risk_per_trade_pct=(1.0,),
    )


def test_runs_every_symbol_and_aggregates() -> None:
    frames = {"AAA": _make_cyclical_df(8), "BBB": _make_cyclical_df(8)}

    report = run_multi_symbol_walk_forward(
        frames, _grid(), train_bars=30, test_bars=10,
        costs=ZERO_COST, min_trades_for_selection=1,
    )

    assert [o.symbol for o in report.outcomes] == ["AAA", "BBB"]
    assert report.combined.num_trades == sum(o.summary.num_trades for o in report.outcomes)
    assert all(o.num_windows == 5 for o in report.outcomes)


def test_a_broken_symbol_does_not_abort_the_whole_run() -> None:
    """1銘柄のデータ不備で検証全体を止めないこと。"""
    frames = {
        "AAA": _make_cyclical_df(8),
        # 行数は足りているがclose列が無い -> run_backtestがValueErrorを投げる
        "BAD": pd.DataFrame({"open": [1.0] * 80}),
        "BBB": _make_cyclical_df(8),
    }

    report = run_multi_symbol_walk_forward(
        frames, _grid(), train_bars=30, test_bars=10,
        costs=ZERO_COST, min_trades_for_selection=1,
    )

    assert [o.symbol for o in report.outcomes] == ["AAA", "BBB"]


def test_symbol_with_too_little_data_yields_no_windows() -> None:
    frames = {"AAA": _make_cyclical_df(8), "SHORT": _make_cyclical_df(1)}

    report = run_multi_symbol_walk_forward(
        frames, _grid(), train_bars=30, test_bars=10,
        costs=ZERO_COST, min_trades_for_selection=1,
    )

    short = next(o for o in report.outcomes if o.symbol == "SHORT")
    assert short.num_windows == 0
    assert short.summary.num_trades == 0


# --- format_report ------------------------------------------------------------------


def test_format_report_lists_every_symbol_and_the_combined_block() -> None:
    report = _report([_outcome("AAA", [10.0, -5.0]), _outcome("BBB", [20.0])])

    text = format_report(report)

    assert "AAA" in text
    assert "BBB" in text
    assert "合算" in text
    assert "口座の損益ではなく" in text


def test_format_report_keeps_the_requested_symbol_order() -> None:
    report = _report([_outcome("BBB", [1.0]), _outcome("AAA", [1.0])])

    text = format_report(report, symbol_order=["AAA", "BBB"])

    assert text.index("AAA") < text.index("BBB")


def test_format_report_renders_infinite_profit_factor() -> None:
    report = _report([_outcome("AAA", [10.0])])  # 負け無し -> inf

    text = format_report(report)

    assert "inf" in text
