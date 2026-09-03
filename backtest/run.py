"""ヒストリカルデータを取得し、バックテスト/ウォークフォワード検証を実行するCLI。

データ源は2通り:
  - IBKR（既定）  … 本番と同じバーを使う。IB Gatewayへの接続が必要。
  - CSV (--csv)   … 外部データ（yfinance/Stooq等）。接続不要でオフライン検証できる。

実行例:
    python -m backtest.run --symbol RIVN --duration "2 Y" --bar-size "1 day"
    python -m backtest.run --symbol RIVN --mode backtest --duration "2 Y"
    python -m backtest.run --csv bars/RIVN.csv --mode backtest
    python -m backtest.run --csv bars/RIVN.csv --no-costs   # コスト影響の比較用
    python -m backtest.run --csv-dir bars              # 複数銘柄で判断する

CSVは `python -m scripts.fetch_bars` で用意できる（IBKR接続不要）。
"""

import argparse
import asyncio
import datetime as dt
import glob
import logging
import os
import shlex
import sys
from dataclasses import asdict, replace
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from backtest.costs import ZERO_COST, CostModel
from backtest.csv_source import load_bars_from_csv
from backtest.engine import BacktestConfig, run_backtest
from backtest.market_reference import DEFAULT_MARKET_MA_WINDOW, attach_market_deviation
from backtest.metrics import PerformanceMetrics, compute_metrics
from backtest.report import (
    InputFingerprint,
    RunReport,
    fingerprint_bars,
    sha256_of_file,
    write_report,
)
from backtest.multi_symbol import format_report, run_multi_symbol_walk_forward
from backtest.walk_forward import (
    DEFAULT_MIN_TRADES_FOR_SELECTION,
    ParameterGrid,
    run_walk_forward,
)
from core.connection import IBKRConnection
from data.market_data import get_historical_bars_async, qualify_stock_async

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ライブでは1サイクルに1回しか鳴らないが、バックテストでは
# 「バー数 × グリッドの組合せ数 × ウィンドウ数 × 銘柄数」回呼ばれるロガー群。
# INFOのまま42銘柄・10年を回すと出力だけで数百MB・十数分かかり、
# 検証そのものより桁違いに重くなる。既定では黙らせ、--verbose で戻す。
_PER_BAR_LOGGERS = (
    "strategy.pullback",
    "strategy.exit_signal",
    "execution.position_sizing",
    "backtest.engine",
)


def _configure_log_levels(verbose: bool) -> None:
    level = logging.INFO if verbose else logging.WARNING
    for name in _PER_BAR_LOGGERS:
        logging.getLogger(name).setLevel(level)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="プルバック戦略のバックテスト/ウォークフォワード検証")
    parser.add_argument(
        "--symbol",
        help="銘柄シンボル。--csv 指定時は省略可（ファイル名から推定する）。",
    )
    parser.add_argument(
        "--csv",
        help="ヒストリカルバーのCSVパス。指定するとIBKRへ接続せずこのファイルを使う。",
    )
    parser.add_argument(
        "--csv-dir",
        help="CSVを並べたディレクトリ。中の全銘柄をウォークフォワード検証して"
             "銘柄横断で集計する（単一銘柄の成績は運なので、判断はこちらで行う）。",
    )
    parser.add_argument(
        "--price-column",
        help="CSVで終値として使う列名。既定は close（無ければ adj close）。",
    )
    parser.add_argument("--duration", default="2 Y", help="reqHistoricalDataのdurationStr（IBKR使用時のみ）")
    parser.add_argument("--bar-size", default="1 day", help="reqHistoricalDataのbarSizeSetting（IBKR使用時のみ）")
    parser.add_argument("--mode", choices=["backtest", "walk-forward"], default="walk-forward")
    parser.add_argument("--train-bars", type=int, default=252)
    parser.add_argument("--test-bars", type=int, default=63)
    parser.add_argument(
        "--step-bars", type=int, default=None,
        help="ウィンドウを1回に前進させるバー数。既定は --test-bars"
             "（検証期間が重複せず隙間なく並ぶ）。",
    )
    parser.add_argument(
        "--min-trades", type=int, default=DEFAULT_MIN_TRADES_FOR_SELECTION,
        help="学習期間でこの回数以上トレードした設定だけを選定対象にする。",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="1バーごとのシグナル判定ログも出す（デバッグ用。大量の出力になる）。",
    )
    parser.add_argument(
        "--initial-equity", type=float, default=BacktestConfig.initial_equity,
        help="検証の初期資金(USD)。ポジションサイジングの基準になるため、"
             "**実際に運用する資金額を指定すること**。既定の100,000ドルのままだと、"
             "1注文あたりの最低手数料(--min-commission)が約定代金に対して相対的に"
             "小さくなり、小口座の成績を大幅に楽観視する。",
    )

    strategy = parser.add_argument_group(
        "戦略パラメータの探索範囲（既定はスイング＝日足で検証済みの範囲）",
        "デイトレード(5分足)を検証するときは、ライブの既定値"
        "（MA20 / -2% / +3% / -1.5% / トレーリング-2%）を含む範囲を明示すること。",
    )
    strategy.add_argument("--ma-window", type=int, nargs="*", default=None,
                          help="移動平均期間の候補")
    strategy.add_argument("--threshold", type=float, nargs="*", default=None,
                          help="買いシグナルの下方乖離率(%%)の候補")
    strategy.add_argument("--take-profit", type=float, nargs="*", default=None,
                          help="利確幅(%%)の候補")
    strategy.add_argument("--stop-loss", type=float, nargs="*", default=None,
                          help="損切り幅(%%)の候補")
    strategy.add_argument("--trailing-stop", type=float, nargs="*", default=None,
                          help="トレーリングストップ幅(%%)の候補")
    strategy.add_argument(
        "--close-at-session-end", action="store_true",
        help="取引日の最後のバーで建玉を手仕舞う（デイトレード検証では必須）。"
             "ライブのデイトレード建玉は15:55 ETに強制決済するため、"
             "これを付けずに日中足を回すと別の戦略を検証することになる。",
    )

    market = parser.add_argument_group(
        "市場フィルター（指数の乖離率による追加条件。--market-csv が必須）"
    )
    market.add_argument(
        "--market-csv",
        help="指数（SPY等）の日足CSV。銘柄のバーへ日付で突き合わせて乖離率を付与する。",
    )
    market.add_argument(
        "--market-ma-window", type=int, default=DEFAULT_MARKET_MA_WINDOW,
        help="指数の乖離率を測る移動平均の本数。パラメータグリッドの軸にはしない"
             "（レジームの物差しまで学習期間に合わせると過剰最適化の余地が広がるため）。",
    )
    market.add_argument(
        "--market-min-deviation", type=float, nargs="*", default=None,
        help="指数の乖離率がこの値以上のときだけ買う（下降レジームを避ける向き）。"
             "複数指定するとグリッドの候補になる。",
    )
    market.add_argument(
        "--market-max-deviation", type=float, nargs="*", default=None,
        help="指数の乖離率がこの値以下のときだけ買う（市場のパニックを待つ向き）。",
    )
    market.add_argument(
        "--relative-threshold", type=float, nargs="*", default=None,
        help="個別銘柄の乖離率が指数をこの幅以上下回るときだけ買う（相対乖離）。",
    )
    market.add_argument(
        "--keep-unfiltered", action="store_true",
        help="上の閾値に加えて『フィルター無し』も候補に残す"
             "（ウォークフォワードにフィルターの要否そのものを選ばせる）。",
    )

    costs = parser.add_argument_group("取引コスト（既定値はIBKR Tiered・米国株相当）")
    costs.add_argument("--commission-per-share", type=float, default=CostModel.commission_per_share)
    costs.add_argument("--min-commission", type=float, default=CostModel.min_commission_per_order)
    costs.add_argument(
        "--max-commission-pct", type=float, default=CostModel.max_commission_pct_of_notional,
        help="約定代金に対する手数料の上限(%%)。0以下で上限なし。",
    )
    costs.add_argument(
        "--slippage-pct", type=float, default=CostModel.slippage_pct,
        help="片道あたりのスリッページ(%%)。買いは高く、売りは安く約定するものとして扱う。",
    )
    costs.add_argument(
        "--no-costs", action="store_true",
        help="手数料・スリッページを一切かけない（コスト影響の比較専用。収益性の判断には使わないこと）。",
    )

    parser.add_argument(
        "--report", metavar="PATH",
        help=(
            "検証結果を、入力の指紋・パラメータ・実行環境つきでファイルへ書き出す。"
            "拡張子が .md なら人が読む形、それ以外はJSON。"
            "同じデータ・同じパラメータの実行は result_digest が一致する。"
        ),
    )

    args = parser.parse_args()

    if args.csv and args.csv_dir:
        parser.error("--csv と --csv-dir は同時に指定できません。")
    if not args.csv and not args.csv_dir and not args.symbol:
        parser.error("--symbol は必須です（--csv / --csv-dir を使う場合のみ省略できます）。")
    if args.csv_dir and args.mode != "walk-forward":
        parser.error("--csv-dir は --mode walk-forward でのみ使えます。")

    uses_market_filter = any(
        value for value in (
            args.market_min_deviation, args.market_max_deviation, args.relative_threshold,
        )
    )
    if uses_market_filter and not args.market_csv:
        parser.error("市場フィルターの閾値を指定する場合は --market-csv も必要です。")
    if args.keep_unfiltered and not uses_market_filter:
        parser.error("--keep-unfiltered は市場フィルターの閾値と併せて指定してください。")

    return args


def _market_axis(values: Optional[List[float]], keep_unfiltered: bool) -> Sequence[Optional[float]]:
    """CLIで受け取った閾値を、グリッドの1軸（Noneを含みうる並び）に変換する。"""
    if not values:
        return (None,)
    if keep_unfiltered:
        # 「フィルター無し」も候補に残し、要否そのものを学習期間に選ばせる。
        return (None, *values)
    return tuple(values)


def _axis(values, default):
    """指定があればその候補列、無ければ既定の探索範囲を返す。"""
    return tuple(values) if values else default


def _build_grid(args: argparse.Namespace) -> ParameterGrid:
    defaults = ParameterGrid()
    return ParameterGrid(
        ma_window=_axis(args.ma_window, defaults.ma_window),
        threshold_pct=_axis(args.threshold, defaults.threshold_pct),
        take_profit_pct=_axis(args.take_profit, defaults.take_profit_pct),
        stop_loss_pct=_axis(args.stop_loss, defaults.stop_loss_pct),
        trailing_stop_pct=_axis(args.trailing_stop, defaults.trailing_stop_pct),
        market_min_deviation_pct=_market_axis(args.market_min_deviation, args.keep_unfiltered),
        market_max_deviation_pct=_market_axis(args.market_max_deviation, args.keep_unfiltered),
        relative_threshold_pct=_market_axis(args.relative_threshold, args.keep_unfiltered),
        close_at_session_end=args.close_at_session_end,
    )


def _load_market_df(args: argparse.Namespace) -> Optional[pd.DataFrame]:
    if not args.market_csv:
        return None
    return load_bars_from_csv(args.market_csv)


def _with_market_deviation(
    df: pd.DataFrame, market_df: Optional[pd.DataFrame], args: argparse.Namespace,
    symbol: Optional[str] = None,
) -> pd.DataFrame:
    if market_df is None:
        return df
    return attach_market_deviation(df, market_df, args.market_ma_window, symbol=symbol)


def _build_cost_model(args: argparse.Namespace) -> CostModel:
    if args.no_costs:
        logger.warning(
            "コストを無視して検証します(--no-costs)。この結果は実運用の収益性の"
            "判断材料にはなりません。"
        )
        return ZERO_COST

    return CostModel(
        commission_per_share=args.commission_per_share,
        min_commission_per_order=args.min_commission,
        max_commission_pct_of_notional=args.max_commission_pct,
        slippage_pct=args.slippage_pct,
    )


def _resolve_symbol(args: argparse.Namespace) -> str:
    if args.symbol:
        return args.symbol
    # --csv のみ指定された場合、ファイル名（拡張子を除く）を銘柄名として扱う。
    # ここはログ表示とTradeレコードのラベルにしか使わないため、推定で足りる。
    return os.path.splitext(os.path.basename(args.csv))[0].upper()


async def _load_bars_async(args: argparse.Namespace) -> pd.DataFrame:
    if args.csv:
        return load_bars_from_csv(args.csv, price_column=args.price_column)

    connection = IBKRConnection()
    try:
        ib = await connection.connect_async()
        contract = await qualify_stock_async(ib, args.symbol)
        return await get_historical_bars_async(
            ib, contract, duration=args.duration, bar_size=args.bar_size,
        )
    finally:
        await connection.disconnect_async()


def _log_metrics(metrics: PerformanceMetrics, total_commission: Optional[float] = None) -> None:
    logger.info(
        "trades=%d win_rate=%.1f%% profit_factor=%.2f total_return=%.2f%% "
        "max_drawdown=%.2f%% sharpe=%.2f",
        metrics.num_trades, metrics.win_rate_pct, metrics.profit_factor,
        metrics.total_return_pct, metrics.max_drawdown_pct, metrics.sharpe_ratio,
    )
    if total_commission is not None:
        logger.info("支払い手数料の合計=%.2f USD（上記の損益は控除後）", total_commission)


def _parameters(args: argparse.Namespace, grid: ParameterGrid, cost_model: CostModel) -> Dict[str, Any]:
    """レポートに残す設定。**結果を変えうるものだけを入れる。**

    `--verbose` や `--report` のような出力の設定は入れない。入れると、
    ログを増やしただけで digest が変わり、再現性の確認が使えなくなる。
    """
    params: Dict[str, Any] = {
        "initial_equity": args.initial_equity,
        "price_column": args.price_column,
        "grid": asdict(grid),
        "costs": asdict(cost_model),
    }
    if args.mode == "walk-forward":
        params["walk_forward"] = {
            "train_bars": args.train_bars,
            "test_bars": args.test_bars,
            "step_bars": args.step_bars,
            "min_trades_for_selection": args.min_trades,
        }
    if args.market_csv:
        # 指数はファイル名だけ残す（中身の指紋は inputs 側に入る）。
        params["market_reference"] = {
            "file": os.path.basename(args.market_csv),
            "ma_window": args.market_ma_window,
        }
    return params


def _fingerprint(symbol: str, df: pd.DataFrame, path: Optional[str]) -> InputFingerprint:
    return fingerprint_bars(
        symbol, df, path=path,
        file_sha256=sha256_of_file(path) if path else None,
    )


def _emit_report(
    args: argparse.Namespace, *, mode: str, inputs: List[InputFingerprint],
    results: Dict[str, Any], parameters: Dict[str, Any],
) -> None:
    if not args.report:
        return
    report = RunReport(
        mode=mode,
        command="python -m backtest.run " + shlex.join(sys.argv[1:]),
        parameters=parameters,
        inputs=inputs,
        results=results,
        generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    )
    digest = write_report(args.report, report)
    logger.info("レポートを書き出しました: %s", args.report)
    logger.info("result_digest=%s（同じ入力・同じ設定なら一致する）", digest)


def _metrics_dict(metrics: PerformanceMetrics) -> Dict[str, Any]:
    return asdict(metrics)


def _csv_paths_by_symbol(directory: str) -> Dict[str, str]:
    """ディレクトリ内の *.csv を「銘柄 -> パス」で返す。

    **読み込みと指紋づくりが同じ関数を見るようにしてある。** 別々に glob すると、
    片方だけ並び順や拡張子の扱いを変えたときに、レポートが実際に検証した
    ファイルと違うものを指しうる。
    """
    return {
        os.path.splitext(os.path.basename(path))[0].upper(): path
        for path in sorted(glob.glob(os.path.join(directory, "*.csv")))
    }


def _load_csv_directory(directory: str, price_column: Optional[str]) -> Dict[str, pd.DataFrame]:
    """ディレクトリ内の *.csv を「銘柄 -> バー」の辞書として読み込む。"""
    paths_by_symbol = _csv_paths_by_symbol(directory)
    if not paths_by_symbol:
        raise ValueError(f"CSVが1件も見つかりません: {directory}")

    frames: Dict[str, pd.DataFrame] = {}
    for symbol, path in paths_by_symbol.items():
        try:
            frames[symbol] = load_bars_from_csv(path, price_column=price_column)
        except ValueError:
            # 1銘柄の不備で検証全体を止めない。
            logger.exception("%s を読み込めなかったため除外します。", path)

    if not frames:
        raise ValueError(f"読み込めたCSVが1件もありません: {directory}")
    return frames


def _run_multi_symbol(args: argparse.Namespace, cost_model: CostModel) -> None:
    frames = _load_csv_directory(args.csv_dir, args.price_column)
    market_df = _load_market_df(args)
    if market_df is not None:
        # 指数そのものが --csv-dir に含まれていると、指数を1銘柄として検証して
        # しまい銘柄横断の集計を汚す。読み込み側では区別できないためここで除く。
        market_symbol = os.path.splitext(os.path.basename(args.market_csv))[0].upper()
        if frames.pop(market_symbol, None) is not None:
            logger.info("%s は指数として使うため検証対象から除外しました。", market_symbol)
        frames = {
            symbol: _with_market_deviation(df, market_df, args, symbol)
            for symbol, df in frames.items()
        }
    logger.info("%d銘柄を読み込みました: %s", len(frames), list(frames))

    report = run_multi_symbol_walk_forward(
        frames, _build_grid(args),
        train_bars=args.train_bars, test_bars=args.test_bars,
        initial_equity=args.initial_equity,
        costs=cost_model, step_bars=args.step_bars,
        min_trades_for_selection=args.min_trades,
    )

    print("\n" + format_report(report, symbol_order=list(frames)))

    total_commission = sum(
        t.commission for outcome in report.outcomes for t in outcome.trades
    )
    print(f"\n  支払い手数料の合計 : {total_commission:,.2f} USD（上記の損益は控除後）")

    # **引数の組み立てを guard の内側に置く。** `asdict` や銘柄別の集計は
    # レポートを書かない実行では純粋な無駄で、呼び出し側の型にも依存する。
    if not args.report:
        return
    paths = _csv_paths_by_symbol(args.csv_dir)
    _emit_report(
        args, mode="walk-forward-multi",
        inputs=[_fingerprint(sym, df, paths.get(sym)) for sym, df in frames.items()],
        parameters=_parameters(args, _build_grid(args), cost_model),
        results={
            "combined": asdict(report.combined),
            "total_commission": total_commission,
            "num_symbols": len(report.outcomes),
            "per_symbol": [
                {
                    "symbol": o.symbol,
                    "num_trades": o.summary.num_trades,
                    "win_rate_pct": o.summary.win_rate_pct,
                    "profit_factor": o.summary.profit_factor,
                    "total_pnl": o.summary.total_pnl,
                    "num_windows": o.num_windows,
                    "skipped_windows": o.skipped_windows,
                }
                for o in report.outcomes
            ],
        },
    )


async def main() -> None:
    args = _parse_args()
    _configure_log_levels(args.verbose)
    cost_model = _build_cost_model(args)

    if args.csv_dir:
        _run_multi_symbol(args, cost_model)
        return

    symbol = _resolve_symbol(args)

    df = await _load_bars_async(args)

    if df.empty:
        logger.error("%s のヒストリカルデータを取得できませんでした。", symbol)
        return

    df = _with_market_deviation(df, _load_market_df(args), args, symbol)

    if args.mode == "backtest":
        # 単発のバックテストはグリッド探索をしないので、各軸の先頭の値を使う。
        grid = _build_grid(args)
        config = replace(
            BacktestConfig(), costs=cost_model, initial_equity=args.initial_equity,
            ma_window=grid.ma_window[0], threshold_pct=grid.threshold_pct[0],
            take_profit_pct=grid.take_profit_pct[0], stop_loss_pct=grid.stop_loss_pct[0],
            trailing_stop_pct=grid.trailing_stop_pct[0],
            close_at_session_end=grid.close_at_session_end,
            market_min_deviation_pct=grid.market_min_deviation_pct[-1],
            market_max_deviation_pct=grid.market_max_deviation_pct[-1],
            relative_threshold_pct=grid.relative_threshold_pct[-1],
        )
        result = run_backtest(symbol, df, config)
        metrics = compute_metrics(result)
        total_commission = sum(t.commission for t in result.trades)
        _log_metrics(metrics, total_commission)
        if not args.report:
            return
        _emit_report(
            args, mode="backtest",
            inputs=[_fingerprint(symbol, df, args.csv)],
            parameters=_parameters(args, grid, cost_model),
            results={
                "metrics": _metrics_dict(metrics),
                "total_commission": total_commission,
                "final_equity": result.final_equity,
            },
        )
        return

    grid = _build_grid(args)
    wf_result = run_walk_forward(
        symbol, df, grid, train_bars=args.train_bars, test_bars=args.test_bars,
        initial_equity=args.initial_equity,
        costs=cost_model, step_bars=args.step_bars,
        min_trades_for_selection=args.min_trades,
    )
    for window in wf_result.windows:
        logger.info(
            "検証期間[%d:%d] best_config=%s test_return=%.2f%% test_trades=%d",
            window.test_start_index, window.test_end_index, window.best_config,
            window.test_metrics.total_return_pct, window.test_metrics.num_trades,
        )

    logger.info("=== Out-of-sample 集計（全ウィンドウの検証期間トレードを合算） ===")
    if wf_result.skipped_windows:
        logger.warning(
            "%d個のウィンドウは、学習期間で%d回以上トレードした候補が無かったため見送りました。",
            wf_result.skipped_windows, args.min_trades,
        )
    summary = wf_result.combined_test_summary
    logger.info(
        "trades=%d win_rate=%.1f%% profit_factor=%.2f total_pnl=%.2f",
        summary.num_trades, summary.win_rate_pct, summary.profit_factor, summary.total_pnl,
    )
    total_commission = sum(t.commission for w in wf_result.windows for t in w.test_trades)
    logger.info("支払い手数料の合計=%.2f USD（上記の損益は控除後）", total_commission)
    if not args.report:
        return
    _emit_report(
        args, mode="walk-forward",
        inputs=[_fingerprint(symbol, df, args.csv)],
        parameters=_parameters(args, grid, cost_model),
        results={
            "combined_test_summary": asdict(summary),
            "total_commission": total_commission,
            "skipped_windows": wf_result.skipped_windows,
            "windows": [
                {
                    "test_start_index": w.test_start_index,
                    "test_end_index": w.test_end_index,
                    "best_config": asdict(w.best_config),
                    "test_metrics": _metrics_dict(w.test_metrics),
                }
                for w in wf_result.windows
            ],
        },
    )


if __name__ == "__main__":
    asyncio.run(main())
