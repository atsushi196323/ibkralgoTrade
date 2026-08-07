"""main.py のオーケストレーションロジックの単体テスト（IB呼び出しはすべてモック化）。"""

import asyncio
import logging
import signal
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

import main as main_module

from core.market_hours import US_EASTERN
from data.market_data import PRICE_SOURCE_HISTORICAL, PRICE_SOURCE_STREAMING, PriceQuote
from execution.order_manager import (
    MAX_POSITION_SIZE,
    BracketResult,
    OrderNotFilledError,
    OrderResult,
    RestingOrderCancelTimeoutError,
    RestingOrderFill,
)
from data.rank_history import RankHistoryStore
from execution.position_manager import PositionManager, STRATEGY_TYPE_DAY, STRATEGY_TYPE_SWING
from execution.trade_journal import TradeJournal
from strategy.exit_signal import REASON_STOP_LOSS, REASON_TAKE_PROFIT
from strategy.pullback import MarketFilterConfig
from execution.position_sizing import calculate_position_size
from main import (
    DAY_STOP_LOSS_PCT,
    MARKET_INDEX_SYMBOL,
    MAX_CONCURRENT_POSITIONS,
    MAX_DAILY_ENTRY_ORDERS,
    CONNECTION_FAILURE_ROUNDS_BEFORE_MANUAL_LOGIN,
    MAX_WATCHLIST_SIZE,
    POLL_INTERVAL_SECONDS,
    SCREENING_RETRY_INTERVAL_SECONDS,
    RISK_PER_TRADE_PCT,
    STRUGGLING_MA_WINDOW,
    SWING_MA_WINDOW,
    SWING_MIN_HISTORY_BARS,
    SWING_STOP_LOSS_PCT,
    WATCHLIST,
    MarketDataCaches,
    WatchlistRefresh,
    _refresh_watchlist_async,
    main,
    process_symbol_async,
    resolve_max_affordable_price,
    resolve_min_tradeable_price,
    run_watchlist_cycle_async,
)


def _fresh_quote(price: float) -> PriceQuote:
    """当日のストリーミング価格として扱えるPriceQuoteを返す。

    鮮度そのものを検証するテスト以外は「価格は新しい」前提で書きたいため、
    エントリー経路のモックはこれを既定にする。
    """
    return PriceQuote(price=price, source=PRICE_SOURCE_STREAMING, is_stale=False)


def _make_df(closes: list) -> pd.DataFrame:
    return pd.DataFrame({"close": closes})


def _make_daily_df(*, drop: bool = False) -> pd.DataFrame:
    """スイング（日足）判定用のバー。drop=Trueで買いシグナルが出る形にする。

    本数をSWING_MIN_HISTORY_BARSから導出しているのは、必要本数を変更したときに
    「本数不足で日足分岐が丸ごとスキップされ、テストは通るがシグナル判定を
    一度も通っていない」状態に陥るのを防ぐため。移動平均が確定する本数
    (SWING_MA_WINDOW)ではなく新規建てに必要な本数を使うのは、前者だけでは
    エントリー経路のテストが本数不足で素通りしてしまうからである。

    **前半を安値にしてあるのは、終値を200日移動平均より上に置くため。**
    平坦な系列だと終値と長期移動平均が一致し、
    `ENTRY_REQUIRES_LONG_TERM_UPTREND` が下降トレンドと判定して
    エントリー経路のテストが丸ごと素通りする。
    """
    base = [50.0] * (SWING_MIN_HISTORY_BARS - SWING_MA_WINDOW)
    if drop:
        # 直近30本の平均に対して約-19%。押し目シグナルが出る形。
        return _make_df(base + [100.0] * (SWING_MA_WINDOW - 1) + [80.0])
    return _make_df(base + [100.0] * SWING_MA_WINDOW)


def _bracket_result(
    quantity: int,
    symbol: str = "AAPL",
    stop_price: float = 76.0,
    take_profit_price: float = 88.0,
) -> BracketResult:
    """新規建て時のブラケット注文の戻り値。

    値段の既定は「現在値80.0でスイングの-5%/+10%」に対応する。呼び出し側は
    ここで返る値段（＝実際にブローカーへ置いた待機注文の値段。呼値へ丸めた後）を
    そのままローカルの記録に使うため、0.0のままにすると記録が実態とずれる。
    """
    return BracketResult(
        symbol=symbol, quantity=quantity,
        stop_price=stop_price, take_profit_price=take_profit_price,
        oca_group="OCA_TEST",
    )


def _order_result(quantity: int = 1, symbol: str = "AAPL", action: str = "SELL") -> OrderResult:
    """ドライラン相当の成行注文の戻り値（約定価格が無く、呼び出し側が観測価格で代用する）。"""
    return OrderResult(symbol=symbol, action=action, quantity=quantity, order_type="MKT")


@pytest.fixture
def trade_journal(tmp_path) -> TradeJournal:
    return TradeJournal(str(tmp_path / "trades.csv"))


# --- 新規エントリー（ポジション未保有） -----------------------------------------


def test_process_symbol_opens_position_on_swing_daily_buy_signal(trade_journal) -> None:
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()

    daily_df = _make_daily_df(drop=True)  # 大きく下落 -> 日足で買いシグナル
    intraday_df = _make_df([100.0] * 20)  # 横ばい -> 短期足はシグナルなし

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
        patch("main.get_intraday_bars_async", new=AsyncMock(return_value=intraday_df)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=80.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(80.0))), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.get_settled_cash_async", new=AsyncMock(return_value=100_000.0)), \
        patch(
            "main.place_bracket_order_async",
            new=AsyncMock(return_value=_bracket_result(quantity=250)),
        ) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    # risk_amount=100,000*1%=1,000 / per_share_risk=80*5%=4.0 -> qty=250
    # 損切り(-5%) / 利確(+10%) はブローカー側の待機注文として発注する。
    mock_order.assert_awaited_once_with(
        ib, contract, quantity=250,
        stop_price=pytest.approx(80.0 * 0.95),
        take_profit_price=pytest.approx(80.0 * 1.10),
        # 待機注文の値段の妥当性検証に使う参照価格（現在値）。
        reference_price=pytest.approx(80.0),
    )
    assert position_manager.has_position("AAPL") is True
    position = position_manager.get_position("AAPL")
    assert position.entry_price == 80.0
    assert position.quantity == 250
    # risk_per_share = 80 * stop_loss_pct(5%) / 100 = 4.0
    assert position.risk_per_share == pytest.approx(4.0)
    assert position.strategy_type == STRATEGY_TYPE_SWING
    # 再起動後も待機注文を把握できるよう、値段とOCAグループを永続化する。
    assert position.stop_price == pytest.approx(80.0 * 0.95)
    assert position.take_profit_price == pytest.approx(80.0 * 1.10)
    assert position.oca_group == "OCA_TEST"


def test_process_symbol_skips_intraday_buy_signal_while_day_trading_is_disabled(trade_journal) -> None:
    """ENABLE_DAY_TRADING=False（既定）なら、短期足に買いシグナルが出ても建てないこと。

    日中足の取得自体を行わないことまで確認する。銘柄あたり毎サイクル1リクエストを
    丸ごと節約できるかどうかが、ペーシング制限の余裕に直結するため。
    """
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()

    daily_df = _make_daily_df()  # 日足は横ばい -> シグナルなし
    intraday_df = _make_df([100.0] * 19 + [90.0])  # 短期足は下落 -> 買いシグナル

    with patch("main.ENABLE_DAY_TRADING", False), \
        patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
        patch(
            "main.get_intraday_bars_async", new=AsyncMock(return_value=intraday_df)
        ) as mock_intraday, \
        patch("main.get_current_price_async", new=AsyncMock(return_value=90.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(90.0))), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.get_settled_cash_async", new=AsyncMock(return_value=100_000.0)), \
        patch(
            "main.place_bracket_order_async",
            new=AsyncMock(return_value=_bracket_result(quantity=222)),
        ) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    mock_order.assert_not_awaited()
    mock_intraday.assert_not_awaited()
    assert position_manager.has_position("AAPL") is False


def test_process_symbol_opens_position_on_intraday_buy_signal_when_daily_flat(trade_journal) -> None:
    """ENABLE_DAY_TRADINGを有効にしたときのデイトレード分岐。

    既定は無効だが、再有効化したときに壊れていることに気付けるよう、
    フラグを立てた状態でのエントリー経路を残してある。
    """
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()

    daily_df = _make_daily_df()  # 日足は横ばい -> シグナルなし
    intraday_df = _make_df([100.0] * 19 + [90.0])  # 短期足は下落 -> 買いシグナル

    with patch("main.ENABLE_DAY_TRADING", True), \
        patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
        patch("main.get_intraday_bars_async", new=AsyncMock(return_value=intraday_df)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=90.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(90.0))), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.get_settled_cash_async", new=AsyncMock(return_value=100_000.0)), \
        patch(
            "main.place_bracket_order_async",
            new=AsyncMock(return_value=_bracket_result(quantity=222)),
        ) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    mock_order.assert_awaited_once()
    assert position_manager.has_position("AAPL") is True
    assert position_manager.get_position("AAPL").strategy_type == STRATEGY_TYPE_DAY


def test_process_symbol_skips_entry_when_risk_based_quantity_is_zero(trade_journal) -> None:
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()

    daily_df = _make_daily_df(drop=True)  # 大きく下落 -> 買いシグナル
    intraday_df = _make_df([100.0] * 20)

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
        patch("main.get_intraday_bars_async", new=AsyncMock(return_value=intraday_df)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=80.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(80.0))), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=1.0)), \
        patch("main.place_market_order_async", new=AsyncMock(return_value=_order_result())) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    mock_order.assert_not_awaited()
    assert position_manager.has_position("AAPL") is False


def test_process_symbol_does_not_open_position_when_no_buy_signal_on_either_timeframe(trade_journal) -> None:
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()

    daily_df = _make_daily_df()  # 横ばい -> シグナルなし
    intraday_df = _make_df([100.0] * 20)  # 横ばい -> シグナルなし

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
        patch("main.get_intraday_bars_async", new=AsyncMock(return_value=intraday_df)), \
        patch("main.get_current_price_async", new=AsyncMock()) as mock_price, \
        patch("main.get_current_price_quote_async", new=AsyncMock()) as mock_quote, \
        patch("main.place_market_order_async", new=AsyncMock(return_value=_order_result())) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    mock_order.assert_not_awaited()
    mock_price.assert_not_awaited()
    mock_quote.assert_not_awaited()
    assert position_manager.has_position("AAPL") is False


def test_process_symbol_skips_entry_when_both_timeframes_have_no_data(trade_journal) -> None:
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=pd.DataFrame())), \
        patch("main.get_intraday_bars_async", new=AsyncMock(return_value=pd.DataFrame())), \
        patch("main.place_market_order_async", new=AsyncMock(return_value=_order_result())) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    mock_order.assert_not_awaited()
    assert position_manager.has_position("AAPL") is False


def test_process_symbol_skips_entry_when_max_concurrent_positions_reached(trade_journal) -> None:
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()
    # 上限ちょうどまでダミーポジションで埋める。定数から導出しているのは、
    # 上限値を変えたときにテストが「上限に達していない状態」を検証する
    # 別物にすり替わらないようにするため。
    for i in range(MAX_CONCURRENT_POSITIONS):
        position_manager.open_position(f"SYM{i}", entry_price=10.0, quantity=1)

    daily_df = _make_daily_df(drop=True)  # 大きく下落 -> 買いシグナル
    intraday_df = _make_df([100.0] * 20)

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)) as mock_qualify, \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
        patch("main.get_intraday_bars_async", new=AsyncMock(return_value=intraday_df)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=80.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(80.0))), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.get_settled_cash_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.place_market_order_async", new=AsyncMock(return_value=_order_result())) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    # 上限チェックはAPI呼び出し前に短絡するべき
    mock_qualify.assert_not_awaited()
    mock_order.assert_not_awaited()
    assert position_manager.has_position("AAPL") is False


def test_process_symbol_skips_entry_when_daily_loss_circuit_breaker_tripped(trade_journal) -> None:
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()

    # 口座資金100,000に対しMAX_DAILY_LOSS_PCT(3%)を超える損失(-4,000)を本日分として記録
    trade_journal.record_trade(
        symbol="MSFT", entry_price=100.0, exit_price=96.0, quantity=1000,
        reason="STOP_LOSS", pnl=-4_000.0, pnl_pct=-4.0, r_multiple=-1.0,
    )

    daily_df = _make_daily_df(drop=True)  # 大きく下落 -> 買いシグナル
    intraday_df = _make_df([100.0] * 20)

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
        patch("main.get_intraday_bars_async", new=AsyncMock(return_value=intraday_df)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=80.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(80.0))), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.get_settled_cash_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.place_market_order_async", new=AsyncMock(return_value=_order_result())) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    mock_order.assert_not_awaited()
    assert position_manager.has_position("AAPL") is False


def test_daily_loss_circuit_breaker_does_not_block_exits(trade_journal) -> None:
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()
    position_manager.open_position("AAPL", entry_price=100.0, quantity=3, risk_per_share=5.0)

    # 本日すでに大きな損失を記録済み(サーキットブレーカー発動中)でも、
    # 既存ポジションの決済(損切り等)は引き続き行われるべき
    trade_journal.record_trade(
        symbol="MSFT", entry_price=100.0, exit_price=96.0, quantity=1000,
        reason="STOP_LOSS", pnl=-4_000.0, pnl_pct=-4.0, r_multiple=-1.0,
    )

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=90.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(90.0))), \
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=150.0)), \
        patch("main.place_market_order_async", new=AsyncMock(return_value=_order_result())) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    mock_order.assert_awaited_once_with(ib, contract, action="SELL", quantity=3)
    assert position_manager.has_position("AAPL") is False


# --- 決済（ポジション保有中） ---------------------------------------------------


def test_process_symbol_closes_position_on_exit_signal(trade_journal) -> None:
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()
    opened_position = position_manager.open_position(
        "AAPL", entry_price=100.0, quantity=3, risk_per_share=5.0,
    )

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=90.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(90.0))), \
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=150.0)), \
        patch("main.place_market_order_async", new=AsyncMock(return_value=_order_result())) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    # pnl = -10% はデフォルトの stop_loss_pct(5%) を下回るため決済される
    mock_order.assert_awaited_once_with(ib, contract, action="SELL", quantity=3)
    assert position_manager.has_position("AAPL") is False

    # 損益トラッキング: ジャーナルに実現損益・R倍率が記録されること
    trades = trade_journal.load_trades()
    assert len(trades) == 1
    trade = trades[0]
    assert trade.symbol == "AAPL"
    assert trade.entry_price == 100.0
    assert trade.exit_price == 90.0
    assert trade.quantity == 3
    assert trade.reason == "STOP_LOSS"
    assert trade.pnl == pytest.approx((90.0 - 100.0) * 3)
    assert trade.pnl_pct == pytest.approx(-10.0)
    # r_multiple = (90-100) / risk_per_share(5.0) = -2.0
    assert trade.r_multiple == pytest.approx(-2.0)
    # ポジションの建玉日時(取得年月日)が決済記録にそのまま引き継がれること
    assert trade.entry_date == opened_position.entry_date
    # 決済時点のUSD/JPYレートが記録され、円換算損益が自動計算されること
    assert trade.commission == pytest.approx(0.0)
    assert trade.usd_jpy_rate == pytest.approx(150.0)
    assert trade.net_pnl_jpy == pytest.approx(trade.pnl * 150.0)

    stats = trade_journal.compute_stats()
    assert stats.num_trades == 1
    assert stats.win_rate_pct == 0.0
    assert stats.avg_r_multiple == pytest.approx(-2.0)


def test_exchange_rate_falls_back_to_the_account_summary(trade_journal) -> None:
    """為替の購読が無い口座でも円換算レートを記録すること。

    IDEALPROのUSD.JPYは追加購読が要るため3経路とも失敗し、2026-08-06の実測では
    usd_jpy_rate が空のまま記録されていた。稼働は続くが確定申告用CSVの円換算が
    その年ぶん埋まらない。口座サマリーのレートは購読なしで読める。
    """
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()
    position_manager.open_position("AAPL", entry_price=100.0, quantity=3, risk_per_share=5.0)

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=90.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(90.0))), \
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=None)), \
        patch("main.get_usd_to_base_rate_async", new=AsyncMock(return_value=160.05)) as mock_rate, \
        patch("main.place_market_order_async", new=AsyncMock(return_value=_order_result())):

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    mock_rate.assert_awaited_once_with(ib)
    assert trade_journal.load_trades()[0].usd_jpy_rate == pytest.approx(160.05)


def test_exchange_rate_is_left_empty_when_no_source_has_it(trade_journal) -> None:
    """どちらの経路でも取れなければ未記録のままにすること（推定で埋めない）。

    記録が無いことは集計側が扱える（円換算合計から除外される）が、
    間違ったレートは後から見分けられない。
    """
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()
    position_manager.open_position("AAPL", entry_price=100.0, quantity=3, risk_per_share=5.0)

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=90.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(90.0))), \
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=None)), \
        patch("main.get_usd_to_base_rate_async", new=AsyncMock(side_effect=ValueError("boom"))), \
        patch("main.place_market_order_async", new=AsyncMock(return_value=_order_result())):

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    trade = trade_journal.load_trades()[0]
    assert trade.usd_jpy_rate is None
    assert trade.net_pnl_jpy is None


def test_process_symbol_records_none_r_multiple_when_risk_per_share_unknown(trade_journal) -> None:
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()
    # risk_per_share未指定(=0.0、ブローカー同期で発見されたポジション相当)
    position_manager.open_position("AAPL", entry_price=100.0, quantity=3)

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=90.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(90.0))), \
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=150.0)), \
        patch("main.place_market_order_async", new=AsyncMock(return_value=_order_result())):

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    trades = trade_journal.load_trades()
    assert len(trades) == 1
    assert trades[0].r_multiple is None


def test_process_symbol_keeps_position_when_no_exit_signal(trade_journal) -> None:
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()
    position_manager.open_position("AAPL", entry_price=100.0, quantity=3)

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=101.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(101.0))), \
        patch("main.place_market_order_async", new=AsyncMock(return_value=_order_result())) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    mock_order.assert_not_awaited()
    assert position_manager.has_position("AAPL") is True
    assert position_manager.get_position("AAPL").highest_price == 101.0
    assert trade_journal.load_trades() == []


def test_process_symbol_updates_highest_price_for_trailing_stop(trade_journal) -> None:
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()
    position_manager.open_position("AAPL", entry_price=100.0, quantity=1)
    position_manager.update_highest_price("AAPL", 115.0)

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=108.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(108.0))), \
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=150.0)), \
        patch("main.place_market_order_async", new=AsyncMock(return_value=_order_result())) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    # pnl=+8%(利確10%未満)、高値115から108へ約6.1%下落（trailing_stop_pct=3.0%）-> トレーリングストップ発火
    mock_order.assert_awaited_once_with(ib, contract, action="SELL", quantity=1)
    assert position_manager.has_position("AAPL") is False
    assert trade_journal.load_trades()[0].reason == "TRAILING_STOP"


# --- 種別ごとの決済パラメータ・大引け前強制決済 -----------------------------------


def test_day_position_uses_tighter_stop_loss_than_swing_position(trade_journal) -> None:
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()
    # entry=100からの-3%は、swingの損切り(5%)には届かないが、dayの損切り(1.5%)には抵触する
    position_manager.open_position(
        "AAPL", entry_price=100.0, quantity=1, strategy_type=STRATEGY_TYPE_DAY,
    )

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=97.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(97.0))), \
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=150.0)), \
        patch("main.is_day_trade_flatten_time", return_value=False), \
        patch("main.place_market_order_async", new=AsyncMock(return_value=_order_result())) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    mock_order.assert_awaited_once_with(ib, contract, action="SELL", quantity=1)
    assert position_manager.has_position("AAPL") is False
    assert trade_journal.load_trades()[0].reason == "STOP_LOSS"


def test_swing_position_keeps_open_at_move_that_would_stop_out_a_day_position(trade_journal) -> None:
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()
    # entry=100からの-3%は、swingの損切り(5%)には届かない
    position_manager.open_position(
        "AAPL", entry_price=100.0, quantity=1, strategy_type=STRATEGY_TYPE_SWING,
    )

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=97.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(97.0))), \
        patch("main.is_day_trade_flatten_time", return_value=False), \
        patch("main.place_market_order_async", new=AsyncMock(return_value=_order_result())) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    mock_order.assert_not_awaited()
    assert position_manager.has_position("AAPL") is True


def test_day_position_force_closed_at_eod_flatten_time_even_without_exit_signal(trade_journal) -> None:
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()
    # pnl=+0.5%: 利確・損切り・トレーリングいずれの価格条件にも該当しない
    position_manager.open_position(
        "AAPL", entry_price=100.0, quantity=2, strategy_type=STRATEGY_TYPE_DAY,
    )

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=100.5)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(100.5))), \
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=150.0)), \
        patch("main.is_day_trade_flatten_time", return_value=True), \
        patch("main.place_market_order_async", new=AsyncMock(return_value=_order_result())) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    mock_order.assert_awaited_once_with(ib, contract, action="SELL", quantity=2)
    assert position_manager.has_position("AAPL") is False
    trades = trade_journal.load_trades()
    assert trades[0].reason == "EOD_FLATTEN"
    assert trades[0].pnl_pct == pytest.approx(0.5)


def test_swing_position_not_force_closed_at_eod_flatten_time(trade_journal) -> None:
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()
    position_manager.open_position(
        "AAPL", entry_price=100.0, quantity=2, strategy_type=STRATEGY_TYPE_SWING,
    )

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=100.5)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(100.5))), \
        patch("main.is_day_trade_flatten_time", return_value=True), \
        patch("main.place_market_order_async", new=AsyncMock(return_value=_order_result())) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    mock_order.assert_not_awaited()
    assert position_manager.has_position("AAPL") is True


def test_process_symbol_opens_day_position_with_day_specific_risk_per_share(trade_journal) -> None:
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()

    daily_df = _make_daily_df()  # 日足は横ばい -> シグナルなし
    intraday_df = _make_df([100.0] * 19 + [90.0])  # 短期足は下落 -> 買いシグナル(day)

    # 既定では無効な分岐だが、再有効化したときにデイトレード固有の損切り幅が
    # 使われなくなっていることに気付けるよう、フラグを立てて検証する。
    with patch("main.ENABLE_DAY_TRADING", True), \
        patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
        patch("main.get_intraday_bars_async", new=AsyncMock(return_value=intraday_df)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=90.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(90.0))), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.get_settled_cash_async", new=AsyncMock(return_value=100_000.0)), \
        patch(
            "main.place_market_order_async",
            new=AsyncMock(
                side_effect=lambda ib, contract, action, quantity: OrderResult(
                    symbol="AAPL", action=action, quantity=quantity, order_type="MKT"
                )
            ),
        ):

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    position = position_manager.get_position("AAPL")
    assert position.strategy_type == STRATEGY_TYPE_DAY
    # risk_per_share = 90 * DAY_STOP_LOSS_PCT(1.5%) / 100 = 1.35 (SWING基準の4.5とは異なる)
    assert position.risk_per_share == pytest.approx(90.0 * DAY_STOP_LOSS_PCT / 100.0)
    assert DAY_STOP_LOSS_PCT != SWING_STOP_LOSS_PCT


# --- 銘柄横断ループ -------------------------------------------------------------


def test_run_watchlist_cycle_continues_after_symbol_error(trade_journal) -> None:
    ib = MagicMock()
    position_manager = PositionManager()

    async def _boom(_ib, symbol, _pm, _tj):
        raise RuntimeError("boom")

    with patch.object(position_manager, "sync_with_broker_async", new=AsyncMock()), \
        patch("main.process_symbol_async", new=AsyncMock(side_effect=_boom)) as mock_process:

        asyncio.run(run_watchlist_cycle_async(ib, ["RIVN", "JOBY"], position_manager, trade_journal))

    assert mock_process.await_count == 2


def test_run_watchlist_cycle_syncs_with_broker_before_processing_symbols(trade_journal) -> None:
    ib = MagicMock()
    position_manager = PositionManager()

    with patch.object(position_manager, "sync_with_broker_async", new=AsyncMock()) as mock_sync, \
        patch("main.process_symbol_async", new=AsyncMock()):

        asyncio.run(run_watchlist_cycle_async(ib, ["RIVN"], position_manager, trade_journal))

    mock_sync.assert_awaited_once_with(ib)


def test_run_watchlist_cycle_still_processes_position_dropped_from_watchlist(trade_journal) -> None:
    # スクリーニング結果の入れ替えで銘柄がウォッチリストから外れても、
    # 保有中ポジションの決済判定(利確・損切り等)は継続されるべき。
    ib = MagicMock()
    position_manager = PositionManager()
    position_manager.open_position("XOM", entry_price=100.0, quantity=1)

    with patch.object(position_manager, "sync_with_broker_async", new=AsyncMock()), \
        patch("main.process_symbol_async", new=AsyncMock()) as mock_process:

        # 今日のスクリーニング結果には("XOM"が外れて)"NEWSTOCK"だけが含まれる
        asyncio.run(run_watchlist_cycle_async(ib, ["NEWSTOCK"], position_manager, trade_journal))

    processed_symbols = [call.args[1] for call in mock_process.await_args_list]
    assert "XOM" in processed_symbols
    assert "NEWSTOCK" in processed_symbols
    assert mock_process.await_count == 2


def test_run_watchlist_cycle_does_not_process_symbol_twice_when_in_both_watchlist_and_positions(
    trade_journal,
) -> None:
    ib = MagicMock()
    position_manager = PositionManager()
    position_manager.open_position("XOM", entry_price=100.0, quantity=1)

    with patch.object(position_manager, "sync_with_broker_async", new=AsyncMock()), \
        patch("main.process_symbol_async", new=AsyncMock()) as mock_process:

        asyncio.run(run_watchlist_cycle_async(ib, ["XOM", "NEWSTOCK"], position_manager, trade_journal))

    processed_symbols = [call.args[1] for call in mock_process.await_args_list]
    assert processed_symbols.count("XOM") == 1
    assert mock_process.await_count == 2


# --- ウォッチリストのスクリーニング更新 -------------------------------------------


def test_refresh_watchlist_uses_screening_result_when_available() -> None:
    ib = MagicMock()

    with patch("main.screen_value_stocks_async", new=AsyncMock(return_value=["CHEAP1", "CHEAP2"])):
        result = asyncio.run(_refresh_watchlist_async(ib, ["FALLBACK"], account_equity=100_000.0))

    assert result == WatchlistRefresh(["CHEAP1", "CHEAP2"], screened=True)


@contextmanager
def _fallback_prices(prices: dict):
    """フォールバック銘柄の株価を日足キャッシュ経由で与える。"""
    async def _bars(ib, contract, now=None):
        price = prices.get(contract.symbol)
        if price is None:
            return pd.DataFrame()
        return _make_df([price] * 5)

    with patch("data.cache.qualify_stock_async",
               new=AsyncMock(side_effect=lambda ib, symbol: MagicMock(symbol=symbol))), \
        patch("data.cache.DailyBarCache.get_async", new=AsyncMock(side_effect=_bars)):
        yield


def test_refresh_watchlist_falls_back_when_screening_returns_empty() -> None:
    ib = MagicMock()

    # 資金1,220の取引可能帯は $24.40〜$244。FALLBACKはその中に入れる。
    with patch("main.screen_value_stocks_async", new=AsyncMock(return_value=[])), \
        _fallback_prices({"FALLBACK": 100.0}):
        result = asyncio.run(_refresh_watchlist_async(ib, ["FALLBACK"], account_equity=1220.0))

    assert result == WatchlistRefresh(["FALLBACK"], screened=False)


def test_refresh_watchlist_falls_back_when_screening_raises() -> None:
    ib = MagicMock()

    with patch("main.screen_value_stocks_async", new=AsyncMock(side_effect=RuntimeError("boom"))), \
        _fallback_prices({"FALLBACK": 100.0}):
        result = asyncio.run(_refresh_watchlist_async(ib, ["FALLBACK"], account_equity=1220.0))

    assert result == WatchlistRefresh(["FALLBACK"], screened=False)


def test_fallback_watchlist_is_filtered_by_the_tradeable_price_band() -> None:
    """フォールバックの固定リストにも株価帯を掛けること。

    掛けないと、資金額と無関係に書かれた main.WATCHLIST の銘柄がそのまま
    監視枠に入る。上限を超える銘柄は数量0株で永久に建たず、下限を下回る銘柄は
    株数クランプでリスクベースのサイジングが効かない。
    """
    ib = MagicMock()
    prices = {
        "PENNY": 3.00,     # 下限($6.10)割れ -> 株数クランプ
        "NORMAL": 100.0,   # 取引可能
        "PRICEY": 336.91,  # 上限($244)超え -> 数量0株
    }

    with patch("main.screen_value_stocks_async", new=AsyncMock(return_value=[])), \
        _fallback_prices(prices):
        result = asyncio.run(_refresh_watchlist_async(
            ib, ["PENNY", "NORMAL", "PRICEY"], account_equity=1220.0,
        ))

    assert result == WatchlistRefresh(["NORMAL"], screened=False)


def test_fallback_watchlist_drops_symbols_with_unknown_price() -> None:
    """株価が取れない銘柄は除外に倒すこと（スクリーナー側と揃える）。"""
    ib = MagicMock()

    with patch("main.screen_value_stocks_async", new=AsyncMock(return_value=[])), \
        _fallback_prices({"NORMAL": 100.0}):
        result = asyncio.run(_refresh_watchlist_async(
            ib, ["NORMAL", "NOBARS"], account_equity=1220.0,
        ))

    assert result == WatchlistRefresh(["NORMAL"], screened=False)


def test_empty_tradeable_band_yields_an_empty_watchlist_not_a_silent_fallback(caplog) -> None:
    """取引可能な銘柄が1つも無いなら、空で返してERRORを出すこと。

    ここで固定リストをそのまま返すと、買えない銘柄を監視し続ける。
    資金$100,000では帯が $2,000〜$20,000 になり該当する米国株がほぼ無いため、
    増資やMAX_POSITION_SIZEの変更でこの状態は現実に起こりうる。

    空にしても保有中のポジションの決済判定は止まらない
    （run_watchlist_cycle_asyncが保有銘柄との和集合を処理するため）。
    """
    ib = MagicMock()

    with patch("main.screen_value_stocks_async", new=AsyncMock(return_value=[])), \
        _fallback_prices({"AAPL": 336.91, "MSFT": 389.10}), \
        caplog.at_level(logging.ERROR):
        result = asyncio.run(_refresh_watchlist_async(
            ib, ["AAPL", "MSFT"], account_equity=100_000.0,
        ))

    assert result == WatchlistRefresh([], screened=False)
    assert any(record.levelno == logging.ERROR for record in caplog.records)


def test_price_band_filter_reuses_the_daily_bar_cache() -> None:
    """フォールバックの株価判定でIBKRリクエストを増やさないこと。

    ペーシング制限(§6.1)の枠は銘柄あたり毎サイクル1件しか無い。ここで
    日足を取り直すと、同じ銘柄の日足をこの後のシグナル判定でも取ることになる。
    """
    ib = MagicMock()
    caches = MarketDataCaches()
    bars = _make_df([100.0] * 5)

    with patch("main.screen_value_stocks_async", new=AsyncMock(return_value=[])), \
        patch("data.cache.qualify_stock_async",
              new=AsyncMock(side_effect=lambda ib, symbol: MagicMock(symbol=symbol))), \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=bars)) as mock_bars:
        asyncio.run(_refresh_watchlist_async(
            ib, ["NORMAL"], account_equity=1220.0, caches=caches,
        ))
        # 同じ取引日の2回目はキャッシュから返るため、リクエストは増えない。
        contract = asyncio.run(caches.contracts.get_async(ib, "NORMAL"))
        asyncio.run(caches.daily_bars.get_async(ib, contract))

    assert mock_bars.await_count == 1


# --- main()のTWS接続維持（切断検知・再接続） ---------------------------------------


def _make_fake_connection(connect_side_effect) -> MagicMock:
    connection = MagicMock()
    connection.connect_async = AsyncMock(side_effect=connect_side_effect)
    connection.disconnect_async = AsyncMock()
    return connection


def test_main_reconnects_when_ib_reports_disconnected() -> None:
    ib_disconnected = MagicMock()
    ib_disconnected.isConnected = MagicMock(return_value=False)
    ib_connected = MagicMock()
    ib_connected.isConnected = MagicMock(return_value=True)
    connection = _make_fake_connection([ib_disconnected, ib_connected])

    sleep_calls = {"n": 0}

    async def fake_sleep(_seconds):
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= 2:
            raise KeyboardInterrupt()

    with patch("main.IBKRConnection", return_value=connection), \
        patch("main.is_regular_trading_hours", return_value=False), \
        patch("main.asyncio.sleep", new=fake_sleep):
        asyncio.run(main())

    # 1回目: ib=None -> connect。2回目: 前回のibがisConnected()=Falseを返す -> 再接続。
    assert connection.connect_async.await_count == 2
    connection.disconnect_async.assert_awaited_once()


def test_main_continues_after_unexpected_exception_in_cycle() -> None:
    ib = MagicMock()
    ib.isConnected = MagicMock(return_value=True)
    connection = _make_fake_connection([ib])

    sleep_calls = {"n": 0}

    async def fake_sleep(_seconds):
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= 2:
            raise KeyboardInterrupt()

    with patch("main.IBKRConnection", return_value=connection), \
        patch("main.is_regular_trading_hours", return_value=True), \
        patch(
            "main._refresh_watchlist_async",
            new=AsyncMock(return_value=WatchlistRefresh(["AAPL"], screened=True)),
        ), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.get_settled_cash_async", new=AsyncMock(return_value=100_000.0)), \
        patch(
            "main.run_watchlist_cycle_async", new=AsyncMock(side_effect=RuntimeError("boom")),
        ) as mock_cycle, \
        patch("main.asyncio.sleep", new=fake_sleep):
        asyncio.run(main())

    # サイクル処理中の例外でプロセス全体が落ちず、次のループでも処理が継続される
    assert mock_cycle.await_count == 2
    connection.disconnect_async.assert_awaited_once()


def test_main_retries_after_connection_error_exhausted() -> None:
    connection = _make_fake_connection(ConnectionError("TWSへの接続に失敗しました"))

    sleep_calls = {"n": 0}

    async def fake_sleep(_seconds):
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= 2:
            raise KeyboardInterrupt()

    with patch("main.IBKRConnection", return_value=connection), \
        patch("main.asyncio.sleep", new=fake_sleep):
        asyncio.run(main())

    # connect_asyncのリトライを使い果たしても、プロセスを落とさず再試行し続ける
    assert connection.connect_async.await_count == 2
    connection.disconnect_async.assert_awaited_once()


# --- 復帰しない切断の切り分け ------------------------------------------------------

# 再試行のログだけでは、瞬断・Auto restart（数分で復帰する）と、再ログインが要る
# 状態（再試行では永久に解けない）が区別できない。2026-08-04のログでは同じ2行が
# 9分おきに6回並んでいた。


_MANUAL_LOGIN_HINT = "再ログインが必要な可能性があります"


def _run_main_with_connection(connection, max_sleeps: int, market_open: bool = True) -> None:
    sleep_calls = {"n": 0}

    async def fake_sleep(_seconds):
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= max_sleeps:
            raise KeyboardInterrupt()

    with patch("main.IBKRConnection", return_value=connection), \
        patch("main.is_regular_trading_hours", return_value=market_open), \
        patch("main.run_watchlist_cycle_async", new=AsyncMock()), \
        patch("main._refresh_watchlist_async",
              new=AsyncMock(return_value=WatchlistRefresh([], screened=True))), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=1220.0)), \
        patch("main.get_settled_cash_async", new=AsyncMock(return_value=1220.0)), \
        patch("main.asyncio.sleep", new=fake_sleep):
        asyncio.run(main())


def test_manual_login_hint_is_not_emitted_for_a_short_outage(caplog) -> None:
    """Auto restart相当の長さでは出さないこと（誤検知するとこの行の意味が薄れる）。"""
    connection = _make_fake_connection(ConnectionError("boom"))

    with caplog.at_level(logging.ERROR, logger="main"):
        _run_main_with_connection(connection, CONNECTION_FAILURE_ROUNDS_BEFORE_MANUAL_LOGIN - 1)

    assert _MANUAL_LOGIN_HINT not in caplog.text


def test_manual_login_hint_is_emitted_once_after_repeated_failures(caplog) -> None:
    """規定ラウンドを超えたら、繰り返しではなく1行だけ出すこと。"""
    connection = _make_fake_connection(ConnectionError("boom"))

    with caplog.at_level(logging.ERROR, logger="main"):
        # 規定ラウンドの倍まで失敗を続けても、この行は増えない。
        _run_main_with_connection(connection, CONNECTION_FAILURE_ROUNDS_BEFORE_MANUAL_LOGIN * 2)

    assert caplog.text.count(_MANUAL_LOGIN_HINT) == 1


def test_closed_market_outages_do_not_trigger_the_manual_login_hint(caplog) -> None:
    """市場時間外の接続失敗は数えないこと。

    IB Gatewayを日次で落とす運用（検証中は日本時間8:00にログアウトし22:30に
    再ログイン）では、閉場中に何十ラウンドも失敗するのが正常な状態である。
    数えると案内が毎日出て、本当に人手が要るときと見分けがつかなくなる。
    """
    connection = _make_fake_connection(ConnectionError("boom"))

    with caplog.at_level(logging.INFO, logger="main"):
        _run_main_with_connection(
            connection, CONNECTION_FAILURE_ROUNDS_BEFORE_MANUAL_LOGIN * 3, market_open=False,
        )

    assert _MANUAL_LOGIN_HINT not in caplog.text
    # 沈黙はさせない（再試行していることは残す）。
    assert "市場時間外" in caplog.text


def test_connection_failure_streak_resets_after_a_successful_connect(caplog) -> None:
    """接続に成功したら連続回数を0に戻すこと。

    戻さないと、断続的な瞬断が積み上がって「再ログインが要る」と誤って
    言い出し、本当に人手が要るときの行と見分けがつかなくなる。
    """
    ib = MagicMock()
    # 接続直後のサイクルはisConnected()=True、その次のサイクルでは切れている
    # （= 再度connect_asyncが呼ばれる）。
    ib.isConnected = MagicMock(side_effect=[True, False] + [False] * 10)
    failures_before = CONNECTION_FAILURE_ROUNDS_BEFORE_MANUAL_LOGIN - 1
    failures_after = CONNECTION_FAILURE_ROUNDS_BEFORE_MANUAL_LOGIN - 1
    connection = _make_fake_connection(
        [ConnectionError("boom")] * failures_before + [ib] + [ConnectionError("boom")] * 10
    )

    with caplog.at_level(logging.ERROR, logger="main"):
        # 合計の失敗回数は規定ラウンドを超えるが、間に成功を挟んでいる。
        _run_main_with_connection(connection, failures_before + 1 + failures_after)

    assert _MANUAL_LOGIN_HINT not in caplog.text


# --- スクリーニング失敗後の再試行 --------------------------------------------------

# スキャナーもPER取得も購読権限が無いと例外ではなく空を返すが（6.2）、同じ形の
# 空応答は起動直後やデータファームの再接続中にも起きる。1回目の結果でその日の
# 選定を確定させると、一時的な失敗が一日ぶんの固定リスト運転になる。


@contextmanager
def _main_loop_at(times, refresh_results):
    """指定した東部時間で main() のサイクルを回す。

    times の要素数だけサイクルを回して KeyboardInterrupt で抜ける。
    """
    clock = iter(times)
    ib = MagicMock()
    ib.isConnected = MagicMock(return_value=True)
    connection = _make_fake_connection([ib] * (len(times) + 1))

    def _now(_tz=None):
        try:
            return next(clock)
        except StopIteration:
            raise KeyboardInterrupt()

    async def fake_sleep(_seconds):
        return None

    refresh = AsyncMock(side_effect=refresh_results)
    fake_datetime = MagicMock()
    fake_datetime.now = _now

    with patch("main.IBKRConnection", return_value=connection), \
        patch("main.is_regular_trading_hours", return_value=True), \
        patch("main.datetime", fake_datetime), \
        patch("main._refresh_watchlist_async", new=refresh), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=1220.0)), \
        patch("main.get_settled_cash_async", new=AsyncMock(return_value=1220.0)), \
        patch("main.run_watchlist_cycle_async", new=AsyncMock()) as cycle, \
        patch("main.asyncio.sleep", new=fake_sleep):
        asyncio.run(main())
        yield refresh, cycle


def test_screening_is_retried_after_a_fallback() -> None:
    """フォールバックした日は、間隔を空けて再試行すること。"""
    start = datetime(2026, 8, 4, 10, 0, tzinfo=US_EASTERN)
    times = [start, start + timedelta(seconds=SCREENING_RETRY_INTERVAL_SECONDS)]

    with _main_loop_at(
        times,
        [
            WatchlistRefresh(["FALLBACK"], screened=False),
            WatchlistRefresh(["SCREENED"], screened=True),
        ],
    ) as (refresh, cycle):
        pass

    assert refresh.await_count == 2
    # 再試行が成功した時点で、そのサイクルからスクリーニング結果を監視する。
    assert cycle.await_args_list[-1].args[1] == ["SCREENED"]


def test_successful_screening_is_not_repeated_within_the_day() -> None:
    """成功した日は再実行しないこと（スキャナー要求を1日1回に保つ）。"""
    start = datetime(2026, 8, 4, 10, 0, tzinfo=US_EASTERN)
    times = [start, start + timedelta(hours=3)]

    with _main_loop_at(times, [WatchlistRefresh(["SCREENED"], screened=True)]) as (refresh, _):
        pass

    assert refresh.await_count == 1


def test_screening_retry_waits_for_the_retry_interval() -> None:
    """再試行は毎サイクルではないこと。

    購読権限が無い口座では失敗が復旧しないため、毎サイクル(300秒)叩くと
    一日中スキャナー要求とログを出し続けることになる。
    """
    start = datetime(2026, 8, 4, 10, 0, tzinfo=US_EASTERN)
    times = [start, start + timedelta(seconds=POLL_INTERVAL_SECONDS)]

    with _main_loop_at(times, [WatchlistRefresh(["FALLBACK"], screened=False)]) as (refresh, cycle):
        pass

    assert refresh.await_count == 1
    # 再試行を待つ間もフォールバックのウォッチリストで監視は続く。
    assert cycle.await_count == 2
    assert cycle.await_args_list[-1].args[1] == ["FALLBACK"]


def test_fallback_source_is_not_narrowed_by_previous_fallbacks() -> None:
    """フォールバック元は成功時だけ入れ替えること。

    株価帯で絞られた結果を次回のフォールバック元にすると、失敗が重なるほど
    監視候補が痩せる。空で返った日には候補そのものが消える。
    """
    start = datetime(2026, 8, 4, 10, 0, tzinfo=US_EASTERN)
    times = [start, start + timedelta(seconds=SCREENING_RETRY_INTERVAL_SECONDS)]

    with _main_loop_at(
        times,
        [
            WatchlistRefresh([], screened=False),
            WatchlistRefresh([], screened=False),
        ],
    ) as (refresh, _):
        pass

    assert [call.args[1] for call in refresh.await_args_list] == [WATCHLIST, WATCHLIST]


# --- ペーシング制限対策 ----------------------------------------------------------


def test_watchlist_is_capped_to_limit_screening_request_volume() -> None:
    """監視銘柄1件につき毎サイクル1回の日中足リクエストが発生するため、
    スクリーニングが何件返しても監視対象は上限で頭打ちにする。"""
    screened = [f"SYM{i}" for i in range(MAX_WATCHLIST_SIZE + 15)]

    with patch("main.screen_value_stocks_async", new=AsyncMock(return_value=screened)):
        result = asyncio.run(_refresh_watchlist_async(MagicMock(), ["FALLBACK"], account_equity=100_000.0))

    assert len(result.symbols) == MAX_WATCHLIST_SIZE
    assert result.symbols == screened[:MAX_WATCHLIST_SIZE]


def test_watchlist_below_cap_is_kept_intact() -> None:
    screened = ["CHEAP1", "CHEAP2"]

    with patch("main.screen_value_stocks_async", new=AsyncMock(return_value=screened)):
        result = asyncio.run(_refresh_watchlist_async(MagicMock(), ["FALLBACK"], account_equity=100_000.0))

    assert result.symbols == screened


def test_poll_interval_keeps_watchlist_within_ibkr_pacing_limit() -> None:
    """IBKRのヒストリカルデータ制限(10分あたり60件)を設定値が満たしていること。

    監視銘柄1件あたり毎サイクル1リクエスト(日中足)なので、
        MAX_WATCHLIST_SIZE * (600 / POLL_INTERVAL_SECONDS) <= 60
    を満たす必要がある。片方だけ変更して制限を割るのを防ぐための番人。
    """
    requests_per_10min = MAX_WATCHLIST_SIZE * (600.0 / POLL_INTERVAL_SECONDS)

    assert requests_per_10min <= 60


def test_fallback_watchlist_has_no_duplicates() -> None:
    assert len(set(WATCHLIST)) == len(WATCHLIST), "重複した銘柄が監視枠を二重に消費します。"


def test_fallback_watchlist_is_truncated_to_the_monitoring_cap() -> None:
    """フォールバック経路も MAX_WATCHLIST_SIZE で切り詰めること。

    株価帯を通る件数は株価しだいで日々変わるため、固定リストの長さだけでは
    上のペーシング不変条件を保証できない。切り詰めが無いと、帯を通る銘柄が
    増えた日に黙って制限を割る。

    切り詰める順序は記載順である（成績を見て決めた順ではない）。
    """
    ib = MagicMock()
    symbols = [f"SYM{i:02d}" for i in range(MAX_WATCHLIST_SIZE + 5)]

    with patch("main.screen_value_stocks_async", new=AsyncMock(return_value=[])), \
        _fallback_prices({symbol: 100.0 for symbol in symbols}):
        result = asyncio.run(_refresh_watchlist_async(ib, symbols, account_equity=1220.0))

    assert result.symbols == symbols[:MAX_WATCHLIST_SIZE]
    assert result.screened is False


def test_daily_bars_are_fetched_once_per_symbol_across_cycles(trade_journal) -> None:
    """日足はキャッシュされ、サイクルごとに取り直さないこと。"""
    ib = MagicMock()
    ib.reqPositionsAsync = AsyncMock(return_value=[])
    position_manager = PositionManager()
    caches = MarketDataCaches()

    daily_df = _make_daily_df()      # 横ばい -> シグナルなし
    intraday_df = _make_df([100.0] * 20)

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=MagicMock(symbol="AAPL"))), \
        patch(
            "data.cache.get_historical_bars_async", new=AsyncMock(return_value=daily_df)
        ) as mock_daily, \
        patch("main.get_intraday_bars_async", new=AsyncMock(return_value=intraday_df)) as mock_intraday:

        async def run():
            for _ in range(3):
                await run_watchlist_cycle_async(
                    ib, ["AAPL"], position_manager, trade_journal, caches,
                )

        asyncio.run(run())

    # 日足は1取引日に1本しか増えないため1回だけ
    mock_daily.assert_awaited_once()
    # 日中足はENABLE_DAY_TRADING=False（既定）では使わないため一度も取得しない。
    # 有効時は「デイトレードのシグナルそのもの」なので毎サイクル取得する
    # （そちらは test_intraday_bars_are_fetched_every_cycle_when_day_trading_is_enabled）。
    assert mock_intraday.await_count == 0


def test_intraday_bars_are_fetched_every_cycle_when_day_trading_is_enabled(trade_journal) -> None:
    """日中足はキャッシュしてはならない（デイトレードのシグナルそのものであるため）。

    ENABLE_DAY_TRADINGを再有効化したときに、日足と同じくキャッシュに載せて
    しまう改変が入っても気付けるようにするための番人。
    """
    ib = MagicMock()
    ib.reqPositionsAsync = AsyncMock(return_value=[])
    position_manager = PositionManager()
    caches = MarketDataCaches()

    daily_df = _make_daily_df()      # 横ばい -> シグナルなし
    intraday_df = _make_df([100.0] * 20)

    with patch("main.ENABLE_DAY_TRADING", True), \
        patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=MagicMock(symbol="AAPL"))), \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
        patch("main.get_intraday_bars_async", new=AsyncMock(return_value=intraday_df)) as mock_intraday:

        async def run():
            for _ in range(3):
                await run_watchlist_cycle_async(
                    ib, ["AAPL"], position_manager, trade_journal, caches,
                )

        asyncio.run(run())

    assert mock_intraday.await_count == 3


def test_contracts_are_qualified_once_per_symbol_across_cycles(trade_journal) -> None:
    ib = MagicMock()
    ib.reqPositionsAsync = AsyncMock(return_value=[])
    position_manager = PositionManager()
    caches = MarketDataCaches()

    with patch(
        "data.cache.qualify_stock_async", new=AsyncMock(return_value=MagicMock(symbol="AAPL"))
    ) as mock_qualify, \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=_make_daily_df())), \
        patch("main.get_intraday_bars_async", new=AsyncMock(return_value=_make_df([100.0] * 20))):

        async def run():
            for _ in range(3):
                await run_watchlist_cycle_async(
                    ib, ["AAPL"], position_manager, trade_journal, caches,
                )

        asyncio.run(run())

    mock_qualify.assert_awaited_once()


# --- ブローカー側の待機注文による決済 ------------------------------------------------


def test_resting_stop_order_closes_the_position_without_a_market_order(trade_journal) -> None:
    """逆指値がブローカー側で約定していたら、ボットからは何も発注しないこと。

    約定価格は、ポーリングで観測できた現在値(85.0)を採る。逆指値(95.0)どおりに
    約定したと仮定するのは楽観的すぎるため（ポーリングの間に窓を開けて
    下抜けた可能性を否定できない）、不利な側に倒して記録する。
    """
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()
    position_manager.open_position(
        "AAPL", entry_price=100.0, quantity=3, risk_per_share=5.0,
        stop_price=95.0, take_profit_price=110.0, oca_group="OCA_1",
    )

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=85.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(85.0))), \
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=150.0)), \
        patch("main.place_market_order_async", new=AsyncMock(return_value=_order_result())) as mock_order, \
        patch("main.cancel_bracket_orders_async", new=AsyncMock()) as mock_cancel:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    # 既にブローカー側で約定しているので、ボットからは何も発注しない。
    mock_order.assert_not_awaited()
    # OCAグループの相方はIBKRが自動で取り消すため、取り消し要求も出さない。
    mock_cancel.assert_not_awaited()

    assert position_manager.has_position("AAPL") is False
    trade = trade_journal.load_trades()[0]
    assert trade.reason == "STOP_LOSS"
    assert trade.exit_price == pytest.approx(85.0)
    assert trade.pnl == pytest.approx((85.0 - 100.0) * 3)


def test_resting_limit_order_closes_position_at_the_take_profit_price(trade_journal) -> None:
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()
    position_manager.open_position(
        "AAPL", entry_price=100.0, quantity=3, risk_per_share=5.0,
        stop_price=95.0, take_profit_price=110.0, oca_group="OCA_1",
    )

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=115.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(115.0))), \
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=150.0)), \
        patch("main.place_market_order_async", new=AsyncMock(return_value=_order_result())) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    mock_order.assert_not_awaited()
    trade = trade_journal.load_trades()[0]
    assert trade.reason == "TAKE_PROFIT"
    assert trade.exit_price == pytest.approx(110.0)


def test_trailing_stop_cancels_resting_orders_before_selling_at_market(trade_journal) -> None:
    """ボット側の判断で成行決済する際は、先に待機注文を取り消すこと。

    取り消さないと建玉が無いのに売り注文だけが残り、次にその銘柄を
    建てた瞬間に意図しない決済が起きる。
    """
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()
    position_manager.open_position(
        "AAPL", entry_price=100.0, quantity=3, risk_per_share=5.0,
        stop_price=95.0, take_profit_price=110.0, oca_group="OCA_1",
    )
    # 高値108まで伸びた後、104へ下落 -> 高値から-3.7%でトレーリング発火(swing基準は-5%…
    # ではなく、ここではデフォルトのswing 5%に届くよう102.5まで下げる)
    position_manager.update_highest_price("AAPL", 108.0)

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=102.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(102.0))), \
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=150.0)), \
        patch("main.place_market_order_async", new=AsyncMock(return_value=_order_result())) as mock_order, \
        patch("main.cancel_bracket_orders_async", new=AsyncMock()) as mock_cancel:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    mock_cancel.assert_awaited_once_with(ib, "AAPL")
    mock_order.assert_awaited_once_with(ib, contract, action="SELL", quantity=3)
    assert trade_journal.load_trades()[0].reason == "TRAILING_STOP"


def test_market_exit_is_skipped_when_the_cancellation_does_not_confirm(trade_journal) -> None:
    """待機注文を取り消せなかったら、成行売りも置き直しもしないこと。

    生きている売り注文へ成行の売りを重ねると売り超過になり、IBKRが空売りと
    見なして拒否する（実測 Error 201）。置き直しに進めば同じ建玉に売り注文が
    二重に並ぶ。取り消せていないということは待機注文がまだ建玉を守っている
    ということでもあるので、次のサイクルへ持ち越すのが安全側である。
    """
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()
    position_manager.open_position(
        "AAPL", entry_price=100.0, quantity=3, risk_per_share=5.0,
        stop_price=95.0, take_profit_price=110.0, oca_group="OCA_1",
    )
    position_manager.update_highest_price("AAPL", 108.0)

    # 取り消しと成行の順序はドライラン/実発注で変わらない（conftestでドライラン固定）。
    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=102.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(102.0))), \
        patch("main.place_market_order_async", new=AsyncMock()) as mock_order, \
        patch("main.place_resting_exit_orders_async", new=AsyncMock()) as mock_restore, \
        patch(
            "main.cancel_bracket_orders_async",
            new=AsyncMock(side_effect=RestingOrderCancelTimeoutError("timeout")),
        ):

        with pytest.raises(RestingOrderCancelTimeoutError):
            asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    mock_order.assert_not_awaited()
    mock_restore.assert_not_awaited()
    # 建玉はローカルにも残る（売れていないのに閉じない）。
    assert position_manager.has_position("AAPL")
    assert trade_journal.load_trades() == []


def test_eod_flatten_cancels_resting_orders_before_selling_at_market(trade_journal) -> None:
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()
    position_manager.open_position(
        "AAPL", entry_price=100.0, quantity=2, strategy_type=STRATEGY_TYPE_DAY,
        stop_price=98.5, take_profit_price=103.0, oca_group="OCA_2",
    )

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=100.5)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(100.5))), \
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=150.0)), \
        patch("main.is_day_trade_flatten_time", return_value=True), \
        patch("main.place_market_order_async", new=AsyncMock(return_value=_order_result())) as mock_order, \
        patch("main.cancel_bracket_orders_async", new=AsyncMock()) as mock_cancel:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    mock_cancel.assert_awaited_once_with(ib, "AAPL")
    mock_order.assert_awaited_once_with(ib, contract, action="SELL", quantity=2)
    assert trade_journal.load_trades()[0].reason == "EOD_FLATTEN"


def test_broker_synced_position_without_resting_orders_still_exits_on_signal(trade_journal) -> None:
    """待機注文を持たない（ブローカー同期で取り込んだ）ポジションも決済できること。"""
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()
    # stop_price / take_profit_price は 0.0 のまま
    position_manager.open_position("AAPL", entry_price=100.0, quantity=3)

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=90.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(90.0))), \
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=150.0)), \
        patch("main.place_market_order_async", new=AsyncMock(return_value=_order_result())) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    mock_order.assert_awaited_once_with(ib, contract, action="SELL", quantity=3)
    assert trade_journal.load_trades()[0].reason == "STOP_LOSS"


# --- 当日中の再エントリー禁止（クールダウン） -----------------------------------------


def test_entry_is_skipped_for_a_symbol_already_closed_today(trade_journal) -> None:
    """同じ日に決済済みの銘柄は、買いシグナルが出ていても買い直さないこと。"""
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()
    position_manager.open_position("AAPL", entry_price=100.0, quantity=3)
    position_manager.close_position("AAPL")

    daily_df = _make_daily_df(drop=True)  # 大きく下落 -> 買いシグナルは出ている

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)) as mock_qualify, \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
        patch("main.get_intraday_bars_async", new=AsyncMock(return_value=_make_df([100.0] * 20))), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=80.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(80.0))), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.get_settled_cash_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.place_bracket_order_async", new=AsyncMock(return_value=_bracket_result(quantity=1))) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    # クールダウン判定はIBKRへの問い合わせより前に短絡すること（無駄なリクエストを出さない）。
    mock_qualify.assert_not_awaited()
    mock_order.assert_not_awaited()
    assert position_manager.has_position("AAPL") is False


def test_cooldown_does_not_block_other_symbols(trade_journal) -> None:
    contract = MagicMock(symbol="MSFT")
    ib = MagicMock()
    position_manager = PositionManager()
    position_manager.open_position("AAPL", entry_price=100.0, quantity=3)
    position_manager.close_position("AAPL")

    daily_df = _make_daily_df(drop=True)

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
        patch("main.get_intraday_bars_async", new=AsyncMock(return_value=_make_df([100.0] * 20))), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=80.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(80.0))), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.get_settled_cash_async", new=AsyncMock(return_value=100_000.0)), \
        patch(
            "main.place_bracket_order_async",
            new=AsyncMock(return_value=_bracket_result(quantity=250, symbol="MSFT")),
        ) as mock_order:

        asyncio.run(process_symbol_async(ib, "MSFT", position_manager, trade_journal))

    mock_order.assert_awaited_once()
    assert position_manager.has_position("MSFT") is True


def test_concurrent_position_limit_fits_within_the_account() -> None:
    """同時保有の上限まで建てても、スイングの建玉合計が資金を超えないこと。

    リスクベースのサイジングでは、1ポジションが占める資金の割合が株価に
    よらず一定になる:
        建玉金額 = 数量 × 株価 = 資金 × (RISK_PER_TRADE_PCT% ÷ 損切り%)
    したがって同時保有数を増やすと、株価と無関係に資金を使い切る。
    小口座では既定値がそのまま資金の上限に当たるため、番人として固定する。
    """
    equity_fraction_per_position = RISK_PER_TRADE_PCT / SWING_STOP_LOSS_PCT
    total = MAX_CONCURRENT_POSITIONS * equity_fraction_per_position

    assert total <= 1.0, (
        f"同時保有{MAX_CONCURRENT_POSITIONS}銘柄で資金の{total * 100:.0f}%を"
        "建玉に使うことになります。"
    )


# --- 1日の新規建て回数の上限 -----------------------------------------------------


def test_entry_is_blocked_after_the_daily_order_limit(trade_journal) -> None:
    """上限に達したら、買いシグナルが出ていても新規エントリーしないこと。

    同時保有数の上限やクールダウンが壊れたときに、損失の垂れ流しを
    有限回で止めるための独立した歯止め。
    """
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()
    # 上限に達した状態を作る（建てては決済を繰り返した後を想定）。
    for i in range(MAX_DAILY_ENTRY_ORDERS):
        symbol = f"SYM{i}"
        position_manager.record_entry_order_attempt()
        position_manager.open_position(symbol, entry_price=100.0, quantity=1)
        position_manager.close_position(symbol)

    daily_df = _make_daily_df(drop=True)  # 買いシグナルは出ている

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)) as mock_qualify, \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
        patch("main.get_intraday_bars_async", new=AsyncMock(return_value=_make_df([100.0] * 20))), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=80.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(80.0))), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.get_settled_cash_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.place_bracket_order_async", new=AsyncMock(return_value=_bracket_result(quantity=1))) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    # 回数判定はIBKRへの問い合わせより前に短絡すること（無駄なリクエストを出さない）。
    mock_qualify.assert_not_awaited()
    mock_order.assert_not_awaited()
    assert position_manager.has_position("AAPL") is False


def test_entry_is_allowed_just_below_the_daily_order_limit(trade_journal) -> None:
    """上限の1つ手前までは通常どおりエントリーできること。"""
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()
    for i in range(MAX_DAILY_ENTRY_ORDERS - 1):
        symbol = f"SYM{i}"
        position_manager.record_entry_order_attempt()
        position_manager.open_position(symbol, entry_price=100.0, quantity=1)
        position_manager.close_position(symbol)

    daily_df = _make_daily_df(drop=True)

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
        patch("main.get_intraday_bars_async", new=AsyncMock(return_value=_make_df([100.0] * 20))), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=80.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(80.0))), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.get_settled_cash_async", new=AsyncMock(return_value=100_000.0)), \
        patch(
            "main.place_bracket_order_async",
            new=AsyncMock(return_value=_bracket_result(quantity=250)),
        ) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    mock_order.assert_awaited_once()
    assert position_manager.count_entry_orders_today() == MAX_DAILY_ENTRY_ORDERS


def test_daily_order_limit_does_not_block_exits(trade_journal) -> None:
    """発注回数の上限は新規建てのみに掛かり、決済は止めないこと。

    止めると、上限に達した瞬間に保有中のポジションが損切りもトレーリングも
    効かない状態で放置される。
    """
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()
    for i in range(MAX_DAILY_ENTRY_ORDERS):
        symbol = f"SYM{i}"
        position_manager.record_entry_order_attempt()
        position_manager.open_position(symbol, entry_price=100.0, quantity=1)
        position_manager.close_position(symbol)
    # 上限に達した後も保有中のポジションが残っている状況
    position_manager.open_position(
        "AAPL", entry_price=100.0, quantity=3, strategy_type=STRATEGY_TYPE_SWING,
    )

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=80.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(80.0))), \
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=150.0)), \
        patch("main.cancel_bracket_orders_async", new=AsyncMock()), \
        patch(
            "main.place_market_order_async",
            new=AsyncMock(return_value=OrderResult("AAPL", "SELL", 3, "MKT")),
        ) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    # -20%で損切り水準に達しているため決済されること。
    mock_order.assert_awaited_once()
    assert position_manager.has_position("AAPL") is False


# --- 市場フィルター（既定は無効） -----------------------------------------------


def test_market_filter_blocks_entry_when_drop_is_market_wide(trade_journal) -> None:
    """指数も同じだけ下げているときは、相対乖離のフィルターで見送ること。"""
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()

    # 指数(SPY)の日足も同じモックから返るため、個別と指数が同じだけ下げた形になる。
    daily_df = _make_daily_df(drop=True)

    with patch("main.SWING_MARKET_FILTER", MarketFilterConfig(relative_threshold_pct=3.0)), \
        patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
        patch("main.get_intraday_bars_async", new=AsyncMock(return_value=_make_df([100.0] * 20))), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=80.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(80.0))), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.get_settled_cash_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.place_bracket_order_async", new=AsyncMock(return_value=_bracket_result(quantity=1))) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    mock_order.assert_not_awaited()
    assert position_manager.has_position("AAPL") is False


def test_market_index_is_not_fetched_while_filter_is_disabled(trade_journal) -> None:
    """既定（フィルター無効）では指数のリクエストを一切出さないこと。

    ペーシング制限(CLAUDE.md 6.1)に効くため、使っていない機能で
    リクエストが増えていないことを固定する。
    """
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)) as mock_qualify, \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=_make_daily_df(drop=True))), \
        patch("main.get_intraday_bars_async", new=AsyncMock(return_value=_make_df([100.0] * 20))), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=80.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(80.0))), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.get_settled_cash_async", new=AsyncMock(return_value=100_000.0)), \
        patch(
            "main.place_bracket_order_async",
            new=AsyncMock(return_value=_bracket_result(quantity=250)),
        ):

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    qualified_symbols = [call.args[1] for call in mock_qualify.await_args_list]
    assert MARKET_INDEX_SYMBOL not in qualified_symbols
    assert position_manager.has_position("AAPL") is True


# --- 買える上限株価（スクリーナーへ渡す） -------------------------------------------


def test_max_affordable_price_is_derived_from_equity_and_swing_stop() -> None:
    """株価がこの値を超えると数量が0株になる、という境界であること。"""
    equity = 1220.0

    max_price = resolve_max_affordable_price(equity)

    assert max_price == pytest.approx(equity * RISK_PER_TRADE_PCT / SWING_STOP_LOSS_PCT)
    # 上限ちょうどなら1株買え、少しでも超えると0株になる。
    assert calculate_position_size(
        equity, entry_price=max_price, stop_loss_pct=SWING_STOP_LOSS_PCT,
        risk_per_trade_pct=RISK_PER_TRADE_PCT,
    ) == 1
    assert calculate_position_size(
        equity, entry_price=max_price * 1.01, stop_loss_pct=SWING_STOP_LOSS_PCT,
        risk_per_trade_pct=RISK_PER_TRADE_PCT,
    ) == 0


def test_max_affordable_price_is_disabled_when_equity_is_unavailable() -> None:
    """資金が取れないときにフィルターを掛けないこと。

    ここで0を返すと全銘柄が除外され、ウォッチリストが空のまま稼働し続ける。
    """
    assert resolve_max_affordable_price(0.0) is None
    assert resolve_max_affordable_price(-1.0) is None


def test_refresh_watchlist_passes_the_price_cap_to_the_screener() -> None:
    with patch("main.screen_value_stocks_async", new=AsyncMock(return_value=["AAPL"])) as mock_screen:
        asyncio.run(_refresh_watchlist_async(MagicMock(), ["FALLBACK"], account_equity=1220.0))

    config = mock_screen.await_args.args[1]
    assert config.max_price == pytest.approx(1220.0 * RISK_PER_TRADE_PCT / SWING_STOP_LOSS_PCT)


# --- 買える下限株価（株数クランプの回避） --------------------------------------------


def test_min_tradeable_price_is_the_boundary_where_the_share_clamp_starts() -> None:
    """この値を下回るとMAX_POSITION_SIZEのクランプが掛かる、という境界であること。"""
    equity = 1220.0

    min_price = resolve_min_tradeable_price(equity)

    # $1,220・リスク1%・損切り5%・40株上限なら $6.10。
    assert min_price == pytest.approx(6.1)
    # 下限ちょうどではクランプが掛からない（ここが上限側の境界）。
    assert calculate_position_size(
        equity, entry_price=min_price, stop_loss_pct=SWING_STOP_LOSS_PCT,
        risk_per_trade_pct=RISK_PER_TRADE_PCT,
    ) == MAX_POSITION_SIZE
    # 下限より上ではクランプに掛かることはない。
    assert calculate_position_size(
        equity, entry_price=min_price * 1.1, stop_loss_pct=SWING_STOP_LOSS_PCT,
        risk_per_trade_pct=RISK_PER_TRADE_PCT,
    ) < MAX_POSITION_SIZE


def test_min_tradeable_price_is_conservative_by_the_rounding_band() -> None:
    """floor()の分だけ下限が安全側に寄っていること。

    連続量の数量が40.x株になる帯（$1,220なら $5.95〜$6.10）では
    floor()で40株に落ちるため、実際にはクランプが「効いて」いない。
    下限をこの帯の上端に置いているので、その分だけ余計に除外する。
    1銘柄あたり数ドル幅の話であり、クランプが掛かる銘柄を取りこぼすより
    安全側に倒す方を選んでいる。
    """
    equity = 1220.0
    exact_binding_price = equity * (RISK_PER_TRADE_PCT / SWING_STOP_LOSS_PCT) / (MAX_POSITION_SIZE + 1)

    assert exact_binding_price < resolve_min_tradeable_price(equity)
    assert calculate_position_size(
        equity, entry_price=exact_binding_price, stop_loss_pct=SWING_STOP_LOSS_PCT,
        risk_per_trade_pct=RISK_PER_TRADE_PCT,
    ) > MAX_POSITION_SIZE


def test_the_symbol_that_exposed_the_clamp_is_now_sized_by_risk() -> None:
    """クランプを表面化させたJOBY($7.05)が、いまは本来の株数で建つこと。

    10株上限だった頃はここが下限($24.40)で弾かれていた。上限を40株へ
    引き上げた目的は、この価格帯でリスクベースのサイジングを取り戻し、
    実運用の条件をバックテスト（クランプを適用しない）と揃えることにある。
    """
    equity = 1220.0

    assert resolve_min_tradeable_price(equity) < 7.05
    quantity = calculate_position_size(
        equity, entry_price=7.05, stop_loss_pct=SWING_STOP_LOSS_PCT,
        risk_per_trade_pct=RISK_PER_TRADE_PCT,
    )
    assert quantity == 34
    # クランプに触れないので、1トレードのリスクが RISK_PER_TRADE_PCT に届く。
    assert quantity <= MAX_POSITION_SIZE
    risk_pct = quantity * 7.05 * SWING_STOP_LOSS_PCT / 100.0 / equity * 100.0
    assert risk_pct == pytest.approx(RISK_PER_TRADE_PCT, abs=0.05)


def test_min_tradeable_price_is_disabled_when_equity_is_unavailable() -> None:
    """資金が取れないときは下限も掛けないこと（ウォッチリストが空になる）。"""
    assert resolve_min_tradeable_price(0.0) is None
    assert resolve_min_tradeable_price(-1.0) is None


def test_min_price_stays_below_max_price() -> None:
    """上下限が交差せず、監視可能な株価帯が必ず残ること。

    交差するとスクリーニングが常に0件になり、固定ウォッチリストへ
    静かにフォールバックし続ける。
    """
    for equity in (500.0, 1220.0, 6100.0, 100_000.0):
        assert resolve_min_tradeable_price(equity) < resolve_max_affordable_price(equity)


def test_refresh_watchlist_passes_the_price_floor_to_the_screener() -> None:
    with patch("main.screen_value_stocks_async", new=AsyncMock(return_value=["AAPL"])) as mock_screen:
        asyncio.run(_refresh_watchlist_async(MagicMock(), ["FALLBACK"], account_equity=1220.0))

    config = mock_screen.await_args.args[1]
    assert config.min_price == pytest.approx(6.1)


# --- 決済済み現金による新規建ての制限（発注拒否の回避） -----------------------------
#
# 受渡し(T+1)前の資金では買えず、その状態で発注するとIBKRは約定させずに注文を
# 拒否する。注文が通った前提でローカルに建玉を記録すると実体の無いポジションを
# 追跡することになるため、入口で数量を現金の裏付けまで落とす。
# （GFVは日本居住者向けのIBSJ口座には適用されないため、ここでの関心事ではない。）


def _entry_patches(*, price: float, equity: float, settled_cash):
    """新規エントリーが成立する最小構成のパッチ一式を返す。

    ENFORCE_SETTLED_CASH_FUNDINGを明示的に立てるのは、既定値が
    「検証用ペーパー口座にSettledCashが無い」という運用上の都合で
    Falseになっているため。ガード自体は実口座で使うロジックなので、
    既定値に関係なく挙動を固定する。
    """
    contract = MagicMock(symbol="AAPL")
    return contract, [
        patch("main.ENFORCE_SETTLED_CASH_FUNDING", True),
        patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)),
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=_make_daily_df(drop=True))),
        patch("main.get_current_price_async", new=AsyncMock(return_value=price)),
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(price))),
        patch("main.get_account_equity_async", new=AsyncMock(return_value=equity)),
        patch("main.get_settled_cash_async", new=AsyncMock(return_value=settled_cash)),
    ]


def _run_entry(trade_journal, *, price: float, equity: float, settled_cash):
    """エントリー処理を1回流し、発注モックとPositionManagerを返す。"""
    contract, patches = _entry_patches(price=price, equity=equity, settled_cash=settled_cash)
    position_manager = PositionManager()
    ib = MagicMock()

    with patch(
        "main.place_bracket_order_async",
        new=AsyncMock(return_value=_bracket_result(quantity=1)),
    ) as mock_order:
        for p in patches:
            p.start()
        try:
            asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))
        finally:
            for p in patches:
                p.stop()

    return mock_order, position_manager


def test_entry_quantity_is_capped_by_settled_cash(trade_journal) -> None:
    """リスク計算上の数量より決済済み現金が少なければ、買える株数まで切り下げること。

    サイジングの基準であるNetLiquidationは未受渡しの代金を含むため、
    そのまま使うと「まだ手元に無い現金」を当てにした数量になる。
    """
    # equity=100,000 / price=80 -> リスク計算では250株だが、
    # 決済済み現金は800ドルしかないので10株しか買えない。
    mock_order, _ = _run_entry(trade_journal, price=80.0, equity=100_000.0, settled_cash=800.0)

    assert mock_order.await_args.kwargs["quantity"] == 10


def test_entry_is_skipped_when_settled_cash_cannot_buy_one_share(trade_journal) -> None:
    mock_order, position_manager = _run_entry(
        trade_journal, price=80.0, equity=100_000.0, settled_cash=79.99,
    )

    mock_order.assert_not_awaited()
    assert position_manager.has_position("AAPL") is False


def test_entry_is_not_blocked_when_settled_cash_is_unavailable(trade_journal) -> None:
    """決済済み現金が取得できない場合は、数量を変えずに発注すること。

    判定できないことの実害は注文が拒否されうることに留まる（GFVは適用されない）。
    ここで止めると、口座種別によるタグの有無だけで新規エントリーが全件停止する。
    """
    mock_order, _ = _run_entry(
        trade_journal, price=80.0, equity=100_000.0, settled_cash=None,
    )

    # equity=100,000 / price=80 のリスク計算どおりの数量が、切り下げられずに渡ること。
    assert mock_order.await_args.kwargs["quantity"] == 250


def test_entry_is_not_capped_when_settled_cash_covers_the_full_size(trade_journal) -> None:
    """現金が足りているときにリスク計算の数量を変えないこと。"""
    mock_order, _ = _run_entry(
        trade_journal, price=80.0, equity=100_000.0, settled_cash=100_000.0,
    )

    # risk_amount=1,000 / per_share_risk=4.0 -> 250株のまま
    assert mock_order.await_args.kwargs["quantity"] == 250


def test_settled_cash_is_not_fetched_while_the_funding_check_is_disabled(trade_journal) -> None:
    """ENFORCE_SETTLED_CASH_FUNDING=Falseなら口座への問い合わせ自体を行わないこと。"""
    contract = MagicMock(symbol="AAPL")
    settled_cash_mock = AsyncMock(return_value=100_000.0)

    with patch("main.ENFORCE_SETTLED_CASH_FUNDING", False), \
        patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=_make_daily_df(drop=True))), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=80.0)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(80.0))), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.get_settled_cash_async", new=settled_cash_mock), \
        patch(
            "main.place_bracket_order_async",
            new=AsyncMock(return_value=_bracket_result(quantity=250)),
        ) as mock_order:

        asyncio.run(process_symbol_async(MagicMock(), "AAPL", PositionManager(), trade_journal))

    settled_cash_mock.assert_not_awaited()
    assert mock_order.await_args.kwargs["quantity"] == 250


def test_max_affordable_price_is_capped_by_settled_cash() -> None:
    """1株の値段が決済済み現金を超える銘柄は、監視枠を与えても買えないこと。"""
    equity = 100_000.0
    # 資金ベースの上限は 100,000 * (1% / 5%) = 20,000ドル。
    # 決済済み現金がそれを下回るなら、そちらが実際の上限になる。
    assert resolve_max_affordable_price(equity, 500.0) == pytest.approx(500.0)


def test_max_affordable_price_ignores_settled_cash_when_it_is_larger() -> None:
    equity = 1220.0
    expected = equity * RISK_PER_TRADE_PCT / SWING_STOP_LOSS_PCT
    assert resolve_max_affordable_price(equity, 100_000.0) == pytest.approx(expected)


def test_max_affordable_price_ignores_unavailable_settled_cash() -> None:
    """決済済み現金が取れないときに上限を狭めないこと。

    ここで絞ると全銘柄が除外されてウォッチリストが空になる。実際に建てられるかは
    エントリー時に再判定するため、絞り込みを外しても未受渡し資金では建たない。
    """
    equity = 1220.0
    expected = equity * RISK_PER_TRADE_PCT / SWING_STOP_LOSS_PCT
    assert resolve_max_affordable_price(equity, None) == pytest.approx(expected)
    assert resolve_max_affordable_price(equity, 0.0) == pytest.approx(expected)


# --- 古い価格での新規建ての抑止 -------------------------------------------------------
#
# 参照価格が古いと、そこから算出する損切り・利確の値段まで実勢からずれる。
# order_manager の値段の妥当性検証は参照価格を基準に測っているため、
# 参照価格そのものがずれているケースはそちらでは検出できない。


def _stale_quote(price: float) -> PriceQuote:
    return PriceQuote(price=price, source=PRICE_SOURCE_HISTORICAL, is_stale=True)


def _run_entry_with_quote(trade_journal, quote, *, reject_stale: bool = True):
    contract = MagicMock(symbol="AAPL")
    position_manager = PositionManager()

    with patch("main.REJECT_STALE_ENTRY_PRICE", reject_stale), \
        patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=_make_daily_df(drop=True))), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=quote)), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.get_settled_cash_async", new=AsyncMock(return_value=100_000.0)), \
        patch(
            "main.place_bracket_order_async",
            new=AsyncMock(return_value=_bracket_result(quantity=250)),
        ) as mock_order:

        asyncio.run(process_symbol_async(MagicMock(), "AAPL", position_manager, trade_journal))

    return mock_order, position_manager


def test_entry_is_skipped_when_the_reference_price_is_stale(trade_journal) -> None:
    mock_order, position_manager = _run_entry_with_quote(trade_journal, _stale_quote(80.0))

    mock_order.assert_not_awaited()
    assert position_manager.has_position("AAPL") is False


def test_entry_proceeds_when_the_reference_price_is_fresh(trade_journal) -> None:
    mock_order, position_manager = _run_entry_with_quote(trade_journal, _fresh_quote(80.0))

    mock_order.assert_awaited_once()
    assert position_manager.has_position("AAPL") is True


def test_stale_price_can_be_allowed_by_the_flag(trade_journal) -> None:
    """フラグを落とせば従来どおり建てられること（取得経路の想定が外れたときの逃げ道）。"""
    mock_order, position_manager = _run_entry_with_quote(
        trade_journal, _stale_quote(80.0), reject_stale=False,
    )

    mock_order.assert_awaited_once()
    assert position_manager.has_position("AAPL") is True


def test_stale_price_does_not_block_exits(trade_journal) -> None:
    """決済判定は古い価格でも走らせること。

    見送ると損切りが必要な場面で何もしないことになり、新規建てを
    見送るのとは危険の向きが逆になる。決済はget_current_price_asyncを使い、
    鮮度を見ないことをここで固定する。
    """
    position_manager = PositionManager()
    position_manager.open_position(
        "AAPL", entry_price=100.0, quantity=10, risk_per_share=5.0,
        strategy_type=STRATEGY_TYPE_SWING,
    )

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=MagicMock(symbol="AAPL"))), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=90.0)), \
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=150.0)), \
        patch(
            "main.place_market_order_async",
            new=AsyncMock(return_value=OrderResult("AAPL", "SELL", 10, "MKT")),
        ) as mock_order:

        asyncio.run(process_symbol_async(MagicMock(), "AAPL", position_manager, trade_journal))

    # -10%で損切り水準に達しているため決済されること。
    mock_order.assert_awaited_once()
    assert position_manager.has_position("AAPL") is False


# --- 決済経路の結合テスト ---------------------------------------------------------

# ドライラン検証（2026-07-30〜07-31）では決済が1件も発生せず、
# logs/trade_journal.csv が生成されなかった。ブラケット約定の検知から
# TradeJournalへの記録・クールダウン登録・日次サーキットブレーカーまでの
# 連鎖が実運用で一度も通っていないため、実市場を待たずにここで押さえる。


@contextmanager
def _entry_then_price(daily_df: pd.DataFrame, entry_price: float, later_price: float,
                      quantity: int, symbol: str, equity: float):
    """エントリー時と以降の決済判定で別々の価格を返すモック環境。

    `get_current_price_quote_async`（新規建ての参照価格）と
    `get_current_price_async`（決済判定）が別関数であることを利用して、
    1回のwith句の中で「建てた後に値が動いた」状況を作る。
    """
    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=MagicMock(symbol=symbol))), \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
        patch("main.get_intraday_bars_async", new=AsyncMock(return_value=_make_df([100.0] * 20))), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(entry_price))), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=later_price)), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=equity)), \
        patch("main.get_settled_cash_async", new=AsyncMock(return_value=equity)), \
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=160.0)), \
        patch("main.cancel_bracket_orders_async", new=AsyncMock()), \
        patch("main.place_market_order_async", new=AsyncMock(return_value=_order_result())), \
        patch(
            "main.place_bracket_order_async",
            new=AsyncMock(return_value=BracketResult(
                symbol=symbol, quantity=quantity,
                stop_price=entry_price * (1 - SWING_STOP_LOSS_PCT / 100.0),
                take_profit_price=entry_price * 1.10,
                oca_group=f"OCA_{symbol}",
            )),
        ):
        yield


def test_resting_stop_fill_records_the_trade_and_starts_the_cooldown(trade_journal) -> None:
    """損切りの待機注文が約定したときの一連の処理が繋がっていること。

    建玉 -> ブラケット約定の検知 -> ポジション解放 -> TradeJournalへの記録 ->
    同日中の再エントリー禁止、までを1本で通す。
    """
    ib = MagicMock()
    position_manager = PositionManager()
    daily_df = _make_daily_df(drop=True)

    # 資金100,000・株価100・損切り5% -> リスク1,000 / 1株あたり5.0 -> 200株。
    with _entry_then_price(daily_df, entry_price=100.0, later_price=95.0,
                           quantity=200, symbol="AAPL", equity=100_000.0):
        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))
        assert position_manager.has_position("AAPL") is True
        assert position_manager.get_position("AAPL").quantity == 200

        # 次のサイクル。観測価格が逆指値(95.0)まで落ちている。
        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    assert position_manager.has_position("AAPL") is False

    records = trade_journal.load_trades()
    assert len(records) == 1
    assert records[0].symbol == "AAPL"
    assert records[0].reason == REASON_STOP_LOSS
    # (95 - 100) * 200 = -1,000（口座資金の1% = 1トレードのリスクどおり）
    assert records[0].pnl == pytest.approx(-1000.0)
    assert records[0].r_multiple == pytest.approx(-1.0)
    assert records[0].usd_jpy_rate == pytest.approx(160.0)

    # 決済した当日は買い直さない。
    assert position_manager.is_in_cooldown("AAPL") is True


def test_resting_take_profit_fill_records_a_win(trade_journal) -> None:
    """利確側の待機注文でも同じ経路が通ること。"""
    ib = MagicMock()
    position_manager = PositionManager()
    daily_df = _make_daily_df(drop=True)

    with _entry_then_price(daily_df, entry_price=100.0, later_price=110.0,
                           quantity=200, symbol="AAPL", equity=100_000.0):
        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))
        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    records = trade_journal.load_trades()
    assert len(records) == 1
    assert records[0].reason == REASON_TAKE_PROFIT
    # 指値なので指値より不利な価格では約定しない。(110 - 100) * 200 = +2,000
    assert records[0].pnl == pytest.approx(2000.0)
    assert records[0].r_multiple == pytest.approx(2.0)


def test_accumulated_stop_losses_trip_the_daily_circuit_breaker(trade_journal) -> None:
    """記録した実現損失が積み上がると新規エントリーが止まること。

    サーキットブレーカーは TradeJournal の実現損益を読むため、決済の記録が
    繋がっていて初めて機能する。損失を直接書き込むのではなく、実際に
    損切りを3回通してから4銘柄目が止まることを見る。
    """
    ib = MagicMock()
    position_manager = PositionManager()
    daily_df = _make_daily_df(drop=True)

    # 1回の損切りが -1,000（資金の1%）。3回で -3,000 = MAX_DAILY_LOSS_PCT(3%)。
    for symbol in ("AAA", "BBB", "CCC"):
        with _entry_then_price(daily_df, entry_price=100.0, later_price=95.0,
                               quantity=200, symbol=symbol, equity=100_000.0):
            asyncio.run(process_symbol_async(ib, symbol, position_manager, trade_journal))
            asyncio.run(process_symbol_async(ib, symbol, position_manager, trade_journal))

    assert len(trade_journal.load_trades()) == 3
    assert trade_journal.compute_daily_pnl() == pytest.approx(-3000.0)

    # 4銘柄目はクールダウンにも同時保有数にも掛からないが、建たない。
    with _entry_then_price(daily_df, entry_price=100.0, later_price=100.0,
                           quantity=200, symbol="DDD", equity=100_000.0):
        asyncio.run(process_symbol_async(ib, "DDD", position_manager, trade_journal))

    assert position_manager.has_position("DDD") is False


# --- 実発注（ペーパー口座）との接続部 ----------------------------------------------

# ドライランは「注文が通った前提」で記録するが、実発注では通らないことがある。
# ここは main 側がその差を正しく扱うかを固定する。


def test_entry_records_the_actual_fill_not_the_reference_price(trade_journal) -> None:
    """建値は参照価格ではなく実約定を記録すること。

    参照価格で記録すると、損益・R倍率・トレーリングの基準がすべて実際の
    約定とずれる。
    """
    ib = MagicMock()
    position_manager = PositionManager()
    daily_df = _make_daily_df(drop=True)

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=MagicMock(symbol="AAPL"))), \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(100.0))), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.get_settled_cash_async", new=AsyncMock(return_value=100_000.0)), \
        patch(
            "main.place_bracket_order_async",
            new=AsyncMock(return_value=BracketResult(
                symbol="AAPL", quantity=200, stop_price=95.0, take_profit_price=110.0,
                oca_group="OCA_AAPL", dry_run=False, fill_price=101.25, commission=0.70,
            )),
        ):
        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    position = position_manager.get_position("AAPL")
    assert position.entry_price == pytest.approx(101.25)
    assert position.entry_commission == pytest.approx(0.70)


def test_rejected_entry_order_does_not_create_a_local_position(trade_journal) -> None:
    """注文が拒否されたらローカルに建玉を記録しないこと。

    記録すると実体の無い建玉を追跡し、存在しない建玉へ決済のSELLを出す。
    """
    ib = MagicMock()
    ib.reqPositionsAsync = AsyncMock(return_value=[])
    position_manager = PositionManager()
    daily_df = _make_daily_df(drop=True)

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=MagicMock(symbol="AAPL"))), \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(100.0))), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.get_settled_cash_async", new=AsyncMock(return_value=100_000.0)), \
        patch(
            "main.place_bracket_order_async",
            new=AsyncMock(side_effect=OrderNotFilledError("rejected")),
        ):
        # 銘柄単位の例外は監視ループが握り潰すため、ここでも例外は外へ出さない。
        asyncio.run(run_watchlist_cycle_async(ib, ["AAPL"], position_manager, trade_journal))

    assert position_manager.has_position("AAPL") is False


def test_real_exit_uses_the_broker_fill_and_records_round_trip_commission(trade_journal) -> None:
    """実発注時は待機注文の実約定で決済を記録すること。

    ドライランの推定（観測した現在値が待機注文の値段に届いたか）は300秒ごとの
    1点しか見ないため、ザラ場で逆指値に触れて戻した動きを取りこぼす。
    """
    ib = MagicMock()
    position_manager = PositionManager()
    position_manager.open_position(
        "AAPL", entry_price=100.0, quantity=10, risk_per_share=5.0,
        strategy_type=STRATEGY_TYPE_SWING,
        stop_price=95.0, take_profit_price=110.0, oca_group="OCA_AAPL",
        entry_commission=0.35,
    )

    resting_fill = RestingOrderFill(order_type="LMT", fill_price=110.0, commission=0.35)

    with patch("main.ENABLE_REAL_ORDERS", True), \
        patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=MagicMock(symbol="AAPL"))), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=104.0)), \
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=150.0)), \
        patch("main.find_filled_resting_exit", return_value=resting_fill), \
        patch("main.place_market_order_async", new=AsyncMock()) as mock_order:
        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    records = trade_journal.load_trades()
    assert len(records) == 1
    # 観測価格(104.0)ではなく、待機注文の約定(110.0)で記録される。
    assert records[0].exit_price == pytest.approx(110.0)
    assert records[0].reason == REASON_TAKE_PROFIT
    # 往復ぶんの手数料。控除後の損益はサーキットブレーカーの判定に使われる。
    assert records[0].commission == pytest.approx(0.70)
    assert records[0].net_pnl_usd == pytest.approx(100.0 - 0.70)
    # 待機注文が約定した銘柄へ、重ねて成行の決済を出さないこと。
    mock_order.assert_not_awaited()
    assert position_manager.has_position("AAPL") is False


def test_main_refuses_to_start_with_real_orders_on_a_non_paper_port() -> None:
    """実発注が有効なまま本番ポートを向いていたら、1件も注文を出す前に止まること。

    ガード自体は order_manager 側にあるが、main() が接続より前に呼んでいなければ
    意味が無い。
    """
    connection = _make_fake_connection([MagicMock()])
    connection.port = 7496

    with patch("main.IBKRConnection", return_value=connection), \
        patch("execution.order_manager.ENABLE_REAL_ORDERS", True), \
        pytest.raises(RuntimeError):
        asyncio.run(main())

    connection.connect_async.assert_not_awaited()


def test_real_mode_does_not_sell_a_position_the_broker_does_not_hold(trade_journal) -> None:
    """ドライラン期間の想定ポジションへ、実発注で成行SELLを出さないこと。

    ブローカーが持っていない株を成行で売ると売り建てになる。状態ファイルには
    ドライラン中に建てた想定ポジションが残りうるため、実発注を有効にした
    瞬間にこれが起きる。
    """
    ib = MagicMock()
    ib.reqPositionsAsync = AsyncMock(return_value=[])
    position_manager = PositionManager()
    position_manager.open_position(
        "JOBY", entry_price=7.05, quantity=10, risk_per_share=0.3525,
        strategy_type=STRATEGY_TYPE_SWING,
    )

    with patch("main.ENABLE_REAL_ORDERS", True), \
        patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=MagicMock(symbol="JOBY"))), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=6.0)), \
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=150.0)), \
        patch("main.find_filled_resting_exit", return_value=None), \
        patch("main.cancel_bracket_orders_async", new=AsyncMock()), \
        patch("main.place_market_order_async", new=AsyncMock()) as mock_order:
        # 損切り水準(-14%)まで下げても売らない。
        asyncio.run(run_watchlist_cycle_async(ib, [], position_manager, trade_journal))

    mock_order.assert_not_awaited()
    assert trade_journal.load_trades() == []
    # 追跡は消さない。消すと、実は建玉があった場合に無防備な建玉が生まれる。
    assert position_manager.has_position("JOBY") is True


def test_broker_confirmed_position_can_still_be_sold_in_real_mode(trade_journal) -> None:
    """ブローカー側に実在する建玉は、従来どおり成行決済できること。"""
    ib = MagicMock()
    broker_position = MagicMock()
    broker_position.contract.symbol = "AAPL"
    broker_position.contract.secType = "STK"
    broker_position.contract.currency = "USD"
    broker_position.position = 10
    broker_position.avgCost = 100.0
    ib.reqPositionsAsync = AsyncMock(return_value=[broker_position])

    position_manager = PositionManager()
    position_manager.open_position(
        "AAPL", entry_price=100.0, quantity=10, risk_per_share=5.0,
        strategy_type=STRATEGY_TYPE_SWING,
    )

    with patch("main.ENABLE_REAL_ORDERS", True), \
        patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=MagicMock(symbol="AAPL"))), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=80.0)), \
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=150.0)), \
        patch("main.find_filled_resting_exit", return_value=None), \
        patch("main.cancel_bracket_orders_async", new=AsyncMock()), \
        patch(
            "main.place_market_order_async",
            new=AsyncMock(return_value=OrderResult("AAPL", "SELL", 10, "MKT",
                                                   dry_run=False, fill_price=80.0, commission=0.35)),
        ) as mock_order:
        asyncio.run(run_watchlist_cycle_async(ib, [], position_manager, trade_journal))

    mock_order.assert_awaited_once()
    assert len(trade_journal.load_trades()) == 1


def test_a_rejected_entry_still_counts_toward_the_daily_order_limit(trade_journal) -> None:
    """拒否された発注も1日の上限に数えること。

    約定だけを数えると、資金不足などで全件拒否される状況で毎サイクル発注し
    続けても上限に掛からない。有限回で打ち切るのがこの上限の役目である。
    """
    ib = MagicMock()
    ib.reqPositionsAsync = AsyncMock(return_value=[])
    position_manager = PositionManager()
    daily_df = _make_daily_df(drop=True)

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=MagicMock(symbol="AAPL"))), \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(100.0))), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.get_settled_cash_async", new=AsyncMock(return_value=100_000.0)), \
        patch(
            "main.place_bracket_order_async",
            new=AsyncMock(side_effect=OrderNotFilledError("rejected")),
        ):
        asyncio.run(run_watchlist_cycle_async(ib, ["AAPL"], position_manager, trade_journal))

    assert position_manager.count_entry_orders_today() == 1
    assert position_manager.has_position("AAPL") is False


def test_short_daily_history_is_reported_instead_of_silently_skipped(trade_journal, caplog) -> None:
    """日足が移動平均の本数に満たない銘柄は、黙って飛ばさず理由を残すこと。

    新規上場銘柄では現実に起きる（ウォッチリストにはIPO直後の銘柄が含まれる）。
    黙ってスキップすると「シグナルが出ない銘柄」と区別がつかず、監視枠を
    占めたまま永久にエントリーされないことに気付けない。
    """
    ib = MagicMock()
    position_manager = PositionManager()
    # 必要本数(SWING_MIN_HISTORY_BARS)に対して10本しか無い日足。
    short_df = _make_df([100.0] * 10)

    with caplog.at_level(logging.WARNING, logger="main"), \
        patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=MagicMock(symbol="IPO"))), \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=short_df)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(100.0))), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.place_bracket_order_async", new=AsyncMock()) as mock_order:
        asyncio.run(process_symbol_async(ib, "IPO", position_manager, trade_journal))

    assert "日足が10本しかなく" in caplog.text
    mock_order.assert_not_awaited()


def test_full_daily_history_does_not_warn(trade_journal, caplog) -> None:
    """本数が足りている銘柄では警告を出さないこと（毎サイクル出ると意味を失う）。"""
    ib = MagicMock()
    position_manager = PositionManager()

    with caplog.at_level(logging.WARNING, logger="main"), \
        patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=MagicMock(symbol="AAPL"))), \
        patch("data.cache.get_historical_bars_async",
              new=AsyncMock(return_value=_make_daily_df(drop=False))), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(100.0))), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.place_bracket_order_async", new=AsyncMock()):
        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    assert "日足が" not in caplog.text


def test_entry_requires_enough_history_to_judge_the_long_term_trend(trade_journal, caplog) -> None:
    """移動平均が確定するだけの本数では新規建てしないこと。

    長期トレンドフィルター(STRUGGLING_MA_WINDOW=200本)は本数が足りないと
    判定不能となり、_drop_struggling_symbols_async はその銘柄を監視対象に
    残す。エントリー側がSWING_MA_WINDOW(30本)で通ってしまうと、
    **トレンド判定を一度も受けていない銘柄がそのまま建つ。**

    2026-08-04のペーパー検証で実際に起きた回帰。上場から35営業日のSPCXが
    MA(30)乖離-16.54%で建った。この乖離は上場直後の値付けの途中経過であって、
    42銘柄・10年で検証した「平均回帰する押し目」ではない。
    """
    ib = MagicMock()
    position_manager = PositionManager()
    # 移動平均(30本)は確定するがトレンド判定(200本)には届かない本数で、
    # かつ乖離率が閾値を割る形。フィルターが無ければ買いシグナルが出る。
    ipo_df = _make_df([100.0] * (SWING_MA_WINDOW * 2 - 1) + [80.0])
    assert SWING_MA_WINDOW <= len(ipo_df) < SWING_MIN_HISTORY_BARS

    with caplog.at_level(logging.WARNING, logger="main"), \
        patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=MagicMock(symbol="IPO"))), \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=ipo_df)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(80.0))), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.place_bracket_order_async", new=AsyncMock()) as mock_order:
        asyncio.run(process_symbol_async(ib, "IPO", position_manager, trade_journal))

    mock_order.assert_not_awaited()
    assert position_manager.has_position("IPO") is False
    assert "長期トレンド" in caplog.text


def test_downtrend_symbol_is_monitored_but_not_bought(trade_journal, caplog) -> None:
    """監視は続けるが、長期移動平均を下回る間は建てないこと。

    下降トレンドの判定をウォッチリストの出入りからエントリー直前へ移した。
    監視に残す以上、ここで止めないと下降トレンドの銘柄がそのまま建つ。
    """
    ib = MagicMock()
    position_manager = PositionManager()
    # 直近30本の平均を大きく割る押し目だが、終値は200日平均を下回る。
    closes = [200.0] * (SWING_MIN_HISTORY_BARS - SWING_MA_WINDOW) \
        + [100.0] * (SWING_MA_WINDOW - 1) + [80.0]
    down_df = _make_df(closes)

    with caplog.at_level(logging.INFO, logger="main"), \
        patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=MagicMock(symbol="FADED"))), \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=down_df)), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(80.0))), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.place_bracket_order_async", new=AsyncMock()) as mock_order:
        asyncio.run(process_symbol_async(ib, "FADED", position_manager, trade_journal))

    mock_order.assert_not_awaited()
    assert position_manager.has_position("FADED") is False
    assert "押し目シグナルは出ましたが" in caplog.text


def test_the_same_pullback_is_bought_once_the_trend_turns_up(trade_journal) -> None:
    """トレンドが上向いたら、同じ押し目で建つこと。

    見送りが恒久的な除外になっていないことを確かめる。上の銘柄と乖離率は
    同じで、終値が200日平均を上回る点だけが違う。
    """
    ib = MagicMock()
    position_manager = PositionManager()

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=MagicMock(symbol="RECOVERED"))), \
        patch("data.cache.get_historical_bars_async",
              new=AsyncMock(return_value=_make_daily_df(drop=True))), \
        patch("main.get_current_price_quote_async", new=AsyncMock(return_value=_fresh_quote(80.0))), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.place_bracket_order_async",
              new=AsyncMock(return_value=_bracket_result(1, "RECOVERED"))) as mock_order:
        asyncio.run(process_symbol_async(ib, "RECOVERED", position_manager, trade_journal))

    mock_order.assert_awaited_once()
    assert position_manager.has_position("RECOVERED") is True


def test_swing_entry_history_requirement_matches_the_trend_filter() -> None:
    """新規建ての必要本数が、長期トレンドフィルターの本数と揃っていること。

    ずれると「監視には残すがトレンド判定は受けていない」銘柄が再びエントリー
    経路を通る。番人としてここで固定する。
    """
    assert SWING_MIN_HISTORY_BARS == STRUGGLING_MA_WINDOW


# --- 売買代金の急上昇による銘柄の入れ替え -------------------------------------------

# スキャナーは順位しか返さないため、履歴と突き合わせて「急に上位へ来た」を判定する。
# 追加のIBKRリクエストはスキャナー2回だけで、銘柄ごとの取得は行わない。
#
# 組み入れ(ENABLE_ATTENTION_WATCHLIST)は既定で無効なので、そこを見るテストは
# 明示的にTrueへ差し替える。下降トレンドの除外(DROP_STRUGGLING_SYMBOLS)は
# 購読権限も追加リクエストも要らないため既定で有効。


def _attention_bars(prices: dict, ma_below: tuple = ()):
    """日足を与えるモック環境。

    ma_below に入れた銘柄は200日移動平均を下回る系列（＝下降トレンド）にする。
    """
    async def _bars(ib, contract, now=None):
        price = prices.get(contract.symbol)
        if price is None:
            return pd.DataFrame()
        if contract.symbol in ma_below:
            # 高値圏から下げてきた系列。直近終値が200日平均を下回る。
            closes = [price * 2] * 200 + [price] * 20
        else:
            closes = [price * 0.5] * 200 + [price] * 20
        return _make_df(closes)

    return patch("data.cache.DailyBarCache.get_async", new=AsyncMock(side_effect=_bars))


def _run_attention(watchlist, ranks, history, prices, ma_below=(), positions=None,
                   equity=1220.0, tmp_path=None):
    ib = MagicMock()
    store = RankHistoryStore(str(tmp_path / "ranks.json"))
    for index, day in enumerate(history):
        store.append(f"day-{index:03d}", day)

    position_manager = positions or PositionManager()
    scans = [list(ranks), []]

    with patch("main.ENABLE_ATTENTION_WATCHLIST", True), \
        patch("data.cache.qualify_stock_async",
              new=AsyncMock(side_effect=lambda ib, symbol: MagicMock(symbol=symbol))), \
        _attention_bars(prices, ma_below), \
        patch("main.run_turnover_scan_async", new=AsyncMock(side_effect=scans)) as mock_scan:
        result = asyncio.run(main_module._apply_attention_watchlist_async(
            ib, list(watchlist), equity, MarketDataCaches(), position_manager, store,
            now=datetime(2026, 8, 4, 10, 0, tzinfo=US_EASTERN),
        ))
    return result, mock_scan, store


def test_surging_symbol_is_added_to_the_watchlist(tmp_path) -> None:
    """ランク外から上位へ来た銘柄が監視対象に入ること。"""
    history = [{"OLD": 1} for _ in range(10)]
    result, mock_scan, _ = _run_attention(
        watchlist=["KEEP"], ranks=["SURGE"], history=history,
        prices={"KEEP": 100.0, "SURGE": 50.0}, tmp_path=tmp_path,
    )

    assert result == ["KEEP", "SURGE"]
    # 取引所ごとに1回ずつ。numberOfRowsの上限が50のため2回に分けている。
    assert mock_scan.await_count == 2


def test_symbol_below_its_long_term_average_stays_monitored(tmp_path) -> None:
    """下降トレンドの銘柄も監視対象に残すこと。

    建てさせない役目はエントリー側(ENTRY_REQUIRES_LONG_TERM_UPTREND)へ
    移してあるので、監視に残しても建たない。残すのは、トレンドが上向いた
    その瞬間にエントリー判定へ入れるため。外すと復帰の判定が
    1日1回のウォッチリスト更新まで遅れる。
    """
    history = [{"OLD": 1} for _ in range(10)]
    result, _, _ = _run_attention(
        watchlist=["HEALTHY", "STRUGGLING"], ranks=[], history=history,
        prices={"HEALTHY": 100.0, "STRUGGLING": 80.0}, ma_below=("STRUGGLING",),
        tmp_path=tmp_path,
    )

    assert result == ["HEALTHY", "STRUGGLING"]


def test_struggling_symbols_can_be_dropped_when_the_watchlist_is_tight(tmp_path) -> None:
    """枠が足りない場合はフラグで落とせること（監視枠の逃げ道を残す）。"""
    history = [{"OLD": 1} for _ in range(10)]
    with patch("main.KEEP_UNTRADEABLE_SYMBOLS_IN_WATCHLIST", False):
        result, _, _ = _run_attention(
            watchlist=["HEALTHY", "STRUGGLING"], ranks=[], history=history,
            prices={"HEALTHY": 100.0, "STRUGGLING": 80.0}, ma_below=("STRUGGLING",),
            tmp_path=tmp_path,
        )

    assert result == ["HEALTHY"]


def test_held_positions_are_never_dropped(tmp_path) -> None:
    """保有中の銘柄は下降トレンドでも監視対象に残すこと。

    外しても決済判定は続く（保有銘柄との和集合を処理するため）が、
    再エントリーの判断ができなくなる。
    """
    position_manager = PositionManager()
    position_manager.open_position("STRUGGLING", entry_price=80.0, quantity=1)
    history = [{"OLD": 1} for _ in range(10)]

    result, _, _ = _run_attention(
        watchlist=["STRUGGLING"], ranks=[], history=history,
        prices={"STRUGGLING": 80.0}, ma_below=("STRUGGLING",),
        positions=position_manager, tmp_path=tmp_path,
    )

    assert result == ["STRUGGLING"]


def test_watchlist_never_exceeds_the_monitoring_cap(tmp_path) -> None:
    """急上昇が何件あっても監視枠を超えないこと（ペーシング制限の不変条件）。"""
    existing = [f"SYM{i}" for i in range(MAX_WATCHLIST_SIZE)]
    surges = [f"SURGE{i}" for i in range(10)]
    prices = {s: 100.0 for s in existing + surges}
    history = [{"OLD": 1} for _ in range(10)]

    result, _, _ = _run_attention(
        watchlist=existing, ranks=surges, history=history, prices=prices, tmp_path=tmp_path,
    )

    assert len(result) == MAX_WATCHLIST_SIZE
    # 既存の監視銘柄が優先され、急上昇は空いた枠にだけ入る。
    assert result == existing


def test_surging_symbol_outside_the_price_band_is_not_added(tmp_path) -> None:
    """買えない株価の銘柄は、急上昇していても入れないこと。"""
    history = [{"OLD": 1} for _ in range(10)]
    result, _, _ = _run_attention(
        watchlist=["KEEP"], ranks=["EXPENSIVE"], history=history,
        prices={"KEEP": 100.0, "EXPENSIVE": 900.0}, tmp_path=tmp_path,
    )

    assert result == ["KEEP"]


def test_nothing_is_added_until_the_history_is_deep_enough(tmp_path) -> None:
    """履歴が浅いうちは組み入れないこと。

    基準順位がランク外に張り付き、上位銘柄が軒並み「急上昇」になる。
    """
    result, _, store = _run_attention(
        watchlist=["KEEP"], ranks=["SURGE"], history=[{"OLD": 1}],
        prices={"KEEP": 100.0, "SURGE": 50.0}, tmp_path=tmp_path,
    )

    assert result == ["KEEP"]
    # 記録だけは進める（翌日以降の基準になる）。
    assert len(store.load()) == 2


def test_scanner_failure_keeps_the_watchlist_running(tmp_path) -> None:
    """スキャナーが落ちてもウォッチリストの手入れは続けること。"""
    ib = MagicMock()
    store = RankHistoryStore(str(tmp_path / "ranks.json"))

    with patch("data.cache.qualify_stock_async",
               new=AsyncMock(side_effect=lambda ib, symbol: MagicMock(symbol=symbol))), \
        _attention_bars({"HEALTHY": 100.0, "STRUGGLING": 80.0}, ("STRUGGLING",)), \
        patch("main.run_turnover_scan_async", new=AsyncMock(side_effect=RuntimeError("boom"))):
        result = asyncio.run(main_module._apply_attention_watchlist_async(
            ib, ["HEALTHY", "STRUGGLING"], 1220.0, MarketDataCaches(),
            PositionManager(), store,
        ))

    assert result == ["HEALTHY", "STRUGGLING"]


def test_symbols_without_enough_history_stay_monitored(tmp_path) -> None:
    """本数不足の銘柄も監視対象に残すこと。

    新規建ては `SWING_MIN_HISTORY_BARS` がエントリー側で止めるので、
    監視に残しても建たない。残すのは本数が揃うまでの経過を追えるようにする
    ためで、2026-08-04の検証で固定リストの3銘柄(SPCX/CBRS/FRVO)が
    黙って消えていたのがこれにあたる。
    """
    ib = MagicMock()
    store = RankHistoryStore(str(tmp_path / "ranks.json"))

    async def _bars(ib, contract, now=None):
        if contract.symbol == "IPO":
            # 移動平均(30本)は確定するがトレンド判定(200本)には届かない本数。
            return _make_df([100.0] * 50)
        # 200日移動平均を上回る系列（トレンドの判定自体は通る）。
        return _make_df([50.0] * SWING_MIN_HISTORY_BARS + [100.0] * 20)

    with patch("data.cache.qualify_stock_async",
               new=AsyncMock(side_effect=lambda ib, symbol: MagicMock(symbol=symbol))), \
        patch("data.cache.DailyBarCache.get_async", new=AsyncMock(side_effect=_bars)):
        result = asyncio.run(main_module._apply_attention_watchlist_async(
            ib, ["KEEP", "IPO"], 1220.0, MarketDataCaches(), PositionManager(), store,
        ))

    assert result == ["KEEP", "IPO"]


def test_held_symbols_without_enough_history_stay_in_the_watchlist(tmp_path) -> None:
    """保有中の銘柄は本数不足でも監視対象に残すこと。

    外すと再エントリーの判断ができなくなる（決済判定自体は保有銘柄との
    和集合で続くが、監視から消えた銘柄は日足の更新を受けない）。
    """
    ib = MagicMock()
    store = RankHistoryStore(str(tmp_path / "ranks.json"))
    position_manager = PositionManager()
    position_manager.open_position(
        symbol="IPO", entry_price=100.0, quantity=1, risk_per_share=5.0,
        strategy_type="swing",
    )

    async def _bars(ib, contract, now=None):
        return _make_df([100.0] * 50)

    with patch("data.cache.qualify_stock_async",
               new=AsyncMock(side_effect=lambda ib, symbol: MagicMock(symbol=symbol))), \
        patch("data.cache.DailyBarCache.get_async", new=AsyncMock(side_effect=_bars)):
        result = asyncio.run(main_module._apply_attention_watchlist_async(
            ib, ["IPO"], 1220.0, MarketDataCaches(), position_manager, store,
        ))

    assert result == ["IPO"]


def test_surges_are_ignored_while_the_feature_is_in_observation_mode(tmp_path) -> None:
    """既定（観測モード）では監視リストを入れ替えないこと。

    スキャナーの購読が無く、かつ「急上昇銘柄がその後どう動くか」を
    誰も見ていない段階では、組み入れを行わない。下降トレンドの除外だけは
    追加リクエストも購読も要らないので動く。
    """
    ib = MagicMock()
    store = RankHistoryStore(str(tmp_path / "ranks.json"))
    for index in range(10):
        store.append(f"day-{index:03d}", {"OLD": 1})

    with patch("data.cache.qualify_stock_async",
               new=AsyncMock(side_effect=lambda ib, symbol: MagicMock(symbol=symbol))), \
        _attention_bars({"KEEP": 100.0, "STRUGGLING": 80.0, "SURGE": 50.0}, ("STRUGGLING",)), \
        patch("main.run_turnover_scan_async", new=AsyncMock(return_value=["SURGE"])) as mock_scan:
        result = asyncio.run(main_module._apply_attention_watchlist_async(
            ib, ["KEEP", "STRUGGLING"], 1220.0, MarketDataCaches(),
            PositionManager(), store,
        ))

    # SURGE が入らないことが要点。下降トレンドの STRUGGLING は監視に残る。
    assert result == ["KEEP", "STRUGGLING"]
    # スキャナーは呼ばない（購読が無い口座で無駄なリクエストを出さない）。
    mock_scan.assert_not_awaited()


def test_attention_symbols_are_carried_to_the_next_day(tmp_path) -> None:
    """前日に組み入れた注目銘柄を翌日も監視すること。

    毎日ゼロから組み直すと、急上昇の翌日にランキングが落ち着いた時点で監視から
    外れ、押し目が出るまで持ち続けられない。
    """
    ib = MagicMock()
    store = RankHistoryStore(str(tmp_path / "ranks.json"))
    for index in range(10):
        store.append(f"day-{index:03d}", {"OLD": 1})
    store.save_attention_symbols(["YESTERDAY"])

    with patch("main.ENABLE_ATTENTION_WATCHLIST", True), \
        patch("data.cache.qualify_stock_async",
              new=AsyncMock(side_effect=lambda ib, symbol: MagicMock(symbol=symbol))), \
        _attention_bars({"KEEP": 100.0, "YESTERDAY": 50.0}), \
        patch("main.run_turnover_scan_async", new=AsyncMock(return_value=[])):
        result = asyncio.run(main_module._apply_attention_watchlist_async(
            ib, ["KEEP"], 1220.0, MarketDataCaches(), PositionManager(), store,
        ))

    assert result == ["KEEP", "YESTERDAY"]
    # 明日も引き継ぐ。
    assert store.load_attention_symbols() == ["YESTERDAY"]


def test_a_carried_symbol_that_turns_down_is_not_carried_again(tmp_path) -> None:
    """引き継いだ銘柄が下降トレンドに入ったら、その日限りで引き継ぎを止めること。

    残すと、翌日また同じ銘柄を組み入れて落とすことを繰り返す。

    **監視に残ることと引き継ぐことは別である。** 監視は継続してよいが、
    引き継ぎは「押し目が出るまで持ち続ける」ための仕組みなので、
    建てられない銘柄を引き継ぐと枠を占めたまま毎日組み入れ直すことになる。
    """
    ib = MagicMock()
    store = RankHistoryStore(str(tmp_path / "ranks.json"))
    for index in range(10):
        store.append(f"day-{index:03d}", {"OLD": 1})
    store.save_attention_symbols(["FADED"])

    with patch("main.ENABLE_ATTENTION_WATCHLIST", True), \
        patch("data.cache.qualify_stock_async",
              new=AsyncMock(side_effect=lambda ib, symbol: MagicMock(symbol=symbol))), \
        _attention_bars({"KEEP": 100.0, "FADED": 50.0}, ma_below=("FADED",)), \
        patch("main.run_turnover_scan_async", new=AsyncMock(return_value=[])):
        result = asyncio.run(main_module._apply_attention_watchlist_async(
            ib, ["KEEP"], 1220.0, MarketDataCaches(), PositionManager(), store,
        ))

    assert result == ["KEEP", "FADED"]
    assert store.load_attention_symbols() == []


def test_sigterm_is_converted_to_keyboard_interrupt():
    """引け後の停止(scripts/after_close.sh)がSIGTERMで送るため。

    KeyboardInterruptへ変換することで main() の
    `finally: disconnect_async()` を通り、IBKRとのソケットが明示的に閉じる。
    SIGTERMの既定動作（即時終了）のままだとこの経路を通らない。
    """
    with pytest.raises(KeyboardInterrupt):
        main_module._raise_keyboard_interrupt_on_sigterm(signal.SIGTERM, None)


# --- 建玉と待機注文の突き合わせ ------------------------------------------------------


def _protection(live=(), filled=False, stop_price=None, take_profit_price=None):
    from execution.order_manager import RestingExitProtection

    return RestingExitProtection(
        live_order_types=frozenset(live), has_filled_exit=filled,
        stop_price=stop_price, take_profit_price=take_profit_price,
    )


def _position_manager_with_one_open_position() -> PositionManager:
    manager = PositionManager()
    manager.open_position(
        "AAPL", entry_price=100.0, quantity=3, risk_per_share=5.0,
        stop_price=95.0, take_profit_price=110.0, strategy_type=STRATEGY_TYPE_SWING,
    )
    return manager


def test_restore_skips_symbols_whose_bracket_is_fully_live() -> None:
    """両方が生きている銘柄は触らないこと（置き直しは冪等）。"""
    ib = MagicMock()
    manager = _position_manager_with_one_open_position()

    with patch("main.ENABLE_REAL_ORDERS", True), \
        patch(
            "main.find_resting_exit_protection_async",
            new=AsyncMock(return_value={"AAPL": _protection(live=("STP", "LMT"))}),
        ), \
        patch("main.cancel_bracket_orders_async", new=AsyncMock()) as mock_cancel, \
        patch("main.place_resting_exit_orders_async", new=AsyncMock()) as mock_place:

        asyncio.run(main_module._restore_missing_resting_orders_async(
            ib, manager, main_module.MarketDataCaches(),
        ))

    mock_cancel.assert_not_awaited()
    mock_place.assert_not_awaited()


def test_recorded_resting_prices_are_corrected_to_the_book() -> None:
    """記録している待機注文の値段が板とずれていたら、板の値へ直すこと。

    **両方が生きていることは、値段が意図どおりであることを意味しない。**
    修正が拒否されても元の注文は生き続けるため（2026-08-06にINTCで
    `Error 10326` として実測。置き直しが両方とも拒否され、板は参照価格ベースの
    まま、positions.json には実約定ベースの値段が残った）、生存確認だけでは
    このずれを毎サイクル見逃す。実際に約定するのは板の注文なので板を正とする。
    """
    ib = MagicMock()
    manager = _position_manager_with_one_open_position()

    with patch("main.ENABLE_REAL_ORDERS", True), \
        patch(
            "main.find_resting_exit_protection_async",
            new=AsyncMock(return_value={"AAPL": _protection(
                live=("STP", "LMT"), stop_price=93.38, take_profit_price=108.12,
            )}),
        ), \
        patch("main.cancel_bracket_orders_async", new=AsyncMock()) as mock_cancel, \
        patch("main.place_resting_exit_orders_async", new=AsyncMock()) as mock_place:

        asyncio.run(main_module._restore_missing_resting_orders_async(
            ib, manager, main_module.MarketDataCaches(),
        ))

    position = manager.get_position("AAPL")
    assert position.stop_price == 93.38
    assert position.take_profit_price == 108.12
    # R倍率の分母も板の逆指値から取り直すこと。据え置くと、板を正としておきながら
    # Rだけが「置くつもりだった損切り」で残り、実際に負ったリスクとずれる。
    assert position.risk_per_share == pytest.approx(100.0 - 93.38)
    # ずれを直すだけで、注文そのものは触らない（板は正しく守っている）。
    mock_cancel.assert_not_awaited()
    mock_place.assert_not_awaited()


def test_resting_prices_that_could_not_be_read_are_left_alone() -> None:
    """板の値段が読めなかった側は記録を触らないこと。

    「確かめられなかった」ことを「一致した」としても「ずれた」としても
    ならない。読めない側を0や推定値で上書きすると、損切りの基準が静かに壊れる。
    """
    ib = MagicMock()
    manager = _position_manager_with_one_open_position()

    with patch("main.ENABLE_REAL_ORDERS", True), \
        patch(
            "main.find_resting_exit_protection_async",
            new=AsyncMock(return_value={"AAPL": _protection(
                live=("STP", "LMT"), stop_price=None, take_profit_price=None,
            )}),
        ), \
        patch("main.cancel_bracket_orders_async", new=AsyncMock()), \
        patch("main.place_resting_exit_orders_async", new=AsyncMock()):

        asyncio.run(main_module._restore_missing_resting_orders_async(
            ib, manager, main_module.MarketDataCaches(),
        ))

    position = manager.get_position("AAPL")
    assert position.stop_price == 95.0
    # 逆指値を触っていないのだからRの分母も動かない。
    assert position.risk_per_share == 5.0
    assert position.take_profit_price == 110.0


def test_restore_places_the_bracket_again_when_nothing_is_live() -> None:
    ib = MagicMock()
    contract = MagicMock(symbol="AAPL")
    manager = _position_manager_with_one_open_position()

    with patch("main.ENABLE_REAL_ORDERS", True), \
        patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.find_resting_exit_protection_async", new=AsyncMock(return_value={})), \
        patch("main.cancel_bracket_orders_async", new=AsyncMock()) as mock_cancel, \
        patch("main.place_resting_exit_orders_async", new=AsyncMock()) as mock_place:

        asyncio.run(main_module._restore_missing_resting_orders_async(
            ib, manager, main_module.MarketDataCaches(),
        ))

    mock_cancel.assert_not_awaited()
    mock_place.assert_awaited_once()


def test_restore_cancels_the_surviving_child_before_replacing_the_bracket() -> None:
    """片方だけ生きている建玉は、残りを消してから置き直すこと。

    2026-08-05の実測では呼値違反で逆指値だけが不成立になり、利確だけが生きた
    建玉が残った（＝下方向に無防備）。片方でもあれば保護ありと数えると
    この状態を毎サイクル見逃す。かといって消さずに両方を置き直すと、建玉を
    超える売り注文が並び、IBKRが超過分を空売りと見なして拒否する。
    """
    ib = MagicMock()
    contract = MagicMock(symbol="AAPL")
    manager = _position_manager_with_one_open_position()
    calls = []

    with patch("main.ENABLE_REAL_ORDERS", True), \
        patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch(
            "main.find_resting_exit_protection_async",
            new=AsyncMock(return_value={"AAPL": _protection(live=("LMT",))}),
        ), \
        patch(
            "main.cancel_bracket_orders_async",
            new=AsyncMock(side_effect=lambda *a, **k: calls.append("cancel")),
        ), \
        patch(
            "main.place_resting_exit_orders_async",
            new=AsyncMock(side_effect=lambda *a, **k: calls.append("place")),
        ):

        asyncio.run(main_module._restore_missing_resting_orders_async(
            ib, manager, main_module.MarketDataCaches(),
        ))

    assert calls == ["cancel", "place"]


def test_restore_skips_symbols_whose_resting_exit_already_filled() -> None:
    """待機注文が約定済みの銘柄へ置き直さないこと。

    約定していれば建玉はもう閉じており（決済の記録は _process_exit_async が行う）、
    ここで置き直すと建玉が無いのに売り注文だけが並ぶ。
    """
    ib = MagicMock()
    manager = _position_manager_with_one_open_position()

    with patch("main.ENABLE_REAL_ORDERS", True), \
        patch(
            "main.find_resting_exit_protection_async",
            new=AsyncMock(return_value={"AAPL": _protection(filled=True)}),
        ), \
        patch("main.place_resting_exit_orders_async", new=AsyncMock()) as mock_place:

        asyncio.run(main_module._restore_missing_resting_orders_async(
            ib, manager, main_module.MarketDataCaches(),
        ))

    mock_place.assert_not_awaited()
