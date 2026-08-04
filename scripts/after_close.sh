#!/bin/bash
#
# 米国市場の引け後に1日を締める。launchdから1日1回呼ばれる
# （com.user.ibkralgotrade.afterclose、日本時間06:05）。
#
# 06:05 JST を選んでいるのは、夏時間(EDT)では17:05 ET、冬時間(EST)では16:05 ET と
# **年間を通じて必ず引け(16:00 ET)の後になる**ため。日本時間05:05にすると
# 冬時間の間は15:05 ETとなり、ザラ場の最中にBotを落として、確定していない
# 売買代金でその日の順位を記録することになる。
#
# やること:
#   1. Botを止める。引け後も動かし続けると、IB Gatewayのログアウト(08:00 JST)以降は
#      再接続の失敗ログだけが積み上がり、翌日のサマリが読みにくくなる
#   2. 売買代金ランキングを記録する（観測。監視リストは変更しない）
#   3. 1日の稼働サマリを出す
#
# 2と3は1が失敗しても実行する（Botが既に落ちている日も記録は残したいため）。

set -u

cd "$(dirname "$0")/.." || exit 1

# launchdはログインシェルを経由せずPATHにpyenvが入らないため、plistと同じく
# インタープリタの実体を指す。環境変数で上書きできるようにしてあるのは、
# 手元で別の環境から叩いて動作を確かめられるようにするため。
PYTHON="${IBKRALGO_PYTHON:-/Users/user/.pyenv/versions/3.11.10/bin/python3.11}"

echo "===== after_close: $(date '+%Y-%m-%d %H:%M:%S %Z') ====="

# SIGTERMを使う。main.pyがこれを KeyboardInterrupt へ変換して
# disconnect_async() まで通す（main._raise_keyboard_interrupt_on_sigterm）。
# SIGINTにしないのは、シェルがバックグラウンドで起動した子プロセスの
# SIGINTを SIG_IGN にする場合があり（実測でcaffeinate配下のsleepが無視した）、
# 届いても止まらない経路があるため。
#
# パターンはpythonの起動行に合わせる。pgrep -f は引数まで見るので、
# 同じ行を引数に持つcaffeinate側も一緒に一致して止まる（意図どおり。
# caffeinateはシグナルを子へ中継しないので、python本体を確実に含める必要がある）。
if pkill -TERM -u "$(id -u)" -f "${PYTHON} main.py"; then
    echo "Botへ停止シグナルを送りました。"
    for _ in $(seq 1 10); do
        sleep 1
        pgrep -u "$(id -u)" -f "${PYTHON} main.py" >/dev/null || break
    done
    if pgrep -u "$(id -u)" -f "${PYTHON} main.py" >/dev/null; then
        echo "10秒待っても終了しないため、SIGKILLで停止します。"
        pkill -KILL -u "$(id -u)" -f "${PYTHON} main.py"
    fi
else
    echo "稼働中のBotは見つかりませんでした（既に停止しています）。"
fi

echo
echo "----- 売買代金ランキングの記録 -----"
"${PYTHON}" -m scripts.rank_turnover || echo "rank_turnover が失敗しました（履歴は更新されていません）。"

echo
echo "----- 稼働サマリ -----"
"${PYTHON}" -m scripts.daily_report || echo "daily_report が失敗しました。"
