"""時価総額・PERによる割安株スクリーニング。

IBKRスキャナーで時価総額により母集団を絞り込み、各候補のPERを取得して
さらにフィルタする2段階構成。先に時価総額で母集団を絞ってからPERを取得
するのは、reqFundamentalDataAsyncを候補全銘柄へ個別リクエストする
コスト（レート制限・往復時間）を抑えるため。

このスクリーニングは過去時点のPER（point-in-timeデータ）をIBKR経由で
遡って取得できないため、backtest/ パッケージでは検証できない。ライブの
ドライラン運用で結果を確認しながら閾値を調整すること。

長期トレンドフィルター（trend_ma_window日移動平均割れの銘柄を除外）について:
S&P500構成銘柄相当75銘柄・447トレードでのプルバック戦略の実データ検証で、
「長期的に明確な下降トレンドにある銘柄」ほどprofit_factorが低い、という
弱いが有意な相関（200日MA上抜け率との相関+0.18、5年リターンとの相関+0.16）
が確認された。相関自体は弱く単独で収益性を保証するものではないため、
（PERのような）ハードな足切りではなく、明確な下降トレンドの銘柄だけを
除外する緩やかな追加フィルターとして扱う。
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import List, Optional

import pandas as pd
from ib_async import IB, Stock

from data.fundamentals import get_pe_ratio_async, run_market_cap_scan_async
from data.market_data import get_historical_bars_async

logger = logging.getLogger(__name__)


@dataclass
class ScreenerConfig:
    market_cap_above: float = 2_000_000_000.0
    market_cap_below: float = 200_000_000_000.0
    max_pe_ratio: float = 15.0
    scan_code: str = "MOST_ACTIVE"
    number_of_rows: int = 50
    # 最大50銘柄分のreqFundamentalDataAsyncを間隔調整なしで連続発行すると
    # IBKR側のペーシング制限に抵触しうるため、リクエスト間に挟む待機秒数。
    pe_request_interval_seconds: float = 1.0
    # 長期トレンドフィルター: 明確な下降トレンド（終値がtrend_ma_window日移動平均を
    # 下回っている）の銘柄を除外する。効果が弱いことが分かった場合に即座に
    # 無効化できるよう、フラグで切り替え可能にしている。
    enable_trend_filter: bool = True
    trend_ma_window: int = 200
    # trend_ma_window日分の移動平均を計算するには、それ以上の期間の
    # ヒストリカルバーが要る（土日・休場日を考慮し余裕を持たせる）。
    trend_lookback_duration: str = "300 D"
    # PERデータの取得失敗（reqFundamentalDataAsyncが空/例外）が何銘柄連続で
    # 続いたら処理を打ち切るか。ファンダメンタルズデータの購読権限が
    # 無い口座では全候補が例外なく失敗し続けるため、その場合は残り候補分の
    # ペーシング待機を無駄に消費せず早期にフォールバックへ委ねる。
    # 個別銘柄がたまたまPERデータを持たないだけの単発ケースと区別するため、
    # 1件ではなく複数件の連続失敗を条件にしている。
    max_consecutive_pe_failures: int = 5
    # 買える上限株価(USD)。Noneなら無効。
    # リスクベースのサイジングでは 数量 = floor((資金×リスク%) ÷ (株価×損切り%))
    # なので、株価が「資金 × (リスク% ÷ 損切り%)」を超えると数量が0株になり、
    # シグナルが出ても永久に発注できない。この銘柄がウォッチリストに残ると、
    # 毎サイクルの日中足リクエスト（＝ペーシング制限の枠）を消費したうえで
    # 必ずスキップされる。監視できる銘柄数は10件しかないため、
    # 買えない銘柄に枠を割く余裕は無い。
    max_price: Optional[float] = None
    # 買える下限株価(USD)。Noneなら無効。
    # 上限とは逆に、株価が安すぎる銘柄は order_manager.MAX_POSITION_SIZE の
    # 株数クランプに掛かり、リスクベースのサイジングが効かなくなる。
    # 数量が上限に達する条件は 株価 < 資金 × (リスク% ÷ 損切り%) ÷ MAX_POSITION_SIZE
    # で、実測($1,220・10株)では $24.40 未満が該当する。
    # クランプが掛かると建玉が小さくなる分だけ1注文あたりの最低手数料($0.35)の
    # 比率が跳ね上がり（JOBY $7.05 の実測で往復0.29% -> 0.99%）、
    # 「検証時の初期資金」節のバックテスト結果が実運用に当てはまらなくなる。
    min_price: Optional[float] = None


def is_in_long_term_uptrend(df: pd.DataFrame, ma_window: int) -> Optional[bool]:
    """終値がma_window日移動平均を上回っているか判定する。

    データ不足で判定できない場合はNoneを返す（除外せず素通しする）。
    """
    if len(df) < ma_window:
        return None

    latest_close = float(df["close"].iloc[-1])
    moving_average = float(df["close"].iloc[-ma_window:].mean())
    return latest_close > moving_average


def _latest_close(df: pd.DataFrame) -> Optional[float]:
    """直近の終値を返す。取れない場合はNone（判定不能）。"""
    if df.empty or "close" not in df.columns:
        return None

    latest_close = float(df["close"].iloc[-1])
    if latest_close <= 0:
        return None
    return latest_close


async def _passes_bar_based_filters_async(
    ib: IB, contract: Stock, config: ScreenerConfig,
) -> bool:
    """日足バーを1回だけ取得し、株価上限と長期トレンドの両方を判定する。

    2つの条件で別々にバーを取得するとIBKRへのリクエストが倍になるため、
    同じバーを使い回す。どちらも無効なら取得自体を行わない。
    """
    price_filter_enabled = config.max_price is not None or config.min_price is not None
    if not config.enable_trend_filter and not price_filter_enabled:
        return True

    daily_df = await get_historical_bars_async(
        ib, contract, duration=config.trend_lookback_duration, bar_size="1 day",
    )

    if price_filter_enabled:
        latest_close = _latest_close(daily_df)
        if latest_close is None:
            # 株価が分からない銘柄は除外に倒す。素通しすると、買えない銘柄が
            # ウォッチリストの枠とペーシング枠を占め続けても気付けない。
            logger.info(
                "[%s] 株価が取得できなかったため、買える銘柄か判定できず除外します。",
                contract.symbol,
            )
            return False
        if config.max_price is not None and latest_close > config.max_price:
            logger.info(
                "[%s] 株価(%.2f USD)が上限(%.2f USD)を超えるため除外しました"
                "（現在の口座資金では数量が0株になる）。",
                contract.symbol, latest_close, config.max_price,
            )
            return False
        if config.min_price is not None and latest_close < config.min_price:
            logger.info(
                "[%s] 株価(%.2f USD)が下限(%.2f USD)を下回るため除外しました"
                "（MAX_POSITION_SIZEの株数クランプでリスクベースのサイジングが効かず、"
                "手数料比率が跳ね上がる）。",
                contract.symbol, latest_close, config.min_price,
            )
            return False

    if config.enable_trend_filter:
        in_uptrend = is_in_long_term_uptrend(daily_df, config.trend_ma_window)
        if in_uptrend is None:
            logger.info(
                "[%s] 長期トレンド判定に必要なデータが不足しているため、フィルターを素通りさせます。",
                contract.symbol,
            )
            return True
        if not in_uptrend:
            logger.info(
                "[%s] 長期トレンドフィルターで除外しました(%d日移動平均割れ)。",
                contract.symbol, config.trend_ma_window,
            )
            return False

    return True


async def screen_value_stocks_async(ib: IB, config: ScreenerConfig) -> List[str]:
    if config.max_pe_ratio <= 0:
        raise ValueError("max_pe_ratio は正の値である必要があります。")

    candidates = await run_market_cap_scan_async(
        ib,
        market_cap_above=config.market_cap_above,
        market_cap_below=config.market_cap_below,
        scan_code=config.scan_code,
        number_of_rows=config.number_of_rows,
    )

    # IBKRへの個別リクエスト(PER取得・トレンド判定用ヒストリカルバー取得)の
    # 発行回数を数え、最初の1件目を除いて毎回間隔を空ける(reqFundamentalDataAsync
    # とreqHistoricalDataAsyncが混在しても、発行するIBKRリクエスト全体でペースを揃える)。
    request_count = 0

    async def _pace() -> None:
        nonlocal request_count
        if request_count > 0 and config.pe_request_interval_seconds > 0:
            await asyncio.sleep(config.pe_request_interval_seconds)
        request_count += 1

    selected: List[str] = []
    consecutive_pe_failures = 0

    for index, contract in enumerate(candidates):
        # 1銘柄あたりのIBKRリクエスト（PER取得・トレンド判定）が一時的な切断や
        # ペーシング制限違反で失敗しても、その銘柄をスキップするだけに留める。
        # ここで例外を伝播させると、呼び出し元(main._refresh_watchlist_async)は
        # スクリーニング全体を失敗扱いにして当日の結果を丸ごと破棄してしまい、
        # 他の候補銘柄まで無駄になる。
        pe_fetch_failed = False
        try:
            await _pace()
            pe_ratio = await get_pe_ratio_async(ib, contract)

            if pe_ratio is None:
                pe_fetch_failed = True
            elif not (0 < pe_ratio <= config.max_pe_ratio):
                pass  # 赤字・割高による通常の除外。購読権限の問題ではない。
            elif (
                config.enable_trend_filter
                or config.max_price is not None
                or config.min_price is not None
            ):
                await _pace()
                if await _passes_bar_based_filters_async(ib, contract, config):
                    selected.append(contract.symbol)
            else:
                selected.append(contract.symbol)
        except Exception:
            pe_fetch_failed = True
            logger.exception(
                "[%s] スクリーニング処理中にエラーが発生したため、この銘柄をスキップします。",
                contract.symbol,
            )

        consecutive_pe_failures = consecutive_pe_failures + 1 if pe_fetch_failed else 0

        if consecutive_pe_failures >= config.max_consecutive_pe_failures:
            remaining = len(candidates) - index - 1
            logger.error(
                "PERデータの取得が%d銘柄連続で失敗しました。ファンダメンタルズデータの購読権限が"
                "無い可能性があるため、残り%d件の候補の処理を打ち切り、既存のウォッチリストに"
                "フォールバックします。",
                consecutive_pe_failures, remaining,
            )
            break

    logger.info(
        "割安株スクリーニング完了: 候補=%d件 -> PER<=%.1f・長期トレンド条件で%d件選定 %s",
        len(candidates), config.max_pe_ratio, len(selected), selected,
    )
    return selected
