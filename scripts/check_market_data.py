"""IB Gatewayのペーパー口座でマーケットデータが実際に取得できるかを診断する。

data.market_data.get_current_price_async は「ストリーミング → スナップショット
→ ヒストリカル終値」の順にフォールバックするため、どれか1つでも通れば
ボットは動く。このスクリプトはその3経路を個別に叩き、口座で実際に
どれが使えるのかを可視化する。

    A. ストリーミング   (reqMktData, snapshot=False) … 遅延データでも配信される
    B. スナップショット (reqTickersAsync)            … リアルタイム購読契約が要る
    C. ヒストリカル終値 (reqHistoricalDataAsync)     … 購読権限が無くても取れやすい
    D. USD/JPY (Forex)                               … 株とは別の購読権限

IBKRからのエラーはerrorEventで全件表示する。価格が取れない原因は
ほぼ必ずこのエラーコードに出るため、ログ末尾ではなくエラー行を見ること。

実行方法:
    python -m scripts.check_market_data
    python -m scripts.check_market_data --symbol MSFT --wait 10
"""

import argparse
import asyncio
import logging
import math
from typing import List, Optional, Tuple

from ib_insync import IB, Contract

from core.connection import IBKRConnection
from data.market_data import (
    _get_last_close_price_async,
    _get_snapshot_price_async,
    _get_streaming_price_async,
    get_usd_jpy_rate_async,
    qualify_stock_async,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

MARKET_DATA_TYPE_LABELS = {
    0: "未通知(データが1件も届いていない)",
    1: "LIVE(リアルタイム購読あり)",
    2: "FROZEN(直近の引け値)",
    3: "DELAYED(15分遅延)",
    4: "DELAYED_FROZEN(遅延+引け値)",
}

_errors: List[Tuple[int, str]] = []


def _is_valid(price: Optional[float]) -> bool:
    return price is not None and not math.isnan(price)


def _fmt(price: Optional[float]) -> str:
    return "取得失敗(None/NaN)" if not _is_valid(price) else f"{price:.4f}"


def _unwrap(quote: Optional[Tuple[float, bool]]) -> Tuple[Optional[float], bool]:
    """低レベル経路が返す (価格, is_stale) を分解する。

    株の3経路は鮮度判定のためにタプルを返すが、Forexは価格だけを返す。
    """
    if quote is None:
        return None, False
    price, is_stale = quote
    return price, is_stale


def _fmt_quote(quote: Optional[Tuple[float, bool]]) -> str:
    price, is_stale = _unwrap(quote)
    if not _is_valid(price):
        return "取得失敗(None/NaN)"
    # 古い値（前営業日の終値）を掴んでいると、新規建ての参照価格としては
    # REJECT_STALE_ENTRY_PRICE に弾かれるため、価格が取れたことと同列に見せない。
    return f"{price:.4f}" + ("  ⚠️ 古い値(前営業日の終値)" if is_stale else "")


def _on_error(reqId: int, errorCode: int, errorString: str, contract) -> None:
    # 2104/2106/2158 等の「接続OK」通知は情報レベルなので区別して表示する。
    if 2100 <= errorCode < 2200:
        logger.info("  [IBKR情報 %s] %s", errorCode, errorString)
        return
    _errors.append((errorCode, errorString))
    logger.error("  [IBKRエラー %s] %s", errorCode, errorString)


async def _check_streaming(
    ib: IB, contract: Contract, wait_seconds: float,
) -> Optional[Tuple[float, bool]]:
    """A: フォールバック連鎖の1段目。snapshot=Falseのストリーミング購読。"""
    print(f"\n--- A. ストリーミング reqMktData ({wait_seconds:.0f}秒待機) ---")

    # 実際に何が配信されたかを見たいので、購読を張って生のフィールドを覗く
    ticker = ib.reqMktData(contract, "", False, False)
    try:
        await asyncio.sleep(wait_seconds)
        data_type = getattr(ticker, "marketDataType", 0) or 0
        print(f"  配信されたデータ種別: {data_type} = "
              f"{MARKET_DATA_TYPE_LABELS.get(data_type, '不明')}")
        print(f"  bid={_fmt(ticker.bid)} ask={_fmt(ticker.ask)} "
              f"last={_fmt(ticker.last)} close={_fmt(ticker.close)}")
    finally:
        ib.cancelMktData(contract)

    # 本番と同じコードパスでも判定する
    quote = await _get_streaming_price_async(ib, contract, wait_seconds)
    print(f"  結果: {_fmt_quote(quote)}")
    return quote


async def _check_snapshot(ib: IB, contract: Contract) -> Optional[Tuple[float, bool]]:
    """B: 2段目。reqTickersAsyncは内部でsnapshot=Trueを使う。"""
    print("\n--- B. スナップショット reqTickersAsync ---")
    quote = await _get_snapshot_price_async(ib, contract)
    print(f"  結果: {_fmt_quote(quote)}")
    return quote


async def _check_historical(ib: IB, contract: Contract) -> Optional[Tuple[float, bool]]:
    """C: 3段目。ヒストリカルバーの最終終値を現在値の代わりに使う。"""
    print("\n--- C. ヒストリカル日足の最終終値 ---")
    quote = await _get_last_close_price_async(ib, contract)
    print(f"  結果: {_fmt_quote(quote)}")
    return quote


async def _check_forex(ib: IB, wait_seconds: float) -> Optional[float]:
    """D: 円換算に使うUSD/JPY。株と別の購読権限なので個別に確認する。"""
    print("\n--- D. USD/JPY (Forex) ---")
    try:
        price = await get_usd_jpy_rate_async(ib, streaming_timeout_seconds=wait_seconds)
    except Exception as exc:
        logger.error("  例外が発生しました: %r", exc)
        return None
    print(f"  結果: {_fmt(price)}")
    return price


def _print_verdict(
    symbol: str, streaming: Optional[Tuple[float, bool]],
    snapshot: Optional[Tuple[float, bool]],
    historical: Optional[Tuple[float, bool]], forex: Optional[float],
) -> None:
    print("\n" + "=" * 70)
    print(f"診断結果 ({symbol})")
    print("=" * 70)
    print(f"  A. ストリーミング   : {_fmt_quote(streaming)}")
    print(f"  B. スナップショット : {_fmt_quote(snapshot)}")
    print(f"  C. ヒストリカル終値 : {_fmt_quote(historical)}")
    print(f"  D. USD/JPY          : {_fmt(forex)}")

    if _errors:
        print("\n  検出したIBKRエラー:")
        for code, message in dict(_errors).items():
            print(f"    - {code}: {message}")
    else:
        print("\n  IBKRエラーは検出されませんでした。")

    streaming_price, streaming_stale = _unwrap(streaming)
    snapshot_price, snapshot_stale = _unwrap(snapshot)
    historical_price, historical_stale = _unwrap(historical)

    print("\n  判定:")
    if _is_valid(streaming_price):
        print("    ✅ 1段目のストリーミングで価格が取れています。最良の状態です。")
        adopted_stale = streaming_stale
    elif _is_valid(snapshot_price):
        print("    ✅ ストリーミングは失敗しましたが、スナップショットで取れています。")
        print("       → ボットは動きますが、なぜ1段目が失敗したかは上のエラーを確認のこと。")
        adopted_stale = snapshot_stale
    elif _is_valid(historical_price):
        print("    ⚠️  リアルタイム系は両方失敗、ヒストリカル終値のみ成功。")
        print("       → ボットは動きますが、価格が直近営業日の終値になるため")
        print("         デイトレードの判定は実質機能しません。マーケットデータの")
        print("         購読を追加するか、スイング検証に絞ってください。")
        adopted_stale = historical_stale
    else:
        print("    ❌ すべて失敗。ボットは価格を取得できず何も発注しません。")
        print("       → 上のIBKRエラーコードと、市場時間内に実行したかを確認してください。")
        adopted_stale = False

    if adopted_stale:
        print("    ⚠️  採用された価格が古い(前営業日の終値)と判定されています。")
        print("       → main.REJECT_STALE_ENTRY_PRICE が既定(True)のままだと")
        print("         新規建ては全件見送られます。決済判定は通常どおり動きます。")

    if not _is_valid(forex):
        print("    ⚠️  USD/JPYが取得できていません。円換算(usd_jpy_rate)は記録されません。")


async def main() -> None:
    parser = argparse.ArgumentParser(description="IB Gatewayのマーケットデータ取得を診断する")
    parser.add_argument("--symbol", default="AAPL", help="確認に使う銘柄 (既定: AAPL)")
    parser.add_argument("--wait", type=float, default=8.0,
                        help="ストリーミング購読の待機秒数 (既定: 8)")
    args = parser.parse_args()

    connection = IBKRConnection()
    print(f"接続先: {connection.host}:{connection.port} "
          f"(clientId={connection.client_id}, "
          f"要求するデータ種別={connection.market_data_type}="
          f"{MARKET_DATA_TYPE_LABELS.get(connection.market_data_type, '不明')})")

    try:
        ib = await connection.connect_async()
        ib.errorEvent += _on_error

        contract = await qualify_stock_async(ib, args.symbol)

        streaming = await _check_streaming(ib, contract, args.wait)
        snapshot = await _check_snapshot(ib, contract)
        historical = await _check_historical(ib, contract)
        forex = await _check_forex(ib, args.wait)

        _print_verdict(args.symbol, streaming, snapshot, historical, forex)
    finally:
        await connection.disconnect_async()


if __name__ == "__main__":
    asyncio.run(main())
