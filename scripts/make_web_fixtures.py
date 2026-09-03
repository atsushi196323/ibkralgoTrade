"""`web/` のビューアが読むレポートJSONの見本を作る（IBKR接続不要）。

**この見本は、Python と TypeScript が同じ digest を出すことの контракт である。**
ビューアは digest を信じずに計算し直すので、正規化（実数の桁・キーの並び・
`inf` の表記）がずれた瞬間に照合が失敗する。両言語が同じバイト列を見ている
状態を保つため、見本はここで生成してコミットし、

- Python 側は `tests/test_web_fixtures.py` が「再生成しても同じ中身か」を見る
- TypeScript 側は `web/lib/canonical.test.ts` が「同じ digest を出せるか」を見る

の2本で挟む。**片方だけ直すとどちらかが落ちる**ので、正規化を変えるときは
必ず両方を更新することになる。

    python -m scripts.make_web_fixtures
"""

import argparse
import json
import os
from typing import Any, Dict, List

import pandas as pd

from backtest.report import RunReport, fingerprint_bars, write_report

FIXTURE_DIR = os.path.join("web", "fixtures")

# 見本は「型の網羅」を狙って作る。実際の検証結果を貼るより、
# 整数と実数の区別・整数値の実数(1220.0)・負のゼロ・inf・null・
# 非ASCII・辞書のリストといった、**正規化がずれやすい形**を集める方が効く。
_GENERATED_AT = "2026-09-03T00:00:00+00:00"


def _bars(closes: List[float]) -> pd.DataFrame:
    dates = pd.to_datetime([f"2026-01-{day:02d}" for day in range(2, 2 + len(closes))])
    return pd.DataFrame({"date": dates, "close": closes})


def _report(**overrides: Any) -> RunReport:
    base: Dict[str, Any] = dict(
        mode="walk-forward-multi",
        command="python -m backtest.run --csv-dir bars --report report.json",
        parameters={
            "initial_equity": 1220.0,
            "price_column": None,
            "costs": {
                "commission_per_share": 0.0035,
                "min_commission_per_order": 1.0,
                "max_commission_pct_of_notional": 1.0,
                "slippage_pct": 0.05,
            },
            "grid": {"ma_window": [20, 30, 40], "close_at_session_end": False},
            "note": "日本語のラベルもそのまま入る",
        },
        inputs=[
            fingerprint_bars("AAPL", _bars([100.0, 101.0, 102.5])),
            fingerprint_bars("KO", _bars([60.0, 60.5])),
        ],
        results={
            "combined": {
                "num_trades": 45,
                "win_rate_pct": 37.777777,
                "profit_factor": 1.164321,
                "total_pnl": -0.0,
            },
            "skipped_windows": 2,
            "unbeaten_profit_factor": float("inf"),
            "per_symbol": [
                {"symbol": "AAPL", "num_trades": 30, "profit_factor": 1.25},
                {"symbol": "KO", "num_trades": 15, "profit_factor": 0.98},
            ],
        },
        generated_at=_GENERATED_AT,
        environment={"python": "3.11.10", "pandas": "2.2.2", "platform": "linux"},
    )
    base.update(overrides)
    return RunReport(**base)


def _variants() -> Dict[str, RunReport]:
    base = _report()

    changed_input = _report(inputs=[
        # 終値を1本だけ動かす。指紋が変わり、digest も変わるべき場所。
        fingerprint_bars("AAPL", _bars([100.0, 101.0, 102.51])),
        fingerprint_bars("KO", _bars([60.0, 60.5])),
    ])

    params = json.loads(json.dumps(base.parameters))
    params["initial_equity"] = 3142.0
    changed_parameters = _report(parameters=params)

    results = json.loads(json.dumps(base.results, default=str))
    results["combined"]["profit_factor"] = 1.164322
    changed_results = _report(results=results)

    # 実行時刻・環境・コマンドだけが違う版。**digest は base と一致すること。**
    same_but_elsewhere = _report(
        generated_at="2026-12-31T23:59:59+00:00",
        command="python -m backtest.run --csv-dir bars --report other.json",
        environment={"python": "3.12.0", "pandas": "3.0.5", "platform": "windows"},
    )

    return {
        "report_base.json": base,
        "report_changed_input.json": changed_input,
        "report_changed_parameters.json": changed_parameters,
        "report_changed_results.json": changed_results,
        "report_same_digest_other_environment.json": same_but_elsewhere,
    }


def write_fixtures(directory: str = FIXTURE_DIR) -> Dict[str, str]:
    """見本を書き出し、「ファイル名 -> digest」を返す。"""
    os.makedirs(directory, exist_ok=True)
    digests: Dict[str, str] = {}
    for name, report in _variants().items():
        digests[name] = write_report(os.path.join(directory, name), report)
    return digests


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default=FIXTURE_DIR)
    args = parser.parse_args()

    for name, digest in write_fixtures(args.out_dir).items():
        print(f"{name}  {digest}")


if __name__ == "__main__":
    main()
