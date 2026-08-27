"""横断ランクのモメンタム（12-1）の判定ロジック。

**IBKRにもI/Oにも依存させない。** バックテスト・一貫性テスト・ライブが
同じ関数を使うためで、ここが分かれると「測ったもの」と「動くもの」が
別物になる（CLAUDE.md「レイヤーの責務」）。

**押し目買いとの決定的な違いは、銘柄単体では判定できないことである。**
押し目は自分の移動平均からの乖離なので1銘柄で決まるが、モメンタムは
「その日の全銘柄の中で上位何%か」なので、母集団のスナップショットが要る。
ライブの監視ループは銘柄を独立に回すため、この判定だけは先に一括で行う。

**決済は時間で決まる。** 利確・損切りの水準ではなく、保有日数で降りて
次のリバランスで入れ替える。したがってブラケットの子注文は**利確・損切り
としてではなく、プロセスが落ちている間の保護として**置く（`main` 側）。

**満期の判定はここに置かない。** `main.ExitParams.max_hold_days` と
`core.market_hours.count_trading_days_between` が持つ——建玉日時から
営業日を数えるのは、純粋なシグナル判定ではなく建玉の状態管理だからである。
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

import pandas as pd

# 12ヶ月モメンタムの定義。直近1ヶ月(21営業日)を除くのは短期反転を避けるためで、
# 文献の標準的な定義に合わせてある。252営業日 ≒ 12ヶ月。
MOMENTUM_LOOKBACK_BARS: int = 252
MOMENTUM_SKIP_BARS: int = 21

# 順位を付けるのに最低限必要な銘柄数。母集団が薄い日に順位を付けると
# 「3銘柄中の1位」が「上位10%」になり、その期間だけシグナルが乱発される。
MIN_SYMBOLS_FOR_RANKING: int = 100


@dataclass(frozen=True)
class MomentumConfig:
    """横断モメンタムのパラメータ。**成績を見て刻み直してはならない。**

    `top_pct` と `hold_days` は一貫性テスト（`backtest/robustness.py`）を
    通した値であり、グリッド探索で選んだものではない。
    """

    top_pct: float = 0.10
    hold_days: int = 60
    slots: int = 5
    min_symbols: int = MIN_SYMBOLS_FOR_RANKING


def momentum_value(closes: pd.Series) -> Optional[float]:
    """直近バーにおける 12-1 モメンタム。本数が足りなければ None。

    **その日までの終値だけで決まる。** 未来のバーを参照しないことが、
    ライブとバックテストで同じ値になる条件である。
    """
    if closes is None or len(closes) < MOMENTUM_LOOKBACK_BARS + 1:
        return None
    values = closes.astype(float)
    old = float(values.iloc[-MOMENTUM_LOOKBACK_BARS - 1])
    recent = float(values.iloc[-MOMENTUM_SKIP_BARS - 1])
    if old <= 0 or recent <= 0:
        return None
    return recent / old - 1.0


def rank_percentiles(values: Dict[str, float]) -> Dict[str, float]:
    """銘柄ごとのモメンタムを、その日の中での百分位(0〜1]へ直す。

    順位付けは同じ日の銘柄間で行うので先読みにならない。**値そのものではなく
    順位を使うのは、絶対閾値（「12ヶ月で+30%以上」）が相場全体の水準で
    意味を変えるためである**——上げ相場では母集団の大半が該当し、下げ相場では
    誰も該当しない（2026-08-26に絶対閾値版を測って t=1.05 だった）。
    """
    if not values:
        return {}
    series = pd.Series(values, dtype=float).dropna()
    if series.empty:
        return {}
    return series.rank(pct=True).to_dict()


def select_targets(
    values: Dict[str, float], config: MomentumConfig = MomentumConfig(),
) -> List[str]:
    """その日に保有すべき銘柄を、モメンタムの高い順に `slots` 件返す。

    **母集団が `min_symbols` 未満なら空を返す。** 順位が意味を持たない日に
    建てると、「薄い母集団の中の1位」を「上位10%」として扱うことになる。
    ライブでは価格が取れない銘柄が出るので、この判定は毎日必要である。

    上位 `top_pct` の中から `slots` 件を採る。**上位decile全部を持てない
    ことは、期待値ではなくばらつきに効く**（2026-08-27の実測: 期待超過は
    枠数にほぼ依存せず、年ごとSDが枠2で40.3% / 枠20で12.0%）。
    """
    if len(values) < config.min_symbols:
        return []
    ranks = rank_percentiles(values)
    eligible = [
        symbol for symbol, rank in ranks.items() if rank > 1.0 - config.top_pct
    ]
    # 同じ順位付けの中で、モメンタムの高い順に採る。**ランダムや記載順に
    # しないのは、日によって選ぶ銘柄が変わると保有が無用に回転するため。**
    eligible.sort(key=lambda symbol: values[symbol], reverse=True)
    return eligible[: max(0, config.slots)]


def select_by_turnover(
    turnovers: Dict[str, float], size: int, min_turnover_usd: float = 0.0,
) -> List[str]:
    """直近の売買代金が大きい順に `size` 銘柄を返す（母集団の絞り込み）。

    **成績と無関係な軸で絞ること。** 売買代金は流動性の指標であり、
    2026-08-27の測定では上位100〜400のどこで切っても結論が変わらなかった。
    成績を見てサイズや銘柄を選ぶと、それ自体が過剰最適化になる。

    **売買代金は `終値 × 出来高` なので、値上がりした銘柄は価格の分だけ
    機械的に増える。** 上位N件を採る操作には、モメンタムと相関する選択が
    弱く混ざっている（未検証の懸念として `CLAUDE.md` に記録済み）。

    **効くのは上位に絞ることではなく、下限を切ることである**（2026-08-06の
    層別測定: 1–100位 PF 1.26 / 201–400位 1.30 / **401位以下 1.10**）。
    `min_turnover_usd` はその下限で、上位N件の枠に入っても流動性が細い銘柄を
    落とす。**スプレッドの差はこの測定に入っていないので、実際の下位はこの
    数字よりさらに悪い**——下限を切る根拠はより強く、上位に絞る根拠はより弱い。

    値が読めない銘柄は落とす。順位を付けられないものを残すと、
    「売買代金が最小」として扱うか「最大」として扱うかで結果が変わる。
    """
    ranked = [
        (symbol, value) for symbol, value in turnovers.items()
        if value is not None and value == value and value > min_turnover_usd
    ]
    ranked.sort(key=lambda row: row[1], reverse=True)
    return [symbol for symbol, _ in ranked[: max(0, size)]] if size > 0 else [
        symbol for symbol, _ in ranked
    ]


def momentum_series(closes: pd.Series) -> pd.Series:
    """各バー時点の 12-1 モメンタムを列で返す（バックテスト・測定用）。

    `momentum_value` は直近1バーの値を返すライブ用で、こちらは全バーぶんを
    返す測定用である。**定義は必ずこの2つだけに置く**——2026-08-27まで
    `scripts/` の3ファイルに同じ2行が書き写されており、片方を直すと
    測っているものとライブで動くものが別々に育つ状態だった。
    """
    values = closes.astype(float)
    return values.shift(MOMENTUM_SKIP_BARS) / values.shift(MOMENTUM_LOOKBACK_BARS) - 1.0


def recent_turnover(closes: pd.Series, volumes: pd.Series, window: int) -> float:
    """直近の売買代金（終値 × 出来高）の中央値。読めなければ NaN。

    **中央値にするのは、1日の異常出来高で母集団が入れ替わるのを避けるため**
    （`strategy.attention` が前日比ではなく中央値を使うのと同じ理由）。
    """
    if closes is None or volumes is None or len(closes) == 0:
        return float("nan")
    turnover = (closes.astype(float) * volumes.astype(float)).tail(window)
    return float(turnover.median()) if not turnover.empty else float("nan")
