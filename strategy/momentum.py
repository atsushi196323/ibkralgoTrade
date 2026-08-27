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
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

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


def is_rebalance_due(bars_held: int, config: MomentumConfig = MomentumConfig()) -> bool:
    """保有日数が満期に達したか。

    **モメンタムの決済は時間で決まる。** 利確・損切りの水準で降りるのでは
    ないので、この判定が唯一の通常の出口である。
    """
    return bars_held >= config.hold_days


def targets_to_trade(
    held: Sequence[str], targets: Sequence[str], due: Sequence[str],
) -> Dict[str, List[str]]:
    """保有中・目標・満期から、売る銘柄と買う銘柄を決める。

    **満期に達していない保有は、目標から外れていても売らない。** 毎日
    順位を付け直すと保有銘柄は日々出入りするので、目標から外れるたびに
    売っていると保有期間60日という前提が崩れ、回転だけが上がる
    （往復$2.00の固定手数料は回転に比例する）。

    買うのは「目標に入っていて、まだ持っていない銘柄」だけである。
    """
    held_set, due_set = set(held), set(due)
    to_sell = [symbol for symbol in held if symbol in due_set]
    remaining = [symbol for symbol in held if symbol not in due_set]
    to_buy = [
        symbol for symbol in targets
        if symbol not in held_set or symbol in due_set
    ]
    return {"sell": to_sell, "buy": to_buy, "hold": remaining}
