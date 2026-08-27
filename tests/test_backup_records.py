"""scripts/backup_records.py の単体テスト（実時間・実ディスクの外部依存なし）。"""

import logging
import os
from datetime import datetime

from core.market_hours import US_EASTERN
from scripts.backup_records import RECORD_FILES, backup_records


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _setup(tmp_path, journal: str = "a,b\n1,2\n"):
    source = os.path.join(tmp_path, "logs")
    _write(os.path.join(source, "trade_journal.csv"), journal)
    _write(os.path.join(source, "positions.json"), "{}")
    _write(os.path.join(source, "turnover_ranks.json"), "{}")
    _write(os.path.join(source, "fills.jsonl"), '{"symbol": "INTC"}\n')
    # 大きいうえに同じ出来事がローテーションで残るので、控えの対象ではない。
    _write(os.path.join(source, "bot.log"), "x" * 1000)
    return source, os.path.join(tmp_path, "backups")


def test_only_the_records_that_cannot_be_regenerated_are_copied(tmp_path) -> None:
    """再生成できない記録だけを控えること（bot.log は対象外）。"""
    source, backup = _setup(str(tmp_path))
    day = datetime(2026, 8, 17, 17, 5, tzinfo=US_EASTERN)

    assert backup_records(source, backup, keep=90, now=day) == 0

    snapshot = os.path.join(backup, "2026-08-17")
    assert sorted(os.listdir(snapshot)) == sorted(RECORD_FILES)


def test_the_snapshot_is_dated_in_us_eastern(tmp_path) -> None:
    """日付は米国東部時間で採ること。

    引け後の締めは日本時間06:05に走る。日本時間で採ると翌日の日付になり、
    稼働サマリ（取引日はET）と控えの日付が1日ずれる。
    """
    source, backup = _setup(str(tmp_path))
    # 2026-08-18 06:05 JST = 2026-08-17 17:05 ET
    jst_morning = datetime.fromisoformat("2026-08-18T06:05:00+09:00")

    backup_records(source, backup, keep=90, now=jst_morning)

    assert os.path.isdir(os.path.join(backup, "2026-08-17"))


def test_a_shrinking_trade_journal_is_reported(tmp_path, caplog) -> None:
    """追記専用の trade_journal.csv が縮んだら警告すること。

    縮むのは切り詰め・書き込み失敗・取り違えのいずれかで、いずれも
    運用を続ける前に気付く必要がある。控えは日付ごとに別ファイルなので、
    過去のぶんが上書きで失われることはない。
    """
    source, backup = _setup(str(tmp_path), journal="a,b\n1,2\n3,4\n5,6\n")
    backup_records(source, backup, keep=90, now=datetime(2026, 8, 17, tzinfo=US_EASTERN))

    _write(os.path.join(source, "trade_journal.csv"), "a,b\n")
    with caplog.at_level(logging.WARNING):
        assert backup_records(
            source, backup, keep=90, now=datetime(2026, 8, 18, tzinfo=US_EASTERN),
        ) == 0

    assert any("小さくなっています" in r.getMessage() for r in caplog.records)
    # 前日の控えはそのまま残っていること（これが戻せる状態そのもの）。
    previous = os.path.join(backup, "2026-08-17", "trade_journal.csv")
    with open(previous, encoding="utf-8") as f:
        assert f.read().count("\n") == 4


def test_a_shrinking_positions_file_is_not_reported(tmp_path, caplog) -> None:
    """positions.json は決済のたびに縮むので、この検査を掛けないこと。"""
    source, backup = _setup(str(tmp_path))
    _write(os.path.join(source, "positions.json"), '{"positions": [1, 2, 3]}')
    backup_records(source, backup, keep=90, now=datetime(2026, 8, 17, tzinfo=US_EASTERN))

    _write(os.path.join(source, "positions.json"), "{}")
    with caplog.at_level(logging.WARNING):
        backup_records(source, backup, keep=90, now=datetime(2026, 8, 18, tzinfo=US_EASTERN))

    assert not [r for r in caplog.records if "小さくなっています" in r.getMessage()]


def test_missing_records_are_not_a_failure(tmp_path) -> None:
    """まだ1件も決済していない間は trade_journal.csv が存在しない。

    無いものは控えられないだけで、締め処理を失敗として残す理由は無い。
    """
    source = os.path.join(str(tmp_path), "logs")
    _write(os.path.join(source, "positions.json"), "{}")
    backup = os.path.join(str(tmp_path), "backups")

    assert backup_records(source, backup, keep=90) == 0


def test_old_snapshots_are_pruned_and_the_newest_are_kept(tmp_path) -> None:
    source, backup = _setup(str(tmp_path))
    for day in range(1, 6):
        backup_records(
            source, backup, keep=3, now=datetime(2026, 8, day, tzinfo=US_EASTERN),
        )

    assert sorted(os.listdir(backup)) == ["2026-08-03", "2026-08-04", "2026-08-05"]


def test_a_snapshot_that_did_not_match_the_source_is_a_failure(tmp_path, monkeypatch) -> None:
    """控えた内容は読み直して確かめること。

    コピーが途中で切れても例外にならない場合がある（ディスク満杯等）。
    確かめずに「控えました」と記録すると、必要になったときに壊れた控えを掴む。
    """
    source, backup = _setup(str(tmp_path))
    digests = iter(["元のハッシュ", "違うハッシュ"])
    monkeypatch.setattr(
        "scripts.backup_records._digest", lambda path: next(digests, "他"),
    )

    assert backup_records(source, backup, keep=90) == 1
