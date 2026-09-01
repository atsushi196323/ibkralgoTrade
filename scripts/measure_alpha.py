"""戦略を「市場を持っていた場合」と比べる（IBKR接続不要）。

**プロフィットファクターが1を超えていることは、エッジの証拠にならない。**
既存の指標はゼロを基準にしているため、上げ相場では保有期間中の市場リターンが
そのままエッジとして計上される。採否はここが出す超過リターンで判断すること。

    python -m scripts.measure_alpha --csv-dir bars --benchmark bars/index/SPY.csv \
        --initial-equity 13300 --slots 3

出力は3段:

1. 口座水準の成績（`backtest/portfolio.py`。資金を共有した実際のCAGRと最大DD）
2. 同じ期間ベンチマークを買って持っていた場合
3. 1トレードあたりの超過リターンと t値（`backtest/benchmark.py`）
"""

import argparse
import glob
import logging
import os
import sys
from typing import Dict, List, Optional

import pandas as pd

from backtest.benchmark import compute_benchmark_alpha
from backtest.costs import CostModel
from backtest.csv_source import load_bars_from_csv
from backtest.portfolio import PortfolioConfig, PortfolioResult, run_portfolio_backtest

logger = logging.getLogger(__name__)

# ウォークフォワードが1ウィンドウも作れない銘柄は、検証の母集団に入れない。
MIN_BARS: int = 400


def _load_directory(path: str) -> Dict[str, pd.DataFrame]:
    bars: Dict[str, pd.DataFrame] = {}
    skipped: List[str] = []
    for csv_path in sorted(glob.glob(os.path.join(path, "*.csv"))):
        symbol = os.path.basename(csv_path)[: -len(".csv")]
        try:
            frame = load_bars_from_csv(csv_path)
        except Exception as error:  # 1銘柄の欠損で母集団全体を落とさない。
            skipped.append(f"{symbol}({error})")
            continue
        if len(frame) < MIN_BARS:
            skipped.append(f"{symbol}({len(frame)}本)")
            continue
        bars[symbol] = frame
    if skipped:
        logger.info("除外した銘柄 %d件: %s", len(skipped), ", ".join(skipped[:10]))
    return bars


def _benchmark_buy_and_hold(bars: pd.DataFrame, equity: float) -> pd.Series:
    closes = bars["close"].astype(float).reset_index(drop=True)
    return closes / closes.iloc[0] * equity


def _max_drawdown_pct(curve: pd.Series) -> float:
    peak = float("-inf")
    worst = 0.0
    for value in curve:
        peak = max(peak, float(value))
        if peak > 0:
            worst = max(worst, (peak - float(value)) / peak * 100.0)
    return worst


def _cagr_pct(curve: pd.Series, bars_per_year: int = 252) -> float:
    years = len(curve) / bars_per_year
    if years <= 0 or curve.iloc[0] <= 0 or curve.iloc[-1] <= 0:
        return 0.0
    return ((curve.iloc[-1] / curve.iloc[0]) ** (1.0 / years) - 1.0) * 100.0


def _report(result: PortfolioResult, benchmark: pd.DataFrame, equity: float) -> None:
    hold = _benchmark_buy_and_hold(benchmark, equity)
    strategy_cagr = result.cagr_pct()
    strategy_dd = result.max_drawdown_pct()
    hold_cagr = _cagr_pct(hold)
    hold_dd = _max_drawdown_pct(hold)

    print("\n=== 口座水準（資金を共有した実際の成績） ===")
    print(f"  トレード数 {len(result.trades)}  平均稼働率 {result.average_exposure_pct:.0f}%")
    print(f"  最終資金 {result.final_equity:,.0f}（初期 {equity:,.0f}）")
    print(f"  CAGR {strategy_cagr:+.2f}%   最大DD {strategy_dd:.1f}%"
          f"   リターン/DD {strategy_cagr / strategy_dd if strategy_dd else 0:.2f}")

    print("\n=== ベンチマークを買って持っていた場合 ===")
    print(f"  CAGR {hold_cagr:+.2f}%   最大DD {hold_dd:.1f}%"
          f"   リターン/DD {hold_cagr / hold_dd if hold_dd else 0:.2f}")

    print("\n=== 1トレードあたりの超過リターン ===")
    alpha = compute_benchmark_alpha(result.trades, benchmark)
    print("  " + alpha.describe().replace("\n", "\n  "))

    print("\n=== 判定 ===")
    if alpha.n < 30:
        print("  トレード数が少なすぎて判定できません。")
    elif alpha.effective_t_stat >= 2.0:
        print(f"  超過リターンは有意です（重なり補正後 t={alpha.effective_t_stat:+.2f}）。")
    elif alpha.mean_excess_pct <= 0:
        print(f"  超過リターンはマイナスです（{alpha.mean_excess_pct:+.3f}%/trade）。"
              "\n  この戦略は、同じ資金でベンチマークを持つより悪い結果になります。")
    else:
        print(f"  超過リターンはゼロと区別がつきません"
              f"（{alpha.mean_excess_pct:+.3f}%/trade, 重なり補正後 t={alpha.effective_t_stat:+.2f}）。"
              "\n  プラスに見えるのは保有期間中の市場リターンです。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv-dir", default="bars", help="銘柄CSVのディレクトリ")
    parser.add_argument("--benchmark", default="bars/index/SPY.csv", help="ベンチマークのCSV")
    parser.add_argument("--initial-equity", type=float, default=1_220.0,
                        help="実際に運用する資金額で回すこと（既定は現行資金）")
    parser.add_argument("--slots", type=int, default=2, help="同時保有ポジション数の上限")
    parser.add_argument(
        "--risk-pct", type=float, default=1.0,
        help="1トレードのリスク（資金比）。**小口座ではこれが最大の設計変数である**"
             "——建玉金額 = 資金 × (リスク%% / 損切り%%) なので、1%%は $1,220 で $244 の"
             "建玉になり、往復$2.00が約定代金の0.82%%を占める。必要な超過リターンが"
             "そのぶん高くなる。",
    )
    parser.add_argument("--watchlist-size", type=int, default=24,
                        help="監視銘柄数の上限（0で無制限）")
    parser.add_argument("--min-commission", type=float, default=CostModel.min_commission_per_order,
                        help="1注文あたりの最低手数料（既定は実測値）")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)

    bars = _load_directory(args.csv_dir)
    if not bars:
        print(f"{args.csv_dir} に検証できる銘柄がありません。", file=sys.stderr)
        return 1
    benchmark = load_bars_from_csv(args.benchmark)

    config = PortfolioConfig(
        initial_equity=args.initial_equity,
        max_concurrent_positions=args.slots,
        max_watchlist_size=args.watchlist_size or None,
        risk_per_trade_pct=args.risk_pct,
        costs=CostModel(min_commission_per_order=args.min_commission),
    )
    notional = args.initial_equity * (args.risk_pct / config.stop_loss_pct)
    print(f"銘柄 {len(bars)}件 / 資金 {args.initial_equity:,.0f} / 枠 {args.slots}"
          f" / 監視上限 {args.watchlist_size or '無制限'} / リスク {args.risk_pct}%")
    print(f"  1建玉 {notional:,.0f}ドル → 往復手数料は約定代金の "
          f"{args.min_commission * 2 / notional * 100:.2f}%"
          f"（超過リターンはこれを上回らなければ意味が無い）")
    result = run_portfolio_backtest(bars, config=config)
    _report(result, benchmark, args.initial_equity)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
