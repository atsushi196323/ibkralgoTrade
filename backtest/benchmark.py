"""トレードの成績を、同じ期間ベンチマークを持っていた場合と比べる。

**既存の指標はすべて「ゼロ」を基準にしている。** プロフィットファクターも
期待値/trade も、勝ちの合計と負けの合計だけで決まるため、上げ相場では
「市場が上げたぶん」がそのままエッジとして計上される。実測（2026-08-26、
42銘柄・10年・$13,300・枠3）では:

    1トレードの純損益         +0.492%
    同期間SPYを持っていた場合 +0.445%
    → 超過リターン            +0.047%（t = 0.14）

つまり `CLAUDE.md` が「有意（t≈2.9）」としていた押し目買いのエッジは、
**ほぼ全額が保有期間中の市場リターンだった**。627銘柄へ広げると超過は
-0.26%〜-0.38%（t = -3.0〜-3.3）で、有意にマイナスへ振れる。

したがって戦略の採否は、ここが返す超過リターンで判断すること。
プロフィットファクターが1を超えていることは、エッジの証拠にならない。
"""

import logging
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

import pandas as pd

from backtest.engine import Trade, _trading_day_of

logger = logging.getLogger(__name__)


@dataclass
class BenchmarkAlpha:
    """ベンチマーク超過リターンの集計。"""

    n: int
    # ベンチマークの日付に突き合わなかったトレード数。**0であることを確認する
    # こと。** 落ちた分は超過リターンの計算から抜けるため、多いと集計が
    # トレードの一部しか見ていないことになる。
    unmatched: int
    mean_trade_pct: float
    mean_benchmark_pct: float
    mean_excess_pct: float
    excess_sd_pct: float
    avg_holding_days: float
    # 同時に建っていたトレードの平均本数。押し目は市場全体の下げで一斉に
    # 出るため、トレードは独立ではない。t値はこの分だけ楽観に出る。
    avg_concurrent_trades: float

    @property
    def t_stat(self) -> float:
        """独立を仮定した t値。**過大に出る**（下の実効値と併せて読むこと）。"""
        if self.n < 2 or self.excess_sd_pct <= 0:
            return 0.0
        return self.mean_excess_pct / (self.excess_sd_pct / math.sqrt(self.n))

    @property
    def effective_n(self) -> float:
        """重なりで割り引いたトレード数。同時に m 本建っていれば実質 n/m 本。"""
        if self.avg_concurrent_trades <= 1.0:
            return float(self.n)
        return self.n / self.avg_concurrent_trades

    @property
    def effective_t_stat(self) -> float:
        if self.effective_n < 2 or self.excess_sd_pct <= 0:
            return 0.0
        return self.mean_excess_pct / (self.excess_sd_pct / math.sqrt(self.effective_n))

    def describe(self) -> str:
        lines = [
            f"トレード数 {self.n}（突き合わなかった {self.unmatched}）"
            f" 平均保有 {self.avg_holding_days:.1f}日",
            f"  1トレードの純損益          {self.mean_trade_pct:+.3f}%",
            f"  同期間ベンチマーク         {self.mean_benchmark_pct:+.3f}%",
            f"  超過リターン（アルファ）   {self.mean_excess_pct:+.3f}%"
            f"  SD={self.excess_sd_pct:.2f}",
            f"  t = {self.t_stat:+.2f}"
            f"（重なり補正後 {self.effective_t_stat:+.2f}"
            f" / 実効 {self.effective_n:.0f}トレード相当）",
        ]
        return "\n".join(lines)


def _close_by_day(bars: pd.DataFrame) -> Dict[object, float]:
    if "date" not in bars.columns:
        raise ValueError("ベンチマークのバーに date 列がありません。")
    return {
        _trading_day_of(date): float(close)
        for date, close in zip(bars["date"], bars["close"])
    }


def compute_benchmark_alpha(
    trades: Sequence[Trade],
    benchmark_bars: pd.DataFrame,
) -> BenchmarkAlpha:
    """各トレードの損益から、同じ日付区間のベンチマーク騰落率を引く。

    **突き合わなかったトレードは 0% として扱わず、除外して数える。**
    ベンチマークが休場でトレードだけが存在する日を 0% と見なすと、
    その分だけ超過リターンが有利側へ寄り、しかも黙って起きる。
    """
    closes = _close_by_day(benchmark_bars)
    excess: List[float] = []
    net: List[float] = []
    bench: List[float] = []
    holding: List[float] = []
    spans: List[tuple] = []
    unmatched = 0

    for trade in trades:
        if trade.entry_date is None or trade.exit_date is None:
            unmatched += 1
            continue
        entry_day = _trading_day_of(trade.entry_date)
        exit_day = _trading_day_of(trade.exit_date)
        start, end = closes.get(entry_day), closes.get(exit_day)
        if start is None or end is None or start <= 0 or exit_day <= entry_day:
            unmatched += 1
            continue
        benchmark_pct = (end / start - 1.0) * 100.0
        net.append(trade.pnl_pct)
        bench.append(benchmark_pct)
        excess.append(trade.pnl_pct - benchmark_pct)
        holding.append((exit_day - entry_day).days)
        spans.append((entry_day, exit_day))

    if unmatched:
        logger.warning(
            "ベンチマークに突き合わなかったトレードが%d件あります"
            "（超過リターンの集計から除外しました）。",
            unmatched,
        )

    n = len(excess)
    if n == 0:
        return BenchmarkAlpha(0, unmatched, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    mean_excess = sum(excess) / n
    variance = sum((x - mean_excess) ** 2 for x in excess) / n
    return BenchmarkAlpha(
        n=n,
        unmatched=unmatched,
        mean_trade_pct=sum(net) / n,
        mean_benchmark_pct=sum(bench) / n,
        mean_excess_pct=mean_excess,
        excess_sd_pct=math.sqrt(variance),
        avg_holding_days=sum(holding) / n,
        avg_concurrent_trades=_average_concurrent_trades(spans),
    )


def _average_concurrent_trades(spans: Sequence[tuple]) -> float:
    """建玉が存在した日について、同時に建っていた本数の平均。

    トレードが重なっているほど、同じ市場の動きを何度も数えることになる。
    t値をそのまま読むと有意性を過大評価するため、割引率としてこれを使う。
    """
    if not spans:
        return 0.0
    open_count: Dict[object, int] = {}
    for entry_day, exit_day in spans:
        day = entry_day
        step = pd.Timedelta(days=1)
        while day <= exit_day:
            open_count[day] = open_count.get(day, 0) + 1
            day = day + step
    if not open_count:
        return 0.0
    return sum(open_count.values()) / len(open_count)
