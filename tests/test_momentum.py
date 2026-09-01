"""横断ランクのモメンタムの判定ロジック。

**この層はバックテスト・一貫性テスト・ライブで共有する。** ここが分かれると
「測ったもの」と「動くもの」が別物になる（docs/DECISIONS.md「レイヤーの責務」）。
"""

import pandas as pd
import pytest

from strategy.momentum import (
    MOMENTUM_LOOKBACK_BARS,
    MOMENTUM_SKIP_BARS,
    MomentumConfig,
    momentum_series,
    momentum_value,
    rank_percentiles,
    recent_turnover,
    select_by_turnover,
    select_targets,
)


def _closes(values):
    return pd.Series(values, dtype=float)


def test_the_most_recent_month_is_excluded_from_the_lookback():
    """直近1ヶ月を除いて測ること（短期反転を避けるための標準的な定義）。

    最後の21本だけを動かしても値が変わらないことで確認する。ここを含めると
    別のシグナル（1ヶ月モメンタム）になり、測ったものと違うものが動く。
    """
    base = [100.0] * (MOMENTUM_LOOKBACK_BARS + 1)
    base[-MOMENTUM_SKIP_BARS - 1] = 150.0     # 参照する側
    quiet = list(base)
    noisy = list(base)
    noisy[-1] = 999.0                          # 除外される側

    assert momentum_value(_closes(quiet)) == momentum_value(_closes(noisy))
    assert momentum_value(_closes(quiet)) == pytest.approx(0.5)


def test_not_enough_history_returns_none_rather_than_a_guess():
    """本数が足りないとき、推定で埋めないこと。

    埋めると上場直後の銘柄が母集団に混じり、順位が壊れる（押し目買い側で
    2026-08-04にSPCXが上場35営業日で建った件と同じ形）。
    """
    assert momentum_value(_closes([100.0] * MOMENTUM_LOOKBACK_BARS)) is None
    assert momentum_value(None) is None


def test_a_thin_population_produces_no_targets():
    """母集団が薄い日に建てないこと。

    「3銘柄中の1位」を「上位10%」として扱うと、母集団が薄い期間だけ
    シグナルが乱発される。ライブでは価格が取れない銘柄が出るので、
    この判定は毎日必要である。
    """
    values = {f"S{i}": float(i) for i in range(50)}

    assert select_targets(values, MomentumConfig(min_symbols=100)) == []


def test_targets_are_the_highest_momentum_names_within_the_top_decile():
    values = {f"S{i:03d}": float(i) for i in range(200)}   # S199 が最強

    targets = select_targets(values, MomentumConfig(top_pct=0.10, slots=5, min_symbols=100))

    assert targets == ["S199", "S198", "S197", "S196", "S195"]


def test_the_target_count_is_capped_by_slots_not_by_the_decile_size():
    """上位decile全部ではなく、枠の数だけ採ること。

    上位decileを全部持てないことは期待値ではなくばらつきに効く
    （2026-08-27の実測: 期待超過は枠数にほぼ依存せず、年ごとSDが
    枠2で40.3% / 枠20で12.0%）。
    """
    values = {f"S{i:03d}": float(i) for i in range(200)}   # 上位10% = 20銘柄

    assert len(select_targets(values, MomentumConfig(slots=5, min_symbols=100))) == 5
    assert len(select_targets(values, MomentumConfig(slots=20, min_symbols=100))) == 20


def test_ranks_are_percentiles_within_the_same_day():
    ranks = rank_percentiles({"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0})

    assert ranks["D"] == pytest.approx(1.0)
    assert ranks["A"] == pytest.approx(0.25)


def test_missing_values_do_not_get_a_rank():
    """値が無い銘柄に順位を付けないこと（順位が1つずれる）。"""
    ranks = rank_percentiles({"A": 1.0, "B": float("nan"), "C": 3.0})

    assert set(ranks) == {"A", "C"}






def test_thin_names_are_dropped_even_inside_the_top_n():
    """上位N件の枠に入っていても、売買代金が下限に届かなければ落とすこと。

    **効くのは上位に絞ることではなく、下限を切ることである**（2026-08-06の
    層別測定: 1–100位 PF 1.26 / 201–400位 1.30 に対し 401位以下 1.10）。
    件数の枠だけだと、相場が薄い日にたまたま枠内へ入った細い銘柄が混じる。
    """
    turnovers = {"A": 5e8, "B": 3e7, "C": 1e6}

    assert select_by_turnover(turnovers, 500, min_turnover_usd=2e7) == ["A", "B"]


def test_unreadable_turnover_is_dropped_rather_than_treated_as_zero_or_infinite():
    """売買代金が読めない銘柄を残さないこと。

    残すと「最小」として扱うか「最大」として扱うかで母集団が変わる。
    """
    assert select_by_turnover({"A": 5e8, "B": float("nan")}, 500) == ["A"]


def test_a_zero_size_keeps_everything_above_the_floor():
    """件数の上限を外しても、下限は効き続けること。"""
    kept = select_by_turnover({"A": 5e8, "C": 1e6}, 0, min_turnover_usd=2e7)

    assert kept == ["A"]


# --- 測定とライブで同じ定義を使う -------------------------------------------------


def test_the_series_and_scalar_definitions_agree():
    """列で返す版と、直近1バーだけを返す版が同じ値になること。

    **2026-08-27まで、この2行が `scripts/` の3ファイルへ書き写されていた。**
    片方だけ直すと、測っているものとライブで動くものが別々に育つ
    （docs/DECISIONS.md「レイヤーの責務」）。
    """
    closes = _closes([100.0 + i for i in range(300)])

    assert momentum_series(closes).iloc[-1] == pytest.approx(momentum_value(closes))


def test_the_turnover_is_a_median_not_a_mean():
    """売買代金を中央値で取ること。

    平均だと1日の異常出来高で母集団が入れ替わる（`strategy.attention` が
    前日比ではなく中央値を使うのと同じ理由）。
    """
    closes = _closes([10.0] * 5)
    volumes = _closes([100.0, 100.0, 100.0, 100.0, 10_000.0])

    assert recent_turnover(closes, volumes, window=5) == pytest.approx(1000.0)


def test_turnover_of_an_empty_series_is_nan():
    assert recent_turnover(_closes([]), _closes([]), window=5) != recent_turnover(
        _closes([]), _closes([]), window=5
    )
