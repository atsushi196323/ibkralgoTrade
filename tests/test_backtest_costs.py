"""backtest/costs.py の単体テスト。"""

import pytest

from backtest.costs import ZERO_COST, CostModel


# --- commission_for ---------------------------------------------------------------


def test_commission_scales_with_share_count() -> None:
    costs = CostModel(
        commission_per_share=0.0035, min_commission_per_order=0.35,
        max_commission_pct_of_notional=0.0,
    )

    assert costs.commission_for(1_000, 50.0) == pytest.approx(3.5)


def test_minimum_commission_dominates_for_small_lots() -> None:
    """10株程度の小ロットでは1株あたり料率より最低手数料が支配的になる。

    $1,220の口座では建玉が$244前後（株価$100なら2株）なので、往復コストは
    ほぼ最低手数料の2倍で決まる。
    """
    costs = CostModel(
        commission_per_share=0.0035, min_commission_per_order=0.35,
        max_commission_pct_of_notional=0.0,
    )

    # 10株 x 0.0035 = 0.035 だが、最低手数料 0.35 が適用される。
    assert costs.commission_for(10, 100.0) == pytest.approx(0.35)


def test_commission_is_capped_at_percentage_of_notional() -> None:
    """低位株では最低手数料より約定代金1%の上限が先に効く。"""
    costs = CostModel(
        commission_per_share=0.0035, min_commission_per_order=0.35,
        max_commission_pct_of_notional=1.0,
    )

    # 10株 x 2 USD = 20 USD の1% = 0.20 USD < 最低手数料 0.35 USD
    assert costs.commission_for(10, 2.0) == pytest.approx(0.20)


def test_no_cap_applied_when_max_pct_is_zero_or_negative() -> None:
    costs = CostModel(
        commission_per_share=0.0035, min_commission_per_order=0.35,
        max_commission_pct_of_notional=0.0,
    )

    assert costs.commission_for(10, 2.0) == pytest.approx(0.35)


@pytest.mark.parametrize("quantity,price", [(0, 100.0), (-5, 100.0), (10, 0.0), (10, -1.0)])
def test_commission_is_zero_for_invalid_inputs(quantity: int, price: float) -> None:
    assert CostModel().commission_for(quantity, price) == 0.0


# --- スリッページ -----------------------------------------------------------------


def test_buy_fill_is_higher_and_sell_fill_is_lower() -> None:
    costs = CostModel(slippage_pct=0.5)

    assert costs.buy_fill_price(100.0) == pytest.approx(100.5)
    assert costs.sell_fill_price(100.0) == pytest.approx(99.5)


# --- ZERO_COST --------------------------------------------------------------------


def test_zero_cost_model_charges_nothing() -> None:
    assert ZERO_COST.commission_for(1_000, 100.0) == 0.0
    assert ZERO_COST.buy_fill_price(100.0) == 100.0
    assert ZERO_COST.sell_fill_price(100.0) == 100.0


def test_default_cost_model_is_not_free() -> None:
    """既定値が誤ってゼロコストへ変更されていないことを固定する。"""
    costs = CostModel()

    assert costs.commission_for(10, 100.0) > 0
    assert costs.buy_fill_price(100.0) > 100.0
    assert costs.sell_fill_price(100.0) < 100.0


def test_the_default_minimum_commission_matches_the_measured_round_trip() -> None:
    """既定の最低手数料は、ペーパー口座で実測した1注文あたりの額と揃える。

    往復4件（2026-08-05〜08-24）の支払い手数料はいずれも $2.004 で、
    株数(2〜3株)によらず 1注文 $1.00 だった。IBKR Tiered の最低額($0.35)を
    既定に戻すと、小口座の検証だけが実運用より良く出る（$1,220 で PF 1.18 対 0.97）。
    """
    assert CostModel().min_commission_per_order == pytest.approx(1.00)

    # 2〜3株の注文では株数比例分ではなく最低額が効く（実測と同じ形）。
    assert CostModel().commission_for(3, 67.44) == pytest.approx(1.00)
