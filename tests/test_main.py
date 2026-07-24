"""main.py のオーケストレーションロジックの単体テスト（IB呼び出しはすべてモック化）。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from execution.order_manager import DryRunOrderResult
from execution.position_manager import PositionManager, STRATEGY_TYPE_DAY, STRATEGY_TYPE_SWING
from execution.trade_journal import TradeJournal
from main import (
    DAY_STOP_LOSS_PCT,
    SWING_STOP_LOSS_PCT,
    _refresh_watchlist_async,
    main,
    process_symbol_async,
    run_watchlist_cycle_async,
)


def _make_df(closes: list) -> pd.DataFrame:
    return pd.DataFrame({"close": closes})


@pytest.fixture
def trade_journal(tmp_path) -> TradeJournal:
    return TradeJournal(str(tmp_path / "trades.csv"))


# --- 新規エントリー（ポジション未保有） -----------------------------------------


def test_process_symbol_opens_position_on_swing_daily_buy_signal(trade_journal) -> None:
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()

    daily_df = _make_df([100.0] * 19 + [80.0])  # 大きく下落 -> 日足で買いシグナル
    intraday_df = _make_df([100.0] * 20)  # 横ばい -> 短期足はシグナルなし

    with patch("main.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
        patch("main.get_intraday_bars_async", new=AsyncMock(return_value=intraday_df)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=80.0)), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch(
            "main.place_dry_run_order_async",
            new=AsyncMock(
                return_value=DryRunOrderResult(symbol="AAPL", action="BUY", quantity=250, order_type="MKT")
            ),
        ) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    # risk_amount=100,000*1%=1,000 / per_share_risk=80*5%=4.0 -> qty=250
    mock_order.assert_awaited_once_with(ib, contract, action="BUY", quantity=250)
    assert position_manager.has_position("AAPL") is True
    position = position_manager.get_position("AAPL")
    assert position.entry_price == 80.0
    assert position.quantity == 250
    # risk_per_share = 80 * stop_loss_pct(5%) / 100 = 4.0
    assert position.risk_per_share == pytest.approx(4.0)
    assert position.strategy_type == STRATEGY_TYPE_SWING


def test_process_symbol_opens_position_on_intraday_buy_signal_when_daily_flat(trade_journal) -> None:
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()

    daily_df = _make_df([100.0] * 20)  # 日足は横ばい -> シグナルなし
    intraday_df = _make_df([100.0] * 19 + [90.0])  # 短期足は下落 -> 買いシグナル

    with patch("main.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
        patch("main.get_intraday_bars_async", new=AsyncMock(return_value=intraday_df)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=90.0)), \
        patch("main.get_account_equity_async", new=AsyncMock(return_value=100_000.0)), \
        patch(
            "main.place_dry_run_order_async",
            new=AsyncMock(
                return_value=DryRunOrderResult(symbol="AAPL", action="BUY", quantity=222, order_type="MKT")
            ),
        ) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    mock_order.assert_awaited_once()
    assert position_manager.has_position("AAPL") is True
    assert position_manager.get_position("AAPL").strategy_type == STRATEGY_TYPE_DAY


def test_process_symbol_skips_entry_when_risk_based_quantity_is_zero(trade_journal) -> None:
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()

    daily_df = _make_df([100.0] * 19 + [80.0])  # 大きく下落 -> 買いシグナル
    intraday_df = _make_df([100.0] * 20)

    with patch("main.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
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

    daily_df = _make_df([100.0] * 20)  # 横ばい -> シグナルなし
    intraday_df = _make_df([100.0] * 20)  # 横ばい -> シグナルなし

    with patch("main.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
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

    with patch("main.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_historical_bars_async", new=AsyncMock(return_value=pd.DataFrame())), \
        patch("main.get_intraday_bars_async", new=AsyncMock(return_value=pd.DataFrame())), \
        patch("main.place_dry_run_order_async", new=AsyncMock()) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    mock_order.assert_not_awaited()
    assert position_manager.has_position("AAPL") is False


def test_process_symbol_skips_entry_when_max_concurrent_positions_reached(trade_journal) -> None:
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()
    # main.MAX_CONCURRENT_POSITIONS(5)分のダミーポジションで上限に到達させる
    for i in range(5):
        position_manager.open_position(f"SYM{i}", entry_price=10.0, quantity=1)

    daily_df = _make_df([100.0] * 19 + [80.0])  # 大きく下落 -> 買いシグナル
    intraday_df = _make_df([100.0] * 20)

    with patch("main.qualify_stock_async", new=AsyncMock(return_value=contract)) as mock_qualify, \
        patch("main.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
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

    daily_df = _make_df([100.0] * 19 + [80.0])  # 大きく下落 -> 買いシグナル
    intraday_df = _make_df([100.0] * 20)

    with patch("main.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
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

    with patch("main.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=90.0)), \
        patch("main.place_dry_run_order_async", new=AsyncMock()) as mock_order:

        asyncio.run(process_symbol_async(ib, "AAPL", position_manager, trade_journal))

    mock_order.assert_awaited_once_with(ib, contract, action="SELL", quantity=3)
    assert position_manager.has_position("AAPL") is False


# --- 決済（ポジション保有中） ---------------------------------------------------


def test_process_symbol_closes_position_on_exit_signal(trade_journal) -> None:
    contract = MagicMock(symbol="AAPL")
    ib = MagicMock()
    position_manager = PositionManager()
    position_manager.open_position("AAPL", entry_price=100.0, quantity=3, risk_per_share=5.0)

    with patch("main.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=90.0)), \
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

    with patch("main.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=90.0)), \
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

    with patch("main.qualify_stock_async", new=AsyncMock(return_value=contract)), \
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

    with patch("main.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=108.0)), \
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

    with patch("main.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=97.0)), \
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

    with patch("main.qualify_stock_async", new=AsyncMock(return_value=contract)), \
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

    with patch("main.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_current_price_async", new=AsyncMock(return_value=100.5)), \
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

    with patch("main.qualify_stock_async", new=AsyncMock(return_value=contract)), \
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

    daily_df = _make_df([100.0] * 20)  # 日足は横ばい -> シグナルなし
    intraday_df = _make_df([100.0] * 19 + [90.0])  # 短期足は下落 -> 買いシグナル(day)

    with patch("main.qualify_stock_async", new=AsyncMock(return_value=contract)), \
        patch("main.get_historical_bars_async", new=AsyncMock(return_value=daily_df)), \
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
        result = asyncio.run(_refresh_watchlist_async(ib, ["FALLBACK"]))

    assert result == ["CHEAP1", "CHEAP2"]


def test_refresh_watchlist_falls_back_when_screening_returns_empty() -> None:
    ib = MagicMock()

    with patch("main.screen_value_stocks_async", new=AsyncMock(return_value=[])):
        result = asyncio.run(_refresh_watchlist_async(ib, ["FALLBACK"]))

    assert result == ["FALLBACK"]


def test_refresh_watchlist_falls_back_when_screening_raises() -> None:
    ib = MagicMock()

    with patch("main.screen_value_stocks_async", new=AsyncMock(side_effect=RuntimeError("boom"))):
        result = asyncio.run(_refresh_watchlist_async(ib, ["FALLBACK"]))

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
