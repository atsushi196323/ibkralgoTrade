"""生存バイアスが結論を覆すかを、注入した「死」の量で縛る。

**手元のデータでは生存バイアスを消せない。** `bars/universe` の銘柄は
2026年時点の生存者で、この10年で破綻・上場廃止した企業が構造的に入っていない。
2026-08-27に実測したところ、yfinanceは上場廃止銘柄の価格を1本も返さない
（SIVB / RAD / PRTY / REV / TWTR / ATVI / VMW / SGEN / XLNX いずれも0本）。
**しかも BBBY は29本返す**——同じティッカーを別法人が再利用しているためで、
気付かずに使うとまったく別の系列が母集団に紛れ込む。

消せない以上、**どれだけの「死」が隠れていれば結論が覆るか**を出す。
これは推定ではなく上限の議論なので、実際の廃止率が分からなくても使える:
求めた損益分岐が現実の廃止率より桁で大きければ、その結論は生存バイアスでは
覆らない。

**死は対数で扱えない**（-100% の対数は -inf）。この計算だけは算術平均で行う。
`backtest/signal_study.py` が長い保有で対数を使うのは右の裾の話で、こちらは
左端の話なので目的が違う。
"""

from dataclasses import dataclass
from typing import Optional

# 破綻して価値がゼロになった場合の期間リターン(%)。
# 上場廃止のすべてがゼロになるわけではない（買収は多くの場合プレミアムがつく）が、
# **こちらは上限を縛るための計算なので、最も不利な側に倒す。**
DEATH_RETURN_PCT: float = -100.0

# 死が上位10%に含まれる割合。母集団に一様に分布するなら0.10（上位10%は
# 名前の10%だから）。**実際には破綻する企業は直前12ヶ月で下げているので
# 下位に偏り、0.10より小さくなる**——つまり0.10でも既に不利側の仮定である。
UNIFORM_TOP_SHARE: float = 0.10


@dataclass(frozen=True)
class DeathAdjustedExcess:
    """死を注入した後の、上位decile対母集団の超過リターン。"""

    death_rate_per_period: float
    top_share_of_deaths: float
    top_mean_pct: float
    population_mean_pct: float
    excess_pct: float


def excess_with_deaths(
    top_mean_pct: float,
    population_mean_pct: float,
    n_top: int,
    n_population: int,
    death_rate_per_period: float,
    top_share_of_deaths: float = UNIFORM_TOP_SHARE,
    death_return_pct: float = DEATH_RETURN_PCT,
) -> DeathAdjustedExcess:
    """観測された平均に、抜け落ちている「死」を戻したときの超過リターン。

    **死は母集団と上位decileの両方に入る。** 片方だけに入れると答えが変わる:
    死が下位に偏るなら母集団の平均も下がるので、**上位decileの相対的な優位は
    むしろ広がる**。生存バイアスがこの結論を甘くしているのか厳しくしているのかは、
    両方に入れて初めて分かる。
    """
    if n_top <= 0 or n_population <= 0:
        raise ValueError("銘柄数は正である必要があります。")
    if not 0.0 <= death_rate_per_period <= 1.0:
        raise ValueError("死亡率は0〜1である必要があります。")
    if not 0.0 <= top_share_of_deaths <= 1.0:
        raise ValueError("上位decileに含まれる割合は0〜1である必要があります。")

    n_dead = n_population * death_rate_per_period
    n_dead_top = n_dead * top_share_of_deaths

    top = (n_top * top_mean_pct + n_dead_top * death_return_pct) / (n_top + n_dead_top)
    population = (
        (n_population * population_mean_pct + n_dead * death_return_pct)
        / (n_population + n_dead)
    )
    return DeathAdjustedExcess(
        death_rate_per_period=death_rate_per_period,
        top_share_of_deaths=top_share_of_deaths,
        top_mean_pct=top,
        population_mean_pct=population,
        excess_pct=top - population,
    )


def break_even_death_rate(
    top_mean_pct: float,
    population_mean_pct: float,
    n_top: int,
    n_population: int,
    top_share_of_deaths: float = UNIFORM_TOP_SHARE,
    death_return_pct: float = DEATH_RETURN_PCT,
    tolerance: float = 1e-9,
) -> Optional[float]:
    """超過リターンがゼロになる、1期間あたりの死亡率。

    **これが求めたい唯一の数字である。** 現実の上場廃止率がこれより十分小さければ、
    その結論は生存バイアスでは覆らない。返り値がNoneなら、死をいくら入れても
    符号が変わらない（＝死が下位に偏る仮定では、生存バイアスは結論を
    **強める**側にしか効かない）。

    死亡率について超過リターンは単調なので二分探索でよい。
    """
    def excess(rate: float) -> float:
        return excess_with_deaths(
            top_mean_pct, population_mean_pct, n_top, n_population,
            rate, top_share_of_deaths, death_return_pct,
        ).excess_pct

    if excess(0.0) <= 0.0:
        return 0.0
    if excess(1.0) > 0.0:
        return None

    low, high = 0.0, 1.0
    while high - low > tolerance:
        mid = (low + high) / 2.0
        if excess(mid) > 0.0:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def annualised_death_rate(rate_per_period: float, periods_per_year: float) -> float:
    """1期間あたりの死亡率を年率へ直す（複利で積む）。"""
    if not 0.0 <= rate_per_period <= 1.0:
        raise ValueError("死亡率は0〜1である必要があります。")
    return 1.0 - (1.0 - rate_per_period) ** periods_per_year
