"""売買代金ランキング履歴の永続化のテスト。"""

import json
from pathlib import Path

from data.rank_history import MAX_HISTORY_DAYS, RankHistoryStore


def test_history_starts_empty(tmp_path: Path) -> None:
    store = RankHistoryStore(str(tmp_path / "ranks.json"))

    assert store.load() == []


def test_append_returns_history_oldest_first(tmp_path: Path) -> None:
    store = RankHistoryStore(str(tmp_path / "ranks.json"))

    store.append("2026-08-03", {"AAA": 1})
    history = store.append("2026-08-04", {"AAA": 2})

    assert history == [{"AAA": 1}, {"AAA": 2}]


def test_same_trading_day_overwrites_instead_of_appending(tmp_path: Path) -> None:
    """1日に複数回スキャンが走っても、履歴は1日1件に保つこと。

    スクリーニングの再試行で同じ日に複数回呼ばれうる。重複させると
    基準順位（中央値）が同じ日の値に引きずられる。
    """
    store = RankHistoryStore(str(tmp_path / "ranks.json"))

    store.append("2026-08-04", {"AAA": 50})
    history = store.append("2026-08-04", {"AAA": 5})

    assert history == [{"AAA": 5}]


def test_history_is_capped(tmp_path: Path) -> None:
    store = RankHistoryStore(str(tmp_path / "ranks.json"))

    for day in range(MAX_HISTORY_DAYS + 10):
        history = store.append(f"day-{day:03d}", {"AAA": day})

    assert len(history) == MAX_HISTORY_DAYS
    # 新しい方が残る。
    assert history[-1] == {"AAA": MAX_HISTORY_DAYS + 9}


def test_corrupt_history_falls_back_to_empty(tmp_path: Path) -> None:
    """履歴が壊れていても稼働を止めないこと（判定が縮退するだけ）。"""
    path = tmp_path / "ranks.json"
    path.write_text("{ broken", encoding="utf-8")
    store = RankHistoryStore(str(path))

    assert store.load() == []


def test_attention_symbols_survive_a_restart(tmp_path: Path) -> None:
    """組み入れた注目銘柄を引き継ぐこと。

    毎日ゼロから組み直すと、急上昇の翌日にランキングが落ち着いた時点で
    監視から外れ、押し目が出るまで持ち続けられない。
    """
    path = str(tmp_path / "ranks.json")
    store = RankHistoryStore(path)
    store.append("2026-08-04", {"AAA": 1})
    store.save_attention_symbols(["AAA", "BBB"])

    assert RankHistoryStore(path).load_attention_symbols() == ["AAA", "BBB"]
    # 履歴は消えない。
    assert RankHistoryStore(path).load() == [{"AAA": 1}]


def test_saved_file_is_valid_json(tmp_path: Path) -> None:
    path = tmp_path / "ranks.json"
    RankHistoryStore(str(path)).append("2026-08-04", {"AAA": 1})

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["days"][0]["date"] == "2026-08-04"


def test_appending_a_day_keeps_the_attention_symbols(tmp_path) -> None:
    """ランキングの追記で注目銘柄のリストを消さないこと。

    同じファイルに同居しているため、追記のたびに落とすと翌日の引き継ぎが
    空になり、急上昇の翌日に監視から外れる。
    """
    path = str(tmp_path / "ranks.json")
    store = RankHistoryStore(path)
    store.save_attention_symbols(["AAA"])

    store.append("2026-08-05", {"AAA": 3})

    assert RankHistoryStore(path).load_attention_symbols() == ["AAA"]
