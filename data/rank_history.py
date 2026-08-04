"""売買代金ランキングの日次履歴の永続化。

「急に上位へ来た」は昨日までの順位が分からないと判定できないので、
日次のランキングをファイルへ残す。**過去の順位はIBKRから遡って取得できない**
（PERと同じくpoint-in-timeデータが無い）ため、記録を落とすとその分だけ
判定不能な日が発生する。

IBKRには触らないが、取得したランキングの保管なので data/ に置いている。
判定ロジックは strategy/attention.py（純粋関数）にある。
"""

import json
import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_RANK_HISTORY_PATH: str = "logs/turnover_ranks.json"

# 保持する取引日数。AttentionConfig.history_window より十分長くしておく。
# 際限なく貯めても判定には使わないうえ、ファイルが膨らむ。
MAX_HISTORY_DAYS: int = 60


@dataclass
class RankHistoryStore:
    """日次ランキングの追記と読み出し。

    同じ取引日に複数回呼ばれた場合は**最後の結果で上書きする**（追記しない）。
    スクリーニングの再試行（main.SCREENING_RETRY_INTERVAL_SECONDS）で1日に
    複数回スキャンが走りうるため、重複させると中央値が同日の値に引きずられる。
    """

    path: str = DEFAULT_RANK_HISTORY_PATH

    def load(self) -> List[Dict[str, int]]:
        """古い順に並んだ日次ランキングを返す。読めなければ空を返す。"""
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            days = payload["days"]
            return [dict(day["ranks"]) for day in days]
        except (OSError, ValueError, KeyError, TypeError):
            # 履歴が壊れていても稼働は止めない。判定が「履歴なし」に縮退するだけ。
            logger.exception(
                "売買代金ランキングの履歴が読めませんでした: %s。履歴なしとして続行します。",
                self.path,
            )
            return []

    def load_days(self) -> List[dict]:
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return list(json.load(f)["days"])
        except (OSError, ValueError, KeyError, TypeError):
            return []

    def append(self, trading_day: str, ranks: Dict[str, int]) -> List[Dict[str, int]]:
        """当日のランキングを記録し、更新後の履歴（古い順）を返す。"""
        days = [day for day in self.load_days() if day.get("date") != trading_day]
        days.append({"date": trading_day, "ranks": dict(ranks)})
        days = days[-MAX_HISTORY_DAYS:]

        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        temp_path = f"{self.path}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump({"days": days}, f, ensure_ascii=False)
            os.replace(temp_path, self.path)
        except OSError:
            # 保存に失敗しても当日の判定は続けられる（戻り値は正しい）。
            logger.exception("売買代金ランキングの履歴を保存できませんでした: %s", self.path)

        return [dict(day["ranks"]) for day in days]

    def load_attention_symbols(self) -> List[str]:
        """前回までに組み入れた注目銘柄を返す。

        毎日ゼロから組み直すと、急上昇の翌日にランキングが落ち着いた時点で
        監視から外れてしまい、押し目が出るまで持ち続けられない。
        """
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return list(json.load(f).get("attention_symbols") or [])
        except (OSError, ValueError, TypeError):
            return []

    def save_attention_symbols(self, symbols: List[str]) -> None:
        payload = {"days": self.load_days(), "attention_symbols": list(symbols)}
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temp_path = f"{self.path}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(temp_path, self.path)
        except OSError:
            logger.exception("注目銘柄リストを保存できませんでした: %s", self.path)


def resolve_store(path: Optional[str] = None) -> RankHistoryStore:
    return RankHistoryStore(path or DEFAULT_RANK_HISTORY_PATH)
