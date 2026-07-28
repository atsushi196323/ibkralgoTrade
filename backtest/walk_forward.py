"""ウォークフォワード検証。

ヒストリカルデータを「学習期間(train)」と直後の「検証期間(test)」に区切り、
学習期間内でのみパラメータグリッドをグリッドサーチして最良の設定を選び、
その設定を未知の検証期間に適用した成績だけを記録する。
これを開始位置をずらしながら繰り返すことで、特定の閾値が
たまたまその期間に効いただけ（カーブフィッティング）ではなく、
将来の未知データでも通用するエッジを持つかどうかを検証する。

ウィンドウの進め方（step_bars）:
    既定では検証期間の長さ（test_bars）だけ前進させる。こうすると検証期間が
    重複せず隙間なくデータを覆うため、手元のバーから最大限の
    out-of-sampleトレードを取り出せる。学習期間は重複するが、
    成績を集計するのは検証期間だけなので二重計上は起きない。

    train+testずつ前進させる（＝ウィンドウを完全に重ねない）と、
    日足2年(約504本)・train=252/test=63の既定値ではウィンドウが1個しか
    生成されず、検証期間63本＝数トレードで判断することになる。
    エッジの有無を見るにはサンプルが足りない。
"""

import logging
import math
from dataclasses import dataclass, replace
from itertools import product
from typing import List, Optional, Sequence, Tuple

import pandas as pd

from backtest.costs import CostModel
from backtest.engine import BacktestConfig, Trade, run_backtest
from backtest.metrics import PerformanceMetrics, TradeSummary, compute_metrics, summarize_trades

logger = logging.getLogger(__name__)


@dataclass
class ParameterGrid:
    ma_window: Sequence[int] = (10, 20, 30)
    threshold_pct: Sequence[float] = (3.0, 5.0, 7.0)
    take_profit_pct: Sequence[float] = (8.0, 10.0, 15.0)
    stop_loss_pct: Sequence[float] = (3.0, 5.0, 7.0)
    trailing_stop_pct: Sequence[float] = (5.0,)
    risk_per_trade_pct: Sequence[float] = (1.0,)

    def combinations(self) -> List[BacktestConfig]:
        fields = [
            "ma_window", "threshold_pct", "take_profit_pct",
            "stop_loss_pct", "trailing_stop_pct", "risk_per_trade_pct",
        ]
        value_lists = [getattr(self, name) for name in fields]
        return [BacktestConfig(**dict(zip(fields, combo))) for combo in product(*value_lists)]


@dataclass
class WalkForwardWindow:
    train_start_index: int
    train_end_index: int
    test_start_index: int
    test_end_index: int
    best_config: BacktestConfig
    train_metrics: PerformanceMetrics
    test_metrics: PerformanceMetrics
    test_trades: List[Trade]


@dataclass
class WalkForwardResult:
    symbol: str
    windows: List[WalkForwardWindow]
    combined_test_summary: TradeSummary
    # 学習期間で有効な設定を1つも選べず、検証を行わなかったウィンドウの数。
    # windowsの件数だけを見て「データが足りなかった」のか
    # 「トレード数が足りず判定を見送った」のかを区別できるようにする。
    skipped_windows: int = 0


# 学習期間の成績が「たまたま1トレード当たっただけ」の設定を選ばないための
# 最低トレード数。ウォークフォワードは過剰最適化を検出する仕組みなのに、
# 少数トレードの偶然を拾ってしまうと目的そのものが崩れる。
DEFAULT_MIN_TRADES_FOR_SELECTION: int = 5


def _score_candidate(
    metrics: PerformanceMetrics, optimize_metric: str
) -> Optional[Tuple[float, float]]:
    """候補設定の比較に使うスコアを返す。比較できない場合はNone。

    profit_factorは負けトレードが1件も無いとinfになる（metrics.py参照）。
    infどうしは大小がつかず、グリッドの並び順で最初に現れたものが
    機械的に選ばれてしまうため、総リターンを第2キーにして決着させる。
    """
    value = float(getattr(metrics, optimize_metric))
    if math.isnan(value):
        return None
    return value, metrics.total_return_pct


def _select_best_config(
    symbol: str,
    train_df: pd.DataFrame,
    candidates: List[BacktestConfig],
    initial_equity: float,
    costs: Optional[CostModel],
    optimize_metric: str,
    min_trades_for_selection: int,
) -> Optional[Tuple[BacktestConfig, PerformanceMetrics]]:
    """学習期間の成績が最良の設定を選ぶ。選べない場合はNone。"""
    best: Optional[Tuple[BacktestConfig, PerformanceMetrics]] = None
    best_score: Optional[Tuple[float, float]] = None

    for candidate in candidates:
        config = replace(candidate, initial_equity=initial_equity)
        if costs is not None:
            config = replace(config, costs=costs)

        train_result = run_backtest(symbol, train_df, config)
        train_metrics = compute_metrics(train_result)

        if train_metrics.num_trades < min_trades_for_selection:
            continue

        score = _score_candidate(train_metrics, optimize_metric)
        if score is None:
            continue

        if best_score is None or score > best_score:
            best_score = score
            best = (config, train_metrics)

    return best


def run_walk_forward(
    symbol: str,
    df: pd.DataFrame,
    grid: ParameterGrid,
    train_bars: int,
    test_bars: int,
    initial_equity: float = 100_000.0,
    optimize_metric: str = "total_return_pct",
    costs: Optional[CostModel] = None,
    step_bars: Optional[int] = None,
    min_trades_for_selection: int = DEFAULT_MIN_TRADES_FOR_SELECTION,
) -> WalkForwardResult:
    """パラメータグリッドをウォークフォワード検証する。

    Args:
        costs: 全候補設定に一律で適用する取引コスト。Noneなら
            BacktestConfigの既定（実際のIBKR相当のコスト）を使う。
            学習期間の最適化もコスト込みで行うため、コスト負けする
            パラメータ（利幅が薄すぎる設定等）はここで自然に脱落する。
        step_bars: ウィンドウを1回に前進させるバー数。Noneならtest_bars
            （＝検証期間が重複せず隙間なく並ぶ）。モジュールのdocstring参照。
        min_trades_for_selection: 学習期間でこの回数以上トレードした設定だけを
            選定対象にする。満たす候補が1つも無いウィンドウは検証を見送る
            （少数トレードの偶然で選んだ設定を検証しても意味がないため）。
    """
    if train_bars <= 0 or test_bars <= 0:
        raise ValueError("train_bars, test_bars は正の整数である必要があります。")

    step = test_bars if step_bars is None else step_bars
    if step <= 0:
        raise ValueError("step_bars は正の整数である必要があります。")

    candidates = grid.combinations()
    if not candidates:
        raise ValueError("パラメータグリッドが空です。")

    windows: List[WalkForwardWindow] = []
    all_test_trades: List[Trade] = []
    skipped_windows = 0

    window_span = train_bars + test_bars
    start = 0
    while start + window_span <= len(df):
        train_df = df.iloc[start: start + train_bars].reset_index(drop=True)
        test_df = df.iloc[start + train_bars: start + window_span].reset_index(drop=True)

        selected = _select_best_config(
            symbol, train_df, candidates, initial_equity, costs,
            optimize_metric, min_trades_for_selection,
        )
        if selected is None:
            skipped_windows += 1
            logger.warning(
                "[%s] 学習期間[%d:%d]で%d回以上トレードした候補が無かったため、"
                "このウィンドウの検証を見送ります。",
                symbol, start, start + train_bars - 1, min_trades_for_selection,
            )
            start += step
            continue

        best_config, best_train_metrics = selected

        test_result = run_backtest(symbol, test_df, best_config)
        test_metrics = compute_metrics(test_result)

        windows.append(
            WalkForwardWindow(
                train_start_index=start,
                train_end_index=start + train_bars - 1,
                test_start_index=start + train_bars,
                test_end_index=start + window_span - 1,
                best_config=best_config,
                train_metrics=best_train_metrics,
                test_metrics=test_metrics,
                test_trades=test_result.trades,
            )
        )
        all_test_trades.extend(test_result.trades)

        start += step

    combined_test_summary = summarize_trades(all_test_trades)

    logger.info(
        "[%s] ウォークフォワード検証完了: windows=%d (見送り%d) "
        "out-of-sample trades=%d win_rate=%.1f%% profit_factor=%.2f",
        symbol, len(windows), skipped_windows, combined_test_summary.num_trades,
        combined_test_summary.win_rate_pct, combined_test_summary.profit_factor,
    )

    return WalkForwardResult(
        symbol=symbol,
        windows=windows,
        combined_test_summary=combined_test_summary,
        skipped_windows=skipped_windows,
    )
