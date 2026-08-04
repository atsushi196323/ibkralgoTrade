"""売買代金ランキングの急上昇判定のテスト（IBKR接続なし）。"""

from strategy.attention import (
    AttentionConfig,
    baseline_rank,
    build_rank_map,
    detect_rank_surges,
    has_enough_history,
)

CONFIG = AttentionConfig(rank_ceiling=50, min_rank_improvement=20, history_window=10)


def _history(*days: dict) -> list:
    return list(days)


def test_rank_map_is_one_based_in_scan_order() -> None:
    assert build_rank_map(["AAA", "BBB", "CCC"]) == {"AAA": 1, "BBB": 2, "CCC": 3}


def test_duplicate_symbols_keep_the_better_rank() -> None:
    """2つの取引所スキャンを統合すると同じ銘柄が二度現れうる。

    低い方（下位）を採ると、統合しただけで順位が悪化して急上昇を取り逃がす。
    """
    assert build_rank_map(["AAA", "BBB", "AAA"]) == {"AAA": 1, "BBB": 2}


def test_symbol_outside_the_ceiling_is_not_a_surge() -> None:
    """上位50位に入っていなければ、いくら順位が上がっても対象外。"""
    today = {"AAA": 51}
    history = _history({"AAA": 100}, {"AAA": 100}, {"AAA": 100}, {"AAA": 100}, {"AAA": 100})

    assert detect_rank_surges(today, history, CONFIG) == []


def test_new_entrant_from_outside_the_ranking_is_a_surge() -> None:
    """ランク外から上位に入ってきた銘柄を拾うこと。

    履歴に無い日は absent_rank(101) として数えるので、上昇幅が自動的に大きくなる。
    """
    today = {"AAA": 12}
    history = _history({"BBB": 1}, {"BBB": 1}, {"BBB": 1}, {"BBB": 1}, {"BBB": 1})

    assert detect_rank_surges(today, history, CONFIG) == ["AAA"]


def test_symbol_that_was_already_at_the_top_is_not_a_surge() -> None:
    """常に上位にいる銘柄（＝注目され「始めた」わけではない）を拾わないこと。"""
    today = {"AAA": 3}
    history = _history({"AAA": 2}, {"AAA": 4}, {"AAA": 3}, {"AAA": 2}, {"AAA": 5})

    assert detect_rank_surges(today, history, CONFIG) == []


def test_small_improvement_is_not_a_surge() -> None:
    """上昇幅が閾値に届かないものは対象外（監視枠に収まらなくなるため）。"""
    today = {"AAA": 30}
    history = _history(*[{"AAA": 45}] * 5)

    assert detect_rank_surges(today, history, CONFIG) == []


def test_surges_are_ordered_by_the_size_of_the_improvement() -> None:
    """監視枠が足りないとき上から採るため、並び順が選定の一部になる。"""
    today = {"BIG": 5, "SMALL": 20}
    history = _history(*[{"BIG": 90, "SMALL": 60}] * 5)

    assert detect_rank_surges(today, history, CONFIG) == ["BIG", "SMALL"]


def test_baseline_uses_the_median_not_the_last_day() -> None:
    """1日だけの跳ねで基準が動かないこと。

    平均や前日比だと、1日ランク外に落ちただけで翌日に急上昇を誤検知する。
    """
    history = _history({"AAA": 10}, {"AAA": 10}, {"AAA": 101}, {"AAA": 10}, {"AAA": 10})

    assert baseline_rank("AAA", history, CONFIG) == 10.0


def test_baseline_only_looks_back_history_window_days() -> None:
    config = AttentionConfig(history_window=3)
    history = _history({"AAA": 1}, {"AAA": 1}, {"AAA": 90}, {"AAA": 90}, {"AAA": 90})

    assert baseline_rank("AAA", history, config) == 90.0


def test_history_shorter_than_half_the_window_is_not_enough() -> None:
    """履歴が浅いうちは全銘柄の基準がランク外になり、上位が軒並み該当する。"""
    assert has_enough_history([], CONFIG) is False
    assert has_enough_history([{"AAA": 1}] * 4, CONFIG) is False
    assert has_enough_history([{"AAA": 1}] * 5, CONFIG) is True
