#!/bin/bash
#
# Botを起動する。launchdから平日22:15(JST)に呼ばれる
# （com.user.ibkralgotrade）。
#
# launchdを直接main.pyへ向けずにこのラッパーを挟むのは、**米国市場の祝日を
# ジョブ定義では表現できない**ためである。StartCalendarInterval が持つのは
# Minute/Hour/Day/Weekday/Month だけで、除外の仕組みも祝日カレンダーも無い。
#
# 祝日に起動しても main.py は is_regular_trading_hours() で時間外と判定して
# 待機し続けるだけなので実害は無いが、7時間ぶんの待機ログとGatewayへの
# 接続試行が積まれ、翌朝のサマリが「なぜ1件も建たなかったのか」を読む用途に
# 使えなくなる。判定はここで済ませ、休場日はプロセスを起こさない。

set -u

cd "$(dirname "$0")/.." || exit 1

# launchd/systemdはログインシェルを経由せずPATHにpyenvやvenvが入らないため、
# インタープリタの実体を指す。環境変数で上書きできるようにしてあるのは、
# 手元で別の環境から叩いて動作を確かめられるようにするためと、**Linuxでは
# パスが違う**ため（VPSでは systemd unit の Environment= で渡す）。
PYTHON="${IBKRALGO_PYTHON:-/Users/user/.pyenv/versions/3.11.10/bin/python3.11}"

# **休場日(終了コード1)と、判定そのものに失敗した場合を区別する。**
# `is_us_trading_day` が返すのは 0=取引日 / 1=休場日 だけなので、それ以外は
# インタープリタのパス違いや依存関係の欠落を意味する。まとめて「起動しない」に
# 倒すと、**設定を間違えた日が休場日と同じ見た目になり、しかも終了コード0で
# スケジューラには成功として記録される**——毎日何も起きないまま気付けない。
# VPSへ移すときに最も起こりやすい間違いがこれである（既定値がmacOSのpyenvを
# 指しているため）。判定できなかったときは失敗として残すこと。
"${PYTHON}" -m scripts.is_us_trading_day
case $? in
    0) ;;
    1)
        echo "$(date '+%Y-%m-%d %H:%M:%S %Z') Botは起動しません。"
        exit 0
        ;;
    *)
        echo "$(date '+%Y-%m-%d %H:%M:%S %Z') ERROR: 取引日を判定できませんでした" \
             "(PYTHON=${PYTHON})。Botを起動しません。" >&2
        exit 1
        ;;
esac

# caffeinateでラップするのは、macOSのアイドルスリープがCPU使用率ではなく
# ユーザー操作の有無で判定されるため（plist側のコメントに経緯がある）。
# **Linuxにこの問題は無い**ので挟まない——サーバはスリープしないうえ、
# caffeinate自体が存在しない。
#
# execで置き換えるのは、launchd/systemdが監視するプロセスをこのシェルではなく
# 実体にするため。このシェルが親のまま残ると、after_close.sh の pkill パターンが
# 一致しないシェルを残したまま子だけを落とすことになる。
if [ "$(uname -s)" = "Darwin" ]; then
    exec /usr/bin/caffeinate -i -s -m "${PYTHON}" main.py
fi
exec "${PYTHON}" main.py
