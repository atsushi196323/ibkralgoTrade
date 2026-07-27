"""エントリーポイント: 非同期イベントループの起動と全体のオーケストレーション。"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from ib_insync import IB, Stock

from core.connection import IBKRConnection
from core.market_hours import US_EASTERN, is_day_trade_flatten_time, is_regular_trading_hours
from data.cache import ContractCache, DailyBarCache
from data.market_data import (
    get_current_price_async,
    get_intraday_bars_async,
    get_usd_jpy_rate_async,
)
from execution.account import get_account_equity_async
from execution.order_manager import place_dry_run_order_async
from execution.position_manager import (
    DEFAULT_STATE_PATH,
    Position,
    PositionManager,
    STRATEGY_TYPE_DAY,
    STRATEGY_TYPE_SWING,
)
from execution.position_sizing import calculate_position_size
from execution.trade_journal import TradeJournal
from strategy.exit_signal import REASON_EOD_FLATTEN, detect_exit_signal
from strategy.pullback import SignalResult, detect_pullback_signal
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

# スイングトレード判定用（日足）のプルバックパラメータ
SWING_MA_WINDOW: int = 20
SWING_THRESHOLD_PCT: float = 5.0

# デイトレード判定用（短期足）のプルバックパラメータ
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

# ポートフォリオ全体のリスク管理
# 銘柄ごとの1%リスクは同時保有数が増えるほど積み上がるため、
# ウォッチリストが拡張されても青天井にならないよう独立して上限を設ける。
MAX_CONCURRENT_POSITIONS: int = 5
# 口座資金に対する1日の最大許容損失（%）。これを超えたら新規エントリーを停止する
# サーキットブレーカー。既存ポジションの決済判定（損切り等）は引き続き有効。
MAX_DAILY_LOSS_PCT: float = 3.0


async def _detect_buy_signal_async(
    ib: IB, contract: Stock, symbol: str, caches: MarketDataCaches,
) -> Optional[Tuple[SignalResult, str]]:
    # 日足は1取引日に1本しか増えないためキャッシュから引く。日中足は
    # デイトレードのシグナルそのものなので毎回取得する。
    daily_df = await caches.daily_bars.get_async(ib, contract)
    intraday_df = await get_intraday_bars_async(
        ib, contract, duration=INTRADAY_DURATION, bar_size=INTRADAY_BAR_SIZE,
    )

    if daily_df.empty and intraday_df.empty:
        logger.warning("%s のヒストリカルデータが取得できなかったためスキップします。", symbol)
        return None

    if len(daily_df) >= SWING_MA_WINDOW:
        swing_signal = detect_pullback_signal(
            symbol, daily_df, ma_window=SWING_MA_WINDOW, threshold_pct=SWING_THRESHOLD_PCT,
        )
        if swing_signal.should_buy:
            logger.info("[%s] スイング(日足)のプルバックシグナルで買い判定しました。", symbol)
            return swing_signal, STRATEGY_TYPE_SWING

    if len(intraday_df) >= INTRADAY_MA_WINDOW:
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

    contract = await caches.contracts.get_async(ib, symbol)

    signal_result = await _detect_buy_signal_async(ib, contract, symbol, caches)
    if signal_result is None:
        return
    _signal, strategy_type = signal_result

    price = await get_current_price_async(ib, contract)
    if price is None:
        logger.warning("%s の現在価格が取得できなかったため発注をスキップします。", symbol)
        return

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

    risk_per_share = price * exit_params.stop_loss_pct / 100.0
    order_result = await place_dry_run_order_async(ib, contract, action="BUY", quantity=quantity)
    position_manager.open_position(
        symbol, entry_price=price, quantity=order_result.quantity, risk_per_share=risk_per_share,
        strategy_type=strategy_type,
    )


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

    if position.strategy_type == STRATEGY_TYPE_DAY and is_day_trade_flatten_time():
        logger.info(
            "[%s] デイトレードポジションが大引け前の強制決済時刻に達したため決済します。", symbol,
        )
        pnl_pct = (price - position.entry_price) / position.entry_price * 100.0
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


async def _refresh_watchlist_async(ib: IB, fallback_watchlist: List[str]) -> List[str]:
    config = ScreenerConfig(
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
                        watchlist = await _refresh_watchlist_async(ib, watchlist)
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
