"""生存バイアスの上限を縛る計算の単体テスト。

**手元のデータでは生存バイアスを消せない**（上場廃止銘柄の価格が取れない）。
そのため「どれだけの破綻が隠れていれば結論が覆るか」で縛る。ここで守るのは、
その縛りが**不利側に倒れている**ことである——甘い側に倒れていると、
「生存バイアスでは覆らない」という結論そのものが信用できなくなる。
"""

import pytest

from backtest.survivorship import (
    DEATH_RETURN_PCT,
    UNIFORM_TOP_SHARE,
    annualised_death_rate,
    break_even_death_rate,
    excess_with_deaths,
)


def test_deaths_are_injected_into_the_population_as_well_as_the_decile():
    """死を上位decileだけに入れないこと。

    片方だけに入れると答えが変わる。**破綻する企業は直前12ヶ月で下げているので
    下位に偏る**——そのとき母集団の平均も下がるので、上位decileの相対的な優位は
    むしろ広がる。両方に入れて初めて向きが分かる。
    """
    # 死がすべて下位（上位decileには1件も入らない）。
    adjusted = excess_with_deaths(
        top_mean_pct=10.0, population_mean_pct=5.0,
        n_top=50, n_population=500,
        death_rate_per_period=0.05, top_share_of_deaths=0.0,
    )

    # 上位decileは無傷、母集団だけが下がる → 超過は元の5.0より広がる。
    assert adjusted.top_mean_pct == pytest.approx(10.0)
    assert adjusted.population_mean_pct < 5.0
    assert adjusted.excess_pct > 5.0


def test_a_uniform_death_distribution_barely_moves_the_excess():
    """死が一様なら、超過リターンはほとんど動かないこと。

    上位decileは母集団の約10%なので、死の10%がそこに入るなら比率はほぼ同じで、
    両方が同じだけ下がる。**「生存バイアスがあるから結果は当てにならない」は、
    一様という仮定のもとでは成立しない。**
    """
    base = excess_with_deaths(10.0, 5.0, 50, 500, 0.0, UNIFORM_TOP_SHARE)
    with_deaths = excess_with_deaths(10.0, 5.0, 50, 500, 0.05, UNIFORM_TOP_SHARE)

    assert with_deaths.excess_pct == pytest.approx(base.excess_pct, abs=0.3)


def test_deaths_concentrated_in_the_winners_can_erase_the_excess():
    """死が上位に偏れば超過は消えること（縛りが機能していることの確認）。

    ここが成り立たないと、どんな仮定でも結論が変わらないことになり、
    この計算は何も検証していないことになる。
    """
    erased = excess_with_deaths(
        10.0, 5.0, 50, 500, death_rate_per_period=0.30, top_share_of_deaths=1.0,
    )

    assert erased.excess_pct < 0.0


def test_the_break_even_rate_is_where_the_excess_reaches_zero():
    rate = break_even_death_rate(10.0, 5.0, 50, 500, top_share_of_deaths=0.5)

    assert rate is not None
    at_break_even = excess_with_deaths(10.0, 5.0, 50, 500, rate, 0.5)
    assert at_break_even.excess_pct == pytest.approx(0.0, abs=1e-4)


def test_a_conclusion_that_deaths_cannot_flip_returns_none():
    """死をいくら入れても符号が変わらない場合を、0と区別すること。

    0を返すと「死が無くても超過はゼロ」と読めてしまい、意味が正反対になる。
    """
    assert break_even_death_rate(10.0, 5.0, 50, 500, top_share_of_deaths=0.0) is None


def test_an_excess_that_is_already_negative_needs_no_deaths():
    assert break_even_death_rate(1.0, 5.0, 50, 500) == 0.0


def test_the_death_return_is_the_worst_case():
    """破綻のリターンを-100%に固定していること。

    上場廃止のすべてがゼロになるわけではない（買収はプレミアムがつく）が、
    **これは上限を縛る計算なので、最も不利な側に倒す。**
    """
    assert DEATH_RETURN_PCT == -100.0


def test_the_annual_rate_compounds_the_period_rate():
    """年率への換算を複利で行うこと。単純な掛け算だと年率を過大に出す。"""
    annual = annualised_death_rate(0.05, periods_per_year=4.0)

    assert annual == pytest.approx(1.0 - 0.95 ** 4)
    assert annual < 0.05 * 4


def test_invalid_inputs_are_rejected():
    with pytest.raises(ValueError):
        excess_with_deaths(10.0, 5.0, 0, 500, 0.01)
    with pytest.raises(ValueError):
        excess_with_deaths(10.0, 5.0, 50, 500, 1.5)
    with pytest.raises(ValueError):
        excess_with_deaths(10.0, 5.0, 50, 500, 0.01, top_share_of_deaths=1.5)
