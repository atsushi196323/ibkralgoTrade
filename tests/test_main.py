"""main.py のオーケストレーションロジックの単体テスト（IB呼び出しはすべてモック化）。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from execution.order_manager import DryRunBracketResult, DryRunOrderResult
from execution.position_manager import PositionManager, STRATEGY_TYPE_DAY, STRATEGY_TYPE_SWING
from execution.trade_journal import TradeJournal
from strategy.pullback import MarketFilterConfig
from execution.position_sizing import calculate_position_size
from main import (
    DAY_STOP_LOSS_PCT,
    MARKET_INDEX_SYMBOL,
    MAX_CONCURRENT_POSITIONS,
    MAX_DAILY_ENTRY_ORDERS,
    MAX_WATCHLIST_SIZE,
    POLL_INTERVAL_SECONDS,
    RISK_PER_TRADE_PCT,
    SWING_MA_WINDOW,
    SWING_STOP_LOSS_PCT,
    MarketDataCaches,
    _refresh_watchlist_async,
    main,
    process_symbol_async,
    resolve_max_affordable_price,
    run_watchlist_cycle_async,
)


def _make_df(closes: list) -> pd.DataFrame:
    return pd.DataFrame({"close": closes})


def _make_daily_df(*, drop: bool = False) -> pd.DataFrame:
    """スイング（日足）判定用のバー。drop=Trueで買いシグナルが出る形にする。

    本数をSWING_MA_WINDOWから導出しているのは、移動平均期間を変更したときに
    「本数不足で日足分岐が丸ごとスキップされ、テストは通るがシグナル判定を
    一度も通っていない」状態に陥るのを防ぐため。
    """
    if drop:
        return _make_df([100.0] * (SWING_MA_WINDOW - 1) + [80.0])
    return _make_df([100.0] * SWING_MA_WINDOW)


def _bracket_result(quantity: int, symbol: str = "AAPL") -> DryRunBracketResult:
    """新規建て時のブラケット注文の戻り値（値段は呼び出し側の検証対象外）。"""
    return DryRunBracketResult(
        symbol=symbol, quantity=quantity, stop_price=0.0, take_profit_price=0.0,
        oca_group="OCA_TEST",
    )


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
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch(
            "main.place_dry_run_bracket_order_async",
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


def test_process_symbol_opens_position_on_intraday_buy_signal_when_daily_flat(trade_journal) -> None:
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()

    daily_df = _make_daily_df()  # 日足は横ばい -> シグナルなし
    intraday_df = _make_df([100.0] * 19 + [90.0])  # 短期足は下落 -> 買いシグナル

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
        patch("main.get_intraday_bars_async", new=AsyncMock(return_value=intraday_df)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=90.0)), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch(
            "main.place_dry_run_bracket_order_async",
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
        patch("main.get_account_equity_async", new=AsyncMock(return_value=1.0)), \
        patch("main.place_dry_run_order_async", new=AsyncMock()) as mock_order:

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
        patch("main.place_dry_run_order_async", new=AsyncMock()) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    mock_order.assert_not_awaited()
    mock_price.assert_not_awaited()
    assert position_manager.has_position("AAPL") is False


def test_process_symbol_skips_entry_when_both_timeframes_have_no_data(trade_journal) -> None:
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=pd.DataFrame())), \
        patch("main.get_intraday_bars_async", new=AsyncMock(return_value=pd.DataFrame())), \
        patch("main.place_dry_run_order_async", new=AsyncMock()) as mock_order:

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
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.place_dry_run_order_async", new=AsyncMock()) as mock_order:

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
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.place_dry_run_order_async", new=AsyncMock()) as mock_order:

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
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=150.0)), \
        patch("main.place_dry_run_order_async", new=AsyncMock()) as mock_order:

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
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=150.0)), \
        patch("main.place_dry_run_order_async", new=AsyncMock()) as mock_order:

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


def test_process_symbol_records_none_r_multiple_when_risk_per_share_unknown(trade_journal) -> None:
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()
    # risk_per_share未指定(=0.0、ブローカー同期で発見されたポジション相当)
    position_manager.open_position("AAPL", entry_price=100.0, quantity=3)

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=90.0)), \
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=150.0)), \
        patch("main.place_dry_run_order_async", new=AsyncMock()):

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
        patch("main.place_dry_run_order_async", new=AsyncMock()) as mock_order:

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
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=150.0)), \
        patch("main.place_dry_run_order_async", new=AsyncMock()) as mock_order:

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
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=150.0)), \
        patch("main.is_day_trade_flatten_time", return_value=False), \
        patch("main.place_dry_run_order_async", new=AsyncMock()) as mock_order:

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
        patch("main.is_day_trade_flatten_time", return_value=False), \
        patch("main.place_dry_run_order_async", new=AsyncMock()) as mock_order:

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
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=150.0)), \
        patch("main.is_day_trade_flatten_time", return_value=True), \
        patch("main.place_dry_run_order_async", new=AsyncMock()) as mock_order:

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
        patch("main.is_day_trade_flatten_time", return_value=True), \
        patch("main.place_dry_run_order_async", new=AsyncMock()) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    mock_order.assert_not_awaited()
    assert position_manager.has_position("AAPL") is True


def test_process_symbol_opens_day_position_with_day_specific_risk_per_share(trade_journal) -> None:
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()

    daily_df = _make_daily_df()  # 日足は横ばい -> シグナルなし
    intraday_df = _make_df([100.0] * 19 + [90.0])  # 短期足は下落 -> 買いシグナル(day)

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
        patch("main.get_intraday_bars_async", new=AsyncMock(return_value=intraday_df)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=90.0)), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch(
            "main.place_dry_run_order_async",
            new=AsyncMock(
                side_effect=lambda ib, contract, action, quantity: DryRunOrderResult(
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

    assert result == ["CHEAP1", "CHEAP2"]


def test_refresh_watchlist_falls_back_when_screening_returns_empty() -> None:
    ib = MagicMock()

    with patch("main.screen_value_stocks_async", new=AsyncMock(return_value=[])):
        result = asyncio.run(_refresh_watchlist_async(ib, ["FALLBACK"], account_equity=100_000.0))

    assert result == ["FALLBACK"]


def test_refresh_watchlist_falls_back_when_screening_raises() -> None:
    ib = MagicMock()

    with patch("main.screen_value_stocks_async", new=AsyncMock(side_effect=RuntimeError("boom"))):
        result = asyncio.run(_refresh_watchlist_async(ib, ["FALLBACK"], account_equity=100_000.0))

    assert result == ["FALLBACK"]


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
        patch("main._refresh_watchlist_async", new=AsyncMock(return_value=["AAPL"])), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
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


# --- ペーシング制限対策 ----------------------------------------------------------


def test_watchlist_is_capped_to_limit_screening_request_volume() -> None:
    """監視銘柄1件につき毎サイクル1回の日中足リクエストが発生するため、
    スクリーニングが何件返しても監視対象は上限で頭打ちにする。"""
    screened = [f"SYM{i}" for i in range(MAX_WATCHLIST_SIZE + 15)]

    with patch("main.screen_value_stocks_async", new=AsyncMock(return_value=screened)):
        result = asyncio.run(_refresh_watchlist_async(MagicMock(), ["FALLBACK"], account_equity=100_000.0))

    assert len(result) == MAX_WATCHLIST_SIZE
    assert result == screened[:MAX_WATCHLIST_SIZE]


def test_watchlist_below_cap_is_kept_intact() -> None:
    screened = ["CHEAP1", "CHEAP2"]

    with patch("main.screen_value_stocks_async", new=AsyncMock(return_value=screened)):
        result = asyncio.run(_refresh_watchlist_async(MagicMock(), ["FALLBACK"], account_equity=100_000.0))

    assert result == screened


def test_poll_interval_keeps_watchlist_within_ibkr_pacing_limit() -> None:
    """IBKRのヒストリカルデータ制限(10分あたり60件)を設定値が満たしていること。

    監視銘柄1件あたり毎サイクル1リクエスト(日中足)なので、
        MAX_WATCHLIST_SIZE * (600 / POLL_INTERVAL_SECONDS) <= 60
    を満たす必要がある。片方だけ変更して制限を割るのを防ぐための番人。
    """
    requests_per_10min = MAX_WATCHLIST_SIZE * (600.0 / POLL_INTERVAL_SECONDS)

    assert requests_per_10min <= 60


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
    # 日中足はデイトレードのシグナルそのものなので毎サイクル取得する
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
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=150.0)), \
        patch("main.place_dry_run_order_async", new=AsyncMock()) as mock_order, \
        patch("main.cancel_dry_run_bracket_orders_async", new=AsyncMock()) as mock_cancel:

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
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=150.0)), \
        patch("main.place_dry_run_order_async", new=AsyncMock()) as mock_order:

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
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=150.0)), \
        patch("main.place_dry_run_order_async", new=AsyncMock()) as mock_order, \
        patch("main.cancel_dry_run_bracket_orders_async", new=AsyncMock()) as mock_cancel:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    mock_cancel.assert_awaited_once_with(ib, "AAPL", "OCA_1")
    mock_order.assert_awaited_once_with(ib, contract, action="SELL", quantity=3)
    assert trade_journal.load_trades()[0].reason == "TRAILING_STOP"


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
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=150.0)), \
        patch("main.is_day_trade_flatten_time", return_value=True), \
        patch("main.place_dry_run_order_async", new=AsyncMock()) as mock_order, \
        patch("main.cancel_dry_run_bracket_orders_async", new=AsyncMock()) as mock_cancel:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    mock_cancel.assert_awaited_once_with(ib, "AAPL", "OCA_2")
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
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=150.0)), \
        patch("main.place_dry_run_order_async", new=AsyncMock()) as mock_order:

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
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.place_dry_run_bracket_order_async", new=AsyncMock()) as mock_order:

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
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch(
            "main.place_dry_run_bracket_order_async",
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
        position_manager.open_position(symbol, entry_price=100.0, quantity=1)
        position_manager.close_position(symbol)

    daily_df = _make_daily_df(drop=True)  # 買いシグナルは出ている

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)) as mock_qualify, \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
        patch("main.get_intraday_bars_async", new=AsyncMock(return_value=_make_df([100.0] * 20))), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=80.0)), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.place_dry_run_bracket_order_async", new=AsyncMock()) as mock_order:

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
        position_manager.open_position(symbol, entry_price=100.0, quantity=1)
        position_manager.close_position(symbol)

    daily_df = _make_daily_df(drop=True)

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("data.cache.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
        patch("main.get_intraday_bars_async", new=AsyncMock(return_value=_make_df([100.0] * 20))), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=80.0)), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch(
            "main.place_dry_run_bracket_order_async",
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
        position_manager.open_position(symbol, entry_price=100.0, quantity=1)
        position_manager.close_position(symbol)
    # 上限に達した後も保有中のポジションが残っている状況
    position_manager.open_position(
        "AAPL", entry_price=100.0, quantity=3, strategy_type=STRATEGY_TYPE_SWING,
    )

    with patch("data.cache.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=80.0)), \
        patch("main.get_usd_jpy_rate_async", new=AsyncMock(return_value=150.0)), \
        patch("main.cancel_dry_run_bracket_orders_async", new=AsyncMock()), \
        patch(
            "main.place_dry_run_order_async",
            new=AsyncMock(return_value=DryRunOrderResult("AAPL", "SELL", 3, "MKT")),
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
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch("main.place_dry_run_bracket_order_async", new=AsyncMock()) as mock_order:

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
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch(
            "main.place_dry_run_bracket_order_async",
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
