"""バックテストの取引コスト（手数料・スリッページ）モデル。

コストを含めないバックテストは、特にデイトレード側（利幅3%・損切り1.5%）で
実運用と乖離した楽観的な数字を出す。エッジの有無を判断する材料にするため、
既定値は実際に発注する口座の条件（IBKR Tiered・米国株）に合わせている。

既定値の根拠（IBKR Tiered, US stocks, 2026年時点の一般的な料率）:
  - 1株あたり 0.0035 USD
  - 1注文あたり最低 0.35 USD  … 小ロットではこちらが支配的になる
  - 約定代金の1%が上限        … 低位株（例: 10株 x 2 USD = 20 USD）では
                                 最低手数料 0.35 USD より 1%上限 0.20 USD が効く

スリッページの既定値 0.05%（片道5bp）は、成行注文でスプレッドの半分＋αを
払う想定。本Botはプルバック（急落中の銘柄）を成行で買うため、
平常時のスプレッドよりは広がる前提でやや保守的に置いている。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    """1約定あたりの手数料とスリッページ。

    すべて片道（1注文）あたりの値であり、往復では2回適用される。
    """

    commission_per_share: float = 0.0035
    # 実測値。ペーパー口座の往復4件（2026-08-05〜08-24、いずれも2〜3株）で
    # 支払い手数料が往復 $2.004 = 1注文 $1.00 だった。IBKR Tiered の最低額
    # ($0.35) を既定にしていた間、小口座の成績はこの差だけ楽観に出ていた
    # （42銘柄・10年・$1,220 で PF 1.18 -> 0.97）。
    min_commission_per_order: float = 1.00
    # 約定代金に対する手数料の上限（%）。0以下なら上限なしとして扱う。
    max_commission_pct_of_notional: float = 1.0
    # 約定価格が不利な方向へずれる割合（%）。買いは高く、売りは安く約定する。
    slippage_pct: float = 0.05

    def commission_for(self, quantity: int, fill_price: float) -> float:
        """1注文分の手数料を返す。"""
        if quantity <= 0 or fill_price <= 0:
            return 0.0

        commission = max(
            quantity * self.commission_per_share, self.min_commission_per_order
        )

        if self.max_commission_pct_of_notional > 0:
            notional = quantity * fill_price
            cap = notional * self.max_commission_pct_of_notional / 100.0
            commission = min(commission, cap)

        return commission

    def buy_fill_price(self, price: float) -> float:
        """買い注文の約定価格（スリッページ分だけ不利＝高く約定する）。"""
        return price * (1.0 + self.slippage_pct / 100.0)

    def sell_fill_price(self, price: float) -> float:
        """売り注文の約定価格（スリッページ分だけ不利＝安く約定する）。"""
        return price * (1.0 - self.slippage_pct / 100.0)


# コストを完全に無視する設定。シグナル判定そのものを検証する単体テストや、
# 「コストがどれだけ成績を削っているか」を比較する用途にのみ使うこと。
# 収益性の判断に使ってはならない。
ZERO_COST = CostModel(
    commission_per_share=0.0,
    min_commission_per_order=0.0,
    max_commission_pct_of_notional=0.0,
    slippage_pct=0.0,
)
