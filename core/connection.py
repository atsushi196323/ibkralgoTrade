"""TWSへの非同期接続管理モジュール。"""

import asyncio
import logging
import os
from typing import Optional

from dotenv import load_dotenv
from ib_insync import IB

load_dotenv()

logger = logging.getLogger(__name__)


class IBKRConnection:
    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        client_id: Optional[int] = None,
        max_retries: int = 5,
        base_delay_seconds: float = 1.0,
    ) -> None:
        self.host: str = host if host is not None else os.getenv("IBKR_HOST", "127.0.0.1")
        self.port: int = int(port if port is not None else os.getenv("IBKR_PORT", "7497"))
        self.client_id: int = int(client_id if client_id is not None else os.getenv("IBKR_CLIENT_ID", "1"))
        self.max_retries: int = max_retries
        self.base_delay_seconds: float = base_delay_seconds

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
                return self.ib
            except Exception as exc:
                last_error = exc
                logger.warning("接続試行 %s に失敗しました: %s", attempt, exc)
                if attempt >= self.max_retries:
                    break
                delay = self.base_delay_seconds * (2 ** (attempt - 1))
                logger.info("%.1f秒後に再接続を試みます（指数的バックオフ）。", delay)
                await asyncio.sleep(delay)

        logger.error("最大リトライ回数(%s)に達したため、接続を諦めます。", self.max_retries)
        raise ConnectionError(f"TWSへの接続に失敗しました: {last_error}")

    def _on_disconnected(self) -> None:
        logger.warning("TWSとの接続が切断されました。")

    async def disconnect_async(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()
            logger.info("TWSから安全に切断しました。")
