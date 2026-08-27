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
from strategy.momentum import momentum_series
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
MOMENTUM_TOP_PCT: float = 0.10  # --top-pct で上書きできる
RANK_COLUMN: str = "cs_momentum_rank"
PHASES: Tuple[int, ...] = (0, 10, 20, 30, 40, 50)
# 現実の「株主価値がゼロになる」上場廃止率の上限側（`scripts/check_survivorship`）。
REFERENCE_ANNUAL_DEATH_RATE: float = 0.02





def _momentum_of(frame: pd.DataFrame) -> pd.Series:
    """バーのDataFrameから 12-1 モメンタムの列を作る。

    定義は `strategy/momentum.py` にだけ置く（測定とライブを同一にするため）。
    ここはその薄い受け口である。
    """
    return momentum_series(frame["close"])


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


# 母集団を絞るときの、直近の売買代金を測る窓（営業日）。
TURNOVER_WINDOW_BARS: int = 60


def _periods(
    bars: Dict[str, pd.DataFrame], hold: int, offset: int, universe_size: int = 0,
    min_population: int = 100,
) -> List[Tuple[pd.Timestamp, Dict[str, float], List[float]]]:
    """非重複リバランスの各期で (日付, {銘柄: 順位と騰落率}, 母集団の騰落率) を返す。

    `universe_size` が正なら、**その日までの売買代金**で上位N銘柄に絞る。
    ライブでは母集団を毎日その件数ぶん取得する必要があるので、実運用可能な
    規模でもエッジが残るかを見るための引数である。

    **絞り込みは先読みになってはならない。** 「10年通しての売買代金上位」で
    選ぶと、後から大きくなった銘柄を最初から知っていたことになる。**その日
    までの直近60営業日の中央値**で毎回選び直す（`RankHistoryStore` が
    前日比ではなく中央値を使うのと同じ理由——1日の異常値で母集団が入れ替わる
    のを避ける）。

    **成績で選んではならない。** 売買代金は成績と無関係な軸であり、しかも
    2026-08-06の測定で「上位100位と201–400位に有意差なし」と分かっている
    （効くのは下限を切ることであって、上位に絞ることではない）。
    """
    frames = {}
    for symbol, frame in bars.items():
        close = frame["close"].to_numpy(float)
        volume = (
            frame["volume"].to_numpy(float)
            if "volume" in frame.columns else np.full(len(close), np.nan)
        )
        frames[symbol] = (
            pd.to_datetime(frame["date"]).dt.normalize().to_numpy(),
            frame["open" if "open" in frame.columns else "close"].to_numpy(float),
            close,
            frame[RANK_COLUMN].to_numpy(float),
            close * volume,
        )
    all_days = sorted({day for days, *_ in frames.values() for day in days})

    out = []
    for day in all_days[252 + offset::hold]:
        candidates = []
        for symbol, (days, entry, close, rank, turnover) in frames.items():
            i = int(np.searchsorted(days, day))
            if i >= len(days) or days[i] != day:
                continue
            start, end = i + 1, i + 1 + hold
            if end >= len(days):
                continue
            p0, p1 = entry[start], close[end]
            if not (p0 > 0 and p1 > 0):
                continue
            window = turnover[max(0, i - TURNOVER_WINDOW_BARS + 1): i + 1]
            liquidity = float(np.nanmedian(window)) if len(window) else float("nan")
            candidates.append((symbol, (p1 / p0 - 1.0) * 100.0, rank[i], liquidity))

        if universe_size > 0:
            # 売買代金が読めない銘柄は落とす（順位を付けられない）。
            candidates = [c for c in candidates if np.isfinite(c[3])]
            candidates.sort(key=lambda c: c[3], reverse=True)
            candidates = candidates[:universe_size]

        ranked: Dict[str, float] = {}
        changes: Dict[str, float] = {}
        everyone: List[float] = []
        # **順位は絞った後の母集団の中で付け直す。** 627銘柄の中での上位10%と、
        # 200銘柄の中での上位10%は別の集合である。付け直さないと、絞った母集団に
        # 「元の627銘柄での上位10%」が何件残るか分からないまま測ることになる。
        momentum = {c[0]: c[2] for c in candidates if np.isfinite(c[2])}
        repercentiled = (
            pd.Series(momentum).rank(pct=True).to_dict() if universe_size > 0 else None
        )
        for symbol, change, rank, _liquidity in candidates:
            everyone.append(change)
            effective = repercentiled.get(symbol) if repercentiled is not None else rank
            if effective is not None and np.isfinite(effective):
                ranked[symbol] = float(effective)
                changes[symbol] = change
        if len(everyone) >= min_population and len(ranked) >= min_population:
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
        "--universe-size", type=int, default=0,
        help="母集団を直近の売買代金で上位N銘柄に絞る（0=全件）。ライブで維持できる"
             "規模でもエッジが残るかを見る",
    )
    parser.add_argument(
        "--top-pct", type=float, default=MOMENTUM_TOP_PCT,
        help="上位何割を保有するか。**成績を見て刻み直してはならない**"
             "（母集団を小さくしたときに保有件数を確保する用途に限る）",
    )
    parser.add_argument(
        "--min-population", type=int, default=100,
        help="1期あたりの母集団の下限。**下げると横断ランクの意味が薄れる**"
             "（20銘柄中の上位10%は2銘柄で、測定した構成ではない）",
    )
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
    global MOMENTUM_TOP_PCT
    MOMENTUM_TOP_PCT = args.top_pct
    bars = add_cross_sectional_percentile(bars, _momentum_of, RANK_COLUMN)
    scope = (
        f"全{len(bars)}件" if args.universe_size <= 0
        else f"{len(bars)}件から売買代金で毎回{args.universe_size}件へ絞る"
    )
    print(f"母集団: {scope} / 上位{MOMENTUM_TOP_PCT*100:.0f}%を保有 / 非重複リバランス\n")

    per_year = 250.0 / args.hold
    # **時系列で並べ直す。** 位相ごとに連結したままだと、部分期間の分割が
    # 「リストの中央」になって暦の中央にならない（前半・後半が混ざる）。
    base = sorted(
        (p for phase in PHASES for p in _periods(bars, args.hold, phase, args.universe_size, args.min_population)),
        key=lambda row: row[0],
    )

    # 1. 保有期間
    by_horizon = {}
    for horizon in args.horizons:
        rows = [p for phase in PHASES for p in _periods(bars, horizon, phase, args.universe_size, args.min_population)]
        by_horizon[horizon] = float(np.mean(_bucket_excess(rows, 1.0 - MOMENTUM_TOP_PCT, 1.01)))  # 対数

    # 2. 位相
    by_phase = [
        float(np.mean(_bucket_excess(_periods(bars, args.hold, phase, args.universe_size, args.min_population), 1.0 - MOMENTUM_TOP_PCT, 1.01)))
        for phase in PHASES
    ]

    # 3. 測定空間（ここだけは3つとも見る。向きが揃うかが検定内容そのもの）
    arithmetic = float(np.mean(_bucket_excess(base, 1.0 - MOMENTUM_TOP_PCT, 1.01, "arith")))
    log_mean = float(np.mean(_bucket_excess(base, 1.0 - MOMENTUM_TOP_PCT, 1.01, "log")))
    median = float(np.mean(_bucket_excess(base, 1.0 - MOMENTUM_TOP_PCT, 1.01, "median")))

    # 4. 単調性（上位10% > 上位20% > 母集団(=0) > 下位10%）
    buckets = [
        float(np.mean(_bucket_excess(base, 1.0 - MOMENTUM_TOP_PCT, 1.01))),
        float(np.mean(_bucket_excess(base, 0.80, 1.01))),
        0.0,
        float(np.mean(_bucket_excess(base, -0.01, 0.10))),
    ]

    if not base:
        # **落ちるのではなく理由を出す。** 母集団を絞りすぎると1期も作れず、
        # 以前はここで AttributeError になっていた（何が起きたか読めない）。
        print(
            f"母集団の下限({args.min_population}件)を満たす期が1つもありません。"
            f"--universe-size {args.universe_size} は下限より小さい可能性があります。"
            "--min-population を下げれば測れますが、**横断ランクの意味は薄れます**"
            f"（{args.universe_size}銘柄中の上位{MOMENTUM_TOP_PCT*100:.0f}%は"
            f"{max(1, int(args.universe_size * MOMENTUM_TOP_PCT))}銘柄です）。",
            file=sys.stderr,
        )
        return 1

    # 5. 部分期間（`base` は暦順に並べてある）
    midpoint = base[len(base) // 2][0]
    halves = {
        f"前半(〜{midpoint.date()})": float(np.mean(
            _bucket_excess([p for p in base if p[0] <= midpoint], 1.0 - MOMENTUM_TOP_PCT, 1.01))),
        f"後半({midpoint.date()}〜)": float(np.mean(
            _bucket_excess([p for p in base if p[0] > midpoint], 1.0 - MOMENTUM_TOP_PCT, 1.01))),
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
