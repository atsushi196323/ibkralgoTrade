"""`web/fixtures/` の見本が、今のPython実装と一致していることの番人。

**これは Python と TypeScript の間の契約である。** ビューアは digest を
信じずに計算し直すので、正規化（実数の桁・キーの並び・`inf` の表記）が
片方だけ変わると照合が黙って失敗する。

この番人が落ちたときの正しい手順は **「見本を作り直し、TypeScript側の
テストも通ることを確かめる」** であって、この比較を緩めることではない。
"""

import json
import os

import pytest

from backtest.report import compute_digest
from scripts.make_web_fixtures import FIXTURE_DIR, write_fixtures

_FIXTURES = [
    "report_base.json",
    "report_changed_input.json",
    "report_changed_parameters.json",
    "report_changed_results.json",
    "report_same_digest_other_environment.json",
]


def _read(directory: str, name: str) -> str:
    with open(os.path.join(directory, name), encoding="utf-8") as handle:
        return handle.read()


@pytest.mark.parametrize("name", _FIXTURES)
def test_the_committed_fixture_matches_a_fresh_generation(name, tmp_path) -> None:
    write_fixtures(str(tmp_path))

    assert _read(str(tmp_path), name) == _read(FIXTURE_DIR, name), (
        f"{name} が古くなっています。`python -m scripts.make_web_fixtures` で作り直し、"
        "web/ 側のテストも通ることを確かめてください。"
    )


@pytest.mark.parametrize("name", _FIXTURES)
def test_each_fixture_hashes_to_the_digest_it_carries(name) -> None:
    """レポートを読む側が digest を計算し直せること。

    **TypeScript側はこれと同じ計算を独立に実装している。** ここが通り、
    向こうも通るなら、2つの言語が同じ正規化に合意している。
    """
    payload = json.loads(_read(FIXTURE_DIR, name))

    recomputed = compute_digest({
        "mode": payload["mode"],
        "parameters": payload["parameters"],
        "inputs": payload["inputs"],
        "results": payload["results"],
    })

    assert recomputed == payload["result_digest"]


def test_only_the_environment_differs_between_the_two_matching_fixtures() -> None:
    """環境と時刻を変えても digest が動かないことを、見本そのもので示す。"""
    base = json.loads(_read(FIXTURE_DIR, "report_base.json"))
    other = json.loads(_read(FIXTURE_DIR, "report_same_digest_other_environment.json"))

    assert base["result_digest"] == other["result_digest"]
    assert base["environment"] != other["environment"]
    assert base["generated_at"] != other["generated_at"]


@pytest.mark.parametrize("name", [
    "report_changed_input.json",
    "report_changed_parameters.json",
    "report_changed_results.json",
])
def test_each_changed_fixture_has_a_different_digest(name) -> None:
    base = json.loads(_read(FIXTURE_DIR, "report_base.json"))
    changed = json.loads(_read(FIXTURE_DIR, name))

    assert base["result_digest"] != changed["result_digest"]


def test_the_fixtures_are_plain_json() -> None:
    """`Infinity` / `NaN` を書かない。

    Pythonの `json` は既定でこれらを出すが、標準JSONには無い表記なので
    `JSON.parse` で読めない。プロフィットファクターは負けトレードが0件だと
    `inf` になるため、見本にわざと1件入れてある。
    """
    text = _read(FIXTURE_DIR, "report_base.json")

    assert "Infinity" not in text and "NaN" not in text
    assert json.loads(text)["results"]["unbeaten_profit_factor"] == "inf"
