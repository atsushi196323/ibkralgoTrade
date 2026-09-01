"""シグナル単体に情報があるかを、ポートフォリオ機構から切り離して測る。

`backtest/portfolio.py` は口座で何が起きるかを測る道具で、シグナルの良し悪しを
測るには**交絡が多すぎる**——枠の取り合い(記載順)・同時保有上限・クールダウン・
株数クランプが、シグナルの情報量とは無関係に成績を動かす。実際、同じシグナルで
リスク%を振っただけで超過リターンが -1.30% 〜 -0.14% まで動いた（2026-08-26）。

そこで探索段階では**イベントスタディ**を使う: シグナルが立った全事象について、
その後N営業日の騰落率から**同じ期間のベンチマークの騰落率を引く**。残った値が
シグナルの持つ情報である。実装した戦略の成績ではなく、シグナルの情報量を測る。

**不変条件は4つで、いずれも破ると「情報がある」ように見えてしまう。**

- **エントリーは翌営業日の始値**（無ければ翌営業日の終値）。シグナルを出した
  バーの終値で建てると、その終値自体が判定に入っているためルックアヘッドになる
- **超過リターンで測る。** 生の騰落率は上げ相場では常にプラスに出る
  （「ベンチマークを基準に測る」節）
- **長い保有では対数と中央値も見る。** 複数期間の騰落率の算術平均は**右の裾に
  支配される**。2026-08-27の実測では、横断モメンタム下位10%（＝最も負けている
  銘柄）の60日超過が算術平均で **+31%** になった——勝率は42%しかなく、少数の
  銘柄が数倍になったことで平均が決まっている。同じ母集団を対数で測ると
  **-5.75%** で符号が逆になる。**算術平均だけを見ると、この裾を「銘柄選択の
  情報」として読む。** 保有が20日を超えるシグナルでは `log_t_stat` を採否に使う
- **t値は日付でクラスタリングして出す。** 押し目は市場全体の下げで一斉に立つ
  ため、同じ日のイベントは独立ではない。イベント単位で検定すると、627銘柄が
  同じ日に反応しただけで t値が2桁になる。日ごとの平均を取り、さらに保有日数
  ぶんの重なりで標本数を割る（20日保有なら、2500営業日は125個の独立な観測に
  しかならない）
"""

import logging
import math
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# シグナル関数: 日足のDataFrameを受け取り、その行でシグナルが立つかの真偽値配列を返す。
# **その行までの情報だけを使うこと**（`shift` を忘れると未来を見る）。
SignalFn = Callable[[pd.DataFrame], np.ndarray]


@dataclass
class HorizonResult:
    """1つの保有日数における集計。"""

    horizon_days: int
    n: int
    mean_excess_pct: float
    sd_pct: float
    win_rate_pct: float
    # 日ごとの平均超過リターン（イベントを発生日でまとめたもの）。
    daily_mean_pct: float
    daily_sd_pct: float
    n_days: int
    # 中央値と対数平均。**長い保有では算術平均より先にこちらを見ること**
    # （モジュールのdocstringを参照）。裾の1件で平均が動いていないかの検算になる。
    median_excess_pct: float = 0.0
    log_daily_mean_pct: float = 0.0
    log_daily_sd_pct: float = 0.0

    @property
    def naive_t_stat(self) -> float:
        """イベントを独立として扱った t値。**読んではならない。**

        627銘柄が同じ日に反応しただけで2桁になる。並べているのは、
        クラスタリングの前後でどれだけ違うかを見せるためである。
        """
        if self.n < 2 or self.sd_pct <= 0:
            return 0.0
        return self.mean_excess_pct / (self.sd_pct / math.sqrt(self.n))

    @property
    def effective_n(self) -> float:
        """独立な観測の数。保有日数ぶん重なるので、日数をそれで割る。"""
        if self.n_days <= 0:
            return 0.0
        return self.n_days / max(1, self.horizon_days)

    @property
    def t_stat(self) -> float:
        """日付クラスタ後の t値。**採否はこれで判断する。**"""
        effective_n = self.effective_n
        if effective_n < 2 or self.daily_sd_pct <= 0:
            return 0.0
        return self.daily_mean_pct / (self.daily_sd_pct / math.sqrt(effective_n))


    @property
    def log_t_stat(self) -> float:
        """対数超過リターンの、日付クラスタ後の t値。

        **保有が20日を超えるシグナルではこちらを採否に使う。** 算術平均側は
        右の裾で符号ごと変わりうる（モジュールのdocstring）。
        """
        effective_n = self.effective_n
        if effective_n < 2 or self.log_daily_sd_pct <= 0:
            return 0.0
        return self.log_daily_mean_pct / (self.log_daily_sd_pct / math.sqrt(effective_n))


@dataclass
class SignalStudy:
    name: str
    horizons: List[HorizonResult]

    def best(self) -> Optional[HorizonResult]:
        """最も t値の高い保有日数。**これを見て保有日数を決めてはならない**——
        複数の保有日数から最良を拾う行為はそれ自体が過剰最適化である。
        シグナルに情報があるかの一次判定にのみ使う。"""
        if not self.horizons:
            return None
        return max(self.horizons, key=lambda h: h.t_stat)

    def describe(self, required_alpha_pct: float = 0.0) -> str:
        rows = [f"{self.name}"]
        for h in self.horizons:
            verdict = ""
            if h.n >= 100 and h.t_stat >= 2.0:
                verdict = " ★有意"
                if required_alpha_pct and h.mean_excess_pct < required_alpha_pct:
                    verdict = f" ★有意だがコスト({required_alpha_pct:.2f}%)割れ"
            rows.append(
                f"   {h.horizon_days:>3}日: n={h.n:>7} 超過={h.mean_excess_pct:+.3f}% "
                f"勝率={h.win_rate_pct:.1f}% "
                f"t={h.t_stat:+5.2f} | 中央値={h.median_excess_pct:+6.2f}% "
                f"対数={h.log_daily_mean_pct:+6.2f}% 対数t={h.log_t_stat:+5.2f} "
                f"実効{h.effective_n:.0f}観測{verdict}"
            )
        return "\n".join(rows)


def _forward_excess(
    frame: pd.DataFrame,
    benchmark_close: pd.Series,
    signal: np.ndarray,
    horizons: Sequence[int],
) -> Dict[int, List[tuple]]:
    """1銘柄について、シグナル各点の (発生日, 超過リターン, 対数超過) を保有日数ごとに返す。"""
    days = pd.to_datetime(frame["date"]).dt.normalize()
    # エントリーは翌営業日。始値が無いCSVでも動くよう終値で代用する。
    entry_col = "open" if "open" in frame.columns else "close"
    entry = frame[entry_col].to_numpy(dtype=float)
    close = frame["close"].to_numpy(dtype=float)

    # ベンチマークを銘柄の日付軸へ突き合わせる。**位置で揃えてはならない**
    # （欠損が1行あるだけで日付がずれ、未来の値を見ることになる）。
    bench = benchmark_close.reindex(days.to_numpy())
    bench_values = bench.to_numpy(dtype=float)

    out: Dict[int, List[tuple]] = {h: [] for h in horizons}
    n = len(frame)
    indices = np.flatnonzero(signal)
    for i in indices:
        start = i + 1  # 翌営業日に建てる
        if start >= n:
            continue
        p0, b0 = entry[start], bench_values[start]
        if not (p0 > 0) or not (b0 > 0):
            continue
        for h in horizons:
            j = start + h
            if j >= n:
                continue
            p1, b1 = close[j], bench_values[j]
            if not (p1 > 0) or not (b1 > 0):
                continue
            excess = (p1 / p0 - 1.0) * 100.0 - (b1 / b0 - 1.0) * 100.0
            # 対数の超過も併せて返す。算術平均は長い保有で右の裾に支配される
            # （モジュールのdocstring）。ここで両方拾っておかないと、
            # 集計側で裾の影響を検算できない。
            log_excess = (math.log(p1 / p0) - math.log(b1 / b0)) * 100.0
            out[h].append((days.iloc[start], excess, log_excess))
    return out


def build_equal_weight_index(
    bars_by_symbol: Dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """母集団そのものを等ウェイトで持った場合の指数を合成する。

    **SPYをベンチマークにすると、母集団の性質を銘柄選択の力と取り違える。**
    実測（2026-08-26、627銘柄・10年）では「銘柄を選ばず常に建てる」だけで
    20日 +0.693%（t=2.02）の超過が出た——中小型株の比重・等ウェイト・
    生存バイアスの合計であって、どのシグナルの手柄でもない。

    したがってシグナルの探索では、この等ウェイト指数を基準にすること。
    残る超過リターンが**銘柄を選んだことの寄与**である。
    """
    daily_returns: Dict[object, List[float]] = {}
    for frame in bars_by_symbol.values():
        if "date" not in frame.columns or len(frame) < 2:
            continue
        days = pd.to_datetime(frame["date"]).dt.normalize()
        returns = frame["close"].astype(float).pct_change()
        for day, value in zip(days.iloc[1:], returns.iloc[1:], strict=True):
            if pd.notna(value):
                daily_returns.setdefault(day, []).append(float(value))

    if not daily_returns:
        raise ValueError("等ウェイト指数を合成できる銘柄がありません。")

    days = sorted(daily_returns)
    level = 100.0
    closes = []
    for day in days:
        values = daily_returns[day]
        level *= 1.0 + sum(values) / len(values)
        closes.append(level)
    return pd.DataFrame({"date": days, "close": closes})


def add_cross_sectional_percentile(
    bars_by_symbol: Dict[str, pd.DataFrame],
    value_fn: "Callable[[pd.DataFrame], pd.Series]",
    column: str,
    min_symbols_per_day: int = 20,
) -> Dict[str, pd.DataFrame]:
    """各日の全銘柄を横断で順位付けし、百分位(0〜1)を列として書き戻す。

    **これが無いと、横断ランクのシグナルを測れない。** `SignalFn` は1銘柄の
    DataFrameしか受け取らないので、「その日の全銘柄の中で上位何%か」は
    銘柄単体からは決まらない。モメンタムのように**分散されたポートフォリオ
    として現れる**アノマリーは、絶対閾値（「12ヶ月で+30%以上」）では
    別物になる——閾値は相場全体の水準で意味が変わり、上げ相場では母集団の
    大半が該当し、下げ相場では誰も該当しなくなる。

    **突き合わせは必ず日付で行う**（`backtest/market_reference.py` と同じ理由）。
    位置で揃えると、銘柄側に1行でも欠損があった瞬間に日付がずれ、**それは
    例外にならず、未来の順位を見て判定する形で成績を良く見せる**。

    `value_fn` はその行までの情報だけで決まる値を返すこと（`shift` を忘れると
    未来を見る）。順位付け自体は同じ日の銘柄間で行うので先読みにならない。

    **その日に値を持つ銘柄が `min_symbols_per_day` 未満なら順位を付けない。**
    データの先頭では算出できる銘柄が数件しかなく、「3銘柄中の1位」を
    「上位10%」として扱うと、母集団が薄い期間だけシグナルが乱発される。

    **生存バイアスはこの関数では解けない。** 順位は「いま手元にあるCSVの中での
    順位」であって、当時実在した全銘柄の中での順位ではない。
    """
    values: Dict[str, pd.Series] = {}
    for symbol, frame in bars_by_symbol.items():
        if "date" not in frame.columns or frame.empty:
            continue
        days = pd.to_datetime(frame["date"]).dt.normalize()
        series = pd.Series(np.asarray(value_fn(frame), dtype=float).ravel(), index=days)
        values[symbol] = series[~series.index.duplicated(keep="last")]

    if not values:
        raise ValueError("横断ランクを付けられる銘柄がありません。")

    wide = pd.DataFrame(values).sort_index()
    ranks = wide.rank(axis=1, pct=True)
    thin_days = wide.notna().sum(axis=1) < min_symbols_per_day
    ranks.loc[thin_days, :] = np.nan

    out: Dict[str, pd.DataFrame] = {}
    for symbol, frame in bars_by_symbol.items():
        frame = frame.copy()
        if symbol in ranks.columns and "date" in frame.columns:
            days = pd.to_datetime(frame["date"]).dt.normalize()
            frame[column] = ranks[symbol].reindex(days).to_numpy()
        else:
            frame[column] = np.nan
        out[symbol] = frame
    return out


def study_signal(
    bars_by_symbol: Dict[str, pd.DataFrame],
    benchmark_bars: pd.DataFrame,
    signal_fn: SignalFn,
    name: str,
    horizons: Sequence[int] = (5, 10, 20),
) -> SignalStudy:
    """全銘柄でシグナルを評価し、保有日数ごとの超過リターンを集計する。"""
    bench = benchmark_bars.copy()
    bench["day"] = pd.to_datetime(bench["date"]).dt.normalize()
    benchmark_close = bench.set_index("day")["close"].astype(float)
    benchmark_close = benchmark_close[~benchmark_close.index.duplicated(keep="last")]

    collected: Dict[int, List[tuple]] = {h: [] for h in horizons}
    for symbol, frame in bars_by_symbol.items():
        if "date" not in frame.columns or frame.empty:
            continue
        try:
            signal = np.asarray(signal_fn(frame), dtype=bool)
        except Exception as error:  # 1銘柄の欠損で母集団全体を落とさない。
            logger.warning("%s のシグナル計算に失敗しました: %s", symbol, error)
            continue
        if signal.shape[0] != len(frame):
            raise ValueError(f"{name}: シグナルの長さがバー数と一致しません。")
        for h, events in _forward_excess(frame, benchmark_close, signal, horizons).items():
            collected[h].extend(events)

    results = []
    for h in horizons:
        events = collected[h]
        if not events:
            results.append(HorizonResult(h, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0))
            continue
        index = pd.DatetimeIndex([e[0] for e in events])
        values = np.array([e[1] for e in events], dtype=float)
        log_values = np.array([e[2] for e in events], dtype=float)
        daily_means = pd.Series(values, index=index).groupby(level=0).mean()
        log_daily_means = pd.Series(log_values, index=index).groupby(level=0).mean()
        results.append(
            HorizonResult(
                horizon_days=h,
                n=len(values),
                mean_excess_pct=float(values.mean()),
                sd_pct=float(values.std()),
                win_rate_pct=float((values > 0).mean() * 100.0),
                daily_mean_pct=float(daily_means.mean()),
                daily_sd_pct=float(daily_means.std()),
                n_days=int(len(daily_means)),
                median_excess_pct=float(np.median(values)),
                log_daily_mean_pct=float(log_daily_means.mean()),
                log_daily_sd_pct=float(log_daily_means.std()),
            )
        )
    return SignalStudy(name=name, horizons=results)
