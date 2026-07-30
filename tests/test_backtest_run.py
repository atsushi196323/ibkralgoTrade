"""backtest/run.py のCLI配線の単体テスト（検証本体は動かさない）。"""

import argparse
import sys
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from backtest.engine import BacktestConfig
from backtest.run import _parse_args, _run_multi_symbol


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
