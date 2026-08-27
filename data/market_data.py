"""株価データ・オプションチェーン情報の取得と前処理。"""

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional, Tuple

import pandas as pd
from ib_async import IB, Contract, Forex, Stock
from ib_async import util as ib_util

from core.market_hours import US_EASTERN
from core.pacing import RequestPacer

logger = logging.getLogger(__name__)

# ヒストリカルデータのリクエストは、このモジュールを経由するものすべてが
# 同じペーサーを共有する。IBKRの制限はクライアント単位で課されるため、
# 呼び出し箇所(メインループ・スクリーナー・バックテスト)ごとに分けては意味がない。
_historical_pacer = RequestPacer()


def get_historical_pacer() -> RequestPacer:
    """ヒストリカルデータ用の共有ペーサーを返す（テスト・監視用）。"""
    return _historical_pacer

# 現在価格を取得する際、ストリーミング購読(reqMktData)を待つ最大秒数と
# その間のポーリング間隔。リアルタイムデータが配信されていれば通常1秒未満で
# ティックが届くため、この上限まで待つのは「データが来ない」ケースのみ。
STREAMING_TIMEOUT_SECONDS: float = 4.0
STREAMING_POLL_INTERVAL_SECONDS: float = 0.5

# ヒストリカルバーで現在価格を代替する際のリクエストパラメータ。
# 直近の営業日の終値が取れれば十分なので短い期間で足りる。
_FALLBACK_BAR_DURATION: str = "5 D"
_FALLBACK_BAR_SIZE: str = "1 day"

# 為替(Forex)コントラクトには出来高を伴う「取引」が無いため、
# reqHistoricalDataのwhatToShowにTRADESを指定するとバーが返らない。
_FOREX_SEC_TYPE: str = "CASH"
_FOREX_WHAT_TO_SHOW: str = "MIDPOINT"

PRICE_SOURCE_STREAMING: str = "streaming"
PRICE_SOURCE_SNAPSHOT: str = "snapshot"
PRICE_SOURCE_HISTORICAL: str = "historical"


@dataclass(frozen=True)
class PriceQuote:
    """現在価格と、その値がどこから来たか・当日のものかを併せて表す。

    価格だけを返していると、フォールバック連鎖の下位（ティッカーのclose、
    ヒストリカル最終終値）が返した**前営業日の値**を「現在価格」として
    受け取ってしまい、呼び出し側から区別できない。注文の値段はこの価格を
    基準に算出されるため、古い値を掴んだまま発注すると、値段の妥当性検証
    (order_manager.MAX_ORDER_PRICE_DEVIATION_PCT)も参照価格自体がずれている
    以上すり抜ける。
    """

    price: float
    source: str
    # 当日の取引セッション由来でないと判定された値かどうか。
    is_stale: bool


async def qualify_stock_async(
    ib: IB,
    symbol: str,
    exchange: str = "SMART",
    currency: str = "USD",
) -> Stock:
    contract = Stock(symbol, exchange, currency)
    qualified = await ib.qualifyContractsAsync(contract)
    if not qualified:
        raise ValueError(f"シンボル {symbol} のコントラクト特定に失敗しました。")
    logger.info("コントラクトを特定しました: %s", qualified[0])
    return qualified[0]


def _to_usable_price(value: object) -> Optional[float]:
    """ティッカーの各フィールドを、価格として採用できる場合のみfloatで返す。

    IBKRは未取得のフィールドをNaNで埋めてくるため、NaN・None・非正数は
    すべて「欠測」として扱う。数値型以外（モックオブジェクト等）も弾く。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    price = float(value)
    if math.isnan(price) or price <= 0:
        return None
    return price


def _extract_ticker_price(ticker: object) -> Optional[Tuple[float, bool]]:
    """Tickerから (価格, 古い値か) を取り出す（marketPrice優先、無ければclose）。

    ib_asyncのmarketPrice()は「bid/askの範囲内にあるlast、無ければ仲値」を返すが、
    どちらも未取得ならNaNになるため、その場合は前日終値(close)にフォールバックする。

    Tickerのcloseは名前のとおり**前営業日の終値**であり、当日の値動きを
    まったく反映しない。marketPrice()が取れなかった＝ティックが届いていない
    ということなので、この経路に落ちた時点で値は古いものとして扱う。
    """
    try:
        price = _to_usable_price(ticker.marketPrice())
    except Exception:  # marketPrice()自体が失敗するケース（データ未受信時等）
        price = None
    if price is not None:
        return price, False

    close = _to_usable_price(getattr(ticker, "close", None))
    if close is None:
        return None
    return close, True


def _ticker_update_time(ticker: object) -> Optional[datetime]:
    """Tickerの最終更新時刻を返す。datetimeとして読めなければNone。

    ib_asyncのTickerは更新のたびにtimeを進める。型が読めない場合
    （モックや将来の仕様変更）にNoneを返すのは、鮮度を判定できないことを
    呼び出し側で「判定しない」に倒すため。
    """
    value = getattr(ticker, "time", None)
    return value if isinstance(value, datetime) else None


def _to_bar_date(value: object) -> Optional[date]:
    """バーの日付欄をdateへ正規化する。解釈できなければNone。

    ib_asyncのDataFrame変換はbarSizeに応じてdate/datetime/文字列のいずれも
    返しうるため、鮮度判定の前にここで型を吸収する。
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return None
    try:
        return pd.Timestamp(value).date()
    except (TypeError, ValueError):
        return None


def drop_unconfirmed_today_bars(
    bars: pd.DataFrame, now: Optional[datetime] = None,
) -> pd.DataFrame:
    """当日（米国東部時間）の日付を持つ末尾のバーを取り除く。

    取引時間中のIBKR日足には**まだ確定していない当日のバー**が並ぶ（寄り付き
    直後を除く。CLAUDE.md「価格の鮮度」の実測表を参照）。この行の終値は
    現在値と一緒に動くため、シグナル判定に含めると次の2つの問題が出る:

    - 日足を取引日単位でキャッシュする前提（DailyBarCache）が崩れる。その日
      最初のサイクルで取得した中途半端な値が、確定値であるかのように1日中使われる
    - 確定した終値で判定しているバックテストと条件が揃わない

    日付が読めない行は残す。判別できないものを消すと、鮮度の判定が
    できていないことに気付けないまま本数だけが減る。
    """
    if bars.empty or "date" not in bars.columns:
        return bars

    reference = now if now is not None else datetime.now(US_EASTERN)
    today = reference.astimezone(US_EASTERN).date()

    keep = [_to_bar_date(value) != today for value in bars["date"]]
    if all(keep):
        return bars
    return bars.loc[keep].reset_index(drop=True)


async def _get_streaming_price_async(
    ib: IB, contract: Contract, timeout_seconds: float,
) -> Optional[Tuple[float, bool]]:
    """reqMktData(snapshot=False)のストリーミング購読から現在価格を取得する。

    スナップショット要求(reqTickersAsync)と違い、遅延データ
    (reqMarketDataType(3))でも配信されるため、リアルタイムデータの購読契約が
    無いペーパー口座ではこちらが本命の経路になる。
    """
    try:
        ticker = ib.reqMktData(contract, "", False, False)
    except Exception:
        logger.exception("%s のストリーミング購読の開始に失敗しました。", contract.symbol)
        return None

    # ib_asyncは同じコントラクトに対して**同一のTickerオブジェクト**を返し、
    # cancelMktDataの後もそこには前回の購読で受け取った値が残る。購読直後に
    # そのまま読むと、前のサイクルで取得した価格を「現在価格」として返してしまい、
    # 以後どれだけ市場が動いても値が更新されない（実測: 決済判定が10分以上
    # 同じ価格で回り続けた）。購読前の更新時刻を控えておき、**新しいティックが
    # 届いたことを確認してから**採用する。
    previous_update = _ticker_update_time(ticker)

    try:
        # ティックが既に届いていることもあるため、待機の前に1度確認する。
        # timeout_seconds=0 なら待機せず即時判定のみ行う。
        polls = max(int(timeout_seconds / STREAMING_POLL_INTERVAL_SECONDS), 0) + 1
        for attempt in range(polls):
            if attempt > 0:
                await asyncio.sleep(STREAMING_POLL_INTERVAL_SECONDS)
            if previous_update is not None:
                current_update = _ticker_update_time(ticker)
                if current_update is None or current_update <= previous_update:
                    continue
            extracted = _extract_ticker_price(ticker)
            if extracted is not None:
                return extracted

        # 使い回しのTickerに値は残っているが更新が来ない場合、その値をそのまま
        # 返すと古い価格を現在価格として扱うことになる。Noneを返して下位の経路
        # （スナップショット・ヒストリカル）へ譲る方が、値の出所がはっきりする。
        if previous_update is not None:
            logger.warning(
                "%s のストリーミング購読に新しいティックが %.1f 秒以内に届きませんでした。"
                "前回取得した価格は古い可能性があるため採用しません。",
                contract.symbol, timeout_seconds,
            )
        return None
    finally:
        try:
            ib.cancelMktData(contract)
        except Exception:
            # **DEBUGで消してはならない。** 解除に失敗した購読は張りっぱなしに
            # なり、積み上がるとIBKRの同時購読数上限を食い潰す（「6.4」）。
            # そうなると症状は「価格が取れない銘柄が増える」で、原因が
            # ここだと分かる手掛かりは1行も残らない。
            logger.warning(
                "%s のストリーミング購読を解除できませんでした。"
                "積み上がるとIBKRの同時購読数の上限に達します。",
                contract.symbol, exc_info=True,
            )


async def _get_snapshot_price_async(ib: IB, contract: Contract) -> Optional[Tuple[float, bool]]:
    """reqTickersAsync（内部でsnapshot=Trueを使う）から現在価格を取得する。

    IBKRはスナップショット要求に遅延データを適用しないため、リアルタイム
    データの購読契約が無い口座では失敗する。契約がある場合は1往復で済み
    ストリーミングより速いため、フォールバック連鎖には残してある。
    """
    try:
        tickers = await ib.reqTickersAsync(contract)
    except Exception:
        logger.exception("%s のスナップショット取得に失敗しました。", contract.symbol)
        return None

    if not tickers:
        return None
    return _extract_ticker_price(tickers[0])


async def _get_last_close_price_async(
    ib: IB, contract: Contract, now: Optional[datetime] = None,
) -> Optional[Tuple[float, bool]]:
    """ヒストリカルバーの最終終値を現在価格の代わりに使う。

    マーケットデータの購読権限が全く無い口座でも、ヒストリカルデータだけは
    取得できることが多いため、最後の砦として用意している。ただし値は
    リアルタイムではない（直近の営業日の終値）点に注意。

    最終バーの日付が米国東部時間の今日と一致するかで鮮度を判定する。取引時間中は
    当日の（まだ確定していない）バーが最後に並ぶため、一致しなければ当日のデータが
    取れていない＝休場明けや連休明けに前営業日の終値を掴んだ状態を意味する。

    日付が読めない場合は**古いものとして扱う**。分からないものを新しい側に
    倒すと、鮮度検証が黙って素通しになったことに気付けない。
    """
    what_to_show = (
        _FOREX_WHAT_TO_SHOW if getattr(contract, "secType", None) == _FOREX_SEC_TYPE else "TRADES"
    )
    try:
        df = await get_historical_bars_async(
            ib, contract,
            duration=_FALLBACK_BAR_DURATION,
            bar_size=_FALLBACK_BAR_SIZE,
            what_to_show=what_to_show,
        )
    except Exception:
        logger.exception("%s のヒストリカルバーによる価格取得に失敗しました。", contract.symbol)
        return None

    if df.empty or "close" not in df.columns:
        return None

    price = _to_usable_price(df["close"].iloc[-1])
    if price is None:
        return None

    today = (now.astimezone(US_EASTERN) if now is not None else datetime.now(US_EASTERN)).date()
    bar_date = _to_bar_date(df["date"].iloc[-1]) if "date" in df.columns else None
    if bar_date is None:
        logger.warning(
            "%s のヒストリカルバーに解釈できる日付が無いため、価格を古いものとして扱います。",
            contract.symbol,
        )
        return price, True

    return price, bar_date != today


async def get_current_price_quote_async(
    ib: IB,
    contract: Contract,
    streaming_timeout_seconds: float = STREAMING_TIMEOUT_SECONDS,
    allow_historical_fallback: bool = True,
    now: Optional[datetime] = None,
) -> Optional[PriceQuote]:
    """現在価格を、取得経路と鮮度の判定を添えて返す。

    経路の順序と意味は get_current_price_async のdocstringを参照。
    値段を新規に決める用途（発注の参照価格など）では、価格だけを返す
    get_current_price_async ではなくこちらを使い、is_stale を必ず見ること。
    """
    extracted = await _get_streaming_price_async(ib, contract, streaming_timeout_seconds)
    source = PRICE_SOURCE_STREAMING

    if extracted is None:
        extracted = await _get_snapshot_price_async(ib, contract)
        source = PRICE_SOURCE_SNAPSHOT

    if extracted is None and allow_historical_fallback:
        extracted = await _get_last_close_price_async(ib, contract, now=now)
        source = PRICE_SOURCE_HISTORICAL

    if extracted is None:
        logger.warning(
            "%s の現在価格をどの経路でも取得できませんでした"
            "（ストリーミング/スナップショット/ヒストリカル）。",
            contract.symbol,
        )
        return None

    price, is_stale = extracted
    if is_stale:
        logger.warning(
            "%s の現在価格 %s は当日のものではない可能性があります（経路: %s）。",
            contract.symbol, price, source,
        )
    else:
        logger.info("%s の現在価格: %s (%s)", contract.symbol, price, source)

    return PriceQuote(price=price, source=source, is_stale=is_stale)


async def get_current_price_async(
    ib: IB,
    contract: Contract,
    streaming_timeout_seconds: float = STREAMING_TIMEOUT_SECONDS,
    allow_historical_fallback: bool = True,
) -> Optional[float]:
    """現在価格を取得する。取得経路を順に試し、最初に成功したものを返す。

    口座のマーケットデータ購読状況によって使える経路が変わるため、
    単一の経路に依存せずフォールバックさせる:

        1. ストリーミング (reqMktData)   … 遅延データでも配信される
        2. スナップショット (reqTickers) … リアルタイム購読契約が要る
        3. ヒストリカル終値              … 購読権限が無くても取れることが多い

    Args:
        streaming_timeout_seconds: 1のストリーミングでティックを待つ秒数。
            0を指定すると待機せず即時判定のみ行う（テスト用）。
        allow_historical_fallback: 3を試すか。ヒストリカルデータのリクエストは
            IBKRのペーシング制限（10分あたり60件）を消費するため、多数の銘柄を
            高頻度でポーリングする場合はFalseにして呼び出し側で制御すること。

    **この関数は鮮度を捨てる。** 下位の経路は前営業日の終値を返しうるが、
    戻り値がfloatだけなので呼び出し側から区別できない。発注の参照価格のように
    「古い値だと困る」用途では get_current_price_quote_async を使うこと。

    Returns:
        取得できた価格。すべての経路が失敗した場合はNone。
    """
    quote = await get_current_price_quote_async(
        ib, contract,
        streaming_timeout_seconds=streaming_timeout_seconds,
        allow_historical_fallback=allow_historical_fallback,
    )
    return quote.price if quote is not None else None


async def get_usd_jpy_rate_async(
    ib: IB,
    streaming_timeout_seconds: float = STREAMING_TIMEOUT_SECONDS,
    allow_historical_fallback: bool = True,
) -> Optional[float]:
    """USD/JPYの現在レートを取得する。

    決済損益を確定申告向けに円換算する際にtrade_journal.TradeRecord.usd_jpy_rate
    として記録するためのもの。ForexコントラクトはStockと異なり曖昧さがないため
    qualifyContractsAsyncは不要。
    """
    return await get_current_price_async(
        ib, Forex("USDJPY"),
        streaming_timeout_seconds=streaming_timeout_seconds,
        allow_historical_fallback=allow_historical_fallback,
    )


# ib_async の reqHistoricalDataAsync の既定タイムアウト。稼働中の取得
# （日足300本・日中足2日分）はこれで十分に収まる。
DEFAULT_HISTORICAL_TIMEOUT_SECONDS: float = 60.0


async def get_historical_bars_async(
    ib: IB,
    contract: Contract,
    duration: str = "60 D",
    bar_size: str = "1 day",
    what_to_show: str = "TRADES",
    timeout: float = DEFAULT_HISTORICAL_TIMEOUT_SECONDS,
) -> pd.DataFrame:
    """ヒストリカルバーを取得する。

    **タイムアウトすると例外ではなく空のバー列が返る**（ib_asyncが要求を
    取り消し、IBKRが `Error 162 API historical data query cancelled` を
    返す）。ペーシング違反と同じく呼び出し側からは「データが無い銘柄」と
    区別がつかないため、重い取得では `timeout` を延ばすこと——1年ぶんの
    5分足（約19,500本）は既定の60秒に収まらない銘柄が多い（2026-08-13に
    実測。42銘柄中41銘柄がちょうど60秒で空を返し、INTCだけが間に合った）。
    """
    # IBKRの「10分あたり60件」制限を超えると、例外ではなく空のバー列が返るため
    # 呼び出し側から違反を検知できない。発行前に必ず枠を確保する。
    await _historical_pacer.acquire()

    bars = await ib.reqHistoricalDataAsync(
        contract,
        endDateTime="",
        durationStr=duration,
        barSizeSetting=bar_size,
        whatToShow=what_to_show,
        useRTH=True,
        formatDate=1,
        timeout=timeout,
    )
    df: pd.DataFrame = ib_util.df(bars)
    if df is None:
        return pd.DataFrame()
    logger.info("%s のヒストリカルバーを%d件取得しました。", contract.symbol, len(df))
    return df


# IBKRのreqHistoricalDataが受け付ける日中足のbarSizeSetting一覧。
# デイトレードのシグナル判定はこのいずれかの短期足を使うこと（日足は不可）。
INTRADAY_BAR_SIZES: frozenset = frozenset(
    {
        "1 secs", "5 secs", "10 secs", "15 secs", "30 secs",
        "1 min", "2 mins", "3 mins", "5 mins", "10 mins", "15 mins", "20 mins", "30 mins",
        "1 hour", "2 hours", "3 hours", "4 hours", "8 hours",
    }
)


async def get_intraday_bars_async(
    ib: IB,
    contract: Stock,
    duration: str = "2 D",
    bar_size: str = "5 mins",
    what_to_show: str = "TRADES",
    timeout: float = DEFAULT_HISTORICAL_TIMEOUT_SECONDS,
) -> pd.DataFrame:
    """デイトレード向けの短期足（分足・秒足・時間足）を取得する。

    IBKRは日中足に対してリクエストできる期間(duration)に制限があるため、
    swing向けの `get_historical_bars_async` のデフォルト(60 D/1 day)とは
    別関数として分離している。
    """
    if bar_size not in INTRADAY_BAR_SIZES:
        raise ValueError(
            f"bar_size は日中足である必要があります（指定値: {bar_size}）。"
            f"利用可能な値: {sorted(INTRADAY_BAR_SIZES)}"
        )

    return await get_historical_bars_async(
        ib, contract, duration=duration, bar_size=bar_size, what_to_show=what_to_show,
        timeout=timeout,
    )
