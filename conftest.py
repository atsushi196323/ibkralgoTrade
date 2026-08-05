"""pytest共通設定。"""

import pytest

from data.market_data import get_historical_pacer


@pytest.fixture(autouse=True)
def _dry_run_orders_by_default(monkeypatch):
    """テストは既定でドライランとして走らせる。

    `ENABLE_REAL_ORDERS` は運用の設定であって、テストが検証したい対象では
    ない。既定値に乗せたままにすると、フラグを立てた瞬間に「ドライランの
    挙動を確かめるテスト」が本物の発注経路へ入って一斉に落ちる（2026-08-05に
    ペーパー実発注を有効化した際、実際に40件が落ちた）。ここで固定して、
    **どちらのモードを検証するかをテスト側が明示する**形に揃える。

    実発注の経路を見るテストは `patch("execution.order_manager.ENABLE_REAL_ORDERS", True)`
    で個別に上書きすること（`tests/test_order_manager.py` がそうしている）。
    """
    monkeypatch.setattr("execution.order_manager.ENABLE_REAL_ORDERS", False)


@pytest.fixture(autouse=True)
def _reset_historical_pacer():
    """ヒストリカルデータ用ペーサーの消費枠をテストごとにリセットする。

    ペーサーはモジュールレベルの共有インスタンスなので、リセットしないと
    テスト間で枠の消費が持ち越され、実行順によっては実時間のsleepが発生する。
    """
    get_historical_pacer().reset()
    yield
    get_historical_pacer().reset()
