"""複数銘柄のウォークフォワード検証と、銘柄横断の集計。

**単一銘柄の成績は運である。** ある銘柄の2年間でプロフィットファクターが
2.0でも、それはその銘柄がその期間たまたま押し目から反発しやすかっただけ
かもしれない。戦略にエッジがあるかどうかは、複数銘柄の検証期間トレードを
合算し、さらに銘柄ごとのバラつきを見て初めて判断できる。

集計の方針（銘柄独立・等ウェイト）:
    銘柄ごとに独立したウォークフォワードを回し、全銘柄のout-of-sample
    トレードを1つの母集団として合算する。各銘柄は同じ初期資金から始まる
    別々の検証であり、資金を共有しない。

    したがってこの集計が答えるのは「**押し目買いにエッジがあるか**」であって、
    「この設定で口座がいくら増えるか」ではない。後者には資金を共有して
    同時保有数(MAX_CONCURRENT_POSITIONS)や日次サーキットブレーカーまで
    再現するポートフォリオ検証が必要で、それは別物である。
    combined.total_pnl を口座の損益として読んではならない。
"""

import logging
import math
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import pandas as pd

from backtest.costs import CostModel
from backtest.engine import Trade
from backtest.metrics import TradeSummary, summarize_trades
from backtest.walk_forward import (
    DEFAULT_MIN_TRADES_FOR_SELECTION,
    ParameterGrid,
    run_walk_forward,
)

logger = logging.getLogger(__name__)


@dataclass
class SymbolOutcome:
    """1銘柄分のウォークフォワード結果（検証期間のみ）。"""

    symbol: str
    summary: TradeSummary
    num_windows: int
    skipped_windows: int
    trades: List[Trade] = field(default_factory=list)

    @property
    def is_profitable(self) -> bool:
        return self.summary.total_pnl > 0


@dataclass
class MultiSymbolReport:
    outcomes: List[SymbolOutcome]
    combined: TradeSummary

    @property
    def num_symbols(self) -> int:
        return len(self.outcomes)

    @property
    def num_symbols_with_trades(self) -> int:
        return sum(1 for o in self.outcomes if o.summary.num_trades > 0)

    @property
    def num_profitable_symbols(self) -> int:
        return sum(1 for o in self.outcomes if o.is_profitable)

    @property
    def profitable_symbol_ratio_pct(self) -> float:
        """検証期間にトレードが発生した銘柄のうち、プラスで終えた割合。

        合算のプロフィットファクターが1銘柄の大当たりで押し上げられて
        いないかを見るための指標。半分程度が目安で、極端に低ければ
        「ごく一部の銘柄だけで勝っている」＝再現性が低い。
        """
        if self.num_symbols_with_trades == 0:
            return 0.0
        return self.num_profitable_symbols / self.num_symbols_with_trades * 100.0

    @property
    def median_profit_factor(self) -> Optional[float]:
        """銘柄ごとのプロフィットファクターの中央値。

        負けトレードが無くinfになった銘柄は中央値の計算から除く
        （infを含めると中央値そのものがinfになりうる）。
        トレードが1件も無い銘柄も対象外。
        """
        values = [
            o.summary.profit_factor for o in self.outcomes
            if o.summary.num_trades > 0 and math.isfinite(o.summary.profit_factor)
        ]
        if not values:
            return None
        return float(statistics.median(values))


def run_multi_symbol_walk_forward(
    frames: Dict[str, pd.DataFrame],
    grid: ParameterGrid,
    train_bars: int,
    test_bars: int,
    initial_equity: float = 100_000.0,
    optimize_metric: str = "total_return_pct",
    costs: Optional[CostModel] = None,
    step_bars: Optional[int] = None,
    min_trades_for_selection: int = DEFAULT_MIN_TRADES_FOR_SELECTION,
) -> MultiSymbolReport:
    """銘柄ごとに独立してウォークフォワードを回し、結果を合算する。

    Args:
        frames: 銘柄 -> バーのDataFrame。
    """
    outcomes: List[SymbolOutcome] = []

    for symbol, df in frames.items():
        try:
            result = run_walk_forward(
                symbol, df, grid,
                train_bars=train_bars, test_bars=test_bars,
                initial_equity=initial_equity, optimize_metric=optimize_metric,
                costs=costs, step_bars=step_bars,
                min_trades_for_selection=min_trades_for_selection,
            )
        except Exception:
            # 1銘柄のデータ不備（列欠損・行数不足など）で検証全体を止めない。
            logger.exception("[%s] のウォークフォワード検証に失敗したため除外します。", symbol)
            continue

        trades = [t for window in result.windows for t in window.test_trades]
        outcomes.append(
            SymbolOutcome(
                symbol=symbol,
                summary=summarize_trades(trades),
                num_windows=len(result.windows),
                skipped_windows=result.skipped_windows,
                trades=trades,
            )
        )

    combined = summarize_trades([t for outcome in outcomes for t in outcome.trades])

    logger.info(
        "銘柄横断の集計: symbols=%d out-of-sample trades=%d win_rate=%.1f%% profit_factor=%.2f",
        len(outcomes), combined.num_trades, combined.win_rate_pct, combined.profit_factor,
    )

    return MultiSymbolReport(outcomes=outcomes, combined=combined)


def format_report(report: MultiSymbolReport, symbol_order: Optional[Sequence[str]] = None) -> str:
    """銘柄別の表と合算結果を人が読める形に整形する。"""
    outcomes = list(report.outcomes)
    if symbol_order is not None:
        rank = {symbol: i for i, symbol in enumerate(symbol_order)}
        outcomes.sort(key=lambda o: rank.get(o.symbol, len(rank)))

    lines: List[str] = []
    lines.append("=" * 78)
    lines.append("銘柄別 out-of-sample 成績")
    lines.append("=" * 78)
    lines.append(f"{'銘柄':<10}{'窓数':>6}{'見送り':>8}{'trades':>8}{'勝率':>8}{'PF':>8}{'損益':>12}")
    lines.append("-" * 78)

    for outcome in outcomes:
        summary = outcome.summary
        profit_factor = (
            "  inf" if math.isinf(summary.profit_factor) else f"{summary.profit_factor:.2f}"
        )
        lines.append(
            f"{outcome.symbol:<10}{outcome.num_windows:>6}{outcome.skipped_windows:>8}"
            f"{summary.num_trades:>8}{summary.win_rate_pct:>7.1f}%{profit_factor:>8}"
            f"{summary.total_pnl:>12,.0f}"
        )

    combined = report.combined
    combined_pf = "inf" if math.isinf(combined.profit_factor) else f"{combined.profit_factor:.2f}"
    median_pf = report.median_profit_factor

    lines.append("=" * 78)
    lines.append("合算（全銘柄の検証期間トレードを1つの母集団として集計）")
    lines.append("=" * 78)
    lines.append(f"  銘柄数            : {report.num_symbols}（うちトレード発生 {report.num_symbols_with_trades}）")
    lines.append(f"  トレード数        : {combined.num_trades}")
    lines.append(f"  勝率              : {combined.win_rate_pct:.1f}%")
    lines.append(f"  プロフィットファクター: {combined_pf}")
    lines.append(f"  平均利益/平均損失 : {combined.avg_win_pct:.2f}% / {combined.avg_loss_pct:.2f}%")
    lines.append(
        f"  PFの中央値(銘柄別) : {'N/A' if median_pf is None else f'{median_pf:.2f}'}"
    )
    lines.append(
        f"  プラスで終えた銘柄 : {report.num_profitable_symbols}/{report.num_symbols_with_trades} "
        f"({report.profitable_symbol_ratio_pct:.0f}%)"
    )
    lines.append("")
    lines.append("  ※ 各銘柄は資金を共有しない独立した検証。合算の損益は口座の損益ではなく、")
    lines.append("     「エッジがあるか」を見るための母集団としての集計値。")

    return "\n".join(lines)
