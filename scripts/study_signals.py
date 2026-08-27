"""候補シグナルを、同じ基準（超過リターン）で一斉に測る（IBKR接続不要）。

    python -m scripts.study_signals --csv-dir bars/universe

**このスクリプトはシグナルに情報があるかだけを答える。** 口座でいくら増えるかは
`backtest/portfolio.py` と `scripts/measure_alpha.py` の担当である。順序は必ず
「情報があるか」→「口座で成立するか」。逆にすると、枠の取り合いや株数クランプの
偶然を、シグナルの良し悪しとして読むことになる。

**読み方の規律（`CLAUDE.md` の「ベンチマークを基準に測る」節と同じ）:**

- 判定は**重なり補正後の t値**で行い、2.0 未満は不採用とする
- **有意でも、資金額から決まる必要アルファを下回るものは実装しない。**
  $1,220・1建玉$244 なら往復手数料だけで 0.82% を持っていかれる
- **一覧から最良を拾って実装してはならない。** 8本並べて最良を選ぶ行為は、
  ウォークフォワードが検出しようとしている過剰最適化を検証の外側でやることに
  等しい。仮説として立てたものを確かめる道具として使うこと
"""

import argparse
import glob
import logging
import os
import sys
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from backtest.csv_source import load_bars_from_csv
from strategy.momentum import momentum_series
from backtest.signal_study import (
    SignalFn,
    add_cross_sectional_percentile,
    build_equal_weight_index,
    study_signal,
)

logger = logging.getLogger(__name__)

MIN_BARS: int = 400


def _ma(close: pd.Series, window: int) -> pd.Series:
    return close.rolling(window).mean()


def _uptrend(close: pd.Series, window: int = 200) -> pd.Series:
    return close > _ma(close, window)


# --- 候補シグナル -----------------------------------------------------------
# いずれも「その行までの情報だけ」で決まること。翌営業日に建てる前提は
# `signal_study` 側が保証する。

def signal_current_pullback(frame: pd.DataFrame) -> np.ndarray:
    """現行のライブ設定。MA30から-5%以下、かつ200日線の上。"""
    close = frame["close"].astype(float)
    ma = _ma(close, 30)
    return ((close <= ma * 0.95) & _uptrend(close)).fillna(False).to_numpy()


def signal_sigma_pullback(frame: pd.DataFrame) -> np.ndarray:
    """乖離を%ではなくσで測る（`CLAUDE.md` が未検証として挙げていた軸）。

    -5%が「異常」かは銘柄のボラティリティ次第である。日次SD 1.7%の大型株では
    3σ級だが、SD 12%のMRNAでは0.4日ぶんの動きにすぎない。
    """
    close = frame["close"].astype(float)
    ma = _ma(close, 30)
    dev = close / ma - 1.0
    # 乖離そのものの分布で測る。日次リターンのSDを期間換算すると、暴落後の
    # 低位株だけが閾値を超える（実測でn=124・平均+195%という別物になった）。
    sd = dev.rolling(252).std()
    return ((dev <= -2.0 * sd) & _uptrend(close)).fillna(False).to_numpy()


def signal_rsi2(frame: pd.DataFrame) -> np.ndarray:
    """RSI(2) < 5。短期平均回帰の古典（Connors）。"""
    close = frame["close"].astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(2).mean()
    loss = (-delta.clip(upper=0)).rolling(2).mean()
    rsi = 100.0 - 100.0 / (1.0 + gain / loss.replace(0, np.nan))
    return ((rsi < 5.0) & _uptrend(close)).fillna(False).to_numpy()


def signal_three_down_days(frame: pd.DataFrame) -> np.ndarray:
    """3日続落、かつ200日線の上。最も単純な短期平均回帰。"""
    close = frame["close"].astype(float)
    down = close.diff() < 0
    return (down & down.shift(1) & down.shift(2) & _uptrend(close)).fillna(False).to_numpy()


def signal_gap_down(frame: pd.DataFrame) -> np.ndarray:
    """前日終値から-3%以上のギャップダウン寄り。過剰反応の逆張り。"""
    if "open" not in frame.columns:
        return np.zeros(len(frame), dtype=bool)
    close = frame["close"].astype(float)
    open_ = frame["open"].astype(float)
    gap = open_ / close.shift(1) - 1.0
    return ((gap <= -0.03) & _uptrend(close)).fillna(False).to_numpy()


def signal_momentum_series(frame: pd.DataFrame) -> np.ndarray:
    """12ヶ月モメンタム（直近1ヶ月を除く）が上位。平均回帰の逆の仮説。

    押し目買いが効かないなら、逆方向を確かめる価値がある。学術的に最も
    頑健とされる株式アノマリーで、日足だけで再現できる。
    """
    close = frame["close"].astype(float)
    mom = close.shift(21) / close.shift(252) - 1.0
    # 銘柄内での相対判定にとどめる（横断ランクは別関数）。
    return ((mom > 0.30) & _uptrend(close)).fillna(False).to_numpy()


def signal_52w_high_breakout(frame: pd.DataFrame) -> np.ndarray:
    """52週高値の更新。モメンタムのもう1つの形。"""
    close = frame["close"].astype(float)
    high = close.rolling(252).max()
    return ((close >= high) & (close.shift(1) < high.shift(1))).fillna(False).to_numpy()


def signal_low_volatility(frame: pd.DataFrame) -> np.ndarray:
    """低ボラティリティ（日次SDが1%未満）で200日線の上。低ボラアノマリー。"""
    close = frame["close"].astype(float)
    sd = close.pct_change().rolling(60).std()
    return ((sd < 0.01) & _uptrend(close)).fillna(False).to_numpy()


# --- 横断ランクのモメンタム -----------------------------------------------------
# 上の `signal_momentum_series` は**絶対閾値**（12ヶ月で+30%以上）で測っている。
# モメンタムのアノマリーとして頑健なのは「その日の全銘柄を順位付けして上位を買う」
# 横断ランクの形であり、両者は別のシグナルである——絶対閾値は相場全体の水準で
# 意味が変わり、上げ相場では母集団の大半が該当し、下げ相場では誰も該当しない。
#
# 順位の列は `main()` が事前に付ける（1銘柄のDataFrameからは決まらないため）。

CS_MOMENTUM_COLUMN: str = "cs_momentum_rank"




def _cs_momentum_between(frame: pd.DataFrame, low: float, high: float) -> np.ndarray:
    rank = frame.get(CS_MOMENTUM_COLUMN)
    if rank is None:
        return np.zeros(len(frame), dtype=bool)
    return ((rank > low) & (rank <= high)).fillna(False).to_numpy()


def signal_cs_momentum_top_decile(frame: pd.DataFrame) -> np.ndarray:
    """横断モメンタム上位10%。**これが本命の仮説である。**"""
    return _cs_momentum_between(frame, 0.90, 1.01)


def signal_cs_momentum_top_quintile(frame: pd.DataFrame) -> np.ndarray:
    """横断モメンタム上位20%。**閾値を選ぶためではなく、頑健性を見るために置く。**

    上位10%と符号も大きさも揃っていなければ、上位10%の結果は閾値の偶然である。
    """
    return _cs_momentum_between(frame, 0.80, 1.01)


def signal_cs_momentum_bottom_decile(frame: pd.DataFrame) -> np.ndarray:
    """**対照群。** 横断モメンタム下位10%（＝負けている銘柄）。

    モメンタムが実在するなら、ここは上位10%と**逆符号**になるはずである。
    上下とも同じ向きに動くなら、それはモメンタムではなく母集団か期間の性質を
    拾っている。上位だけを見ていては区別がつかない。
    """
    return _cs_momentum_between(frame, -0.01, 0.10)


def signal_always_in(frame: pd.DataFrame) -> np.ndarray:
    """**対照群。** 常に建てる。銘柄を選んでいない。

    これが正の超過リターンを持つなら、それは母集団そのものがSPYを上回って
    いるという意味であり（等ウェイト・生存バイアス・中小型株の比重）、
    どのシグナルの超過リターンからもこの分を差し引いて読む必要がある。
    **これを置かないと、シグナルの情報量と母集団の性質を取り違える**——
    「ゼロ基準で測っていた」のと同じ形の誤りが、一段上で起きる。
    """
    return np.ones(len(frame), dtype=bool)


def signal_above_200ma(frame: pd.DataFrame) -> np.ndarray:
    """**対照群。** 200日線の上なら常に建てる。全シグナルに共通する条件だけ。"""
    close = frame["close"].astype(float)
    return _uptrend(close).fillna(False).to_numpy()


SIGNALS: Dict[str, SignalFn] = {
    "対照群: 常に建てる（銘柄を選ばない）": signal_always_in,
    "対照群: 200日線の上なら常に建てる": signal_above_200ma,
    "現行: MA30 -5% + 200日線上": signal_current_pullback,
    "σ正規化: MA30 -2σ + 200日線上": signal_sigma_pullback,
    "RSI(2) < 5 + 200日線上": signal_rsi2,
    "3日続落 + 200日線上": signal_three_down_days,
    "ギャップダウン -3% + 200日線上": signal_gap_down,
    "モメンタム 12-1 > 30%（絶対閾値）": signal_momentum_series,
    "横断モメンタム 上位10%": signal_cs_momentum_top_decile,
    "横断モメンタム 上位20%": signal_cs_momentum_top_quintile,
    "対照群: 横断モメンタム 下位10%": signal_cs_momentum_bottom_decile,
    "52週高値ブレイク": signal_52w_high_breakout,
    "低ボラ (日次SD<1%) + 200日線上": signal_low_volatility,
}



def _momentum_of(frame: pd.DataFrame) -> pd.Series:
    """バーのDataFrameから 12-1 モメンタムの列を作る。

    定義は `strategy/momentum.py` にだけ置く（測定とライブを同一にするため）。
    ここはその薄い受け口である。
    """
    return momentum_series(frame["close"])


def _load_directory(path: str) -> Dict[str, pd.DataFrame]:
    bars: Dict[str, pd.DataFrame] = {}
    for csv_path in sorted(glob.glob(os.path.join(path, "*.csv"))):
        symbol = os.path.basename(csv_path)[: -len(".csv")]
        try:
            frame = load_bars_from_csv(csv_path)
        except Exception as error:
            logger.info("%s を読めませんでした: %s", symbol, error)
            continue
        if len(frame) >= MIN_BARS:
            bars[symbol] = frame
    return bars


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv-dir", default="bars/universe")
    parser.add_argument(
        "--benchmark", default="equal-weight",
        help="ベンチマーク。'equal-weight' は母集団自身を等ウェイトで持った指数を"
             "合成する（**探索ではこちらを使う**——SPYを基準にすると、母集団が"
             "SPYを上回っているぶんを銘柄選択の力と取り違える）。CSVのパスも取れる。",
    )
    parser.add_argument("--horizons", type=int, nargs="+", default=[5, 10, 20])
    parser.add_argument(
        "--required-alpha", type=float, default=0.92,
        help="このコストを上回らないシグナルは実装しない。既定は $1,220・"
             "1建玉$244（リスク1%/損切り5%）での往復手数料+スリッページ。",
    )
    parser.add_argument("--only", nargs="*", default=None, help="名前の部分一致で絞る")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)

    bars = _load_directory(args.csv_dir)
    if not bars:
        print(f"{args.csv_dir} に検証できる銘柄がありません。", file=sys.stderr)
        return 1
    # 横断ランクは1銘柄のDataFrameからは決まらないので、シグナル評価の前に
    # 列として付けておく（`add_cross_sectional_percentile` の説明を参照）。
    bars = add_cross_sectional_percentile(bars, _momentum_of, CS_MOMENTUM_COLUMN)

    if args.benchmark == "equal-weight":
        benchmark = build_equal_weight_index(bars)
        benchmark_name = f"等ウェイト母集団（{len(bars)}銘柄）"
    else:
        benchmark = load_bars_from_csv(args.benchmark)
        benchmark_name = os.path.basename(args.benchmark)

    print(f"銘柄 {len(bars)}件 / ベンチマーク {benchmark_name}")
    print("エントリーは翌営業日の始値。値はすべて同期間ベンチマークに対する超過リターン。")
    print(f"必要アルファ {args.required_alpha:.2f}%/trade（これを下回るものは実装しない）\n")

    for name, fn in SIGNALS.items():
        if args.only and not any(key in name for key in args.only):
            continue
        study = study_signal(bars, benchmark, fn, name, horizons=args.horizons)
        print(study.describe(required_alpha_pct=args.required_alpha))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
