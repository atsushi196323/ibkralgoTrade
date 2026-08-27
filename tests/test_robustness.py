"""採否の判定基準（単一のt値ではなく一貫性）の単体テスト。

**ここで守るのは「通りやすさ」ではなく「後から動かせないこと」である。**
基準が結果を見てから動くなら、8本並べて最良を拾うのと同じことになる。
"""

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


def _passing(name: str) -> object:
    return check_benchmark(1.0)


def test_every_check_must_pass_for_the_signal_to_be_a_candidate():
    """部分点を認めないこと。

    1件でも落ちたものを「今回はたまたま」と読む余地を残すと、基準が
    判定ではなく感想になる。
    """
    report = RobustnessReport("x", [check_benchmark(1.0), check_benchmark(-1.0)])

    assert report.passed is False
    assert len(report.failures) == 1


def test_an_empty_battery_is_not_a_pass():
    """検定を1つも走らせていない状態を合格にしないこと。

    `all([])` は True なので、素朴に書くと**何も測っていないシグナルが
    採用候補になる。**
    """
    assert RobustnessReport("x", []).passed is False


def test_a_sign_flip_across_horizons_fails():
    """保有期間で符号が変わるものを落とすこと。"""
    assert check_horizons({20: 1.0, 60: 2.0, 120: 3.0}).passed is True
    assert check_horizons({20: 1.0, 60: -0.5, 120: 3.0}).passed is False


def test_a_sign_flip_across_rebalance_phases_fails():
    """位相で符号が変わるものを落とすこと。

    どの日から60日区切りを始めるかは恣意的な選択なので、そこで符号が
    変わるなら実装できる形でのエッジは無い。
    """
    assert check_phases([1.0, 2.0, 1.5]).passed is True
    assert check_phases([1.0, -0.9, 1.5]).passed is False


def test_the_arithmetic_tail_cannot_carry_a_signal_alone():
    """算術平均だけが正のものを落とすこと。

    2026-08-27の実測では、モメンタム下位10%の60日超過が算術+33.8%
    （勝率42%）・対数-5.75%だった。算術だけで判断すると、この裾を
    「銘柄選択の情報」として読む。
    """
    assert check_measurement_spaces(33.8, -5.75, -4.38).passed is False
    assert check_measurement_spaces(3.20, 2.46, 1.81).passed is True


def test_monotonicity_requires_the_whole_ordering():
    """順位の上から下へ、単調に落ちることを要求すること。

    1点だけ良い（上位10%だけ高く、上位20%は母集団以下）ものは、
    順位付けに情報があるのではなく、その1バケットの偶然である。
    """
    assert check_monotonicity([2.46, 1.71, 0.0, -2.05]).passed is True
    assert check_monotonicity([2.46, -0.5, 0.0, -2.05]).passed is False
    # バケットが足りなければ判定不能として落とす（通してはならない）。
    assert check_monotonicity([2.0, 1.0]).passed is False


def test_a_signal_that_only_works_in_one_half_fails():
    assert check_subperiods({"前半": 2.08, "後半": 2.84}).passed is True
    assert check_subperiods({"前半": 3.22, "後半": -1.66}).passed is False


def test_the_benchmark_is_the_population_not_spy():
    """対照群超えを、母集団の等ウェイト指数で判定していること。

    SPY基準にすると、母集団がSPYを上回っているぶんを銘柄選択の力と
    取り違える（2026-08-26に「銘柄を選ばず常に建てる」がSPY基準で
    20日 +0.693%・t=2.02 を出した）。
    """
    check = check_benchmark(2.46)

    assert check.passed is True
    assert "母集団" in check.name


def test_survivorship_passes_when_deaths_cannot_flip_the_sign():
    """死をいくら入れても符号が変わらない場合を合格にすること。"""
    assert check_survivorship(None, 0.02).passed is True


def test_survivorship_needs_a_margin_over_the_real_delisting_rate():
    """損益分岐が現実の廃止率に近いだけでは通さないこと。

    廃止率の推定そのものに幅がある（年0.5〜2%）ため、同水準では
    「覆らない」と言い切れない。
    """
    assert check_survivorship(0.10, 0.02).passed is True     # 5倍
    assert check_survivorship(0.058, 0.02).passed is False   # 2.9倍


def test_the_prior_must_be_declared_rather_than_inferred():
    """事前の根拠を、測定後に付け足せない形にしてあること。

    結果を見てから「これには文献がある」と言うのは、8本並べて最良を
    拾うのと同じ後知恵である。判定は宣言されたかどうかだけを見る。
    """
    assert check_prior(True, "複数市場で再現が報告されている").passed is True
    assert check_prior(False, "事前の根拠は宣言されていない").passed is False


def test_the_report_names_what_failed():
    """落ちた検定が読み手に分かること（次の仮説の材料になる）。"""
    report = RobustnessReport(
        "テスト", [check_benchmark(1.0), check_subperiods({"前半": 1.0, "後半": -1.0})],
    )
    rendered = report.describe()

    assert "FAIL" in rendered
    assert "部分期間" in rendered
    assert "不採用" in rendered
