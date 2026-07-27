"""トレードジャーナルから、確定申告用のCSVを出力するCLI。

`logs/trade_journal.csv` に蓄積された決済記録を読み、税理士へ渡せる形
（取得年月日・譲渡年月日・数量・取得価額・譲渡価額・手数料・適用為替レート・
円換算後の実現損益）のCSVに変換する。

実行方法:
    python -m scripts.export_tax_report                      # 前年分（確定申告の対象年）
    python -m scripts.export_tax_report --year 2026
    python -m scripts.export_tax_report --all-years
    python -m scripts.export_tax_report --year 2026 --output ~/Desktop/tax2026.csv

注意:
- 出力される usd_jpy_rate は決済時点にIBKRから取得したライブレートであり、
  国税庁が想定するTTB/TTM(その日の公表相場)とは厳密には一致しない可能性がある。
  税理士へ渡す際は「参考レートであり、必要なら公表レートで再計算してほしい」旨を
  申し添えること。
- 取得年月日(entry_date)が空欄の行は、ブローカー側で発見した未追跡ポジション
  由来の決済で、実際の建玉日が不明なもの。IBKRの明細から別途確認が必要。
"""

import argparse
import logging
import os
from datetime import datetime
from typing import List, Optional

from core.market_hours import JST
from execution.tax_export import export_tax_report_csv
from execution.trade_journal import DEFAULT_JOURNAL_PATH, TradeJournal, TradeRecord

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR: str = "logs"


def _default_tax_year(now: Optional[datetime] = None) -> int:
    """既定の対象年（前年）。確定申告は前年分を申告するため。"""
    reference = now if now is not None else datetime.now(JST)
    return reference.year - 1


def _build_output_path(tax_year: Optional[int]) -> str:
    suffix = "all" if tax_year is None else str(tax_year)
    return os.path.join(DEFAULT_OUTPUT_DIR, f"tax_report_{suffix}.csv")


def _count_missing_entry_dates(trades: List[TradeRecord], tax_year: Optional[int]) -> int:
    """取得年月日が不明な行数（税理士に補足が必要な件数）を数える。"""
    from execution.tax_export import build_tax_export_rows

    rows = build_tax_export_rows(trades, tax_year=tax_year)
    return sum(1 for row in rows if not row.entry_date)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="トレードジャーナルから確定申告用CSVを出力する",
    )
    parser.add_argument(
        "--year", type=int, default=None,
        help=f"対象年（日本時間基準の決済日で絞り込む）。既定は前年（{_default_tax_year()}）",
    )
    parser.add_argument(
        "--all-years", action="store_true",
        help="年で絞り込まず、全期間を出力する",
    )
    parser.add_argument(
        "--journal", default=DEFAULT_JOURNAL_PATH,
        help=f"読み込むトレードジャーナルのパス（既定: {DEFAULT_JOURNAL_PATH}）",
    )
    parser.add_argument(
        "--output", default=None,
        help=f"出力先CSVのパス（既定: {DEFAULT_OUTPUT_DIR}/tax_report_<年>.csv）",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    if args.all_years and args.year is not None:
        logger.error("--year と --all-years は同時に指定できません。")
        return 2

    tax_year: Optional[int] = None if args.all_years else (args.year or _default_tax_year())
    output_path = args.output or _build_output_path(tax_year)

    if not os.path.exists(args.journal):
        logger.error(
            "トレードジャーナルが見つかりません: %s。"
            "まだ決済が1件も記録されていない可能性があります。",
            args.journal,
        )
        return 1

    trades = TradeJournal(args.journal).load_trades()
    if not trades:
        logger.error("トレードジャーナルに決済記録がありません: %s", args.journal)
        return 1

    directory = os.path.dirname(output_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    count = export_tax_report_csv(trades, output_path, tax_year=tax_year)

    if count == 0:
        logger.warning(
            "対象年(%s)に決済された取引がありませんでした。ヘッダーのみのCSVを出力しています。",
            tax_year if tax_year is not None else "全期間",
        )
        return 0

    missing = _count_missing_entry_dates(trades, tax_year)
    if missing:
        logger.warning(
            "%d件の行で取得年月日(entry_date)が空欄です。ブローカー側で発見した"
            "未追跡ポジション由来の決済のため、IBKRの明細から取得日を確認してください。",
            missing,
        )

    logger.info("出力しました: %s (%d件)", output_path, count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
