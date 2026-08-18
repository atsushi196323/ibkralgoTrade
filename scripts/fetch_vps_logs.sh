#!/bin/bash
#
# VPSで稼働しているBotのログを手元へ持ってくる。
#
# **VPSへ移すと、改善の材料になるログはすべて向こう側に出る。** `logs/` は
# Git管理外（実際の取引記録なのでコミットしてはならない）なので、gitでは
# 流れてこない。手元でログを読んで直す——という進め方を続けるには、
# この同期が要る。
#
# 同期先を `logs/` にしないのは、**手元の `logs/` にはmacOSで稼働していた
# 期間の記録が入っている**ためである。混ぜると、どちらの環境のものか
# 区別できない履歴になる（`trade_journal.csv` は追記型なので特に危険）。
# `logs_vps/` は .gitignore の `logs*/` に含まれるのでコミットされない。
#
# 使い方:
#   IBKRALGO_VPS=user@vps.example.com bash scripts/fetch_vps_logs.sh
#   python -m scripts.daily_report --log logs_vps/bot.log --journal logs_vps/trade_journal.csv

set -eu

cd "$(dirname "$0")/.." || exit 1

if [ -z "${IBKRALGO_VPS:-}" ]; then
    echo "ERROR: 接続先を IBKRALGO_VPS で指定すること（例: user@vps.example.com）。" >&2
    exit 1
fi

REMOTE_DIR="${IBKRALGO_VPS_DIR:-ibkralgoTrade/logs/}"
LOCAL_DIR="logs_vps"

mkdir -p "${LOCAL_DIR}"

# --delete は付けない。VPS側でローテーションによって消えた世代を
# 手元から消す理由が無く、消すと過去の調査ができなくなる。
# 圧縮するのは bot.log が10MB×10世代まで育つため。
#
# **再帰なので logs/backups/ の控えも一緒に来る。これがVPS消失に対する
# 唯一の備えである**（`scripts/backup_records.py` の控えはVPSの同じディスクに
# あるので、壊した・消したには効くが、ディスクごと失う側には効かない）。
# trade_journal.csv は現フェーズが生み出す唯一の成果物なので、往復が
# 積み始まったらこの同期を定期的に回すこと。
rsync -avz --partial \
    "${IBKRALGO_VPS}:${REMOTE_DIR}" \
    "${LOCAL_DIR}/"

echo
echo "同期しました: ${LOCAL_DIR}/"
ls -la "${LOCAL_DIR}/" 2>/dev/null | tail -n +2

cat <<'EOS'

次に読むもの:
  python -m scripts.daily_report --log logs_vps/bot.log --journal logs_vps/trade_journal.csv
  （--date で特定の取引日を指定できる。省略時はログ内の直近の取引日）

  logs_vps/systemd.out / systemd.err  … 起動しなかった日の切り分け
                                        （祝日でスキップしたのか、失敗したのか）
EOS
