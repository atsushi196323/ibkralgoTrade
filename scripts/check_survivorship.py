"""横断モメンタムの結論が、生存バイアスで覆るかを縛る（IBKR接続不要）。

    python -m scripts.check_survivorship --csv-dir bars/universe

**手元のデータでは生存バイアスを消せない**（`backtest/survivorship.py`）。
そこで「どれだけの破綻が隠れていれば超過リターンがゼロになるか」を出し、
現実の上場廃止率と比べる。求めた損益分岐が現実より桁で大きければ、
その結論は生存バイアスでは覆らない。

**この計算だけは算術平均で行う。** 破綻(-100%)は対数で扱えない。
"""

import argparse
import glob
import logging
import math
import os
import sys
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from backtest.csv_source import load_bars_from_csv
from backtest.signal_study import add_cross_sectional_percentile
from backtest.survivorship import (
    UNIFORM_TOP_SHARE,
    annualised_death_rate,
    break_even_death_rate,
    excess_with_deaths,
)

logger = logging.getLogger(__name__)

MIN_BARS: int = 400
# 米国上場株の年間の上場廃止率のうち、**株主価値がゼロになるもの**の目安。
# 買収による消滅は多くの場合プレミアムがつくので、ここには数えない。
# 幅で持つのは出典によって定義が違うため（この幅の外に出る年もある）。
REFERENCE_ANNUAL_DEATH_RATE = (0.005, 0.02)


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


def momentum_12_1(frame: pd.DataFrame) -> pd.Series:
    close = frame["close"].astype(float)
    return close.shift(21) / close.shift(252) - 1.0


def collect_period_returns(
    bars: Dict[str, pd.DataFrame], hold: int, offset: int, top_pct: float,
) -> List[tuple]:
    """非重複リバランスの各期について (上位decileの騰落率, 母集団の騰落率) を返す。

    **重複させない。** 重なった窓で平均すると、同じ保有が何度も数えられて
    死の注入量と釣り合わなくなる。
    """
    frames = {}
    for symbol, frame in bars.items():
        days = pd.to_datetime(frame["date"]).dt.normalize().to_numpy()
        entry = frame["open" if "open" in frame.columns else "close"].to_numpy(float)
        frames[symbol] = (
            days, entry, frame["close"].to_numpy(float),
            frame["cs_momentum_rank"].to_numpy(float),
        )

    all_days = sorted({day for days, *_ in frames.values() for day in days})
    periods = []
    for day in all_days[252 + offset::hold]:
        top, everyone = [], []
        for days, entry, close, rank in frames.values():
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
            if np.isfinite(rank[i]) and rank[i] > 1.0 - top_pct:
                top.append(change)
        if len(top) >= 10 and len(everyone) >= 100:
            periods.append((top, everyone))
    return periods


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv-dir", default="bars/universe")
    parser.add_argument("--hold", type=int, default=60, help="保有営業日数")
    parser.add_argument("--top-pct", type=float, default=0.10, help="上位何割を買うか")
    parser.add_argument(
        "--top-share", type=float, nargs="+", default=[0.10, 0.20, 0.30],
        help="死のうち上位decileに含まれる割合。一様なら0.10。**実際は破綻企業は"
             "直前12ヶ月で下げているので下位に偏り、0.10より小さい**",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)

    bars = _load(args.csv_dir)
    if not bars:
        print(f"{args.csv_dir} に検証できる銘柄がありません。", file=sys.stderr)
        return 1
    bars = add_cross_sectional_percentile(bars, momentum_12_1, "cs_momentum_rank")

    # 位相をずらして平均する。1つの位相だけだと37標本の偶然を拾う。
    tops, pops, n_top, n_pop, count = [], [], [], [], 0
    for offset in (0, 10, 20, 30, 40, 50):
        for top, everyone in collect_period_returns(bars, args.hold, offset, args.top_pct):
            tops.append(float(np.mean(top)))
            pops.append(float(np.mean(everyone)))
            n_top.append(len(top))
            n_pop.append(len(everyone))
            count += 1

    if not tops:
        # 母集団が薄いと横断ランクに意味が無い（「3銘柄中の1位」は上位10%ではない）。
        print(
            f"{args.csv_dir} では横断ランクを測れる期がありません"
            "（1期あたり母集団100銘柄以上・上位decile10銘柄以上が要る）。"
            " bars/universe のような広い母集団を指定してください。",
            file=sys.stderr,
        )
        return 1

    top_mean, pop_mean = float(np.mean(tops)), float(np.mean(pops))
    top_n, pop_n = int(np.mean(n_top)), int(np.mean(n_pop))
    per_year = 250.0 / args.hold
    observed = top_mean - pop_mean

    print(f"銘柄 {len(bars)}件 / 非重複リバランス {count}期（位相6通り） / 保有 {args.hold}営業日")
    print(f"上位{args.top_pct*100:.0f}%は平均{top_n}銘柄、母集団は平均{pop_n}銘柄\n")
    print(f"観測（算術・死は含まれていない）:")
    print(f"  上位decile {top_mean:+.2f}% / 母集団 {pop_mean:+.2f}% "
          f"→ 超過 {observed:+.2f}%/期 = {observed*per_year:+.2f}%/年\n")

    print("どれだけの破綻が隠れていれば、この超過がゼロになるか:")
    print(f"{'死の偏り':>28} {'損益分岐(期)':>12} {'損益分岐(年率)':>14} {'現実との比':>12}")
    for share in args.top_share:
        rate = break_even_death_rate(top_mean, pop_mean, top_n, pop_n, share)
        if rate is None:
            print(f"{_share_label(share):>28} {'覆らない':>12} {'—':>14} {'—':>12}")
            continue
        annual = annualised_death_rate(rate, per_year)
        ratio = annual / REFERENCE_ANNUAL_DEATH_RATE[1]
        print(f"{_share_label(share):>28} {rate*100:>11.2f}% {annual*100:>13.1f}% "
              f"{ratio:>11.0f}倍")

    low, high = REFERENCE_ANNUAL_DEATH_RATE
    print(f"\n参考: 米国上場株が株主価値ゼロで消える率は年 {low*100:.1f}〜{high*100:.1f}% 程度。")
    print("買収による消滅はプレミアムがつくので、ここには数えない"
          "（数えると結論は**さらに強くなる**方向に動く）。")

    print("\n現実的な死亡率を入れたときの超過リターン:")
    print(f"{'年率の死亡率':>14} " + " ".join(f"{_share_label(s):>16}" for s in args.top_share))
    for annual in (0.005, 0.01, 0.02, 0.05):
        per_period = 1.0 - (1.0 - annual) ** (1.0 / per_year)
        row = f"{annual*100:>13.1f}% "
        for share in args.top_share:
            adjusted = excess_with_deaths(
                top_mean, pop_mean, top_n, pop_n, per_period, share,
            )
            row += f"{adjusted.excess_pct*per_year:>+15.2f}% "
        print(row)
    return 0


def _share_label(share: float) -> str:
    if math.isclose(share, UNIFORM_TOP_SHARE):
        return f"上位に{share*100:.0f}%（一様）"
    return f"上位に{share*100:.0f}%（{share/UNIFORM_TOP_SHARE:.0f}倍偏る）"


if __name__ == "__main__":
    raise SystemExit(main())
