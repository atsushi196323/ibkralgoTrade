"""確定申告向けに、trade_journalの記録を税理士へ渡せるCSVとして出力する。

各行は日本の確定申告(「株式等に係る譲渡所得等の金額の計算明細書」相当)で
必要になる「取得年月日・譲渡年月日・数量・取得価額・譲渡価額・手数料・
適用為替レート・円換算後の実現損益」を並べたものである。

注意:
- usd_jpy_rateは決済時点にIBKRから取得したライブレートであり、国税庁が
  想定するTTB/TTM(その日の公表相場)とは厳密には一致しない可能性がある。
  税理士へ渡す際は「参考レートであり、必要なら公表レートで再計算してほしい」
  旨を申し添えること。
- entry_date(取得年月日)は、ブローカー側で発見した未追跡ポジション
  (このBotが建てたものではない)由来の決済では不明なためNoneになる。
  その場合はIBKRの明細から取得日を別途確認する必要がある。
"""

import csv
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import List, Optional

from core.market_hours import JST
from execution.trade_journal import TradeRecord

logger = logging.getLogger(__name__)

_EXPORT_FIELDNAMES: List[str] = [
    "symbol", "entry_date", "exit_date", "quantity",
    "entry_price_usd", "exit_price_usd", "cost_usd", "proceeds_usd",
    "commission_usd", "usd_jpy_rate", "pnl_usd", "pnl_jpy",
]


@dataclass
class TaxExportRow:
    symbol: str
    entry_date: Optional[str]
    exit_date: str
    quantity: int
    entry_price_usd: float
    exit_price_usd: float
    cost_usd: float
    proceeds_usd: float
    commission_usd: float
    usd_jpy_rate: Optional[float]
    pnl_usd: float
    pnl_jpy: Optional[float]


def _closed_at_to_jst_year(closed_at: str) -> int:
    """決済日時(ISO8601)を日本居住者の確定申告年(1/1-12/31、日本時間基準)に変換する。"""
    return datetime.fromisoformat(closed_at).astimezone(JST).year


# 金額列の丸め桁数。二進浮動小数の誤差がそのままCSVに出ると
# (例: 27033.250000000004) 税理士へ渡す書類として体裁が悪いため、
# 通貨の最小単位より1桁細かいところで丸める。
# 税務上の端数処理そのものは税理士の判断に委ねるため、ここでは切り捨て等は行わない。
_MONEY_DIGITS: int = 2


def _round_money(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, _MONEY_DIGITS)


def _to_export_row(trade: TradeRecord) -> TaxExportRow:
    return TaxExportRow(
        symbol=trade.symbol,
        entry_date=trade.entry_date,
        exit_date=trade.closed_at,
        quantity=trade.quantity,
        entry_price_usd=trade.entry_price,
        exit_price_usd=trade.exit_price,
        cost_usd=_round_money(trade.entry_price * trade.quantity),
        proceeds_usd=_round_money(trade.exit_price * trade.quantity),
        commission_usd=trade.commission,
        usd_jpy_rate=trade.usd_jpy_rate,
        pnl_usd=_round_money(trade.net_pnl_usd),
        pnl_jpy=_round_money(trade.net_pnl_jpy),
    )


def build_tax_export_rows(
    trades: List[TradeRecord], tax_year: Optional[int] = None,
) -> List[TaxExportRow]:
    """税理士向け一覧の行データを組み立てる（決済日時の昇順）。

    tax_yearを指定すると、その年(日本時間基準)に決済された取引のみに絞り込む。
    省略時は全期間を対象にする。
    """
    rows = [
        _to_export_row(trade) for trade in trades
        if tax_year is None or _closed_at_to_jst_year(trade.closed_at) == tax_year
    ]
    rows.sort(key=lambda row: row.exit_date)
    return rows


def export_tax_report_csv(
    trades: List[TradeRecord], file_path: str, tax_year: Optional[int] = None,
) -> int:
    """税理士向けの取引一覧をCSVに出力する。

    Excelで開いた際に日本語が文字化けしないよう、BOM付きUTF-8(utf-8-sig)で書き出す。

    Returns:
        出力した行数。
    """
    rows = build_tax_export_rows(trades, tax_year=tax_year)

    with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=_EXPORT_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))

    logger.info(
        "税理士向け確定申告用CSVを出力しました: file=%s tax_year=%s 件数=%d",
        file_path, tax_year if tax_year is not None else "全期間", len(rows),
    )

    # **為替レートが欠けた行は、黙って渡してはならない。** 円換算後の損益が空欄に
    # なるだけで出力自体は成功するため、気付かないまま税理士へ渡すと、その行だけ
    # 申告額から抜け落ちる。レートは決済時点にしか取れず（`main._resolve_usd_jpy_rate_async`）、
    # 後から推定で埋めることは禁じている（間違ったレートは後から見分けられない）ので、
    # **公表レートで手当てする必要がある行としてここで名指しする。**
    # 2026-08-05のAMBQが実例で、当時は為替のマーケットデータ購読が無く
    # (`Error 162 No market data permissions for IDEALPRO`)、口座サマリーの
    # ExchangeRate へのフォールバックもまだ無かった。
    missing = [row for row in rows if row.usd_jpy_rate is None]
    if missing:
        logger.warning(
            "為替レートが記録されていない決済が %d 件あります: %s。"
            "円換算後の損益(pnl_jpy)が空欄になっているため、"
            "その日の公表レート(TTM等)で手当てしてから提出してください。",
            len(missing), ", ".join(f"{row.symbol}({row.exit_date[:10]})" for row in missing),
        )

    return len(rows)
