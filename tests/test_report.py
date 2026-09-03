"""検証レポートと再現性の番人。

**このファイルが守っているのは「同じ入力なら同じ digest」という1点である。**
バックテストの数字が前と違ったとき、データ・パラメータ・コードのどれが
動いたのかを切り分ける唯一の手掛かりがこれなので、digest が入力に
反応しなくなる（＝何を変えても一致する）変更は、静かに再現性の確認を
無効化する。
"""

import json

import pandas as pd
import pytest

from backtest.report import (
    RunReport,
    compute_digest,
    fingerprint_bars,
    format_markdown,
    sha256_of_file,
    write_report,
)


def _bars(closes=(100.0, 101.0, 102.0)) -> pd.DataFrame:
    return pd.DataFrame({
        "date": pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06"][: len(closes)]),
        "close": list(closes),
    })


def _report(**overrides) -> RunReport:
    base = dict(
        mode="backtest",
        command="python -m backtest.run --csv x.csv",
        parameters={"initial_equity": 1220.0},
        inputs=[fingerprint_bars("AAPL", _bars())],
        results={"metrics": {"num_trades": 3, "profit_factor": 1.25}},
        generated_at="2026-01-06T00:00:00+00:00",
        environment={"python": "3.11.10", "pandas": "2.2.2"},
    )
    base.update(overrides)
    return RunReport(**base)  # type: ignore[arg-type]


def test_the_same_input_produces_the_same_digest() -> None:
    assert _report().result_digest == _report().result_digest


def test_the_generation_time_does_not_change_the_digest() -> None:
    # 実行時刻を混ぜると、同じ入力の2回の実行が必ず別の digest になり、
    # 再現性の確認という目的そのものが消える。
    early = _report(generated_at="2026-01-06T00:00:00+00:00")
    late = _report(generated_at="2026-09-03T12:34:56+00:00")
    assert early.result_digest == late.result_digest


def test_the_environment_does_not_change_the_digest() -> None:
    # **意図的である。** 環境が変わっても数字が変わらないことを確かめたいので、
    # 環境を混ぜると「pandasを上げたら digest が変わった」が常に起き、
    # 本当に結果が動いたのかを区別できなくなる。
    other = _report(environment={"python": "3.12.0", "pandas": "3.0.5"})
    assert other.result_digest == _report().result_digest


def test_the_command_line_does_not_change_the_digest() -> None:
    # 出力先(--report)やログの設定を変えただけで digest が動くと、
    # 「結果が変わった」と読めてしまう。
    assert _report(command="python -m backtest.run --report other.json").result_digest \
        == _report().result_digest


def test_a_single_changed_bar_changes_the_digest() -> None:
    changed = _report(inputs=[fingerprint_bars("AAPL", _bars((100.0, 101.0, 102.01)))])
    assert changed.result_digest != _report().result_digest


def test_a_changed_parameter_changes_the_digest() -> None:
    assert _report(parameters={"initial_equity": 1221.0}).result_digest \
        != _report().result_digest


def test_a_changed_result_changes_the_digest() -> None:
    assert _report(results={"metrics": {"num_trades": 3, "profit_factor": 1.26}}).result_digest \
        != _report().result_digest


def test_the_fingerprint_ignores_how_the_file_was_written() -> None:
    """**確かめたいのは「同じデータか」であって「同じファイルか」ではない。**

    列の順序や小数の書き方が違っても、値が同じなら同じ指紋になること。
    ファイルを取り直しただけで digest が変わると、再現性の確認が
    「同じファイルを使い回したか」の確認に落ちる。
    """
    original = _bars()
    reordered = original[["close", "date"]].copy()
    assert fingerprint_bars("AAPL", original).bars_sha256 \
        == fingerprint_bars("AAPL", reordered).bars_sha256


def test_the_fingerprint_records_the_span_and_the_row_count() -> None:
    fingerprint = fingerprint_bars("AAPL", _bars())
    assert fingerprint.num_bars == 3
    assert fingerprint.first_date is not None and fingerprint.first_date.startswith("2026-01-02")
    assert fingerprint.last_date is not None and fingerprint.last_date.startswith("2026-01-06")


def test_floats_are_rounded_before_hashing() -> None:
    """実数は桁を決めてから文字列にする。

    `repr(float)` は環境と版で揺れうるので、そのまま連結すると
    「数字は同じなのに digest が違う」が起きる。
    """
    assert compute_digest({"x": 1.0000001}) == compute_digest({"x": 1.0000002})
    assert compute_digest({"x": 1.0}) != compute_digest({"x": 1.1})


def test_nan_and_inf_do_not_break_the_digest() -> None:
    # プロフィットファクターは負けトレードが0件だと inf になる（metrics.py）。
    assert compute_digest({"pf": float("inf")}) == compute_digest({"pf": float("inf")})
    assert compute_digest({"pf": float("nan")}) != compute_digest({"pf": float("inf")})


def test_the_digest_of_a_fixed_payload_is_stable_across_environments() -> None:
    """**この値を書き換えて通してはならない。**

    ここが変わるということは、同じ入力から違う digest が出るということで、
    過去のレポートと今日のレポートを比べられなくなる。CIは pandas 2系と
    3系の両方でこれを回しており、版差でハッシュが動かないことの番人になる。
    """
    payload = {"mode": "backtest", "trades": 45, "profit_factor": 1.16, "flag": True}
    assert compute_digest(payload) == \
        "6dded8de8980cdddd04f84245465d434491c6a4eec863e65b65ee3fa26b43b25"


def test_the_json_report_carries_the_digest_and_the_inputs(tmp_path) -> None:
    path = tmp_path / "report.json"
    digest = write_report(str(path), _report())
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["result_digest"] == digest
    assert payload["inputs"][0]["symbol"] == "AAPL"
    assert payload["schema_version"] == 1


def test_the_markdown_report_puts_the_digest_first(tmp_path) -> None:
    # 2回の実行を比べるとき最初に見る値なので、下まで読ませない。
    text = format_markdown(_report())
    assert text.splitlines()[2].startswith("- **result_digest**")
    assert "AAPL" in text


def test_the_markdown_report_flattens_nested_settings() -> None:
    # 見出しの数字が辞書表記の中に埋もれると、開いた人が最初に見たい値が読めない。
    text = format_markdown(_report(parameters={"costs": {"slippage_pct": 0.05}}))
    assert "| costs.slippage_pct |" in text


def test_the_extension_decides_the_format(tmp_path) -> None:
    json_path, md_path = tmp_path / "r.json", tmp_path / "r.md"
    write_report(str(json_path), _report())
    write_report(str(md_path), _report())
    assert json_path.read_text(encoding="utf-8").lstrip().startswith("{")
    assert md_path.read_text(encoding="utf-8").startswith("# バックテストレポート")


def test_an_unreadable_file_does_not_stop_the_report(tmp_path) -> None:
    # 指紋はバー側で取れているので、ファイルのハッシュは欠けてよい。
    assert sha256_of_file(str(tmp_path / "missing.csv")) is None


@pytest.mark.parametrize("closes", [(100.0, 101.0), (100.0, 101.0, 102.0)])
def test_the_bar_count_is_part_of_the_fingerprint(closes) -> None:
    assert fingerprint_bars("AAPL", _bars(closes)).num_bars == len(closes)


def test_a_non_finite_number_is_still_valid_json(tmp_path) -> None:
    """`inf` を数値のまま書かない。

    Pythonの `json` は既定で `Infinity` という**標準JSONに無い表記**を出すため、
    その行が入ったレポートは `JSON.parse` 等で読めなくなる。プロフィット
    ファクターは負けトレードが0件だと `inf` になるので、例外的な状況ではない。
    """
    path = tmp_path / "report.json"
    write_report(str(path), _report(results={"profit_factor": float("inf")}))

    text = path.read_text(encoding="utf-8")
    assert "Infinity" not in text
    assert json.loads(text)["results"]["profit_factor"] == "inf"


def test_the_written_report_still_hashes_to_its_own_digest(tmp_path) -> None:
    """書き出したファイルから計算し直した digest が、中の値と一致すること。

    **これは他言語の実装との契約でもある**（`web/` のビューアは digest を
    信じるのではなく計算し直して照合する）。書き出しの都合で値の表記を
    変えると、その照合が黙って失敗する。
    """
    path = tmp_path / "report.json"
    write_report(str(path), _report(results={"profit_factor": float("inf"), "num_trades": 3}))

    payload = json.loads(path.read_text(encoding="utf-8"))
    recomputed = compute_digest({
        "mode": payload["mode"],
        "parameters": payload["parameters"],
        "inputs": payload["inputs"],
        "results": payload["results"],
    })
    assert recomputed == payload["result_digest"]
