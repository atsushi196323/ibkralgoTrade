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

    **`main` 側も固定する。** `main` はこのフラグを import 時に値で束ねるため、
    order_manager だけ固定しても `main.ENABLE_REAL_ORDERS` は実際の設定値
    （現在True）のまま残る。そうなると同じテストの結果が実行順で変わる:
    `tests/test_logging_setup.py` が `importlib.reload(main)` を行うと、その時点で
    main はドライラン側の値を焼き込み、以降のテストだけ挙動が変わっていた
    （実際に `pytest tests/test_main.py -k trailing_stop_cancels` 単体では落ち、
    全体実行では通る状態になっていた）。実発注側を検証するテストが、順序次第で
    黙ってドライラン側を通ることになるため、ここで両方を固定する。
    """
    monkeypatch.setattr("execution.order_manager.ENABLE_REAL_ORDERS", False)
    monkeypatch.setattr("main.ENABLE_REAL_ORDERS", False)


@pytest.fixture(autouse=True)
def _reset_historical_pacer():
    """ヒストリカルデータ用ペーサーの消費枠をテストごとにリセットする。

    ペーサーはモジュールレベルの共有インスタンスなので、リセットしないと
    テスト間で枠の消費が持ち越され、実行順によっては実時間のsleepが発生する。
    """
    get_historical_pacer().reset()
    yield
    get_historical_pacer().reset()


@pytest.fixture(autouse=True)
def _no_concentrated_symbol_by_default(monkeypatch):
    """テストは既定で通常のウォッチリスト運用として走らせる。

    `CONCENTRATED_SYMBOL` は運用の設定であって、テストが検証したい対象では
    ない。既定値に乗せたままにすると、銘柄を1つ指定した瞬間に
    `_refresh_watchlist_async` が集中モードへ短絡し、スクリーニング・
    フォールバック・株価帯・グロース枠を検証しているテストが一斉に落ちる
    （2026-08-25にMRNAを設定した際、実際に13件が落ちた）。

    ここで固定して、**どちらのモードを検証するかをテスト側が明示する**形に
    揃える。集中モードの経路は
    `patch("main.CONCENTRATED_SYMBOL", "…")` で個別に上書きすること。
    """
    monkeypatch.setattr("main.CONCENTRATED_SYMBOL", None)
