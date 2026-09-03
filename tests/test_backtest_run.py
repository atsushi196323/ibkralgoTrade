"""backtest/run.py のCLI配線の単体テスト（検証本体は動かさない）。"""

import argparse
import json
import sys
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from backtest.engine import BacktestConfig
from backtest.costs import CostModel
from backtest.metrics import TradeSummary
from backtest.run import _build_grid, _parameters, _parse_args, _run_multi_symbol


def _parse(argv: list) -> argparse.Namespace:
    with patch.object(sys, "argv", ["run.py", *argv]):
        return _parse_args()


# --- --initial-equity ------------------------------------------------------------


def test_initial_equity_defaults_to_the_backtest_config_value() -> None:
    args = _parse(["--csv", "bars/AAPL.csv"])

    assert args.initial_equity == BacktestConfig.initial_equity


def test_initial_equity_is_parsed() -> None:
    args = _parse(["--csv", "bars/AAPL.csv", "--initial-equity", "1220"])

    assert args.initial_equity == pytest.approx(1220.0)


def test_initial_equity_reaches_the_multi_symbol_run() -> None:
    """--initial-equity が銘柄横断のウォークフォワードまで届くこと。

    ここが繋がっていないと、小口座での検証のつもりで既定の100,000ドルの
    成績を見ることになる。1注文あたりの最低手数料は約定代金に対する比率が
    口座サイズで大きく変わるため、取り違えると成績を大幅に楽観視する。
    """
    args = _parse(["--csv-dir", "bars", "--initial-equity", "1220"])
    frames = {"AAPL": pd.DataFrame({"close": [1.0, 2.0]})}

    with patch("backtest.run._load_csv_directory", return_value=frames), \
        patch("backtest.run.run_multi_symbol_walk_forward") as mock_run, \
        patch("backtest.run.format_report", return_value=""):
        mock_run.return_value = MagicMock(outcomes=[])

        _run_multi_symbol(args, cost_model=None)

    assert mock_run.call_args.kwargs["initial_equity"] == pytest.approx(1220.0)


# --- --report --------------------------------------------------------------------


def test_no_report_is_written_unless_it_is_asked_for(tmp_path) -> None:
    """既定では書き出さない。**検証の副作用でファイルを作らない。**"""
    args = _parse(["--csv-dir", "bars"])
    frames = {"AAPL": pd.DataFrame({"close": [1.0, 2.0]})}

    with patch("backtest.run._load_csv_directory", return_value=frames), \
        patch("backtest.run.run_multi_symbol_walk_forward") as mock_run, \
        patch("backtest.run.format_report", return_value=""), \
        patch("backtest.run.write_report") as mock_write:
        mock_run.return_value = MagicMock(outcomes=[])

        _run_multi_symbol(args, cost_model=None)

    mock_write.assert_not_called()


def test_the_report_records_the_bars_that_were_actually_tested(tmp_path) -> None:
    """レポートの指紋は、検証に渡したバーそのものから取ること。

    CSVを読み直して指紋を作ると、**検証した後にファイルが差し替わっても
    気付けない**。同じ理由で、銘柄とパスの対応も読み込みと同じ関数
    (`_csv_paths_by_symbol`) から引く。
    """
    path = tmp_path / "report.json"
    args = _parse(["--csv-dir", "bars", "--report", str(path)])
    frames = {"AAPL": pd.DataFrame({
        "date": pd.to_datetime(["2026-01-02", "2026-01-05"]), "close": [1.0, 2.0],
    })}

    with patch("backtest.run._load_csv_directory", return_value=frames), \
        patch("backtest.run.run_multi_symbol_walk_forward") as mock_run, \
        patch("backtest.run.format_report", return_value=""), \
        patch("backtest.run._csv_paths_by_symbol", return_value={}):
        mock_run.return_value = MagicMock(
            outcomes=[],
            combined=TradeSummary(
                num_trades=0, win_rate_pct=0.0, profit_factor=0.0,
                avg_win_pct=0.0, avg_loss_pct=0.0, total_pnl=0.0,
            ),
        )

        _run_multi_symbol(args, cost_model=CostModel())

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["inputs"][0]["symbol"] == "AAPL"
    assert payload["inputs"][0]["num_bars"] == 2
    assert payload["mode"] == "walk-forward-multi"
    assert len(payload["result_digest"]) == 64


def test_output_only_flags_stay_out_of_the_parameters() -> None:
    """`--verbose` や `--report` は digest の対象に入れない。

    入れると、ログを増やしただけで digest が変わり、
    「同じ入力なら一致する」という確認そのものが使えなくなる。
    """
    args = _parse([
        "--csv", "bars/AAPL.csv", "--mode", "walk-forward",
        "--verbose", "--report", "out.json",
    ])

    params = _parameters(args, _build_grid(args), CostModel())

    flattened = json.dumps(params)
    assert "verbose" not in flattened
    assert "out.json" not in flattened
