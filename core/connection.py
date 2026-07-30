"""TWSへの非同期接続管理モジュール。"""

import asyncio
import logging
import os
from typing import Optional

from dotenv import load_dotenv
from ib_insync import IB

load_dotenv()

logger = logging.getLogger(__name__)

# ib.reqMarketDataType()に渡すマーケットデータ種別。
# https://interactivebrokers.github.io/tws-api/market_data_type.html
MARKET_DATA_TYPE_LIVE: int = 1
MARKET_DATA_TYPE_FROZEN: int = 2
MARKET_DATA_TYPE_DELAYED: int = 3
MARKET_DATA_TYPE_DELAYED_FROZEN: int = 4


class IBKRConnection:
    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        client_id: Optional[int] = None,
        market_data_type: Optional[int] = None,
        max_retries: int = 10,
        base_delay_seconds: float = 1.0,
        max_delay_seconds: float = 60.0,
    ) -> None:
        self.host: str = host if host is not None else os.getenv("IBKR_HOST", "127.0.0.1")
        self.port: int = int(port if port is not None else os.getenv("IBKR_PORT", "7497"))
        self.client_id: int = int(client_id if client_id is not None else os.getenv("IBKR_CLIENT_ID", "1"))
        # ペーパー口座はリアルタイムデータの購読契約を持たないことが多く、
        # 未設定のままだとreqMktData/reqHistoricalDataが実データを返せず
        # 検証ができない。デフォルトは遅延データ(3)とし、購読契約がある
        # 場合のみ環境変数でLIVE(1)に切り替えられるようにする。
        self.market_data_type: int = int(
            market_data_type if market_data_type is not None
            else os.getenv("IBKR_MARKET_DATA_TYPE", str(MARKET_DATA_TYPE_DELAYED))
        )
        # リトライは「IB Gatewayの再起動が終わるまで待てる」長さにしてある。
        # Gatewayは Auto restart（推奨設定。パスワード再入力が不要）で1日1回
        # 再起動し、その間ソケットは接続不能になる。復帰まで数分かかるため、
        # 純粋な指数的バックオフだと待ち時間が短すぎる（5回・初期1秒だと
        # 合計15秒で使い切る）一方、上限を設けないと待ち時間が青天井に伸びて
        # 復帰の検知が遅れる。そこで1回あたりの待ちを max_delay_seconds で
        # 頭打ちにしつつ、回数で総待ち時間を稼ぐ。
        # 既定値(10回・初期1秒・上限60秒)の待ち時間は
        #     1+2+4+8+16+32+60+60+60 = 243秒（約4分）
        # で、通常のGateway再起動はこの中に収まる。
        self.max_retries: int = max_retries
        self.base_delay_seconds: float = base_delay_seconds
        self.max_delay_seconds: float = max_delay_seconds

        self.ib: IB = IB()
        self.ib.disconnectedEvent += self._on_disconnected

    async def connect_async(self) -> IB:
        attempt = 0
        last_error: Optional[Exception] = None

        while attempt < self.max_retries:
            attempt += 1
            try:
                logger.info(
                    "TWSへ接続を試みます: %s:%s (clientId=%s) [試行 %s/%s]",
                    self.host, self.port, self.client_id, attempt, self.max_retries,
                )
                await self.ib.connectAsync(self.host, self.port, clientId=self.client_id)
                logger.info("TWSへの接続に成功しました。")
                self.ib.reqMarketDataType(self.market_data_type)
                logger.info("マーケットデータタイプを設定しました: %s", self.market_data_type)
                return self.ib
            except Exception as exc:
                last_error = exc
                logger.warning("接続試行 %s に失敗しました: %s", attempt, exc)
                if attempt >= self.max_retries:
                    break
                delay = min(
                    self.base_delay_seconds * (2 ** (attempt - 1)), self.max_delay_seconds,
                )
                logger.info(
                    "%.1f秒後に再接続を試みます（指数的バックオフ、上限%.1f秒）。",
                    delay, self.max_delay_seconds,
                )
                await asyncio.sleep(delay)

        logger.error("最大リトライ回数(%s)に達したため、接続を諦めます。", self.max_retries)
        raise ConnectionError(f"TWSへの接続に失敗しました: {last_error}")

    def _on_disconnected(self) -> None:
        logger.warning("TWSとの接続が切断されました。")

    async def disconnect_async(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()
            logger.info("TWSから安全に切断しました。")
