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

# launchd/systemdはログインシェルを経由せずPATHにpyenvやvenvが入らないため、
# インタープリタの実体を指す。環境変数で上書きできるようにしてあるのは、
# 手元で別の環境から叩いて動作を確かめられるようにするためと、**Linuxでは
# パスが違う**ため（VPSでは systemd unit の Environment= で渡す）。
PYTHON="${IBKRALGO_PYTHON:-/Users/user/.pyenv/versions/3.11.10/bin/python3.11}"

# systemdで動かしている場合のユニット名（VPS運用。deploy/systemd/ を参照）。
BOT_UNIT="${IBKRALGO_SYSTEMD_UNIT:-ibkralgotrade.service}"

echo "===== after_close: $(date '+%Y-%m-%d %H:%M:%S %Z') ====="

# いずれの経路でも **SIGTERM** で止める。main.pyがこれを KeyboardInterrupt へ
# 変換して disconnect_async() まで通す（main._raise_keyboard_interrupt_on_sigterm）。
# SIGINTにしないのは、シェルがバックグラウンドで起動した子プロセスの
# SIGINTを SIG_IGN にする場合があり（実測でcaffeinate配下のsleepが無視した）、
# 届いても止まらない経路があるため。
#
# systemd配下では systemctl から止める。pkillで殺すとユニットが失敗扱いで残り、
# 翌日の起動時に状態を読み違える。KillMode の既定(control-group)により
# SIGTERMはmain.pyへ届くので、停止の作法は同じである。
if command -v systemctl >/dev/null 2>&1 \
        && systemctl --user is-active --quiet "${BOT_UNIT}" 2>/dev/null; then
    echo "systemdユニット(${BOT_UNIT})を停止します。"
    systemctl --user stop "${BOT_UNIT}"
    echo "Botを停止しました。"
# パターンはpythonの起動行に合わせる。pgrep -f は引数まで見るので、
# 同じ行を引数に持つcaffeinate側(macOS)も一緒に一致して止まる（意図どおり。
# caffeinateはシグナルを子へ中継しないので、python本体を確実に含める必要がある）。
elif pkill -TERM -u "$(id -u)" -f "${PYTHON} main.py"; then
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

# 休場日は2と3を行わない。**Botの停止(1)は休場日でも行う**——手動で起動した
# ものが残っていれば止める必要があり、止め損ねると翌日のログに混ざる。
#
# ランキングを記録しない理由は、yfinanceが返す最終終値が前営業日のもので、
# それを休場日の日付で記録すると**同じ順位の日が2日ぶん履歴に入る**ためである。
# 直近10取引日の中央値がその値へ引きずられ、翌日の急上昇判定がずれる
# （`data/rank_history.RankHistoryStore` は日付が違えば別の日として追記する）。
#
# サマリを出さない理由は、その取引日が存在しないためである。出すと空の要約が
# 積まれ、`daily_report` が「直近の引けた取引日より古い」と警告する行だけが
# 残って、Botが起動しなかった日と見分けがつかなくなる。
#
# **休場日(終了コード1)と判定失敗を区別する**理由は start_bot.sh と同じ。
# まとめて「休場日」に倒すと、インタープリタのパス違いが休場日と同じ見た目に
# なり、終了コード0でスケジューラには成功として記録される。
"${PYTHON}" -m scripts.is_us_trading_day
case $? in
    0) ;;
    1)
        echo "ランキングの記録と稼働サマリは行いません。"
        exit 0
        ;;
    *)
        echo "ERROR: 取引日を判定できませんでした (PYTHON=${PYTHON})。" \
             "ランキングの記録と稼働サマリは行いません。" >&2
        exit 1
        ;;
esac

echo
echo "----- 売買代金ランキングの記録 -----"
"${PYTHON}" -m scripts.rank_turnover || echo "rank_turnover が失敗しました（履歴は更新されていません）。"

echo
echo "----- 稼働サマリ -----"
"${PYTHON}" -m scripts.daily_report || echo "daily_report が失敗しました。"
