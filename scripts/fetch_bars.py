"""検証用のヒストリカル日足をCSVへ保存する（yfinance経由、IBKR接続不要）。

IB Gatewayへログインできない環境でもバックテストを進められるようにするための
補助ツール。`backtest/csv_source.py` が読める形式で1銘柄1ファイルに保存する。

注意:
  - **IBKRのバーとは調整方法（配当調整の有無等）が異なり、結果は完全には
    一致しない。** エッジの有無を見る用途には十分だが、実発注前の最終確認は
    IBKRのデータで行うこと。
  - 分足は直近60日程度しか遡れないため、このスクリプトは日足のみを扱う。
    デイトレード分岐の長期検証にはIBKR接続が要る。
  - yfinanceは requirements-dev.txt にのみ含まれる検証用の依存であり、
    ボット本体(main.py)は一切依存しない。

実行方法:
    python -m scripts.fetch_bars --symbols AAPL MSFT --out-dir bars
    python -m scripts.fetch_bars --symbols-file symbols.txt --period 5y
"""

import argparse
import logging
import os
from typing import List, Optional

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_OUT_DIR: str = "bars"
DEFAULT_PERIOD: str = "5y"
# バックテストが最低限必要とする行数の目安（既定の train 252 + test 63）。
# これを下回るCSVはウォークフォワードでウィンドウを1つも作れない。
MIN_USABLE_ROWS: int = 315


def _load_yfinance():
    """yfinanceを遅延importする（未インストール時に分かりやすく落とすため）。"""
    try:
        import yfinance  # noqa: PLC0415  (遅延importは意図的)
    except ImportError as exc:
        raise SystemExit(
            "yfinance がインストールされていません。\n"
            "  pip install -r requirements-dev.txt"
        ) from exc
    return yfinance


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """yfinanceの戻り値を csv_source が読める列名・並びに整える。"""
    # 単一銘柄でも MultiIndex の列で返ることがあるため、最初の階層だけ残す。
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df.reset_index()
    df.columns = [str(col).strip().lower() for col in df.columns]

    # 日付列の名前は index の名前次第で date / datetime / index になりうる。
    for candidate in ("date", "datetime", "index"):
        if candidate in df.columns:
            df = df.rename(columns={candidate: "date"})
            break

    keep = [col for col in ("date", "open", "high", "low", "close", "volume") if col in df.columns]
    df = df[keep]
    # OHLCを残すのは、バックテストがバー内での逆指値・指値の約定を
    # 判定するため（backtest/engine.py の待機注文モデル）。
    df = df.dropna(subset=["close"])
    return df


def fetch_symbol(symbol: str, period: str, out_dir: str) -> Optional[str]:
    """1銘柄分の日足を取得してCSVへ保存する。保存できなければNone。"""
    yfinance = _load_yfinance()

    try:
        raw = yfinance.download(
            symbol, period=period, interval="1d",
            auto_adjust=False, progress=False, threads=False,
        )
    except Exception:
        logger.exception("[%s] の取得に失敗しました。", symbol)
        return None

    if raw is None or raw.empty:
        logger.warning("[%s] のバーが0件でした（銘柄コードを確認してください）。", symbol)
        return None

    df = _normalize(raw)
    if df.empty:
        logger.warning("[%s] に有効な終値がありませんでした。", symbol)
        return None

    if len(df) < MIN_USABLE_ROWS:
        logger.warning(
            "[%s] は%d行しかなく、既定のウォークフォワード設定(train252+test63)では"
            "ウィンドウを作れません。--period を延ばすか、この銘柄を除外してください。",
            symbol, len(df),
        )

    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{symbol}.csv")
    df.to_csv(path, index=False)
    logger.info("[%s] %d行を保存しました: %s", symbol, len(df), path)
    return path


def _read_symbols_file(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        # 1行1銘柄。空行と # 始まりのコメントは無視する。
        return [
            line.strip() for line in f
            if line.strip() and not line.strip().startswith("#")
        ]


def main() -> None:
    parser = argparse.ArgumentParser(description="検証用の日足をCSVへ保存する（yfinance）")
    parser.add_argument("--symbols", nargs="+", help="銘柄コード（スペース区切り）")
    parser.add_argument("--symbols-file", help="1行1銘柄のテキストファイル")
    parser.add_argument("--period", default=DEFAULT_PERIOD, help=f"取得期間 (既定: {DEFAULT_PERIOD})")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help=f"保存先 (既定: {DEFAULT_OUT_DIR})")
    args = parser.parse_args()

    symbols: List[str] = list(args.symbols or [])
    if args.symbols_file:
        symbols.extend(_read_symbols_file(args.symbols_file))
    if not symbols:
        parser.error("--symbols または --symbols-file のどちらかを指定してください。")

    # 重複を除きつつ入力順を保つ。
    symbols = list(dict.fromkeys(symbols))

    saved = [symbol for symbol in symbols if fetch_symbol(symbol, args.period, args.out_dir) is not None]

    logger.info("完了: %d/%d銘柄を %s に保存しました。", len(saved), len(symbols), args.out_dir)
    if len(saved) < len(symbols):
        failed = [symbol for symbol in symbols if symbol not in saved]
        logger.warning("取得できなかった銘柄: %s", failed)


if __name__ == "__main__":
    main()
