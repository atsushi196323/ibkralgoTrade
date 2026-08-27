"""横断ランクのシグナルを、一貫性の基準（`backtest/robustness.py`）で判定する。

    python -m scripts.check_robustness --csv-dir bars/universe

**`t ≥ 2.0` の代わりに使う。** あちらはこの母集団と保有期間では構造的に
達成できず（実効観測42・情報比0.63が必要）、本物のプレミアムでも落ちる。

判定は7項目すべてを通ることが条件で、部分点は無い。t値は参考として併記する。
"""

import argparse
import glob
import logging
import os
import sys
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from backtest.csv_source import load_bars_from_csv
from strategy.momentum import MOMENTUM_LOOKBACK_BARS, MOMENTUM_SKIP_BARS
from backtest.robustness import (
    RobustnessReport,
    check_benchmark,
    check_horizons,
    check_measurement_spaces,
    check_monotonicity,
    check_phases,
    check_prior,
    check_subperiods,
    check_survivorship,
)
from backtest.signal_study import add_cross_sectional_percentile
from backtest.survivorship import annualised_death_rate, break_even_death_rate

logger = logging.getLogger(__name__)

MIN_BARS: int = 400
RANK_COLUMN: str = "cs_momentum_rank"
PHASES: Tuple[int, ...] = (0, 10, 20, 30, 40, 50)
# 現実の「株主価値がゼロになる」上場廃止率の上限側（`scripts/check_survivorship`）。
REFERENCE_ANNUAL_DEATH_RATE: float = 0.02


def momentum_12_1(frame: pd.DataFrame) -> pd.Series:
    """12ヶ月モメンタム（直近1ヶ月を除く）。

    **定義は `strategy/momentum.py` から取る。** ここで書き直すと、測っている
    ものとライブで動くものが別々に育つ（CLAUDE.md「レイヤーの責務」）。
    """
    close = frame["close"].astype(float)
    return close.shift(MOMENTUM_SKIP_BARS) / close.shift(MOMENTUM_LOOKBACK_BARS) - 1.0


def _load(path: str) -> Dict[str, pd.DataFrame]:
    bars: Dict[str, pd.DataFrame] = {}
    for csv_path in sorted(glob.glob(os.path.join(path, "*.csv"))):
        try:
            frame = load_bars_from_csv(csv_path)
        except Exception:
            continue
        if len(frame) >= MIN_BARS:
            bars[os.path.basename(csv_path)[: -len(".csv")]] = frame
    return bars


def _periods(
    bars: Dict[str, pd.DataFrame], hold: int, offset: int,
) -> List[Tuple[pd.Timestamp, Dict[str, float], List[float]]]:
    """非重複リバランスの各期で (日付, {銘柄: 順位と騰落率}, 母集団の騰落率) を返す。"""
    frames = {}
    for symbol, frame in bars.items():
        frames[symbol] = (
            pd.to_datetime(frame["date"]).dt.normalize().to_numpy(),
            frame["open" if "open" in frame.columns else "close"].to_numpy(float),
            frame["close"].to_numpy(float),
            frame[RANK_COLUMN].to_numpy(float),
        )
    all_days = sorted({day for days, *_ in frames.values() for day in days})

    out = []
    for day in all_days[252 + offset::hold]:
        ranked: Dict[str, float] = {}
        changes: Dict[str, float] = {}
        everyone: List[float] = []
        for symbol, (days, entry, close, rank) in frames.items():
            i = int(np.searchsorted(days, day))
            if i >= len(days) or days[i] != day:
                continue
            start, end = i + 1, i + 1 + hold
            if end >= len(days):
                continue
            p0, p1 = entry[start], close[end]
            if not (p0 > 0 and p1 > 0):
                continue
            change = (p1 / p0 - 1.0) * 100.0
            everyone.append(change)
            if np.isfinite(rank[i]):
                ranked[symbol] = float(rank[i])
                changes[symbol] = change
        if len(everyone) >= 100 and len(ranked) >= 100:
            out.append((pd.Timestamp(day), {s: (ranked[s], changes[s]) for s in ranked}, everyone))
    return out


def _to_log(values: Sequence[float]) -> float:
    """騰落率(%)の対数平均。-100%を扱えないので最悪 -99.9% で止める。"""
    return float(np.mean([np.log(max(1.0 + v / 100.0, 0.001)) * 100.0 for v in values]))


def _bucket_excess(periods, low: float, high: float, space: str = "log") -> List[float]:
    """順位が (low, high] に入る銘柄の、母集団に対する超過を期ごとに返す。

    **既定は対数である。** 算術平均は60日以上の保有で右の裾に支配され、
    符号ごと変わる（`backtest/signal_study.py` のdocstring。2026-08-27に
    下位10%の60日超過が算術+33.8% / 対数-5.75% と実測）。**この検定は
    「向きが一貫しているか」を見るものなので、符号が裾で決まる測り方を
    使ってはならない。** 算術は `check_measurement_spaces` が別途見る。
    """
    reduce = {
        "log": _to_log,
        "arith": lambda xs: float(np.mean(xs)),
        "median": lambda xs: float(np.median(xs)),
    }[space]
    values = []
    for _, ranked, everyone in periods:
        picked = [c for r, c in ranked.values() if low < r <= high]
        if len(picked) >= 5:
            values.append(reduce(picked) - reduce(everyone))
    return values


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv-dir", default="bars/universe")
    parser.add_argument("--horizons", type=int, nargs="+", default=[20, 60, 120])
    parser.add_argument("--hold", type=int, default=60, help="主判定に使う保有営業日数")
    parser.add_argument(
        "--no-prior", action="store_true",
        help="事前の根拠が無い仮説として判定する（新しい仮説はまずこちらで測ること）",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)

    bars = _load(args.csv_dir)
    if not bars:
        print(f"{args.csv_dir} に検証できる銘柄がありません。", file=sys.stderr)
        return 1
    bars = add_cross_sectional_percentile(bars, momentum_12_1, RANK_COLUMN)
    print(f"銘柄 {len(bars)}件 / 上位10%を保有 / 非重複リバランス\n")

    per_year = 250.0 / args.hold
    # **時系列で並べ直す。** 位相ごとに連結したままだと、部分期間の分割が
    # 「リストの中央」になって暦の中央にならない（前半・後半が混ざる）。
    base = sorted(
        (p for phase in PHASES for p in _periods(bars, args.hold, phase)),
        key=lambda row: row[0],
    )

    # 1. 保有期間
    by_horizon = {}
    for horizon in args.horizons:
        rows = [p for phase in PHASES for p in _periods(bars, horizon, phase)]
        by_horizon[horizon] = float(np.mean(_bucket_excess(rows, 0.90, 1.01)))  # 対数

    # 2. 位相
    by_phase = [
        float(np.mean(_bucket_excess(_periods(bars, args.hold, phase), 0.90, 1.01)))
        for phase in PHASES
    ]

    # 3. 測定空間（ここだけは3つとも見る。向きが揃うかが検定内容そのもの）
    arithmetic = float(np.mean(_bucket_excess(base, 0.90, 1.01, "arith")))
    log_mean = float(np.mean(_bucket_excess(base, 0.90, 1.01, "log")))
    median = float(np.mean(_bucket_excess(base, 0.90, 1.01, "median")))

    # 4. 単調性（上位10% > 上位20% > 母集団(=0) > 下位10%）
    buckets = [
        float(np.mean(_bucket_excess(base, 0.90, 1.01))),
        float(np.mean(_bucket_excess(base, 0.80, 1.01))),
        0.0,
        float(np.mean(_bucket_excess(base, -0.01, 0.10))),
    ]

    # 5. 部分期間（`base` は暦順に並べてある）
    midpoint = base[len(base) // 2][0] if base else None
    halves = {
        f"前半(〜{midpoint.date()})": float(np.mean(
            _bucket_excess([p for p in base if p[0] <= midpoint], 0.90, 1.01))),
        f"後半({midpoint.date()}〜)": float(np.mean(
            _bucket_excess([p for p in base if p[0] > midpoint], 0.90, 1.01))),
    }

    # 7. 生存バイアス
    top_means = [np.mean([c for r, c in ranked.values() if r > 0.90]) for _, ranked, _ in base]
    pop_means = [np.mean(everyone) for _, _, everyone in base]
    n_top = int(np.mean([sum(1 for r, _ in ranked.values() if r > 0.90) for _, ranked, _ in base]))
    n_pop = int(np.mean([len(everyone) for _, _, everyone in base]))
    rate = break_even_death_rate(
        float(np.mean(top_means)), float(np.mean(pop_means)), n_top, n_pop,
        top_share_of_deaths=0.30,   # 現実と逆向きの、最も不利な仮定
    )
    annual = annualised_death_rate(rate, per_year) if rate is not None else None

    report = RobustnessReport(
        signal=f"横断モメンタム 12-1 上位10%（保有{args.hold}日）",
        checks=[
            check_prior(
                not args.no_prior,
                "複数市場・複数資産クラスで再現が報告されている（測定前に宣言）"
                if not args.no_prior else "事前の根拠は宣言されていない",
            ),
                    check_benchmark(log_mean),
            check_horizons(by_horizon),
            check_phases(by_phase),
            check_measurement_spaces(arithmetic, log_mean, median),
            check_monotonicity(buckets),
            check_subperiods(halves),
            check_survivorship(annual, REFERENCE_ANNUAL_DEATH_RATE),
        ],
    )
    print(report.describe())
    print(f"\n（参考）年率換算した対母集団の超過: 対数 {log_mean * per_year:+.2f}% "
          f"/ 算術 {arithmetic * per_year:+.2f}%")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
