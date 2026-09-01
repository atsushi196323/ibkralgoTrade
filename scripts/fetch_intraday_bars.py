"""検証用の日中足をIBKRから取得してCSVへ保存する（IB Gatewayへの接続が要る）。

日足を取る `scripts/fetch_bars.py` は yfinance を使うが、**yfinanceの分足は
直近60日程度しか遡れない**ためデイトレード分岐の検証には足りない。IBKRは
5分足を **1年ぶん** 返す（2026-08-13にペーパー口座で実測。INTCで19,506本、
`2 Y` は上限を超えて0本）。**この取得にマーケットデータの購読契約は要らない。**
購読が要るのはリアルタイムのストリーミングとスキャナーであって、
ヒストリカルバーではない。

出力は `backtest/csv_source.py` が読める形式（date,open,high,low,close,volume）。

**IBKRのデータ取得APIを直接呼ばず、`data.market_data` を経由する**
（docs/DECISIONS.md「6.3 必ず経由すべき入口」）。ペーシング制限（10分あたり60件）は
そちらのレートリミッターが守る。42銘柄なら1銘柄1リクエストで収まる。

実行方法:
    python -m scripts.fetch_intraday_bars --symbols-from-dir bars
    python -m scripts.fetch_intraday_bars --symbols INTC AAPL --duration "6 M"
"""

import argparse
import asyncio
import glob
import logging
import os
from typing import List, Optional

import pandas as pd
from dotenv import load_dotenv

from core.connection import IBKRConnection
from data.market_data import get_intraday_bars_async, qualify_stock_async

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_OUT_DIR: str = "bars/intraday"
DEFAULT_DURATION: str = "1 Y"
DEFAULT_BAR_SIZE: str = "5 mins"

# 1年ぶんの5分足（約19,500本）は ib_async の既定タイムアウト(60秒)に
# 収まらない。**タイムアウトは例外ではなく空のバー列として返る**ため、
# 短すぎると「データが無い銘柄」と区別がつかない（2026-08-13に42銘柄中41銘柄が
# ちょうど60秒で空を返した）。取得は1回きりなので長めに取る。
DEFAULT_TIMEOUT_SECONDS: float = 300.0

# 本体(main.py)と衝突しないクライアントID。稼働中に流すと同じIDでは接続できない。
DEFAULT_CLIENT_ID: int = 9

# 1日は 6.5時間 = 78本（5分足）。ウォークフォワードは
# train_bars + test_bars を要求するので、日数で見て足りない銘柄は警告する。
BARS_PER_SESSION: int = 78


def _symbols_from_dir(directory: str) -> List[str]:
    """既存の日足CSVが並ぶディレクトリから銘柄名を拾う。

    日中足の検証母集団を日足の検証母集団と一致させるための入口。別々に
    指定すると、銘柄の入れ替えが片方に反映されないまま比較してしまう。
    """
    paths = sorted(glob.glob(os.path.join(directory, "*.csv")))
    return [os.path.splitext(os.path.basename(path))[0] for path in paths]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="IBKRから日中足を取得してCSVへ保存する")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--symbols", nargs="+", help="銘柄シンボル")
    source.add_argument("--symbols-file", help="1行1銘柄のファイル")
    source.add_argument(
        "--symbols-from-dir",
        help="既存CSVが並ぶディレクトリ（例: bars）。ファイル名を銘柄名として使う。",
    )
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--duration", default=DEFAULT_DURATION,
                        help="reqHistoricalDataのdurationStr。5分足の上限は 1 Y。")
    parser.add_argument("--bar-size", default=DEFAULT_BAR_SIZE)
    parser.add_argument("--client-id", type=int, default=DEFAULT_CLIENT_ID)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
                        help="1リクエストの待ち時間の上限(秒)。超えると空で返る。")
    parser.add_argument("--force", action="store_true",
                        help="既存のCSVがあっても取得し直す。")
    return parser.parse_args()


def _load_symbols(args: argparse.Namespace) -> List[str]:
    if args.symbols:
        return [symbol.upper() for symbol in args.symbols]
    if args.symbols_from_dir:
        return _symbols_from_dir(args.symbols_from_dir)
    with open(args.symbols_file, encoding="utf-8") as handle:
        return [line.strip().upper() for line in handle if line.strip()]


async def _fetch_one_async(ib, symbol: str, args: argparse.Namespace) -> Optional[pd.DataFrame]:
    contract = await qualify_stock_async(ib, symbol)
    if contract is None:
        logger.warning("[%s] コントラクトを特定できませんでした。", symbol)
        return None
    bars = await get_intraday_bars_async(
        ib, contract, duration=args.duration, bar_size=args.bar_size,
        timeout=args.timeout,
    )
    if bars is None or bars.empty:
        # ペーシング違反は例外ではなく空のバー列として返る（docs/DECISIONS.md「6.1」）。
        # 「データが無い銘柄」と区別がつかないため、空は必ず記録に残す。
        logger.warning("[%s] バーが0本でした（ペーシング違反・期間の上限超過の可能性）。", symbol)
        return None
    return bars


async def main_async() -> int:
    args = _parse_args()
    load_dotenv()
    symbols = _load_symbols(args)
    os.makedirs(args.out_dir, exist_ok=True)

    pending = []
    for symbol in symbols:
        path = os.path.join(args.out_dir, f"{symbol}.csv")
        if os.path.exists(path) and not args.force:
            logger.info("[%s] 既に取得済みのためスキップします: %s", symbol, path)
            continue
        pending.append((symbol, path))

    if not pending:
        logger.info("取得対象がありません。")
        return 0

    connection = IBKRConnection(
        host=os.getenv("IBKR_HOST", "127.0.0.1"),
        port=int(os.getenv("IBKR_PORT", "4002")),
        client_id=args.client_id,
        market_data_type=int(os.getenv("IBKR_MARKET_DATA_TYPE", "3")),
    )
    ib = await connection.connect_async()
    saved, failed = 0, []
    try:
        for index, (symbol, path) in enumerate(pending, start=1):
            logger.info("[%s] 取得します (%s/%s)", symbol, index, len(pending))
            try:
                bars = await _fetch_one_async(ib, symbol, args)
            except Exception:
                logger.warning("[%s] 取得に失敗しました。", symbol, exc_info=True)
                failed.append(symbol)
                continue
            if bars is None:
                failed.append(symbol)
                continue
            bars.to_csv(path, index=False)
            saved += 1
            logger.info(
                "[%s] 保存しました: %s 本 (%s 営業日相当) -> %s",
                symbol, len(bars), len(bars) // BARS_PER_SESSION, path,
            )
    finally:
        await connection.disconnect_async()

    logger.info("保存 %s 件 / 失敗 %s 件 %s", saved, len(failed), failed or "")
    return 0 if not failed else 1


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
