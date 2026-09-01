"""売買代金ランキングの急上昇（＝注目され始めた銘柄）の判定。

IBKRのスキャナー(MOST_ACTIVE_USD)が返す順位を日次で記録し、「昨日まで下位
だった銘柄が今日いきなり上位に来た」ものを拾う。順位だけを見るのは、
スキャナーが売買代金の**数値を返さない**ためであり、数値を得るには銘柄ごとに
日足を取り直すことになってペーシング枠を食い潰すため（docs/DECISIONS.md「6.1」）。

**このモジュールは純粋な判定ロジックである。** IBKRにもファイルにも依存しない
（strategy/ の責務。docs/DECISIONS.md「4. レイヤーの責務」）。取得は data/fundamentals.py、
履歴の永続化は data/rank_history.py が持つ。

**検証されていない軸である。** 押し目買いのエッジは42銘柄・10年の日足で確認した
ものだが、この選定はその母集団と無関係に銘柄を入れ替える。過去時点の売買代金
ランキングをIBKRから遡れないため、バックテストで検証する方法も無い
（PERと同じ制約）。閾値を成績を見て刻み直してはならない——それはウォーク
フォワードが検出しようとしている過剰最適化を、検証の外側で人間がやることに
等しい（docs/DECISIONS.md「市場フィルター」節と同じ理由）。
"""

from dataclasses import dataclass
from typing import Dict, List, Sequence


@dataclass(frozen=True)
class AttentionConfig:
    """急上昇の判定条件。

    Attributes:
        rank_ceiling: 今日この順位以内に入っていることを要求する。運用者の
            指定により1〜50位。
        min_rank_improvement: 基準順位からこの幅以上上がっていることを要求する。
            **検証で決めた値ではない。** 100銘柄をスキャンして「少しでも上がった」
            を採ると毎日20〜30銘柄が該当し、監視枠(20)に収まらないため置いている
            足切りである。
        history_window: 基準順位を取る際に遡る取引日数。中央値を使うので、
            1日だけの跳ねでは基準が動かない。
        absent_rank: ランキング外だった日に与える順位。スキャン件数(100)より
            大きい値にしておくと、「ランク外から入ってきた」銘柄の上昇幅が
            自動的に大きくなる。
    """

    rank_ceiling: int = 50
    min_rank_improvement: int = 20
    history_window: int = 10
    absent_rank: int = 101


#: 引数を省略したときの既定値。frozen なのでモジュール全体で共有してよい。
DEFAULT_ATTENTION_CONFIG = AttentionConfig()


def build_rank_map(symbols: Sequence[str]) -> Dict[str, int]:
    """スキャナーが返した順序を「銘柄 -> 順位(1始まり)」に変換する。

    重複するシンボル（同じ銘柄が複数の取引所スキャンに現れる場合）は、
    **上位の順位を採用する。** 2つの取引所のランキングを統合すると、
    片方で上位・片方で下位ということが起こりうるため。
    """
    ranks: Dict[str, int] = {}
    for position, symbol in enumerate(symbols, start=1):
        if symbol not in ranks or position < ranks[symbol]:
            ranks[symbol] = position
    return ranks


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def baseline_rank(
    symbol: str, history: Sequence[Dict[str, int]], config: AttentionConfig,
) -> float:
    """直近 history_window 日の順位の中央値を返す。

    履歴に現れない日は `absent_rank` として数える。平均ではなく中央値なのは、
    1日だけランク外に落ちた（あるいは1日だけ跳ねた）ことで基準が動くと、
    その翌日に「急上昇」を誤検知するため。
    """
    window = list(history)[-config.history_window:]
    if not window:
        return float(config.absent_rank)
    return _median([float(day.get(symbol, config.absent_rank)) for day in window])


def detect_rank_surges(
    today_ranks: Dict[str, int],
    history: Sequence[Dict[str, int]],
    config: AttentionConfig = DEFAULT_ATTENTION_CONFIG,
) -> List[str]:
    """急に上位へ来た銘柄を、上昇幅の大きい順に返す。

    条件は2つで、どちらも満たすものだけを返す:

    1. 今日の順位が `rank_ceiling` 以内であること（＝実際に注目されている）
    2. 基準順位（直近の中央値）から `min_rank_improvement` 以上上がっていること

    履歴が空の初日は、全銘柄の基準が `absent_rank` になるため上位が軒並み
    該当する。**呼び出し側は初日の結果をそのまま採用しないこと**（履歴が
    貯まるまでは「注目され始めた」ではなく単なる上位銘柄である）。
    `has_enough_history` で判定できる。
    """
    surges: List[tuple] = []
    for symbol, rank in today_ranks.items():
        if rank > config.rank_ceiling:
            continue
        improvement = baseline_rank(symbol, history, config) - rank
        if improvement >= config.min_rank_improvement:
            surges.append((improvement, rank, symbol))

    # 上昇幅の降順、同幅なら順位の昇順。監視枠が足りないときに上から採るため、
    # 並び順そのものが選定の一部になる。
    surges.sort(key=lambda item: (-item[0], item[1]))
    return [symbol for _, _, symbol in surges]


def has_enough_history(history: Sequence[Dict[str, int]], config: AttentionConfig) -> bool:
    """基準順位を意味のある形で計算できるだけの履歴があるか。

    中央値を取る以上、最低でもウィンドウの半分は欲しい。足りないうちに
    採用すると「昨日までランク外＝全銘柄が急上昇」になる。
    """
    return len(history) >= max(1, config.history_window // 2)
