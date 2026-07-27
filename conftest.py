"""pytest共通設定。"""

import pytest

from data.market_data import get_historical_pacer


@pytest.fixture(autouse=True)
def _reset_historical_pacer():
    """ヒストリカルデータ用ペーサーの消費枠をテストごとにリセットする。

    ペーサーはモジュールレベルの共有インスタンスなので、リセットしないと
    テスト間で枠の消費が持ち越され、実行順によっては実時間のsleepが発生する。
    """
    get_historical_pacer().reset()
    yield
    get_historical_pacer().reset()
