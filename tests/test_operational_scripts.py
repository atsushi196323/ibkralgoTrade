"""起動・締めのシェルスクリプトと、取引日判定CLIの不変条件。

**この2本のシェルは、Pythonのテストが1行も見ていなかった**（2026-09-01に
変異テストで確認）。`start_bot.sh` の

    "${PYTHON}" -m scripts.is_us_trading_day
    case $? in 0) ;; 1) exit 0 ;; *) exit 1 ;; esac

を `if ! "${PYTHON}" -m scripts.is_us_trading_day; then exit 0; fi` へ書き換えても
938件すべてが緑のまま通った。**これは本プロジェクトが「VPSへ移すときに最も
起こりやすい間違い」と名指ししている変更そのもの**で（`docs/DECISIONS.md`
「3. 実行環境と設定」）、症状は「毎日何も起きない」・スケジューラには
終了コード0で成功として記録される、という静かな縮退である。

検査は**静的に**行う。実際にシェルを起動すると Bot を停止しに行ったり
IB Gateway へ繋ぎに行ったりするため、テストから実行してよいものではない。
壊れ方が分岐の形に限られているので、そこだけを機械的に見れば足りる
（`tests/test_cli_help.py` が `--help` を静的に見るのと同じ理由）。
"""

import pathlib
import re
from datetime import date

import pytest

from scripts.is_us_trading_day import main as trading_day_main

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SHELL_SCRIPTS = ("scripts/start_bot.sh", "scripts/after_close.sh")

# 米国市場が開いている平日と、休場日（2026-01-01は元日）。
_TRADING_DAY = date(2026, 8, 3)
_HOLIDAY = date(2026, 1, 1)
_WEEKEND = date(2026, 8, 2)


def _source(relative: str) -> str:
    return (_ROOT / relative).read_text(encoding="utf-8")


def _trading_day_branch(source: str) -> str:
    """取引日判定の呼び出しから `esac` までを切り出す。"""
    start = source.index("-m scripts.is_us_trading_day")
    end = source.index("esac", start)
    return source[start:end]


# ---------------------------------------------------------------------------
# 終了コードの契約（シェル側がこれに依存している）
# ---------------------------------------------------------------------------


def test_a_trading_day_exits_zero() -> None:
    assert trading_day_main(["--date", _TRADING_DAY.isoformat(), "--quiet"]) == 0


@pytest.mark.parametrize("closed", [_HOLIDAY, _WEEKEND], ids=["holiday", "weekend"])
def test_a_closed_day_exits_one(closed: date) -> None:
    """休場日は1。**シェルはこの1と「それ以外」を区別している**ので、
    ここが0や2に変わると起動判定が静かに反転する。"""
    assert trading_day_main(["--date", closed.isoformat(), "--quiet"]) == 1


def test_quiet_prints_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    """`--quiet` は終了コードだけを返す。ラッパーのログに混ぜないための口である。"""
    trading_day_main(["--date", _HOLIDAY.isoformat(), "--quiet"])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


# ---------------------------------------------------------------------------
# 休場日と「判定できなかった」の区別（両スクリプト共通）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script", _SHELL_SCRIPTS)
def test_a_failed_trading_day_check_is_not_treated_as_a_holiday(script: str) -> None:
    """`case $?` の3分岐（0 / 1 / それ以外）を保つこと。

    `if ! ...` へまとめると、インタープリタのパス違いや依存関係の欠落が
    **休場日と同じ見た目**になり、しかも終了コード0でスケジューラには成功として
    記録される。判定できなかった日は失敗として残さなければ気付けない。
    """
    branch = _trading_day_branch(_source(script))

    assert "case $?" in branch, "取引日判定の結果を case で3分岐させること"
    assert re.search(r"^\s*0\)", branch, re.MULTILINE), "0（取引日）の分岐が要る"
    assert re.search(r"^\s*1\)", branch, re.MULTILINE), "1（休場日）の分岐が要る"
    assert re.search(r"^\s*\*\)", branch, re.MULTILINE), (
        "それ以外（判定できなかった）の分岐が要る"
    )


@pytest.mark.parametrize("script", _SHELL_SCRIPTS)
def test_an_undecidable_trading_day_exits_non_zero(script: str) -> None:
    """判定できなかった分岐は、必ず非ゼロで終わること。

    0で終わると `systemctl --user list-units --failed` に残らず、
    毎日何も起きないまま設定ミスに気付けない。
    """
    branch = _trading_day_branch(_source(script))
    fallback = branch[branch.index("*)"):]

    assert "exit 1" in fallback, "判定失敗は exit 1 で残すこと"
    assert ">&2" in fallback, "判定失敗は標準エラーへ出すこと（journalに残す）"


@pytest.mark.parametrize("script", _SHELL_SCRIPTS)
def test_the_scripts_reject_unset_variables(script: str) -> None:
    """`set -u` を外さないこと。

    どちらのスクリプトも `${PYTHON}` / `${BOT_UNIT}` を展開して外部プロセスを
    起動する。未定義のまま素通しすると、空文字へ展開されたコマンドが
    別のものを起動しうる。
    """
    assert re.search(r"^set -u", _source(script), re.MULTILINE)


# ---------------------------------------------------------------------------
# start_bot.sh 固有
# ---------------------------------------------------------------------------


def test_the_bot_replaces_the_wrapper_shell() -> None:
    """`exec` でBot本体に置き換えること。

    このシェルが親のまま残ると、systemd/launchd が監視するプロセスが
    ラッパーになり、`after_close.sh` の pkill パターンが一致しないシェルを
    残したまま子だけを落とすことになる。
    """
    lines = [
        line.strip()
        for line in _source("scripts/start_bot.sh").splitlines()
        if "main.py" in line and not line.lstrip().startswith("#")
    ]

    assert lines, "main.py を起動する行が見当たらない"
    assert all(line.startswith("exec ") for line in lines), (
        f"main.py の起動は exec で行うこと: {lines}"
    )


# ---------------------------------------------------------------------------
# after_close.sh 固有
# ---------------------------------------------------------------------------


def test_the_bot_is_stopped_with_sigterm_not_sigint() -> None:
    """停止は SIGTERM で送ること。

    `main.py` はSIGTERMを `KeyboardInterrupt` へ変換して `disconnect_async()`
    まで通す。SIGINTにしないのは、シェルがバックグラウンドで起動した子プロセスの
    SIGINTを `SIG_IGN` にする場合があるため（実測でcaffeinate配下が無視した）。
    """
    source = _source("scripts/after_close.sh")

    assert "pkill -TERM" in source
    assert "-INT" not in source, "SIGINTでは届かない経路がある"


def test_a_missing_pkill_is_reported_instead_of_assumed_stopped() -> None:
    """pkill が無い環境を「既に停止しています」と読み替えないこと。

    procps未導入の最小構成では pkill が非ゼロで返る。そのまま else へ落とすと、
    Botが動き続けていても停止済みと同じ文面になり、翌日のログに混ざる。
    """
    source = _source("scripts/after_close.sh")

    assert 'command -v pkill' in source, "pkill の存在を確かめること"
    guard = source[source.index("command -v pkill"):]
    assert "BOT_STOP_FAILED=1" in guard[: guard.index("elif pkill")], (
        "pkill が無い場合は失敗として残すこと"
    )


def test_the_records_are_backed_up_before_they_are_rewritten() -> None:
    """控え(2) を、ランキングの記録(3) より先に行うこと。

    `rank_turnover` は `turnover_ranks.json` を書き換える。順序が逆になると、
    yfinanceの仕様変更などで壊れた内容を書き込んだ日に、前日の状態へ戻せない。
    """
    source = _source("scripts/after_close.sh")

    assert source.index("scripts.backup_records") < source.index("scripts.rank_turnover")


def test_the_fill_price_check_runs_after_the_bot_is_stopped() -> None:
    """約定価格の読み取り確認は、Botを停止した**後**に行うこと。

    稼働中に約定した注文は `avgFillPrice` が埋まっているため、Fillからの復元は
    通常のサイクルでは一度も通らない。まっさらな接続＝再起動直後と同じ視点に
    なるのは、Botを止めた後だけである。
    """
    source = _source("scripts/after_close.sh")

    assert source.index("pkill -TERM") < source.index("scripts.check_fill_price_recovery")
