"""銘柄スクリーニング（時価総額スキャン・PER取得）が実際に使えるかを診断する。

`scripts/check_market_data.py` が価格データの取得経路を診断するのに対し、
こちらは**銘柄選定**に使う2つのAPIの購読権限を診断する。

    A. 時価総額スキャン (reqScannerData)      … マーケットスキャナーの購読が要る
    B. PER取得          (reqFundamentalData)  … ファンダメンタルズデータの購読が要る

この2つは購読権限が無くても例外にならず、静かに空を返す。ボットは
`strategy/screener.py` のフォールバックで動き続けるため、**銘柄選定が
無効化されて固定ウォッチリスト(main.WATCHLIST)で回っていることに
気付けない**。それを起動前に切り分けるのがこのスクリプトの目的。

閾値は main.py の定数をそのまま読み込むため、本番と同じ条件で診断できる。

IBKRからのエラーはerrorEventで全件表示する。権限の問題はほぼ必ず
エラーコードに出るため、件数だけでなくエラー行を見ること。

実行方法:
    python -m scripts.check_screener
    python -m scripts.check_screener --pe-samples 10
    python -m scripts.check_screener --full   # 本番と同じパイプラインを丸ごと実行
"""

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ib_async import IB, Stock

from core.connection import IBKRConnection
from data.fundamentals import get_pe_ratio_async, run_market_cap_scan_async
from main import (
    MAX_WATCHLIST_SIZE,
    SCREENER_ENABLE_TREND_FILTER,
    SCREENER_MAX_MARKET_CAP,
    SCREENER_MAX_PE_RATIO,
    SCREENER_MIN_MARKET_CAP,
    SCREENER_NUM_CANDIDATES,
    SCREENER_PE_REQUEST_INTERVAL_SECONDS,
    SCREENER_SCAN_CODE,
    SCREENER_TREND_LOOKBACK_DURATION,
    SCREENER_TREND_MA_WINDOW,
    WATCHLIST,
)
from strategy.screener import ScreenerConfig, screen_value_stocks_async

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_errors: List[Tuple[int, str]] = []


@dataclass
class PeRatioSample:
    symbol: str
    pe_ratio: Optional[float]

    @property
    def fetched(self) -> bool:
        """PERの値そのものを取得できたか（購読権限の判定に使う）。"""
        return self.pe_ratio is not None

    @property
    def passes_filter(self) -> bool:
        """本番のスクリーニング条件（0 < PER <= 上限）を通るか。"""
        return self.pe_ratio is not None and 0 < self.pe_ratio <= SCREENER_MAX_PE_RATIO


@dataclass
class ScreenerDiagnosis:
    """診断結果。表示とテストのために集計だけを持つ。"""

    scan_hits: int
    pe_samples: List[PeRatioSample] = field(default_factory=list)

    @property
    def scanner_available(self) -> bool:
        return self.scan_hits > 0

    @property
    def num_pe_fetched(self) -> int:
        return sum(1 for sample in self.pe_samples if sample.fetched)

    @property
    def fundamentals_available(self) -> bool:
        """1銘柄でもPERが取れればファンダメンタルズは使えていると判断する。

        個別銘柄がPERを持たない（赤字等）ケースと購読権限の欠如を
        区別するため、「全滅かどうか」で判定する。
        """
        return self.num_pe_fetched > 0

    @property
    def num_passing_filter(self) -> int:
        return sum(1 for sample in self.pe_samples if sample.passes_filter)

    @property
    def falls_back_to_fixed_watchlist(self) -> bool:
        """このままボットを動かすと固定ウォッチリストで回ることになるか。"""
        return not self.scanner_available or not self.fundamentals_available


def _on_error(reqId: int, errorCode: int, errorString: str, contract) -> None:
    # 2104/2106/2158 等の「接続OK」通知は情報レベルなので区別して表示する。
    if 2100 <= errorCode < 2200:
        logger.info("  [IBKR情報 %s] %s", errorCode, errorString)
        return
    _errors.append((errorCode, errorString))
    logger.error("  [IBKRエラー %s] %s", errorCode, errorString)


async def _check_scanner(ib: IB, number_of_rows: int) -> List[Stock]:
    """A: 時価総額スキャン。母集団が取れなければ後段は全て無意味になる。"""
    print("\n--- A. 時価総額スキャン reqScannerData ---")
    print(f"  条件: scan_code={SCREENER_SCAN_CODE} "
          f"時価総額=[{SCREENER_MIN_MARKET_CAP:,.0f}, {SCREENER_MAX_MARKET_CAP:,.0f}] "
          f"最大{number_of_rows}件")

    try:
        candidates = await run_market_cap_scan_async(
            ib,
            market_cap_above=SCREENER_MIN_MARKET_CAP,
            market_cap_below=SCREENER_MAX_MARKET_CAP,
            scan_code=SCREENER_SCAN_CODE,
            number_of_rows=number_of_rows,
        )
    except Exception as exc:
        logger.error("  例外が発生しました: %r", exc)
        return []

    print(f"  結果: {len(candidates)}件")
    if candidates:
        preview = [contract.symbol for contract in candidates[:10]]
        print(f"  先頭10件: {preview}")
    return candidates


async def _check_pe_ratios(
    ib: IB, candidates: List[Stock], num_samples: int, interval_seconds: float,
) -> List[PeRatioSample]:
    """B: PER取得。候補の先頭数銘柄だけを叩いて権限の有無を切り分ける。"""
    print(f"\n--- B. PER取得 reqFundamentalData (先頭{num_samples}銘柄) ---")

    samples: List[PeRatioSample] = []
    for index, contract in enumerate(candidates[:num_samples]):
        # 本番と同じ間隔を空ける（連続発行はペーシング制限に触れうる）。
        if index > 0 and interval_seconds > 0:
            await asyncio.sleep(interval_seconds)

        try:
            pe_ratio = await get_pe_ratio_async(ib, contract)
        except Exception as exc:
            logger.error("  [%s] 例外が発生しました: %r", contract.symbol, exc)
            pe_ratio = None

        sample = PeRatioSample(symbol=contract.symbol, pe_ratio=pe_ratio)
        samples.append(sample)

        if not sample.fetched:
            status = "取得失敗"
        elif sample.passes_filter:
            status = f"PER={pe_ratio:.2f} (条件通過)"
        else:
            status = f"PER={pe_ratio:.2f} (条件で除外)"
        print(f"  {contract.symbol}: {status}")

    return samples


async def _check_full_pipeline(ib: IB, number_of_rows: int) -> List[str]:
    """本番と同じスクリーニングを丸ごと実行する（--full）。

    長期トレンドフィルター（200日移動平均）まで含めて、実際に
    ウォッチリストへ何件残るかを確認する。候補全件へリクエストするため
    時間がかかる（1銘柄あたり最大2リクエスト＋待機）。
    """
    print("\n--- C. 本番と同じスクリーニングパイプライン ---")
    config = ScreenerConfig(
        market_cap_above=SCREENER_MIN_MARKET_CAP,
        market_cap_below=SCREENER_MAX_MARKET_CAP,
        max_pe_ratio=SCREENER_MAX_PE_RATIO,
        scan_code=SCREENER_SCAN_CODE,
        number_of_rows=number_of_rows,
        pe_request_interval_seconds=SCREENER_PE_REQUEST_INTERVAL_SECONDS,
        enable_trend_filter=SCREENER_ENABLE_TREND_FILTER,
        trend_ma_window=SCREENER_TREND_MA_WINDOW,
        trend_lookback_duration=SCREENER_TREND_LOOKBACK_DURATION,
    )

    try:
        selected = await screen_value_stocks_async(ib, config)
    except Exception as exc:
        logger.error("  例外が発生しました: %r", exc)
        return []

    print(f"  最終選定: {len(selected)}件 {selected}")
    if len(selected) > MAX_WATCHLIST_SIZE:
        print(f"  → 実際に監視するのは上位{MAX_WATCHLIST_SIZE}件 "
              f"{selected[:MAX_WATCHLIST_SIZE]}")
    return selected


def _print_verdict(diagnosis: ScreenerDiagnosis) -> None:
    print("\n" + "=" * 70)
    print("診断結果（銘柄スクリーニング）")
    print("=" * 70)
    print(f"  A. 時価総額スキャン : {diagnosis.scan_hits}件")
    print(f"  B. PER取得          : {diagnosis.num_pe_fetched}/{len(diagnosis.pe_samples)}銘柄で成功 "
          f"(うちPER<={SCREENER_MAX_PE_RATIO:.0f}の条件通過: {diagnosis.num_passing_filter}銘柄)")

    if _errors:
        print("\n  検出したIBKRエラー:")
        for code, message in dict(_errors).items():
            print(f"    - {code}: {message}")
    else:
        print("\n  IBKRエラーは検出されませんでした。")

    print("\n  判定:")
    if not diagnosis.scanner_available:
        print("    ❌ 時価総額スキャンが0件です。マーケットスキャナーの購読権限が")
        print("       無い可能性が高いです（権限があれば MOST_ACTIVE で0件は考えにくい）。")
    else:
        print("    ✅ 時価総額スキャンで母集団を取得できています。")

    if not diagnosis.pe_samples:
        print("    ⚠️  候補が無いためPERを確認できませんでした。")
    elif not diagnosis.fundamentals_available:
        print("    ❌ PERが1銘柄も取得できていません。ファンダメンタルズデータの")
        print("       購読権限が無い可能性が高いです。")
    elif diagnosis.num_pe_fetched < len(diagnosis.pe_samples):
        print("    ✅ PERを取得できています（一部の銘柄はデータを持っていません）。")
    else:
        print("    ✅ 全サンプルでPERを取得できています。")

    if diagnosis.falls_back_to_fixed_watchlist:
        print("\n    → このまま main.py を動かすと、銘柄選定は無効化され")
        print(f"       固定ウォッチリスト {WATCHLIST} で回り続けます。")
        print("       「分析して銘柄を特定している」状態にはなりません。")
    elif diagnosis.num_passing_filter == 0:
        print("\n    ⚠️  APIは使えていますが、サンプル内にPER条件を通る銘柄がありません。")
        print("       閾値が厳しすぎる可能性があります（--full で最終件数を確認してください）。")
    else:
        print("\n    → 銘柄選定は機能します。--full で最終的な選定件数を確認できます。")


async def main() -> None:
    parser = argparse.ArgumentParser(description="銘柄スクリーニングの購読権限を診断する")
    parser.add_argument(
        "--num-candidates", type=int, default=SCREENER_NUM_CANDIDATES,
        help=f"スキャンで取得する候補数 (既定: {SCREENER_NUM_CANDIDATES})",
    )
    parser.add_argument(
        "--pe-samples", type=int, default=5,
        help="PERを確認する銘柄数 (既定: 5)。権限の切り分けには少数で足りる。",
    )
    parser.add_argument(
        "--full", action="store_true",
        help="本番と同じスクリーニングを丸ごと実行する（時間がかかる）。",
    )
    args = parser.parse_args()

    connection = IBKRConnection()
    print(f"接続先: {connection.host}:{connection.port} (clientId={connection.client_id})")

    try:
        ib = await connection.connect_async()
        ib.errorEvent += _on_error

        candidates = await _check_scanner(ib, args.num_candidates)
        samples = await _check_pe_ratios(
            ib, candidates, args.pe_samples, SCREENER_PE_REQUEST_INTERVAL_SECONDS,
        )

        _print_verdict(ScreenerDiagnosis(scan_hits=len(candidates), pe_samples=samples))

        if args.full:
            await _check_full_pipeline(ib, args.num_candidates)
    finally:
        await connection.disconnect_async()


if __name__ == "__main__":
    asyncio.run(main())
