"""エントリーポイント: 非同期イベントループの起動と全体のオーケストレーション。"""

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd
from ib_insync import IB, Stock

from core.connection import IBKRConnection
from core.market_hours import US_EASTERN, is_day_trade_flatten_time, is_regular_trading_hours
from data.cache import ContractCache, DailyBarCache
from data.market_data import (
    get_current_price_async,
    get_current_price_quote_async,
    get_intraday_bars_async,
    get_usd_jpy_rate_async,
)
from execution.account import get_account_equity_async, get_settled_cash_async
from execution.order_manager import (
    cancel_dry_run_bracket_orders_async,
    place_dry_run_bracket_order_async,
    place_dry_run_order_async,
)
from execution.position_manager import (
    DEFAULT_STATE_PATH,
    Position,
    PositionManager,
    STRATEGY_TYPE_DAY,
    STRATEGY_TYPE_SWING,
)
from execution.position_sizing import calculate_position_size
from execution.trade_journal import TradeJournal
from strategy.exit_signal import (
    REASON_EOD_FLATTEN,
    detect_exit_signal,
    detect_resting_order_exit,
    resolve_stop_price,
    resolve_take_profit_price,
)
from strategy.pullback import (
    MarketFilterConfig,
    SignalResult,
    compute_deviation_pct,
    detect_pullback_signal,
)
from strategy.screener import ScreenerConfig, screen_value_stocks_async

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# フォールバック用の固定ウォッチリスト。銘柄選定は本来スクリーニング
# （時価総額+PER）で毎日動的に決定するが、起動直後の初回スクリーニング前や
# スクリーニング失敗時にはこのリストで動作を継続する。
WATCHLIST: List[str] = ["RIVN", "JOBY", "AAPL", "MSFT", "JNJ", "JPM", "KO", "XOM"]

# ファンダメンタルズスクリーニング（割安株抽出）のパラメータ。
# 1日1回（取引時間の最初のサイクル）だけ実行し、ウォッチリストを入れ替える。
SCREENER_MIN_MARKET_CAP: float = 2_000_000_000.0
SCREENER_MAX_MARKET_CAP: float = 200_000_000_000.0
SCREENER_MAX_PE_RATIO: float = 15.0
SCREENER_SCAN_CODE: str = "MOST_ACTIVE"
SCREENER_NUM_CANDIDATES: int = 50
# スクリーニング結果から実際に監視する銘柄数の上限。
# 監視銘柄1件につき毎サイクル1回の日中足リクエストが発生するため、
# ここを絞ることがIBKRのペーシング制限(10分あたり60件)対策の要になる。
# 目安: 監視銘柄数 <= POLL_INTERVAL_SECONDS / 10 なら制限内に収まる。
MAX_WATCHLIST_SIZE: int = 10
# IBKRのペーシング制限を避けるため、PER取得(reqFundamentalDataAsync)を
# 連続発行せずこの秒数だけ間隔を空ける
SCREENER_PE_REQUEST_INTERVAL_SECONDS: float = 1.0
# 長期トレンドフィルター: 明確な下降トレンド(200日移動平均割れ)の銘柄を除外する。
# 75銘柄・447トレードの実データ検証で、長期トレンドと平均回帰戦略の
# profit_factorに弱い正の相関(+0.16〜+0.18)が確認されたための追加条件。
SCREENER_ENABLE_TREND_FILTER: bool = True
SCREENER_TREND_MA_WINDOW: int = 200
SCREENER_TREND_LOOKBACK_DURATION: str = "300 D"

# 監視ループのポーリング間隔（秒）: 市場時間中/時間外で切り替える。
# 市場時間中の間隔は、IBKRのヒストリカルデータ制限(10分あたり60件)から逆算して
# 決めている。監視銘柄1件あたり毎サイクル1リクエスト(日中足)なので、
#     MAX_WATCHLIST_SIZE * (600 / POLL_INTERVAL_SECONDS) <= 60
# を満たす必要がある。10銘柄・180秒なら約33件/10分で、日足の初回取得や
# スクリーニングの分の余裕も残る。
POLL_INTERVAL_SECONDS: float = 180.0
CLOSED_MARKET_POLL_INTERVAL_SECONDS: float = 300.0

# スイングトレード判定用（日足）のプルバックパラメータ。
# 移動平均30本は、42銘柄・10年の日足で移動平均期間だけを固定した
# ウォークフォワード検証（out-of-sample・コスト込み）で選んだ値。
# MA10/20/25/30/40/50/60 の比較では30が逆U字のピークで、profit_factorの
# 中央値1.42・プラスで終えた銘柄36/42(85.7%)といずれも最良だった
# （20は1.18・69.0%）。60まで伸ばすと中央値0.89まで崩れるため、
# 「長いほど良い」という単調な傾向を拾ったものではない。
SWING_MA_WINDOW: int = 30
SWING_THRESHOLD_PCT: float = 5.0

# 市場全体（指数）の状況によるエントリーの追加条件。日足＝スイング判定にのみ掛かる。
#
# **既定は無効（すべてNone）。** 有効化してよいのは、
#     python -m backtest.run --csv-dir bars --market-csv bars/SPY.csv \
#         --relative-threshold 2 3 4 --keep-unfiltered
# のような銘柄横断のウォークフォワードで、フィルター有りが選ばれ、かつ
# 合算PFだけでなくPFの中央値・プラスで終えた銘柄の割合まで改善したときだけ。
#
# デイトレード（5分足）分岐には掛けていない。5分足は外部データで数十日分しか
# 遡れず（CLAUDE.md「バックテストのデータ源」）、検証を経ていない条件を
# ライブにだけ入れることになるため。
MARKET_INDEX_SYMBOL: str = "SPY"
MARKET_INDEX_MA_WINDOW: int = 30
SWING_MARKET_FILTER: MarketFilterConfig = MarketFilterConfig()

# デイトレード（短期足）でのエントリーを行うか。**既定は無効。**
#
# 無効にしている理由は3つあり、それぞれ独立している:
#
# 1. **検証実績がゼロ。** 5分足のバックテストは1件も行っていない（外部データでは
#    60日程度しか遡れないため。CLAUDE.md「バックテストのデータ源」）。MA30の選定も
#    市場フィルターの不採用も小口座での成績も、すべて日足＝スイングの検証であり、
#    下のデイトレード用パラメータは誰も検証していない初期値のまま残っている。
# 2. **資金設計が成立しない。** 建玉金額 = 資金 × (リスク% ÷ 損切り%) なので、
#    損切り1.5%では1銘柄で資金の67%を使う。MAX_CONCURRENT_POSITIONS=2 でも
#    2銘柄目が入らない。現状これが表面化しないのはMAX_POSITION_SIZEの株数クランプが
#    効いているからで、それは検証用の安全弁であって資金設計ではない。
# 3. **キャッシュ口座の受渡し(T+1)。** デイトレードは定義上「同日中に買って売る」
#    ロジックで、未受渡しの売却代金で建てて同日に決済するとGood Faith Violationに
#    なりうる。
#
# 再有効化するときは、(1) IBKR接続で5分足を取得しスイングと同じ基準（銘柄横断・
# コスト込み・ウォークフォワード）で検証してPFが1を超えること、(2) 建玉サイズの
# 問題を解決すること、(3) マージン口座にするかGFVの制約が無いと確認できること、
# の3つを揃えること。
#
# 無効でも、既存のデイトレードポジション（状態ファイルからの復元など）の決済判定と
# 大引け前の強制決済は動く。エントリーだけを止めている。
ENABLE_DAY_TRADING: bool = False

INTRADAY_BAR_SIZE: str = "5 mins"
INTRADAY_DURATION: str = "2 D"
INTRADAY_MA_WINDOW: int = 20
INTRADAY_THRESHOLD_PCT: float = 2.0

# 決済ロジックのパラメータ（利確・損切り・トレーリングストップ）。
# スイングは日足の押し目、デイトレードは5分足の押し目と値幅のスケールが
# 異なるため、種別ごとに別基準を設ける。
SWING_TAKE_PROFIT_PCT: float = 10.0
SWING_STOP_LOSS_PCT: float = 5.0
SWING_TRAILING_STOP_PCT: float = 5.0

DAY_TAKE_PROFIT_PCT: float = 3.0
DAY_STOP_LOSS_PCT: float = 1.5
DAY_TRAILING_STOP_PCT: float = 2.0


@dataclass(frozen=True)
class ExitParams:
    take_profit_pct: float
    stop_loss_pct: float
    trailing_stop_pct: float


@dataclass(frozen=True)
class MarketDataCaches:
    """サイクルをまたいで使い回すIBKRデータのキャッシュ束。

    ペーシング制限対策の中心。main()で1つ生成してサイクル間で共有する。
    省略した場合は呼び出しごとに新規生成される（＝キャッシュが効かない）ため、
    単体テスト以外では必ず共有インスタンスを渡すこと。
    """

    contracts: ContractCache = field(default_factory=ContractCache)
    daily_bars: DailyBarCache = field(default_factory=DailyBarCache)


# strategy_type("swing"/"day")ごとの決済パラメータ。ブローカー同期で発見された
# STRATEGY_TYPE_UNKNOWNのポジションは、より安全側であるswing基準にフォールバックする
# （_process_exit_async参照）。
EXIT_PARAMS_BY_STRATEGY_TYPE: Dict[str, ExitParams] = {
    STRATEGY_TYPE_SWING: ExitParams(
        take_profit_pct=SWING_TAKE_PROFIT_PCT,
        stop_loss_pct=SWING_STOP_LOSS_PCT,
        trailing_stop_pct=SWING_TRAILING_STOP_PCT,
    ),
    STRATEGY_TYPE_DAY: ExitParams(
        take_profit_pct=DAY_TAKE_PROFIT_PCT,
        stop_loss_pct=DAY_STOP_LOSS_PCT,
        trailing_stop_pct=DAY_TRAILING_STOP_PCT,
    ),
}

# 1トレードあたり口座資金の何%をリスクに晒すか（ポジションサイジングの基準）
RISK_PER_TRADE_PCT: float = 1.0

# 銘柄ごとの1%リスクは同時保有数が増えるほど積み上がるため、
# ウォッチリストが拡張されても青天井にならないよう独立して上限を設ける。
#
# 上限が2なのは、リスクベースのサイジングでは1ポジションが占める資金の割合が
# 株価によらず一定になるため。
#     数量     = (資金 × RISK_PER_TRADE_PCT%) ÷ (株価 × 損切り%)
#     建玉金額 = 数量 × 株価 = 資金 × (RISK_PER_TRADE_PCT% ÷ 損切り%)
# スイングの損切り5%なら資金の20%。5銘柄まで許すと資金を使い切り、
# 現金の裏付けが無い注文が並ぶ（キャッシュ口座では受渡し前の資金を
# 当てにすることになり、なおさら成立しない）。2銘柄なら40%に収まる。
# ここを増やす場合は、必ず上式で「同時保有数 × 建玉金額 ≦ 資金」を確認すること。
#
# なおデイトレード（損切り1.5%）は1銘柄で資金の67%を使うため、この上限が2でも
# 2銘柄同時には成立しない（MAX_POSITION_SIZEの株数クランプが効いて実際には
# もっと小さくなるが、それは資金設計ではなく検証用の安全弁に頼っている状態）。
# 損切り幅が狭いほど建玉が大きくなるという関係自体は正しく、
# デイトレード分岐を小口座で使う場合はこの点を別途詰める必要がある。
MAX_CONCURRENT_POSITIONS: int = 2
# 口座資金に対する1日の最大許容損失（%）。これを超えたら新規エントリーを停止する
# サーキットブレーカー。既存ポジションの決済判定（損切り等）は引き続き有効。
MAX_DAILY_LOSS_PCT: float = 3.0
# 1日に出してよい新規建ての回数。MAX_CONCURRENT_POSITIONSは「同時に何銘柄持つか」
# の制限であって、建てては決済を繰り返す回数は抑えない。同日中の再エントリー禁止
# (PositionManager.is_in_cooldown)により通常はウォッチリストの銘柄数(10)が事実上の
# 上限になるが、それはクールダウンが正しく効いている前提の話である。この上限は
# その前提が壊れたとき（状態ファイルの消失、日付判定のバグ等）に、損失の垂れ流しを
# 有限回で止めるための独立した歯止め。損失額ベースのサーキットブレーカーとは違い、
# 実現損益が確定する前の発注ラッシュにも効く。
MAX_DAILY_ENTRY_ORDERS: int = 10
# 新規建ての数量を「決済済み現金で買える株数」に制限するか。
#
# キャッシュ口座では、未受渡しの売却代金で買った建玉を受渡し(T+1)より前に売ると
# Good Faith Violationになる。この経路はデイトレード固有ではない:
#   1. ある銘柄を決済する → 代金は翌営業日まで未受渡し
#   2. 同じ日に別銘柄のシグナルが出て、その未受渡し代金で建てる
#   3. ブラケットの利確指値/損切り逆指値に当日中に触れて決済 → GFV
# 同日中の再エントリー禁止は同一銘柄しか止めないため2は素通りし、3は
# 待機注文が板に乗っている以上ボット側では選べない。よってステップ2、すなわち
# 「入口で未受渡しの資金を使わせない」ことが唯一の止めどころになる。
#
# 同日決済そのものを禁止する対処を採らないのは、利確・損切りをブローカー側に
# 置いている意味（プロセスが落ちても約定する）を失うため。GFVを避けるために
# 損切りを外すのは本末転倒である。
#
# **既定は無効。** 検証に使っているペーパー口座がSettledCashタグを返さないため
# （実測: アカウントサマリー45タグ中に存在せず、BuyingPower/FullInitMarginReq/
# Cushionが並ぶマージン型口座だった）。有効なままだと決済済み現金が常に取得
# できず、新規エントリーが全件停止して他の経路の検証ができない。
# ペーパーではGFV自体が再現できないため、ここでガードを効かせても検証にならない。
#
# **実口座（キャッシュ口座）へ移す際は必ずTrueに戻すこと。** 上記のとおり実口座では
# この経路が現実に成立する。戻す前にSettledCashが実際に取得できることを
# scripts/check_market_data.py 等で確認し、取れないなら受渡し済み残高を
# 別のタグから求める方法を先に決めること。
ENFORCE_SETTLED_CASH_FUNDING: bool = False
# 当日のものでない価格を掴んだまま新規建てするのを止めるか。
#
# get_current_price_async のフォールバック連鎖は、下位の経路
# （ティッカーのclose・ヒストリカル最終終値）で**前営業日の終値**を返しうる。
# 購読権限の無い口座ほど下位に落ちやすいため、休場明けやギャップ後には
# 現実に起こる。この値は発注の参照価格になり、損切り・利確の指値もここから
# 算出されるので、古い値を掴むとブラケット一式が実勢からずれた値段で並ぶ。
# order_manager の値段の妥当性検証は参照価格を基準に測っている以上、
# 参照価格そのものがずれているケースは検出できない（CLAUDE.md該当節）。
#
# 決済側には掛けていない。古い価格で決済を見送ると、損切りが必要な場面で
# 何もしないことになり、新規建てを見送るのとは危険の向きが逆になる。
REJECT_STALE_ENTRY_PRICE: bool = True


async def _get_market_deviation_pct_async(
    ib: IB, caches: MarketDataCaches,
) -> Optional[float]:
    """指数の乖離率を返す。フィルターが無効なら取得もしない（=リクエスト0件）。

    指数の日足も `DailyBarCache` を通すため、追加のリクエストは
    「1取引日あたり1件」で済み、ペーシング制限(CLAUDE.md 6.1)には実質響かない。
    """
    if not SWING_MARKET_FILTER.is_enabled:
        return None

    try:
        contract = await caches.contracts.get_async(ib, MARKET_INDEX_SYMBOL)
        bars = await caches.daily_bars.get_async(ib, contract)
    except Exception:
        # 指数が取れないことで監視ループ全体を落とさない。Noneを返すと
        # フィルターは「条件を満たさない」＝エントリー見送りに倒れる。
        logger.exception("%s の日足を取得できませんでした。", MARKET_INDEX_SYMBOL)
        return None

    if len(bars) < MARKET_INDEX_MA_WINDOW:
        logger.warning(
            "%s の日足が%d本しかなく、移動平均(%d本)を計算できません。",
            MARKET_INDEX_SYMBOL, len(bars), MARKET_INDEX_MA_WINDOW,
        )
        return None

    return compute_deviation_pct(bars["close"], MARKET_INDEX_MA_WINDOW)


async def _detect_buy_signal_async(
    ib: IB, contract: Stock, symbol: str, caches: MarketDataCaches,
) -> Optional[Tuple[SignalResult, str]]:
    # 日足は1取引日に1本しか増えないためキャッシュから引く。日中足は
    # デイトレードのシグナルそのものなので毎回取得する。
    # デイトレードが無効なら日中足は使わないので、取得自体を行わない
    # （銘柄あたり毎サイクル1リクエストを丸ごと節約でき、ペーシング制限に効く）。
    daily_df = await caches.daily_bars.get_async(ib, contract)
    intraday_df = (
        await get_intraday_bars_async(
            ib, contract, duration=INTRADAY_DURATION, bar_size=INTRADAY_BAR_SIZE,
        )
        if ENABLE_DAY_TRADING else pd.DataFrame()
    )

    if daily_df.empty and intraday_df.empty:
        logger.warning("%s のヒストリカルデータが取得できなかったためスキップします。", symbol)
        return None

    if len(daily_df) >= SWING_MA_WINDOW:
        market_deviation_pct = await _get_market_deviation_pct_async(ib, caches)
        swing_signal = detect_pullback_signal(
            symbol, daily_df, ma_window=SWING_MA_WINDOW, threshold_pct=SWING_THRESHOLD_PCT,
            market_deviation_pct=market_deviation_pct, market_filter=SWING_MARKET_FILTER,
        )
        if swing_signal.should_buy:
            logger.info("[%s] スイング(日足)のプルバックシグナルで買い判定しました。", symbol)
            return swing_signal, STRATEGY_TYPE_SWING

    if ENABLE_DAY_TRADING and len(intraday_df) >= INTRADAY_MA_WINDOW:
        intraday_signal = detect_pullback_signal(
            symbol, intraday_df, ma_window=INTRADAY_MA_WINDOW, threshold_pct=INTRADAY_THRESHOLD_PCT,
        )
        if intraday_signal.should_buy:
            logger.info(
                "[%s] デイトレード(%s足)のプルバックシグナルで買い判定しました。",
                symbol, INTRADAY_BAR_SIZE,
            )
            return intraday_signal, STRATEGY_TYPE_DAY

    return None


async def _process_entry_async(
    ib: IB, symbol: str, position_manager: PositionManager, trade_journal: TradeJournal,
    caches: MarketDataCaches,
) -> None:
    if position_manager.count_open_positions() >= MAX_CONCURRENT_POSITIONS:
        logger.info(
            "[%s] 同時保有ポジション数の上限(%d)に達しているため新規エントリーをスキップします。",
            symbol, MAX_CONCURRENT_POSITIONS,
        )
        return

    # 発注回数の上限。データ取得より前に判定するのは、上限に達した後の
    # サイクルで無駄なヒストリカルリクエストを撃たないため（ペーシング制限）。
    entry_orders_today = position_manager.count_entry_orders_today()
    if entry_orders_today >= MAX_DAILY_ENTRY_ORDERS:
        logger.warning(
            "[%s] 本日の新規建て回数(%d)が上限(%d)に達したため、新規エントリーを停止します。",
            symbol, entry_orders_today, MAX_DAILY_ENTRY_ORDERS,
        )
        return

    # 決済した当日は同じ銘柄を買い直さない。日足の乖離率はその日の間ほぼ
    # 変わらないため、この判定が無いと損切り直後のサイクルで同じシグナルが
    # 再び成立し、下落トレンド中に損失を刻み続ける。
    if position_manager.is_in_cooldown(symbol):
        logger.info(
            "[%s] 本日すでに決済済みのため、新規エントリーをスキップします"
            "（当日中の再エントリー禁止）。",
            symbol,
        )
        return

    contract = await caches.contracts.get_async(ib, symbol)

    signal_result = await _detect_buy_signal_async(ib, contract, symbol, caches)
    if signal_result is None:
        return
    _signal, strategy_type = signal_result

    quote = await get_current_price_quote_async(ib, contract)
    if quote is None:
        logger.warning("%s の現在価格が取得できなかったため発注をスキップします。", symbol)
        return

    if quote.is_stale and REJECT_STALE_ENTRY_PRICE:
        logger.warning(
            "[%s] 現在価格 %.2f が当日のものではない可能性があるため（経路: %s）、"
            "新規エントリーを見送ります。この価格を参照価格にすると、"
            "損切り・利確の値段まで実勢からずれたブラケットが並ぶため。",
            symbol, quote.price, quote.source,
        )
        return

    price = quote.price

    account_equity = await get_account_equity_async(ib)

    daily_pnl = trade_journal.compute_daily_pnl()
    max_daily_loss = -account_equity * MAX_DAILY_LOSS_PCT / 100.0
    if daily_pnl <= max_daily_loss:
        logger.warning(
            "[%s] 本日の実現損益(%.2f)が最大許容損失(%.2f)に達したため、"
            "サーキットブレーカーが発動し新規エントリーをスキップします。",
            symbol, daily_pnl, max_daily_loss,
        )
        return

    exit_params = EXIT_PARAMS_BY_STRATEGY_TYPE[strategy_type]

    quantity = calculate_position_size(
        account_equity=account_equity,
        entry_price=price,
        stop_loss_pct=exit_params.stop_loss_pct,
        risk_per_trade_pct=RISK_PER_TRADE_PCT,
    )
    if quantity <= 0:
        logger.warning(
            "[%s] リスクベースの計算数量が0のため発注をスキップします"
            "（口座資金 %.2f に対して株価 %.2f が高すぎる可能性があります）。",
            symbol, account_equity, price,
        )
        return

    quantity = await _clamp_quantity_to_settled_cash_async(ib, symbol, quantity, price)
    if quantity <= 0:
        return

    risk_per_share = price * exit_params.stop_loss_pct / 100.0

    # 損切りと利確はブローカー側に置く（ブラケット注文）。ボットのプロセスが
    # 落ちていても、TWSとの接続が切れていても、ポーリングを待たずに約定する。
    stop_price = resolve_stop_price(price, exit_params.stop_loss_pct)
    take_profit_price = resolve_take_profit_price(price, exit_params.take_profit_pct)

    order_result = await place_dry_run_bracket_order_async(
        ib, contract, quantity=quantity,
        stop_price=stop_price, take_profit_price=take_profit_price,
        reference_price=price,
    )
    position_manager.open_position(
        symbol, entry_price=price, quantity=order_result.quantity, risk_per_share=risk_per_share,
        strategy_type=strategy_type,
        stop_price=stop_price, take_profit_price=take_profit_price,
        oca_group=order_result.oca_group,
    )


async def _clamp_quantity_to_settled_cash_async(
    ib: IB, symbol: str, quantity: int, price: float,
) -> int:
    """新規建ての数量を、決済済み現金で実際に支払える株数まで切り下げる。

    リスクベースのサイジングはNetLiquidation（未受渡しの代金を含む評価額）を
    基準にしているため、キャッシュ口座では「まだ手元に無い現金」を当てにした
    数量が出うる。それで建てるとGFVの経路に乗るので、ここで現金の裏付けまで
    数量を落とす（ENFORCE_SETTLED_CASH_FUNDINGの説明を参照）。

    決済済み現金が取得できない場合は0を返して**建てない**。ここを素通しすると、
    資金の裏付けを確認できないままGFVを踏みうる注文を出すことになる。
    エントリーだけが止まり決済判定は動き続けるため、既存ポジションが
    損切りも無く放置されることはない。
    """
    if not ENFORCE_SETTLED_CASH_FUNDING:
        return quantity

    settled_cash = await get_settled_cash_async(ib)
    if settled_cash is None:
        logger.error(
            "[%s] 決済済み現金が取得できなかったため、新規エントリーを見送ります"
            "（未受渡し資金での建玉はGood Faith Violationにつながるため）。",
            symbol,
        )
        return 0

    affordable_quantity = max(math.floor(settled_cash / price), 0)
    if affordable_quantity <= 0:
        logger.warning(
            "[%s] 決済済み現金 %.2f USD では1株(%.2f USD)も買えないため、"
            "新規エントリーを見送ります。",
            symbol, settled_cash, price,
        )
        return 0

    if affordable_quantity < quantity:
        logger.info(
            "[%s] 決済済み現金 %.2f USD の範囲に数量を切り下げます: %d株 -> %d株"
            "（残りは未受渡しの代金であり、これで建てるとGFVの対象になりうる）。",
            symbol, settled_cash, quantity, affordable_quantity,
        )
        return affordable_quantity

    return quantity


async def _record_closed_trade(
    ib: IB, trade_journal: TradeJournal, closed_position: Position, exit_price: float, reason: str, pnl_pct: float,
) -> None:
    pnl = (exit_price - closed_position.entry_price) * closed_position.quantity
    r_multiple = (
        (exit_price - closed_position.entry_price) / closed_position.risk_per_share
        if closed_position.risk_per_share > 0 else None
    )

    # ドライラン中は実約定の手数料が発生しないため0固定。
    # 実発注(placeOrder)を有効化する際は、FillのcommissionReport.commissionを渡すこと。
    commission = 0.0
    usd_jpy_rate = await get_usd_jpy_rate_async(ib)

    trade_journal.record_trade(
        symbol=closed_position.symbol,
        entry_price=closed_position.entry_price,
        exit_price=exit_price,
        quantity=closed_position.quantity,
        reason=reason,
        pnl=pnl,
        pnl_pct=pnl_pct,
        r_multiple=r_multiple,
        commission=commission,
        usd_jpy_rate=usd_jpy_rate,
        entry_date=closed_position.entry_date,
    )

    stats = trade_journal.compute_stats()
    logger.info(
        "トレード集計(累計): trades=%d win_rate=%.1f%% total_pnl=%.2f profit_factor=%.2f avg_R=%s",
        stats.num_trades, stats.win_rate_pct, stats.total_pnl, stats.profit_factor,
        f"{stats.avg_r_multiple:.2f}" if stats.avg_r_multiple is not None else "N/A",
    )


async def _process_exit_async(
    ib: IB, symbol: str, position_manager: PositionManager, trade_journal: TradeJournal,
    caches: MarketDataCaches,
) -> None:
    position = position_manager.get_position(symbol)
    if position is None:
        return

    contract = await caches.contracts.get_async(ib, symbol)
    price = await get_current_price_async(ib, contract)
    if price is None:
        logger.warning("%s の現在価格が取得できなかったため決済判定をスキップします。", symbol)
        return

    position_manager.update_highest_price(symbol, price)

    # 1. ブローカー側に置いた待機注文（損切りの逆指値・利確の指値）が約定していないか。
    #    こちらはポーリングを待たずに市場で約定しているため、他の判定より先に確認する。
    #    ブローカー同期で取り込んだ未追跡ポジションは待機注文を持たない(値段が0)ため対象外。
    if position.stop_price > 0 and position.take_profit_price > 0:
        resting_exit = detect_resting_order_exit(
            stop_price=position.stop_price,
            take_profit_price=position.take_profit_price,
            # ポーリングではバー内の値動きが分からないため、観測した現在値だけで判定する。
            bar_low=price,
            bar_high=price,
        )
        if resting_exit is not None:
            logger.info(
                "[%s] ブローカー側の待機注文が約定しました: reason=%s fill=%.2f",
                symbol, resting_exit.reason, resting_exit.fill_price,
            )
            fill_price = resting_exit.fill_price
            pnl_pct = (fill_price - position.entry_price) / position.entry_price * 100.0
            # OCAグループの相方はIBKR側が自動で取り消すため、ここでの取り消しは不要。
            closed_position = position_manager.close_position(symbol)
            await _record_closed_trade(
                ib, trade_journal, closed_position, fill_price, resting_exit.reason, pnl_pct,
            )
            return

    # 2. ボット側で判定するもの（大引け前の強制決済・トレーリングストップ）。
    #    どちらも成行で出すため、先に待機注文を取り消さないと、決済済みの銘柄に
    #    売り注文だけが残る。
    if position.strategy_type == STRATEGY_TYPE_DAY and is_day_trade_flatten_time():
        logger.info(
            "[%s] デイトレードポジションが大引け前の強制決済時刻に達したため決済します。", symbol,
        )
        pnl_pct = (price - position.entry_price) / position.entry_price * 100.0
        await cancel_dry_run_bracket_orders_async(ib, symbol, position.oca_group)
        await place_dry_run_order_async(ib, contract, action="SELL", quantity=position.quantity)
        closed_position = position_manager.close_position(symbol)
        await _record_closed_trade(ib, trade_journal, closed_position, price, REASON_EOD_FLATTEN, pnl_pct)
        return

    exit_params = EXIT_PARAMS_BY_STRATEGY_TYPE.get(
        position.strategy_type, EXIT_PARAMS_BY_STRATEGY_TYPE[STRATEGY_TYPE_SWING]
    )

    result = detect_exit_signal(
        symbol,
        entry_price=position.entry_price,
        current_price=price,
        highest_price_since_entry=position.highest_price,
        take_profit_pct=exit_params.take_profit_pct,
        stop_loss_pct=exit_params.stop_loss_pct,
        trailing_stop_pct=exit_params.trailing_stop_pct,
    )
    if not result.should_sell:
        return

    await cancel_dry_run_bracket_orders_async(ib, symbol, position.oca_group)
    await place_dry_run_order_async(ib, contract, action="SELL", quantity=position.quantity)
    closed_position = position_manager.close_position(symbol)
    await _record_closed_trade(ib, trade_journal, closed_position, price, result.reason, result.pnl_pct)


async def process_symbol_async(
    ib: IB, symbol: str, position_manager: PositionManager, trade_journal: TradeJournal,
    caches: Optional[MarketDataCaches] = None,
) -> None:
    caches = caches if caches is not None else MarketDataCaches()

    if position_manager.has_position(symbol):
        await _process_exit_async(ib, symbol, position_manager, trade_journal, caches)
    else:
        await _process_entry_async(ib, symbol, position_manager, trade_journal, caches)


async def run_watchlist_cycle_async(
    ib: IB, watchlist: List[str], position_manager: PositionManager, trade_journal: TradeJournal,
    caches: Optional[MarketDataCaches] = None,
) -> None:
    caches = caches if caches is not None else MarketDataCaches()

    await position_manager.sync_with_broker_async(ib)

    # スクリーニング結果でウォッチリストが日次で入れ替わっても、既に保有中の
    # ポジションは（ウォッチリストから外れていても）決済判定を継続する必要が
    # あるため、ウォッチリストと保有中銘柄の和集合を処理対象にする。
    symbols_to_process = list(dict.fromkeys([*watchlist, *position_manager.open_symbols()]))

    for symbol in symbols_to_process:
        try:
            await process_symbol_async(ib, symbol, position_manager, trade_journal, caches)
        except Exception:
            logger.exception("%s の処理中にエラーが発生しました。", symbol)


def resolve_max_affordable_price(
    account_equity: float, settled_cash: Optional[float] = None,
) -> Optional[float]:
    """現在の口座資金で1株でも買える上限株価を返す。

    リスクベースのサイジングは
        数量 = floor((資金 × RISK_PER_TRADE_PCT%) ÷ (株価 × 損切り%))
    なので、数量が1株以上になる条件は
        株価 ≦ 資金 × (RISK_PER_TRADE_PCT% ÷ 損切り%)
    となる。

    損切り幅にはスイングの値(SWING_STOP_LOSS_PCT)を使う。損切りが広いほど
    上限株価は低くなるため、スイング・デイトレードのどちらの基準でも
    買える銘柄だけが残る。デイトレードの狭い損切り(1.5%)で計算すると、
    スイングでは数量0になる銘柄まで通してしまう。

    キャッシュ口座では、1株の値段が決済済み現金を超える銘柄も買えない
    （_clamp_quantity_to_settled_cash_asyncが数量0に切り下げる）。そのため
    settled_cashが渡された場合は、上式との**小さい方**を上限とする。
    これを無視すると、買えない銘柄が監視枠を占め続ける。

    資金が取得できない場合(0以下)はNoneを返して**フィルターを掛けない**。
    ここで0を返すと全銘柄が除外され、ウォッチリストが空になったまま
    稼働し続けることになる。決済済み現金が取れなかった場合(None)も同じ理由で
    上限を狭めない。実際に建てられるかどうかはエントリー時に必ず再判定するため、
    ここで絞り込みを外しても未受渡し資金で建ててしまうことはない。
    """
    if account_equity <= 0:
        logger.warning(
            "口座資金が取得できなかったため(%.2f)、株価上限フィルターを無効にします。",
            account_equity,
        )
        return None

    max_price = account_equity * (RISK_PER_TRADE_PCT / SWING_STOP_LOSS_PCT)

    if settled_cash is not None and 0 < settled_cash < max_price:
        logger.info(
            "決済済み現金 %.2f USD の方が小さいため、上限株価をこちらに合わせます"
            "（%.2f USD -> %.2f USD）。",
            settled_cash, max_price, settled_cash,
        )
        return settled_cash

    return max_price


async def _refresh_watchlist_async(
    ib: IB, fallback_watchlist: List[str], account_equity: float,
    settled_cash: Optional[float] = None,
) -> List[str]:
    max_price = resolve_max_affordable_price(account_equity, settled_cash)
    if max_price is not None:
        logger.info(
            "口座資金 %.2f USD で買える上限株価は %.2f USD です（これを超える銘柄は"
            "数量が0株になるため、監視対象から除外します）。",
            account_equity, max_price,
        )

    config = ScreenerConfig(
        max_price=max_price,
        market_cap_above=SCREENER_MIN_MARKET_CAP,
        market_cap_below=SCREENER_MAX_MARKET_CAP,
        max_pe_ratio=SCREENER_MAX_PE_RATIO,
        scan_code=SCREENER_SCAN_CODE,
        number_of_rows=SCREENER_NUM_CANDIDATES,
        pe_request_interval_seconds=SCREENER_PE_REQUEST_INTERVAL_SECONDS,
        enable_trend_filter=SCREENER_ENABLE_TREND_FILTER,
        trend_ma_window=SCREENER_TREND_MA_WINDOW,
        trend_lookback_duration=SCREENER_TREND_LOOKBACK_DURATION,
    )

    try:
        screened = await screen_value_stocks_async(ib, config)
    except Exception:
        logger.exception("銘柄スクリーニングに失敗しました。既存のウォッチリストを維持します。")
        return fallback_watchlist

    if not screened:
        logger.warning("スクリーニング結果が0件のため、既存のウォッチリストを維持します。")
        return fallback_watchlist

    # 監視銘柄1件につき毎サイクル1回の日中足リクエストが発生するため、
    # スクリーニングが何件返しても監視対象は上限で頭打ちにする。
    # これを外すとIBKRのペーシング制限に張り付き、全銘柄の処理が遅延する。
    if len(screened) > MAX_WATCHLIST_SIZE:
        logger.info(
            "スクリーニング結果%d件のうち、上位%d件のみを監視対象にします"
            "（IBKRのペーシング制限対策）。",
            len(screened), MAX_WATCHLIST_SIZE,
        )
        screened = screened[:MAX_WATCHLIST_SIZE]

    logger.info("スクリーニング結果でウォッチリストを更新しました: %s", screened)
    return screened


async def main() -> None:
    connection = IBKRConnection()
    # 状態ファイルを指定して、再起動しても保有ポジションと
    # トレーリングストップの基準（高値）を引き継げるようにする。
    position_manager = PositionManager(state_path=DEFAULT_STATE_PATH)
    trade_journal = TradeJournal()
    # キャッシュはサイクル間で共有する。ここで毎サイクル作り直すと
    # ペーシング制限対策の意味が無くなる。
    caches = MarketDataCaches()
    watchlist: List[str] = list(WATCHLIST)
    last_screened_date: Optional[date] = None

    ib: Optional[IB] = None
    try:
        while True:
            try:
                # TWSとの接続はメンテナンスやネットワーク瞬断で稼働中に切れうるため、
                # サイクルの先頭で毎回接続状態を確認し、切れていれば再接続する
                # （connect_async自体は指数的バックオフ付きリトライを内包している）。
                if ib is None or not ib.isConnected():
                    ib = await connection.connect_async()

                if is_regular_trading_hours():
                    today = datetime.now(US_EASTERN).date()
                    if today != last_screened_date:
                        # 銘柄選定は口座資金に依存する（買えない株価の銘柄を除外する）。
                        # スクリーニングは1日1回なので、資金の取得もこのタイミングだけで足りる。
                        account_equity = await get_account_equity_async(ib)
                        # キャッシュ口座では1株の値段が決済済み現金を超える銘柄も
                        # 買えないため、株価上限の判定に併せて渡す。
                        settled_cash = (
                            await get_settled_cash_async(ib)
                            if ENFORCE_SETTLED_CASH_FUNDING else None
                        )
                        watchlist = await _refresh_watchlist_async(
                            ib, watchlist, account_equity, settled_cash,
                        )
                        last_screened_date = today

                    await run_watchlist_cycle_async(
                        ib, watchlist, position_manager, trade_journal, caches,
                    )
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
                else:
                    logger.info("市場時間外のため、シグナル評価をスキップします。")
                    await asyncio.sleep(CLOSED_MARKET_POLL_INTERVAL_SECONDS)
            except ConnectionError:
                # connect_asyncのリトライを使い果たした場合（延長メンテナンス等）。
                # プロセスを落とさず、時間外ポーリング間隔でリトライし続ける。
                logger.exception(
                    "TWSへの再接続に失敗しました。%.0f秒後に再試行します。",
                    CLOSED_MARKET_POLL_INTERVAL_SECONDS,
                )
                await asyncio.sleep(CLOSED_MARKET_POLL_INTERVAL_SECONDS)
            except Exception:
                # サイクル処理中の予期しないエラー（サイクル途中の切断等を含む）で
                # プロセス全体を落とさない。次のループ先頭でisConnected()により
                # 再接続要否を判定する。
                logger.exception("監視サイクルの処理中にエラーが発生しました。次のサイクルで再試行します。")
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        logger.info("ユーザーの割り込みにより停止します。")
    finally:
        await connection.disconnect_async()


if __name__ == "__main__":
    asyncio.run(main())
