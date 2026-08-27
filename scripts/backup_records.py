"""失うと復元できない記録を、日付つきのスナップショットとして控える。

**対象は「再生成できないもの」だけである。** `bot.log` は同じ出来事が
ローテーションで残るうえ大きいので入れない。入れるのは次の3つ。

| | 失うと |
| --- | --- |
| `trade_journal.csv` | **実約定・実手数料を伴う往復の記録が消える。** 現フェーズが生み出す唯一の成果物であり、実資金へ進む条件そのもの（CLAUDE.md「1. プロジェクト概要」）。ブローカーの取引報告から手で作り直すしかない |
| `positions.json` | 建値・R倍率の分母・トレーリングの高値・クールダウンが消える。建玉自体はブローカー同期で拾い直せるが、建値が**手数料込みの `avgCost`** に化け、損切り判定と置き直しの基準がずれる |
| `turnover_ranks.json` | 売買代金の順位は**過去に遡って取得できない**（yfinanceもIBKRも当日ぶんしか返さない）。落とすとその日数ぶん急上昇の判定ができなくなる |
| `fills.jsonl` | 想定価格と実約定価格の乖離が消える。**参照価格・鮮度・取得経路はその瞬間にしか存在しない値で、ブローカーの取引報告からも再構成できない**（あちらに残るのは約定価格だけで、こちらが何を想定していたかは残らない） |

**これはVPSの消失に対する備えではない。** 同じディスクに置くので、守れるのは
「壊れた・消した・間違えて上書きした」側である。ディスクごと失う側は、
`scripts/fetch_vps_logs.sh` で手元へ同期して初めて備えになる（控えは `logs/`
配下に置いてあるので、同期すればそのまま手元にも複製される）。

**`trade_journal.csv` は追記専用である。縮んだら異常である。** 直近の控えより
小さくなっていたら WARNING を出す（控え自体は日付が違えば別ファイルなので、
過去のぶんが上書きで失われることはない）。`positions.json` は決済のたびに
縮むので、この検査は掛けない。

実行方法:
    python -m scripts.backup_records              # logs/backups/YYYY-MM-DD/ へ
    python -m scripts.backup_records --keep 90
"""

import argparse
import hashlib
import logging
import os
import shutil
from datetime import datetime
from typing import List, Optional

from core.market_hours import US_EASTERN

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("backup_records")

DEFAULT_SOURCE_DIR: str = "logs"
DEFAULT_BACKUP_DIR: str = "logs/backups"
DEFAULT_KEEP_DAYS: int = 90

# 控える対象。**追加するときは「再生成できないか」を基準にすること。**
RECORD_FILES = ("trade_journal.csv", "positions.json", "turnover_ranks.json", "fills.jsonl")

# 追記専用のファイル。縮んでいたら異常として報告する。
APPEND_ONLY_FILES = frozenset({"trade_journal.csv", "fills.jsonl"})


def _digest(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _snapshot_dirs(backup_dir: str) -> List[str]:
    """日付名のスナップショットを古い順に返す。"""
    if not os.path.isdir(backup_dir):
        return []
    names = [
        name for name in os.listdir(backup_dir)
        if os.path.isdir(os.path.join(backup_dir, name)) and _is_date_name(name)
    ]
    return sorted(names)


def _is_date_name(name: str) -> bool:
    try:
        datetime.strptime(name, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _latest_snapshot_size(backup_dir: str, filename: str, exclude: str) -> Optional[int]:
    """直近の控え（今回ぶんを除く）にあるそのファイルのサイズ。"""
    for name in reversed(_snapshot_dirs(backup_dir)):
        if name == exclude:
            continue
        path = os.path.join(backup_dir, name, filename)
        if os.path.exists(path):
            return os.path.getsize(path)
    return None


def _prune(backup_dir: str, keep: int) -> None:
    """古いスナップショットを削除する（新しい方から keep 個を残す）。"""
    snapshots = _snapshot_dirs(backup_dir)
    for name in snapshots[:-keep] if keep > 0 else []:
        shutil.rmtree(os.path.join(backup_dir, name), ignore_errors=True)
        logger.info("古い控えを削除しました: %s", name)


def backup_records(
    source_dir: str = DEFAULT_SOURCE_DIR,
    backup_dir: str = DEFAULT_BACKUP_DIR,
    keep: int = DEFAULT_KEEP_DAYS,
    now: Optional[datetime] = None,
) -> int:
    """記録を控える。戻り値は終了コード（0=成功 / 1=1つでも失敗）。

    日付は**米国東部時間**で採る。取引日の区切り（市場時間・クールダウン・
    日次サーキットブレーカー）と揃えるためで、日本時間だと引け後の締めが
    翌日の日付になり、サマリと控えの日付が1日ずれる。
    """
    stamp = (now or datetime.now(US_EASTERN)).astimezone(US_EASTERN).date().isoformat()
    destination = os.path.join(backup_dir, stamp)
    os.makedirs(destination, exist_ok=True)

    failures = 0
    copied = 0
    for filename in RECORD_FILES:
        source = os.path.join(source_dir, filename)
        if not os.path.exists(source):
            # まだ1件も決済していない間は trade_journal.csv が存在しない。
            # 「無いものは控えられない」だけなので失敗にはしない。
            logger.info("%s はまだ存在しないため控えません。", source)
            continue

        if filename in APPEND_ONLY_FILES:
            previous = _latest_snapshot_size(backup_dir, filename, exclude=stamp)
            current = os.path.getsize(source)
            if previous is not None and current < previous:
                # 追記専用のファイルが縮むのは、切り詰め・書き込み失敗・
                # 取り違えのいずれかである。控え自体は日付ごとに別なので
                # 過去のぶんは残るが、気付けないまま運用を続ける方が危ない。
                logger.warning(
                    "%s が直近の控えより小さくなっています（%d -> %d バイト）。"
                    "追記専用のファイルなので、切り詰めや取り違えが疑われます。"
                    "過去の控えは %s に残っています。",
                    filename, previous, current, backup_dir,
                )

        target = os.path.join(destination, filename)
        try:
            shutil.copy2(source, target)
            # **控えたことを、読み直して確かめる。** コピーが途中で切れても
            # 例外にならない場合があり（ディスク満杯・NFS等）、確かめずに
            # 「控えました」と記録すると、必要になったときに壊れた控えを掴む。
            if _digest(source) != _digest(target):
                raise OSError("控えた内容が元と一致しません。")
        except OSError:
            logger.exception("%s の控えに失敗しました。", source)
            failures += 1
            continue

        copied += 1

    _prune(backup_dir, keep)

    if failures:
        logger.error("記録の控えに失敗したファイルがあります（%d件）。", failures)
        return 1

    logger.info("記録を控えました: %s（%d件、保持 %d 日ぶん）", destination, copied, keep)
    return 0


def main_cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=DEFAULT_SOURCE_DIR, help="記録のあるディレクトリ")
    parser.add_argument("--dest", default=DEFAULT_BACKUP_DIR, help="控えの置き場所")
    parser.add_argument(
        "--keep", type=int, default=DEFAULT_KEEP_DAYS, help="残す日数（既定90）",
    )
    args = parser.parse_args()
    return backup_records(args.source, args.dest, args.keep)


if __name__ == "__main__":
    raise SystemExit(main_cli())
