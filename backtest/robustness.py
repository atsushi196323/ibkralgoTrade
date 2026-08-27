"""シグナルの採否を、単一のt値ではなく一貫性で判定する。

**`t ≥ 2.0` という基準は、この母集団と保有期間では構造的に達成できない。**
60日保有を10年で測ると日付クラスタ後の実効観測は約42しかなく、

    t = (超過リターン / トラッキングエラー) × √年数 = 情報比 × 3.16

なので t=2.0 には情報比0.63が要る。**株式アノマリーの情報比は文献でも
0.3〜0.5である。** つまり本物のプレミアムが存在していても10年では届かない。
2026-08-27まで、この基準で9本のシグナルを落としていた。

かといって単に閾値を下げると偽陽性が入る。**そこで、1つの強い検定を
「多数の弱いが向きの揃うべき検定」へ置き換える。** 本物の効果なら、
測り方を変えても符号は変わらないはずである——これは大きさではなく
**一貫性**を問う検定であり、少ない標本でも意味を持つ。

**この基準は測定より先に決めること。** 測ってから基準を作れば、通るように
作ってしまう。本プロジェクトの主要な故障モード（8本並べて最良を拾う）と
同じ形の誤りが、一段上で起きる。

**t値は捨てない。** 参考として必ず併記する。一貫性は「向きが本物か」を見る
道具で、「どれだけ儲かるか」は答えないためである。
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

# 一貫性の判定に使う最小の余裕。符号だけを見ると、ゼロ同然の値が
# たまたま正になっただけのものを「一貫している」と読む。
SIGN_EPSILON_PCT: float = 0.0

# 生存バイアスの損益分岐が、現実の廃止率の何倍あれば合格とするか。
# 3倍にしているのは、廃止率の推定そのものに幅があるため（年0.5〜2%）。
SURVIVORSHIP_SAFETY_FACTOR: float = 3.0


@dataclass(frozen=True)
class Check:
    """1つの一貫性検定の結果。"""

    name: str
    passed: bool
    detail: str
    # 参考値。合否には使わないが、僅差で落ちたのか大差で落ちたのかが分かる。
    margin: Optional[float] = None


@dataclass(frozen=True)
class RobustnessReport:
    signal: str
    checks: List[Check]
    t_stat: Optional[float] = None

    @property
    def passed(self) -> bool:
        """**すべて通ったときだけ採用候補になる。**

        部分点を認めないのは、通らなかった検定を「今回はたまたま」と読む
        余地を残さないためである。落ちた検定はそのまま次の仮説の材料になる。
        """
        return bool(self.checks) and all(check.passed for check in self.checks)

    @property
    def failures(self) -> List[Check]:
        return [check for check in self.checks if not check.passed]

    def describe(self) -> str:
        rows = [f"{self.signal}"]
        for check in self.checks:
            mark = "PASS" if check.passed else "FAIL"
            rows.append(f"  [{mark}] {check.name}: {check.detail}")
        verdict = "採用候補" if self.passed else f"不採用（{len(self.failures)}件が不合格）"
        rows.append(f"  => {verdict}")
        if self.t_stat is not None:
            rows.append(
                f"     参考: 日付クラスタ後の t = {self.t_stat:+.2f}"
                "（合否には使わない。大きさは別途 measure_alpha で見ること）"
            )
        return "\n".join(rows)


def _all_same_sign(values: Sequence[float], epsilon: float = SIGN_EPSILON_PCT) -> bool:
    if not values:
        return False
    return all(value > epsilon for value in values) or all(value < -epsilon for value in values)


def check_horizons(excess_by_horizon: Dict[int, float]) -> Check:
    """保有期間を変えても符号が変わらないこと。

    本物の効果が特定の保有期間にだけ現れる理由は無い。ここで落ちるものは、
    その保有期間に合わせて閾値を選んだ結果であることが多い。
    """
    values = [excess_by_horizon[key] for key in sorted(excess_by_horizon)]
    detail = " / ".join(f"{k}日 {v:+.2f}%" for k, v in sorted(excess_by_horizon.items()))
    return Check("保有期間の一貫性", _all_same_sign(values), detail)


def check_phases(excess_by_phase: Sequence[float]) -> Check:
    """非重複リバランスの位相を変えても符号が変わらないこと。

    どの日から60日区切りを始めるかは恣意的な選択である。**位相で符号が
    変わるなら、それは実装できる形でのエッジが無いということである**
    （2026-08-27に、重なりのある日次イベントスタディでは正、非重複では
    位相しだいという例を実測した）。
    """
    detail = f"{len(excess_by_phase)}位相: " + " / ".join(f"{v:+.2f}%" for v in excess_by_phase)
    return Check("リバランス位相の一貫性", _all_same_sign(excess_by_phase), detail)


def check_measurement_spaces(arithmetic: float, log: float, median: float) -> Check:
    """算術平均・対数平均・中央値で向きが揃うこと。

    **算術平均は右の裾に支配される。** 2026-08-27の実測では、モメンタム下位10%の
    60日超過が算術平均で+33.8%（勝率42%）、対数では-5.75%と符号が逆になった。
    3つが揃わないものは、分布の形を情報として読んでいる。
    """
    values = [arithmetic, log, median]
    detail = f"算術 {arithmetic:+.2f}% / 対数 {log:+.2f}% / 中央値 {median:+.2f}%"
    return Check("測定空間の一貫性", _all_same_sign(values), detail)


def check_monotonicity(bucket_excess: Sequence[float]) -> Check:
    """順位の上から下へ、超過リターンが単調に落ちること。

    **これが最も強い検定である。** 上位10% > 上位20% > 母集団 > 下位10% の
    順序が偶然そろう確率は低く、しかも「順位付けそのものに情報がある」ことを
    直接示す。閾値を1点だけ見ていては区別できない。

    引数は順位の高い方から順に並べること。
    """
    if len(bucket_excess) < 3:
        return Check("順位の単調性", False, "バケットが3つ未満で判定できません")
    ordered = all(
        bucket_excess[i] > bucket_excess[i + 1] for i in range(len(bucket_excess) - 1)
    )
    detail = " > ".join(f"{v:+.2f}%" for v in bucket_excess)
    return Check("順位の単調性", ordered, detail)


def check_subperiods(excess_by_subperiod: Dict[str, float]) -> Check:
    """期間を前後に割っても符号が変わらないこと。

    片方の期間だけで成立するものは、その期間に固有の相場つきを拾っている。
    10年を2つに割るので各5年しかなく、**弱い検定である**——ここを通っても
    強い証拠にはならないが、落ちるものは疑わしい。
    """
    values = [excess_by_subperiod[key] for key in sorted(excess_by_subperiod)]
    detail = " / ".join(f"{k} {v:+.2f}%" for k, v in sorted(excess_by_subperiod.items()))
    return Check("部分期間の一貫性", _all_same_sign(values), detail)


def check_benchmark(vs_population_pct: float) -> Check:
    """**母集団自身の等ウェイト指数**を上回ること（SPYではない）。

    SPYを基準にすると、母集団がSPYを上回っているぶんを銘柄選択の力と
    取り違える。2026-08-26の実測では「銘柄を選ばず常に建てる」だけで
    SPY基準で20日+0.693%(t=2.02)の超過が出た。
    """
    return Check(
        "対照群（等ウェイト母集団）超え",
        vs_population_pct > SIGN_EPSILON_PCT,
        f"対母集団 {vs_population_pct:+.2f}%",
        margin=vs_population_pct,
    )


def check_survivorship(
    break_even_annual_rate: Optional[float],
    reference_annual_rate: float,
    safety_factor: float = SURVIVORSHIP_SAFETY_FACTOR,
) -> Check:
    """生存バイアスの損益分岐が、現実の廃止率より十分大きいこと。

    `break_even_annual_rate` が None なら「死をいくら入れても符号が変わらない」
    という意味なので合格である（`backtest/survivorship.py`）。
    """
    if break_even_annual_rate is None:
        return Check("生存バイアス耐性", True, "死をいくら入れても符号が変わらない")
    required = reference_annual_rate * safety_factor
    ratio = break_even_annual_rate / reference_annual_rate if reference_annual_rate else 0.0
    return Check(
        "生存バイアス耐性",
        break_even_annual_rate >= required,
        f"損益分岐 年{break_even_annual_rate*100:.1f}%"
        f"（現実 年{reference_annual_rate*100:.1f}% の {ratio:.0f}倍。{safety_factor:.0f}倍以上が必要）",
        margin=ratio,
    )


def check_prior(has_external_evidence: bool, note: str) -> Check:
    """このデータセットの外に、事前の証拠があること。

    **これは測定の前に宣言すること。** 結果を見てから「モメンタムには文献がある」
    と言うのは、8本並べて最良を拾うのと同じ後知恵である。事前登録された仮説か
    どうかだけを見る。
    """
    return Check("事前の根拠（測定前に宣言）", has_external_evidence, note)
