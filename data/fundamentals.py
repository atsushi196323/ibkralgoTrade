"""IBKRのスキャナー・ファンダメンタルズデータ取得。

株価スクリーニング（時価総額・PER等での銘柄抽出）に使う低レベルAPIラッパー。
候補選定のビジネスロジック（閾値判定・2段階フィルタの組み立て）は
strategy/screener.py に置く。
"""

import logging
import xml.etree.ElementTree as ET
from typing import List, Optional

from ib_async import IB, ScannerSubscription, Stock

logger = logging.getLogger(__name__)

DEFAULT_SCAN_CODE: str = "MOST_ACTIVE"
# 売買代金順のスキャン。MOST_ACTIVE は株数ベースなので低位株が上位に来る。
TURNOVER_SCAN_CODE: str = "MOST_ACTIVE_USD"
DEFAULT_LOCATION_CODE: str = "STK.US.MAJOR"
DEFAULT_INSTRUMENT: str = "STK"

# ReportSnapshot XML内のPERを表すFieldName（Reuters系ファンダメンタルズの標準タグ）。
# 特別損益を除いた実績PER。
PE_RATIO_FIELD_NAME: str = "PEEXCLXOR"


async def run_market_cap_scan_async(
    ib: IB,
    market_cap_above: float,
    market_cap_below: float,
    scan_code: str = DEFAULT_SCAN_CODE,
    number_of_rows: int = 50,
) -> List[Stock]:
    if market_cap_above < 0 or market_cap_below < 0:
        raise ValueError("market_cap_above, market_cap_below は0以上である必要があります。")
    if market_cap_above > market_cap_below:
        raise ValueError("market_cap_above は market_cap_below 以下である必要があります。")

    subscription = ScannerSubscription(
        instrument=DEFAULT_INSTRUMENT,
        locationCode=DEFAULT_LOCATION_CODE,
        scanCode=scan_code,
        marketCapAbove=market_cap_above,
        marketCapBelow=market_cap_below,
        numberOfRows=number_of_rows,
    )
    scan_data = await ib.reqScannerDataAsync(subscription)

    stocks = [item.contractDetails.contract for item in scan_data]
    if not stocks:
        # MOST_ACTIVE等の一般的なscan_codeで0件になるのは通常考えにくく、
        # 口座にマーケットスキャナーの購読権限が無いケースが多い。
        logger.warning(
            "時価総額スキャンの結果が0件でした: scan_code=%s cap=[%.0f, %.0f]。"
            "マーケットスキャナーの購読権限が無い可能性があります。",
            scan_code, market_cap_above, market_cap_below,
        )
    else:
        logger.info(
            "時価総額スキャン完了: scan_code=%s cap=[%.0f, %.0f] hits=%d",
            scan_code, market_cap_above, market_cap_below, len(stocks),
        )
    return stocks


def _extract_ratio(xml_report: str, field_name: str) -> Optional[float]:
    try:
        root = ET.fromstring(xml_report)
    except ET.ParseError:
        logger.warning("ファンダメンタルズレポートのXMLパースに失敗しました。")
        return None

    for ratio in root.iter("Ratio"):
        if ratio.get("FieldName") == field_name and ratio.text:
            try:
                return float(ratio.text)
            except ValueError:
                return None
    return None


async def get_pe_ratio_async(ib: IB, contract: Stock) -> Optional[float]:
    xml_report = await ib.reqFundamentalDataAsync(contract, "ReportSnapshot")
    if not xml_report:
        logger.warning("%s のファンダメンタルズレポートを取得できませんでした。", contract.symbol)
        return None

    pe_ratio = _extract_ratio(xml_report, PE_RATIO_FIELD_NAME)
    logger.info("%s のPER: %s", contract.symbol, pe_ratio)
    return pe_ratio


async def run_turnover_scan_async(
    ib: IB,
    location_code: str,
    number_of_rows: int = 50,
    above_price: Optional[float] = None,
    below_price: Optional[float] = None,
    scan_code: str = TURNOVER_SCAN_CODE,
) -> List[str]:
    """売買代金の上位銘柄を、順位の順に並んだシンボルとして返す。

    **`numberOfRows` の上限はIBKR側で50である。** 上位100件が欲しい場合は
    `location_code` を `STK.NASDAQ` と `STK.NYSE` に分けて2回呼び、統合する。

    **返るのは順位だけで、売買代金の数値は含まれない。** 数値まで得るには
    銘柄ごとに日足を取り直すことになり、ペーシング枠（10分あたり60件）を
    使い切る。順位の変化だけで判定するのはこのためである。

    株価の帯は `above_price` / `below_price` としてサーバー側へ渡す。手元で
    絞ると、買えない銘柄のぶんまでランキングの枠を消費してしまう。

    スキャナーは購読権限が無いと**例外ではなく空を返す**（CLAUDE.md「6.2」）ので、
    0件のときは警告を出す。
    """
    subscription = ScannerSubscription(
        instrument=DEFAULT_INSTRUMENT,
        locationCode=location_code,
        scanCode=scan_code,
        numberOfRows=number_of_rows,
    )
    if above_price is not None:
        subscription.abovePrice = above_price
    if below_price is not None:
        subscription.belowPrice = below_price

    scan_data = await ib.reqScannerDataAsync(subscription)
    symbols = [item.contractDetails.contract.symbol for item in scan_data]

    if not symbols:
        logger.warning(
            "売買代金スキャンの結果が0件でした: scan_code=%s location=%s。"
            "マーケットスキャナーの購読権限が無い可能性があります。",
            scan_code, location_code,
        )
    else:
        logger.info(
            "売買代金スキャン完了: location=%s hits=%d 上位5件=%s",
            location_code, len(symbols), symbols[:5],
        )
    return symbols
