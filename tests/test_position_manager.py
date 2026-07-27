"""execution/position_manager.py の単体テスト。"""

import asyncio
import json
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from execution.position_manager import (
    Position,
    PositionManager,
    STRATEGY_TYPE_DAY,
    STRATEGY_TYPE_SWING,
    STRATEGY_TYPE_UNKNOWN,
)


def _make_broker_position(
    symbol: str, position: float, avg_cost: float,
    sec_type: str = "STK", currency: str = "USD",
):
    contract = MagicMock(symbol=symbol, secType=sec_type, currency=currency)
    return MagicMock(contract=contract, position=position, avgCost=avg_cost)


def _make_mock_ib(broker_positions: list) -> MagicMock:
    ib = MagicMock()
    ib.reqPositionsAsync = AsyncMock(return_value=broker_positions)
    return ib


def test_has_position_false_when_empty() -> None:
    manager = PositionManager()

    assert manager.has_position("AAPL") is False
    assert manager.get_position("AAPL") is None


def test_count_open_positions() -> None:
    manager = PositionManager()

    assert manager.count_open_positions() == 0

    manager.open_position("AAPL", entry_price=100.0, quantity=1)
    manager.open_position("MSFT", entry_price=200.0, quantity=1)
    assert manager.count_open_positions() == 2

    manager.close_position("AAPL")
    assert manager.count_open_positions() == 1


def test_open_symbols_returns_empty_list_when_no_positions() -> None:
    manager = PositionManager()

    assert manager.open_symbols() == []


def test_open_symbols_returns_all_held_symbols() -> None:
    manager = PositionManager()
    manager.open_position("AAPL", entry_price=100.0, quantity=1)
    manager.open_position("MSFT", entry_price=200.0, quantity=1)

    assert set(manager.open_symbols()) == {"AAPL", "MSFT"}

    manager.close_position("AAPL")
    assert manager.open_symbols() == ["MSFT"]


def test_open_position_creates_position_with_highest_price_at_entry() -> None:
    manager = PositionManager()

    position = manager.open_position("AAPL", entry_price=100.0, quantity=5)

    assert isinstance(position, Position)
    assert manager.has_position("AAPL") is True
    assert position.symbol == "AAPL"
    assert position.entry_price == 100.0
    assert position.quantity == 5
    assert position.highest_price == 100.0
    assert position.risk_per_share == 0.0
    assert position.strategy_type == STRATEGY_TYPE_UNKNOWN
    assert manager.get_position("AAPL") is position


def test_open_position_stamps_entry_date_in_utc() -> None:
    manager = PositionManager()
    before = datetime.now(timezone.utc)

    position = manager.open_position("AAPL", entry_price=100.0, quantity=5)

    after = datetime.now(timezone.utc)
    assert position.entry_date is not None
    entry_dt = datetime.fromisoformat(position.entry_date)
    assert before <= entry_dt <= after


def test_sync_discovered_position_has_no_entry_date() -> None:
    manager = PositionManager()
    ib = _make_mock_ib([_make_broker_position("AAPL", 10, 150.0)])

    asyncio.run(manager.sync_with_broker_async(ib))

    # ブローカー側で発見した未追跡ポジションは実際の建玉日が不明なためNone
    assert manager.get_position("AAPL").entry_date is None


def test_open_position_stores_risk_per_share_when_given() -> None:
    manager = PositionManager()

    position = manager.open_position("AAPL", entry_price=100.0, quantity=5, risk_per_share=4.5)

    assert position.risk_per_share == 4.5


def test_open_position_stores_strategy_type_when_given() -> None:
    manager = PositionManager()

    swing_position = manager.open_position(
        "AAPL", entry_price=100.0, quantity=5, strategy_type=STRATEGY_TYPE_SWING
    )
    day_position = manager.open_position(
        "MSFT", entry_price=200.0, quantity=1, strategy_type=STRATEGY_TYPE_DAY
    )

    assert swing_position.strategy_type == STRATEGY_TYPE_SWING
    assert day_position.strategy_type == STRATEGY_TYPE_DAY


def test_open_position_raises_on_empty_strategy_type() -> None:
    manager = PositionManager()

    with pytest.raises(ValueError):
        manager.open_position("AAPL", entry_price=100.0, quantity=1, strategy_type="")


@pytest.mark.parametrize("entry_price,quantity", [(0.0, 1), (-1.0, 1), (100.0, 0), (100.0, -1)])
def test_open_position_raises_on_invalid_args(entry_price, quantity) -> None:
    manager = PositionManager()

    with pytest.raises(ValueError):
        manager.open_position("AAPL", entry_price=entry_price, quantity=quantity)


def test_open_position_raises_on_negative_risk_per_share() -> None:
    manager = PositionManager()

    with pytest.raises(ValueError):
        manager.open_position("AAPL", entry_price=100.0, quantity=1, risk_per_share=-1.0)


def test_open_position_raises_when_already_open() -> None:
    manager = PositionManager()
    manager.open_position("AAPL", entry_price=100.0, quantity=1)

    with pytest.raises(ValueError):
        manager.open_position("AAPL", entry_price=110.0, quantity=1)


def test_update_highest_price_raises_the_bar() -> None:
    manager = PositionManager()
    manager.open_position("AAPL", entry_price=100.0, quantity=1)

    position = manager.update_highest_price("AAPL", 120.0)

    assert position.highest_price == 120.0


def test_update_highest_price_does_not_decrease() -> None:
    manager = PositionManager()
    manager.open_position("AAPL", entry_price=100.0, quantity=1)
    manager.update_highest_price("AAPL", 120.0)

    position = manager.update_highest_price("AAPL", 110.0)

    assert position.highest_price == 120.0


def test_update_highest_price_raises_keyerror_when_no_position() -> None:
    manager = PositionManager()

    with pytest.raises(KeyError):
        manager.update_highest_price("AAPL", 100.0)


def test_close_position_removes_and_returns_position() -> None:
    manager = PositionManager()
    manager.open_position("AAPL", entry_price=100.0, quantity=1)

    closed = manager.close_position("AAPL")

    assert closed.symbol == "AAPL"
    assert manager.has_position("AAPL") is False


def test_close_position_raises_keyerror_when_no_position() -> None:
    manager = PositionManager()

    with pytest.raises(KeyError):
        manager.close_position("AAPL")


# --- sync_with_broker_async ------------------------------------------------


def test_sync_discovers_untracked_broker_position() -> None:
    manager = PositionManager()
    ib = _make_mock_ib([_make_broker_position("AAPL", 10, 150.0)])

    asyncio.run(manager.sync_with_broker_async(ib))

    assert manager.has_position("AAPL") is True
    position = manager.get_position("AAPL")
    assert position.entry_price == 150.0
    assert position.quantity == 10
    assert position.highest_price == 150.0
    assert position.strategy_type == STRATEGY_TYPE_UNKNOWN


def test_sync_ignores_zero_quantity_broker_positions() -> None:
    manager = PositionManager()
    ib = _make_mock_ib([_make_broker_position("AAPL", 0, 150.0)])

    asyncio.run(manager.sync_with_broker_async(ib))

    assert manager.has_position("AAPL") is False


def test_sync_overwrites_existing_position_with_broker_values() -> None:
    manager = PositionManager()
    manager.open_position("AAPL", entry_price=100.0, quantity=5)
    ib = _make_mock_ib([_make_broker_position("AAPL", 8, 105.0)])

    asyncio.run(manager.sync_with_broker_async(ib))

    position = manager.get_position("AAPL")
    assert position.entry_price == 105.0
    assert position.quantity == 8


def test_sync_preserves_highest_price_above_broker_avg_cost() -> None:
    manager = PositionManager()
    manager.open_position("AAPL", entry_price=100.0, quantity=5)
    manager.update_highest_price("AAPL", 120.0)
    ib = _make_mock_ib([_make_broker_position("AAPL", 5, 100.0)])

    asyncio.run(manager.sync_with_broker_async(ib))

    assert manager.get_position("AAPL").highest_price == 120.0


def test_sync_keeps_local_only_position_not_reported_by_broker() -> None:
    # ドライラン注文でローカルに建てたが、ブローカー側には未反映のポジション。
    manager = PositionManager()
    manager.open_position("AAPL", entry_price=100.0, quantity=1)
    ib = _make_mock_ib([])

    asyncio.run(manager.sync_with_broker_async(ib))

    assert manager.has_position("AAPL") is True
    assert manager.get_position("AAPL").entry_price == 100.0


def test_sync_prevents_duplicate_entry_for_symbol_already_held_at_broker() -> None:
    manager = PositionManager()
    ib = _make_mock_ib([_make_broker_position("RIVN", 3, 20.0)])

    asyncio.run(manager.sync_with_broker_async(ib))

    # 既にブローカー側で保有中なので、新規エントリー判定側は
    # has_position=True を見て買い注文をスキップできる。
    assert manager.has_position("RIVN") is True


# --- 状態の永続化 ---------------------------------------------------------------


def _state_path(tmp_path) -> str:
    return str(tmp_path / "positions.json")


def test_positions_survive_restart(tmp_path) -> None:
    """ドライラン中はブローカー側に建玉が無いため、永続化が唯一の復元手段。"""
    path = _state_path(tmp_path)

    manager = PositionManager(state_path=path)
    manager.open_position(
        "AAPL", entry_price=100.0, quantity=5, risk_per_share=5.0,
        strategy_type=STRATEGY_TYPE_SWING,
    )
    manager.update_highest_price("AAPL", 130.0)

    restored = PositionManager(state_path=path)
    position = restored.get_position("AAPL")

    assert position is not None
    assert position.entry_price == 100.0
    assert position.quantity == 5
    assert position.risk_per_share == 5.0
    assert position.strategy_type == STRATEGY_TYPE_SWING
    # トレーリングストップの基準となる高値が失われないこと
    assert position.highest_price == 130.0


def test_entry_date_survives_restart(tmp_path) -> None:
    path = _state_path(tmp_path)
    manager = PositionManager(state_path=path)
    manager.open_position("AAPL", entry_price=100.0, quantity=1)
    original = manager.get_position("AAPL").entry_date

    restored = PositionManager(state_path=path)

    assert restored.get_position("AAPL").entry_date == original


def test_closed_position_is_removed_from_state(tmp_path) -> None:
    path = _state_path(tmp_path)
    manager = PositionManager(state_path=path)
    manager.open_position("AAPL", entry_price=100.0, quantity=1)
    manager.close_position("AAPL")

    restored = PositionManager(state_path=path)

    assert restored.has_position("AAPL") is False
    assert restored.count_open_positions() == 0


def test_broker_synced_positions_are_persisted(tmp_path) -> None:
    path = _state_path(tmp_path)
    manager = PositionManager(state_path=path)
    ib = _make_mock_ib([_make_broker_position("RIVN", 3, 20.0)])

    asyncio.run(manager.sync_with_broker_async(ib))
    restored = PositionManager(state_path=path)

    assert restored.get_position("RIVN").quantity == 3


def test_starts_empty_when_state_file_does_not_exist(tmp_path) -> None:
    manager = PositionManager(state_path=_state_path(tmp_path))

    assert manager.count_open_positions() == 0


def test_corrupt_state_file_does_not_prevent_startup(tmp_path) -> None:
    path = _state_path(tmp_path)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{壊れたJSON")

    manager = PositionManager(state_path=path)

    assert manager.count_open_positions() == 0
    # 中身を後から確認できるよう、破棄せず退避されていること
    assert os.path.exists(f"{path}.corrupt")


def test_state_file_with_unexpected_shape_does_not_prevent_startup(tmp_path) -> None:
    path = _state_path(tmp_path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"positions": [{"unknown_field": 1}]}, f)

    manager = PositionManager(state_path=path)

    assert manager.count_open_positions() == 0


def test_no_state_file_is_written_without_state_path(tmp_path) -> None:
    # state_path未指定ならインメモリのみ（単体テスト用の既定動作）
    manager = PositionManager()
    manager.open_position("AAPL", entry_price=100.0, quantity=1)

    assert list(tmp_path.iterdir()) == []


def test_highest_price_is_not_rewritten_when_unchanged(tmp_path) -> None:
    path = _state_path(tmp_path)
    manager = PositionManager(state_path=path)
    manager.open_position("AAPL", entry_price=100.0, quantity=1)

    with patch.object(PositionManager, "_save") as mock_save:
        manager.update_highest_price("AAPL", 90.0)   # 高値未更新 -> 保存しない
        assert mock_save.call_count == 0
        manager.update_highest_price("AAPL", 110.0)  # 高値更新 -> 保存する
        assert mock_save.call_count == 1


# --- ブローカー同期の対象絞り込み ------------------------------------------------


def test_sync_ignores_option_position_with_the_same_underlying_symbol() -> None:
    """AAPLのコールオプションもcontract.symbolは"AAPL"になる。

    シンボル文字列だけで突き合わせると、オプションの建玉を現物ポジションとして
    取り込み、存在しない現物株の決済判定を始めてしまう。
    """
    manager = PositionManager()
    ib = _make_mock_ib([_make_broker_position("AAPL", 1, 500.0, sec_type="OPT")])

    asyncio.run(manager.sync_with_broker_async(ib))

    assert manager.has_position("AAPL") is False


@pytest.mark.parametrize("sec_type", ["OPT", "FUT", "CASH", "BOND"])
def test_sync_only_tracks_stock_positions(sec_type) -> None:
    manager = PositionManager()
    ib = _make_mock_ib([_make_broker_position("XYZ", 10, 100.0, sec_type=sec_type)])

    asyncio.run(manager.sync_with_broker_async(ib))

    assert manager.count_open_positions() == 0


def test_sync_ignores_non_usd_listing_of_the_same_symbol() -> None:
    # 例: トロント上場銘柄はcontract.symbolが米国株と衝突しうる
    manager = PositionManager()
    ib = _make_mock_ib([_make_broker_position("SHOP", 10, 100.0, currency="CAD")])

    asyncio.run(manager.sync_with_broker_async(ib))

    assert manager.has_position("SHOP") is False


def test_sync_ignores_short_positions() -> None:
    """本Botはロング専用。ショートを取り込むと決済時のSELL数量が負になる。"""
    manager = PositionManager()
    ib = _make_mock_ib([_make_broker_position("AAPL", -50, 100.0)])

    asyncio.run(manager.sync_with_broker_async(ib))

    assert manager.has_position("AAPL") is False


def test_sync_tracks_us_stock_long_position() -> None:
    manager = PositionManager()
    ib = _make_mock_ib([_make_broker_position("AAPL", 10, 100.0)])

    asyncio.run(manager.sync_with_broker_async(ib))

    assert manager.get_position("AAPL").quantity == 10


def test_sync_picks_tracked_position_out_of_a_mixed_portfolio() -> None:
    manager = PositionManager()
    ib = _make_mock_ib([
        _make_broker_position("AAPL", 2, 500.0, sec_type="OPT"),
        _make_broker_position("EUR", 10000, 1.1, sec_type="CASH"),
        _make_broker_position("MSFT", -20, 400.0),
        _make_broker_position("AAPL", 10, 190.0),
    ])

    asyncio.run(manager.sync_with_broker_async(ib))

    assert manager.open_symbols() == ["AAPL"]
    assert manager.get_position("AAPL").quantity == 10


def test_sync_does_not_persist_when_nothing_is_tracked(tmp_path) -> None:
    path = str(tmp_path / "positions.json")
    manager = PositionManager(state_path=path)
    ib = _make_mock_ib([_make_broker_position("AAPL", 1, 500.0, sec_type="OPT")])

    asyncio.run(manager.sync_with_broker_async(ib))

    assert not os.path.exists(path)
