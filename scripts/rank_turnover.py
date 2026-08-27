"""売買代金の日次ランキングを記録する（yfinance経由、IBKR接続不要）。

**観測専用のツールである。** 出力した `logs/turnover_ranks.json` は
`strategy/attention.py` の急上昇判定に食わせられるが、ボット本体は
`main.ENABLE_ATTENTION_WATCHLIST` が False の間これを読まない。
「急上昇と判定された銘柄がその後どう動くか」を、監視銘柄を実際に
入れ替える前に見るためのものである。

**IBKRのスキャナーの代わりにはならない。** Yahooには「全米国株を売買代金順に
並べる」入口が無いため、ここで得られるのは**あらかじめ決めたユニバースの中での
順位**でしかない。ユニバースに入れていない銘柄は、どれだけ売買代金が急増しても
見つからない。取引所全体から発見したい場合はIBKRのスキャナー
(`data.fundamentals.run_turnover_scan_async`) が要る。

yfinanceはYahoo Financeの非公開エンドポイントを叩く非公式ライブラリで、
契約もSLAも無い。仕様変更で動かなくなることが繰り返し起きているため、
**取得できた銘柄数が想定を大きく下回ったら履歴を更新せずに終了する**
（中途半端なランキングを書くと、翌日以降の基準順位が壊れる）。
`requirements-dev.txt` にのみ含まれる検証用の依存であり、ボット本体は
これに依存しない。

実行方法:
    python -m scripts.rank_turnover                      # universe.txt を使う
    python -m scripts.rank_turnover --top 100
    python -m scripts.rank_turnover --universe-file my_universe.txt
"""

import argparse
import logging
import sys
from datetime import datetime
from typing import Dict, List, Optional


from core.market_hours import US_EASTERN
from data.rank_history import DEFAULT_RANK_HISTORY_PATH, RankHistoryStore
from strategy.attention import AttentionConfig, build_rank_map, detect_rank_surges, has_enough_history

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("rank_turnover")

DEFAULT_UNIVERSE_FILE: str = "universe.txt"
DEFAULT_TOP: int = 100

# 取得できた銘柄がユニバースのこの割合を下回ったら、履歴を更新しない。
# yfinanceが壊れたときの典型的な症状は「一部または全部が空で返る」であり、
# そのまま順位を付けると、実際には売買代金が落ちていない銘柄が
# ランク外に落ちたことになって翌日に急上昇を誤検知する。
MIN_FETCH_RATIO: float = 0.8


def load_universe(path: str) -> List[str]:
    """1行1銘柄のユニバースを読む。空行と # のコメント行は無視する。"""
    with open(path, "r", encoding="utf-8") as f:
        symbols = [line.strip().upper() for line in f]
    return [s for s in symbols if s and not s.startswith("#")]


def compute_turnover(symbols: List[str], lookback_days: int = 7) -> Dict[str, float]:
    """銘柄ごとの直近営業日の売買代金（終値 × 出来高）を返す。

    取得できなかった銘柄・出来高や終値が欠けている銘柄は結果に含めない
    （0として扱うと「売買代金が最下位の銘柄」として順位に入ってしまう）。
    """
    import yfinance as yf

    frame = yf.download(
        tickers=symbols,
        period=f"{lookback_days}d",
        interval="1d",
        group_by="ticker",
        auto_adjust=False,
        progress=False,
        threads=True,
    )
    if frame is None or frame.empty:
        return {}

    turnover: Dict[str, float] = {}
    for symbol in symbols:
        try:
            bars = frame[symbol] if len(symbols) > 1 else frame
        except KeyError:
            continue
        bars = bars.dropna(subset=["Close", "Volume"])
        if bars.empty:
            continue

        close = float(bars["Close"].iloc[-1])
        volume = float(bars["Volume"].iloc[-1])
        if close <= 0 or volume <= 0:
            continue
        turnover[symbol] = close * volume

    return turnover


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe-file", default=DEFAULT_UNIVERSE_FILE)
    parser.add_argument("--top", type=int, default=DEFAULT_TOP,
                        help="記録する上位件数（IBKRのスキャナーが返す件数に合わせて既定100）")
    parser.add_argument("--history-path", default=DEFAULT_RANK_HISTORY_PATH)
    parser.add_argument("--dry-run", action="store_true",
                        help="順位を表示するだけで履歴を更新しない")
    args = parser.parse_args(argv)

    universe = load_universe(args.universe_file)
    if not universe:
        logger.error("ユニバースが空です: %s", args.universe_file)
        return 1
    logger.info("ユニバース%d銘柄の売買代金を取得します。", len(universe))

    turnover = compute_turnover(universe)
    fetched_ratio = len(turnover) / len(universe)
    if fetched_ratio < MIN_FETCH_RATIO:
        logger.error(
            "売買代金を取得できたのは%d/%d銘柄(%.0f%%)で、下限(%.0f%%)を下回りました。"
            "履歴は更新しません（欠けたまま順位を付けると、翌日以降の基準が壊れます）。"
            "yfinanceの仕様変更やネットワーク障害を疑ってください。",
            len(turnover), len(universe), fetched_ratio * 100, MIN_FETCH_RATIO * 100,
        )
        return 1

    ordered = [
        symbol for symbol, _ in
        sorted(turnover.items(), key=lambda item: item[1], reverse=True)
    ][:args.top]
    ranks = build_rank_map(ordered)

    logger.info(
        "売買代金の上位10件: %s",
        [(s, f"{turnover[s] / 1e6:.0f}M USD") for s in ordered[:10]],
    )

    store = RankHistoryStore(args.history_path)
    history = store.load()

    config = AttentionConfig()
    if has_enough_history(history, config):
        surges = detect_rank_surges(ranks, history, config)
        if surges:
            logger.info(
                "急上昇と判定された銘柄（観測のみ・監視リストは変更しません）: %s",
                [(s, f"{ranks[s]}位") for s in surges],
            )
        else:
            logger.info("急上昇と判定された銘柄はありません。")
    else:
        logger.info(
            "履歴が%d日ぶんしかないため、急上昇の判定は見送ります"
            "（基準順位が確定するまでは単なる上位銘柄と区別できません）。",
            len(history),
        )

    if args.dry_run:
        logger.info("--dry-run のため履歴を更新しませんでした。")
        return 0

    trading_day = datetime.now(US_EASTERN).date().isoformat()
    updated = store.append(trading_day, ranks)
    logger.info(
        "%s のランキングを記録しました: %d銘柄（履歴%d日ぶん）-> %s",
        trading_day, len(ranks), len(updated), args.history_path,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
