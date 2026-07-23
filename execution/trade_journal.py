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
    "pnl", "pnl_pct", "r_multiple", "closed_at",
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


@dataclass
class TradeStats:
    num_trades: int
    win_rate_pct: float
    total_pnl: float
    profit_factor: float
    avg_r_multiple: Optional[float]


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
        )
        self._append_to_file(record)

        logger.info(
            "[%s] トレードジャーナルに記録しました: pnl=%.2f(%.2f%%) r=%s reason=%s",
            symbol, pnl, pnl_pct,
            f"{r_multiple:.2f}" if r_multiple is not None else "N/A", reason,
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
                    )
                )
        return trades

    def compute_stats(self) -> TradeStats:
        return summarize_trade_records(self.load_trades())

    def compute_daily_pnl(self, reference_date: Optional[date] = None) -> float:
        """指定日（省略時は米国東部時間の当日）に決済されたトレードの実現損益合計。

        取引日の区切りを市場時間（core.market_hours）と揃えるため、米国東部時間で判定する。
        """
        target_date = reference_date or datetime.now(US_EASTERN).date()
        return sum(
            trade.pnl for trade in self.load_trades()
            if datetime.fromisoformat(trade.closed_at).astimezone(US_EASTERN).date() == target_date
        )


def summarize_trade_records(trades: List[TradeRecord]) -> TradeStats:
    num_trades = len(trades)
    if num_trades == 0:
        return TradeStats(
            num_trades=0, win_rate_pct=0.0, total_pnl=0.0, profit_factor=0.0, avg_r_multiple=None,
        )

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]

    win_rate_pct = len(wins) / num_trades * 100.0
    total_pnl = sum(t.pnl for t in trades)

    gross_profit = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = float("inf") if gross_profit > 0 else 0.0

    r_values = [t.r_multiple for t in trades if t.r_multiple is not None]
    avg_r_multiple = (sum(r_values) / len(r_values)) if r_values else None

    return TradeStats(
        num_trades=num_trades,
        win_rate_pct=win_rate_pct,
        total_pnl=total_pnl,
        profit_factor=profit_factor,
        avg_r_multiple=avg_r_multiple,
    )
