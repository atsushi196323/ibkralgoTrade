"""実現損益・勝率・平均R倍率を記録・集計するトレードジャーナル。

決済のたびにCSVへ1行追記し、ボットが実際に稼働した結果を後から検証
できるようにする。ヒストリカルデータでの検証を行う `backtest/` パッケージ
とは独立した、ライブ運用の実績記録である。

R倍率 = 実現損益(1株あたり) / エントリー時点の1株あたり想定リスク額
（= entry_price * stop_loss_pct / 100）。勝率だけでは「小さく何度も勝って
大きく1回負ける」ような損益構造を見落とすため、リスクに対してどれだけ
リターンを得られたかを示すR倍率も併せて記録する。
"""

import csv
import logging
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import List, Optional

from core.market_hours import US_EASTERN

logger = logging.getLogger(__name__)

DEFAULT_JOURNAL_PATH: str = "logs/trade_journal.csv"

_FIELDNAMES: List[str] = [
    "symbol", "entry_price", "exit_price", "quantity", "reason",
    "pnl", "pnl_pct", "r_multiple", "closed_at", "commission", "usd_jpy_rate", "entry_date",
]


@dataclass
class TradeRecord:
    symbol: str
    entry_price: float
    exit_price: float
    quantity: int
    reason: str
    pnl: float
    pnl_pct: float
    r_multiple: Optional[float]
    closed_at: str
    # 決済往復にかかった手数料（USD建て）。ドライラン中は実約定が無いため0.0。
    commission: float = 0.0
    # 決済時点のUSD/JPYレート。確定申告向けの円換算に使う（未記録の古いレコードはNone）。
    usd_jpy_rate: Optional[float] = None
    # 建玉日時(ISO8601)。確定申告の取得年月日として使う。
    # ブローカー同期で発見した未追跡ポジション由来の決済等、不明な場合はNone。
    entry_date: Optional[str] = None

    @property
    def net_pnl_usd(self) -> float:
        """手数料控除後の実現損益（USD）。"""
        return self.pnl - self.commission

    @property
    def net_pnl_jpy(self) -> Optional[float]:
        """手数料控除後の実現損益を、決済時点のUSD/JPYレートで円換算した額。

        確定申告で必要な「約定日ごとのレートでの円換算」作業を自動化するためのもの。
        usd_jpy_rateが記録されていない場合（実発注導入前の記録等）はNoneを返す。
        """
        if self.usd_jpy_rate is None:
            return None
        return self.net_pnl_usd * self.usd_jpy_rate


@dataclass
class TradeStats:
    num_trades: int
    win_rate_pct: float
    total_pnl: float
    profit_factor: float
    avg_r_multiple: Optional[float]
    # usd_jpy_rateが記録されている取引の、手数料控除後・円換算後の実現損益合計。
    # 1件もusd_jpy_rateを持たない場合はNone（確定申告の年間集計に使う想定）。
    total_pnl_jpy: Optional[float] = None


class TradeJournal:
    def __init__(self, file_path: str = DEFAULT_JOURNAL_PATH) -> None:
        self.file_path = file_path

    def record_trade(
        self,
        symbol: str,
        entry_price: float,
        exit_price: float,
        quantity: int,
        reason: str,
        pnl: float,
        pnl_pct: float,
        r_multiple: Optional[float],
        commission: float = 0.0,
        usd_jpy_rate: Optional[float] = None,
        entry_date: Optional[str] = None,
    ) -> TradeRecord:
        record = TradeRecord(
            symbol=symbol,
            entry_price=entry_price,
            exit_price=exit_price,
            quantity=quantity,
            reason=reason,
            pnl=pnl,
            pnl_pct=pnl_pct,
            r_multiple=r_multiple,
            closed_at=datetime.now(timezone.utc).isoformat(),
            commission=commission,
            usd_jpy_rate=usd_jpy_rate,
            entry_date=entry_date,
        )
        self._append_to_file(record)

        logger.info(
            "[%s] トレードジャーナルに記録しました: pnl=%.2f(%.2f%%) r=%s reason=%s "
            "commission=%.2f usd_jpy_rate=%s",
            symbol, pnl, pnl_pct,
            f"{r_multiple:.2f}" if r_multiple is not None else "N/A", reason,
            commission,
            f"{usd_jpy_rate:.4f}" if usd_jpy_rate is not None else "N/A",
        )
        return record

    def _append_to_file(self, record: TradeRecord) -> None:
        directory = os.path.dirname(self.file_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        file_exists = os.path.exists(self.file_path)
        with open(self.file_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_FIELDNAMES)
            if not file_exists:
                writer.writeheader()
            writer.writerow(asdict(record))

    def load_trades(self) -> List[TradeRecord]:
        if not os.path.exists(self.file_path):
            return []

        trades: List[TradeRecord] = []
        with open(self.file_path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                trades.append(
                    TradeRecord(
                        symbol=row["symbol"],
                        entry_price=float(row["entry_price"]),
                        exit_price=float(row["exit_price"]),
                        quantity=int(row["quantity"]),
                        reason=row["reason"],
                        pnl=float(row["pnl"]),
                        pnl_pct=float(row["pnl_pct"]),
                        r_multiple=float(row["r_multiple"]) if row.get("r_multiple") else None,
                        closed_at=row["closed_at"],
                        # commission/usd_jpy_rate/entry_date列が無い旧フォーマットのCSVでも読めるようにフォールバックする。
                        commission=float(row["commission"]) if row.get("commission") else 0.0,
                        usd_jpy_rate=float(row["usd_jpy_rate"]) if row.get("usd_jpy_rate") else None,
                        entry_date=row.get("entry_date") or None,
                    )
                )
        return trades

    def compute_stats(self) -> TradeStats:
        return summarize_trade_records(self.load_trades())

    def compute_daily_pnl(self, reference_date: Optional[date] = None) -> float:
        """指定日（省略時は米国東部時間の当日）に決済されたトレードの実現損益合計。

        日次損失サーキットブレーカー(main.MAX_DAILY_LOSS_PCT)の判定基準になるため、
        **手数料控除後**の純損益を返す。グロスで判定すると、往復手数料の分だけ
        実際の資金の減りを過小評価し、上限を超えてから発動することになる。

        取引日の区切りを市場時間（core.market_hours）と揃えるため、米国東部時間で判定する。
        """
        target_date = reference_date or datetime.now(US_EASTERN).date()
        return sum(
            trade.net_pnl_usd for trade in self.load_trades()
            if datetime.fromisoformat(trade.closed_at).astimezone(US_EASTERN).date() == target_date
        )


def summarize_trade_records(trades: List[TradeRecord]) -> TradeStats:
    """トレード記録を集計する。**すべて手数料控除後の純損益で計算する。**

    `TradeRecord.pnl` は手数料を引く前の値で、手数料は別列に持っている
    （`net_pnl_usd`）。グロスで集計すると3つが同時に楽観へ寄る:

    - `total_pnl` が往復手数料のぶんだけ多い
    - **グロスでプラス・ネットでマイナスのトレードが勝ちとして数えられる**
    - プロフィットファクターがその両方の影響を受ける

    小口座ではこの差が大きい。2026-08-05の実測（AMBQ 3株・約定代金$203）は
    グロス -13.18 に対し往復手数料 2.00 で、ネットは -15.19 だった。
    往復手数料が約定代金の1%を占めるため、**利幅の薄いトレードは符号ごと
    変わりうる**。

    加えて `backtest/` の成績はコスト込みで出している（`backtest/costs.py`）。
    ライブ側をグロスで集計すると、実運用と検証を比べる指標そのものが
    別物になり、実資金へ進む判断の材料にならない。
    """
    num_trades = len(trades)
    if num_trades == 0:
        return TradeStats(
            num_trades=0, win_rate_pct=0.0, total_pnl=0.0, profit_factor=0.0, avg_r_multiple=None,
        )

    wins = [t for t in trades if t.net_pnl_usd > 0]
    losses = [t for t in trades if t.net_pnl_usd <= 0]

    win_rate_pct = len(wins) / num_trades * 100.0
    total_pnl = sum(t.net_pnl_usd for t in trades)

    net_profit = sum(t.net_pnl_usd for t in wins)
    net_loss = -sum(t.net_pnl_usd for t in losses)
    if net_loss > 0:
        profit_factor = net_profit / net_loss
    else:
        profit_factor = float("inf") if net_profit > 0 else 0.0

    r_values = [t.r_multiple for t in trades if t.r_multiple is not None]
    avg_r_multiple = (sum(r_values) / len(r_values)) if r_values else None

    jpy_values = [t.net_pnl_jpy for t in trades if t.net_pnl_jpy is not None]
    total_pnl_jpy = sum(jpy_values) if jpy_values else None

    return TradeStats(
        num_trades=num_trades,
        win_rate_pct=win_rate_pct,
        total_pnl=total_pnl,
        profit_factor=profit_factor,
        avg_r_multiple=avg_r_multiple,
        total_pnl_jpy=total_pnl_jpy,
    )
