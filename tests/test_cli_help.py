"""argparse の説明文が `--help` を壊さないこと。

**レビュアーも運用者も、最初に叩くのは `--help` である。** ここが落ちると、
そのツールは「存在するが使えない」状態になる。

実際に3つのCLIが落ちていた（2026-09-01に発見。`measure_alpha` /
`study_signals` / `check_robustness`）。原因は argparse が `help=` の文字列を
**`%` 書式として解釈する**ことで、`リスク1%/損切り5%` のような説明文が
そのまま `ValueError: unsupported format character` になる。

**このエラーは import 時にも通常の実行時にも出ない。`--help` を叩いたとき
だけ出る**——本プロジェクトが警戒している「例外を出さずに壊れている」状態の
一種で、テストが無ければ気付けなかった。

検査は**静的に**行う。実際に `--help` を実行すると、モジュールの
トップレベルやCLIの初期化が走って作業ディレクトリに `logs/` を作りうる
（`tests/test_logging_setup.py` が禁じている副作用）。壊れ方が書式文字列に
限られているので、そこだけを機械的に見れば足りる。
"""

import ast
import pathlib

import pytest

_ARGPARSE_TEXT_KEYWORDS = ("help", "description", "epilog")

# `%` の次がこれらなら書式指定として正しい。`%%` はリテラルの %、
# `%(default)s` のような名前参照は argparse が自分で埋める。
_SAFE_AFTER_PERCENT = ("%", "(")


def _cli_sources():
    root = pathlib.Path(__file__).resolve().parent.parent
    for directory in ("scripts", "backtest"):
        yield from sorted((root / directory).glob("*.py"))


def _bare_percent_offsets(text: str):
    """argparse が書式指定と誤解する `%` の位置を返す。"""
    offsets = []
    index = 0
    while index < len(text):
        if text[index] == "%":
            following = text[index + 1: index + 2]
            if following == "%":
                index += 2
                continue
            if following == "(":
                index += 1
                continue
            offsets.append(index)
        index += 1
    return offsets


@pytest.mark.parametrize("path", list(_cli_sources()), ids=lambda p: p.name)
def test_argparse_texts_do_not_break_help(path: pathlib.Path) -> None:
    """`help=` / `description=` / `epilog=` に裸の `%` が無いこと。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg not in _ARGPARSE_TEXT_KEYWORDS:
                continue
            if not isinstance(keyword.value, ast.Constant):
                continue
            text = keyword.value.value
            if not isinstance(text, str):
                continue
            for offset in _bare_percent_offsets(text):
                offenders.append(
                    f"{path.name}:{keyword.value.lineno} …{text[max(0, offset - 20):offset + 10]}…"
                )

    assert not offenders, (
        "argparse の説明文に裸の `%` があります（`--help` が ValueError で落ちます）。"
        "`%%` へ直すこと:\n  " + "\n  ".join(offenders)
    )
