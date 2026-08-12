"""core/connection.py の単体テスト（IBへの実接続は行わずすべてモック化）。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.connection import IBKRConnection


def _make_mock_ib() -> MagicMock:
    """ib_async.IB() の代わりに使うモックインスタンスを作る。"""
    mock_ib = MagicMock()
    mock_ib.connectAsync = AsyncMock(return_value=None)
    mock_ib.isConnected = MagicMock(return_value=False)
    mock_ib.disconnect = MagicMock()
    # disconnectedEvent は ib_async の Event 型で `+=` (__iadd__) をサポートする。
    mock_ib.disconnectedEvent = MagicMock()
    return mock_ib


@pytest.fixture
def mock_ib_class():
    mock_instance = _make_mock_ib()
    with patch("core.connection.IB", return_value=mock_instance) as mock_cls:
        yield mock_cls, mock_instance


# --- __init__ / 設定読み込み -------------------------------------------------


def test_init_uses_hardcoded_defaults_when_no_args_or_env(monkeypatch, mock_ib_class) -> None:
    monkeypatch.delenv("IBKR_HOST", raising=False)
    monkeypatch.delenv("IBKR_PORT", raising=False)
    monkeypatch.delenv("IBKR_CLIENT_ID", raising=False)
    monkeypatch.delenv("IBKR_MARKET_DATA_TYPE", raising=False)

    connection = IBKRConnection()

    assert connection.host == "127.0.0.1"
    assert connection.port == 4002
    assert connection.client_id == 1
    # ペーパー口座はリアルタイムデータの購読契約を持たないことが多いため、
    # 未設定時は遅延データ(3)をデフォルトにする。
    assert connection.market_data_type == 3


def test_init_reads_from_env_vars(monkeypatch, mock_ib_class) -> None:
    monkeypatch.setenv("IBKR_HOST", "10.0.0.5")
    monkeypatch.setenv("IBKR_PORT", "4002")
    monkeypatch.setenv("IBKR_CLIENT_ID", "42")
    monkeypatch.setenv("IBKR_MARKET_DATA_TYPE", "1")

    connection = IBKRConnection()

    assert connection.host == "10.0.0.5"
    assert connection.port == 4002
    assert connection.client_id == 42
    assert connection.market_data_type == 1


def test_init_explicit_args_override_env(monkeypatch, mock_ib_class) -> None:
    monkeypatch.setenv("IBKR_HOST", "10.0.0.5")
    monkeypatch.setenv("IBKR_PORT", "4002")
    monkeypatch.setenv("IBKR_CLIENT_ID", "42")
    monkeypatch.setenv("IBKR_MARKET_DATA_TYPE", "1")

    connection = IBKRConnection(host="192.168.1.1", port=7496, client_id=99, market_data_type=4)

    assert connection.host == "192.168.1.1"
    assert connection.port == 7496
    assert connection.client_id == 99
    assert connection.market_data_type == 4


def test_init_registers_disconnected_handler(mock_ib_class) -> None:
    _, mock_instance = mock_ib_class
    # `+=` は disconnectedEvent を __iadd__ の戻り値で上書きするため、
    # 呼び出し前に元のオブジェクトへの参照を保持しておく。
    original_event = mock_instance.disconnectedEvent

    connection = IBKRConnection()

    original_event.__iadd__.assert_called_once_with(connection._on_disconnected)


# --- connect_async -----------------------------------------------------------


def test_connect_async_succeeds_on_first_attempt(mock_ib_class) -> None:
    _, mock_instance = mock_ib_class
    connection = IBKRConnection(host="127.0.0.1", port=7497, client_id=1)

    result = asyncio.run(connection.connect_async())

    assert result is mock_instance
    mock_instance.connectAsync.assert_awaited_once_with(
        "127.0.0.1", 7497, clientId=1
    )


def test_connect_async_sets_market_data_type_on_success(mock_ib_class) -> None:
    _, mock_instance = mock_ib_class
    connection = IBKRConnection(host="127.0.0.1", port=7497, client_id=1, market_data_type=3)

    asyncio.run(connection.connect_async())

    mock_instance.reqMarketDataType.assert_called_once_with(3)


def test_connect_async_does_not_set_market_data_type_when_all_attempts_fail(
    mock_ib_class,
) -> None:
    _, mock_instance = mock_ib_class
    mock_instance.connectAsync = AsyncMock(side_effect=ConnectionRefusedError("always fails"))
    connection = IBKRConnection(max_retries=2, base_delay_seconds=1.0)

    with patch("core.connection.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(ConnectionError):
            asyncio.run(connection.connect_async())

    mock_instance.reqMarketDataType.assert_not_called()


def test_connect_async_retries_with_exponential_backoff_then_succeeds(
    mock_ib_class,
) -> None:
    _, mock_instance = mock_ib_class
    mock_instance.connectAsync = AsyncMock(
        side_effect=[ConnectionRefusedError("fail1"), ConnectionRefusedError("fail2"), None]
    )
    connection = IBKRConnection(max_retries=5, base_delay_seconds=1.0)

    with patch("core.connection.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        result = asyncio.run(connection.connect_async())

    assert result is mock_instance
    assert mock_instance.connectAsync.await_count == 3
    # 1回目失敗後は1.0秒、2回目失敗後は2.0秒（指数的バックオフ）待つ。
    assert mock_sleep.await_args_list == [
        ((1.0,),),
        ((2.0,),),
    ]


def test_connect_async_caps_the_backoff_delay(mock_ib_class) -> None:
    """1回あたりの待ち時間は上限で頭打ちにすること。

    上限が無いと待ち時間が青天井に伸び、Gatewayが復帰済みでも
    次の試行まで延々と待つことになる。
    """
    _, mock_instance = mock_ib_class
    mock_instance.connectAsync = AsyncMock(
        side_effect=[ConnectionRefusedError("fail")] * 5 + [None]
    )
    connection = IBKRConnection(max_retries=10, base_delay_seconds=1.0, max_delay_seconds=8.0)

    with patch("core.connection.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        asyncio.run(connection.connect_async())

    # 1, 2, 4, 8, 8 — 上限の8秒を超えない。
    assert [call.args[0] for call in mock_sleep.await_args_list] == [1.0, 2.0, 4.0, 8.0, 8.0]


def test_default_retry_window_covers_a_gateway_restart(mock_ib_class) -> None:
    """既定のリトライ設定で、Gatewayの再起動（数分）を待ち切れること。

    Gatewayは Auto restart で1日1回再起動し、その間ソケットは接続不能になる。
    リトライを使い切るのが早すぎると、復帰しているのに接続を諦めることになる。
    """
    _, mock_instance = mock_ib_class
    mock_instance.connectAsync = AsyncMock(side_effect=ConnectionRefusedError("always fails"))
    connection = IBKRConnection()

    with patch("core.connection.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        with pytest.raises(ConnectionError):
            asyncio.run(connection.connect_async())

    total_wait = sum(call.args[0] for call in mock_sleep.await_args_list)
    assert total_wait >= 180.0, f"リトライの総待ち時間が短すぎます: {total_wait}秒"


def test_connect_async_raises_after_exhausting_retries(mock_ib_class) -> None:
    _, mock_instance = mock_ib_class
    mock_instance.connectAsync = AsyncMock(side_effect=ConnectionRefusedError("always fails"))
    connection = IBKRConnection(max_retries=3, base_delay_seconds=1.0)

    with patch("core.connection.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        with pytest.raises(ConnectionError):
            asyncio.run(connection.connect_async())

    assert mock_instance.connectAsync.await_count == 3
    # 最終試行の後はスリープしない（3回失敗 -> スリープは2回のみ）。
    assert mock_sleep.await_count == 2


# --- disconnect_async ---------------------------------------------------------


def test_disconnect_async_disconnects_when_connected(mock_ib_class) -> None:
    _, mock_instance = mock_ib_class
    mock_instance.isConnected.return_value = True
    connection = IBKRConnection()

    asyncio.run(connection.disconnect_async())

    mock_instance.disconnect.assert_called_once()


def test_disconnect_async_noop_when_not_connected(mock_ib_class) -> None:
    _, mock_instance = mock_ib_class
    mock_instance.isConnected.return_value = False
    connection = IBKRConnection()

    asyncio.run(connection.disconnect_async())

    mock_instance.disconnect.assert_not_called()


# --- _on_disconnected ----------------------------------------------------------


def test_on_disconnected_logs_warning(mock_ib_class, caplog) -> None:
    connection = IBKRConnection()

    with caplog.at_level("WARNING", logger="core.connection"):
        connection._on_disconnected()

    assert any("切断" in record.message for record in caplog.records)
