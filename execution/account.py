"""IBKRアカウントの資金状況取得。"""

import logging
from typing import Optional

from ib_async import IB

logger = logging.getLogger(__name__)

# 決済済み現金（受渡しが完了し、次の建玉に自由に使える現金）のタグ。
# NetLiquidationは未受渡しの売却代金も含んだ評価額なので、キャッシュ口座では
# 「いま実際に支払える額」と一致しない。受渡し(T+1)前の資金で発注するとIBKRに
# 拒否されるため、新規建ての資金判定にはこちらを使う。
SETTLED_CASH_TAG: str = "SettledCash"

# 本Botが売買する通貨。ポジションサイジングも株価も損切り幅もUSD建てなので、
# 資金の基準もUSDで揃える必要がある。
TRADING_CURRENCY: str = "USD"

# 口座資金のタグ。**NetLiquidationではなくNetLiquidationByCurrencyを使う。**
#
# 口座の基準通貨が円の場合、NetLiquidationは円建ての行しか持たない
# （実測: NetLiquidation=196059.62 JPY に対し
#   NetLiquidationByCurrency=1220.00 USD）。タグ名だけで最初の一致を返すと、
# 円の数値をドルとして扱い、資金を為替レート倍（約160倍）に見積もる。
# クランプ(MAX_POSITION_SIZE/MAX_ORDER_NOTIONAL_USD)が無ければ、
# 数量がそのまま2桁多い注文になる。
#
# 通貨別の内訳を持つByCurrency側から、USD建ての行を明示的に選ぶ。
# 割り算でレート換算しないのは、ExchangeRateの適用時点と評価額の時点が
# ずれて端数が合わないため（実測でも基準通貨額 ÷ ExchangeRate は
# USD建ての額と一致しない）。両替せずに使える額を直接読む方が確実である。
EQUITY_TAG: str = "NetLiquidationByCurrency"

# 通貨別の内訳を持たないタグ。基準通貨の行しか返さないため、
# **金額としては使わないが、その行の通貨が口座の基準通貨を示す**
# （実測: NetLiquidation の行は currency='JPY'）。
BASE_CURRENCY_PROBE_TAG: str = "NetLiquidation"

# 基準通貨に対する為替レートのタグ（実測: ExchangeRate=160.05, currency='USD'）。
EXCHANGE_RATE_TAG: str = "ExchangeRate"


async def get_account_equity_async(
    ib: IB,
    tag: str = EQUITY_TAG,
    currency: Optional[str] = TRADING_CURRENCY,
) -> float:
    """口座資金を返す。既定ではUSD建ての純資産。

    Args:
        currency: この通貨の行だけを対象にする。Noneなら通貨を問わない
            （タグが通貨別の内訳を持たない場合に使う）。

    Raises:
        ValueError: 該当する行が無い場合。**基準通貨の値へフォールバックしない。**
            通貨の取り違えは例外もログも出さずに数量だけが桁違いになるため、
            ここで落として気付けるようにする。
    """
    summary = await ib.accountSummaryAsync()
    for item in summary:
        if item.tag != tag:
            continue
        if currency is not None and getattr(item, "currency", None) != currency:
            continue

        equity = float(item.value)
        logger.info("口座資金(%s, %s)を取得しました: %.2f", tag, currency or "通貨指定なし", equity)
        return equity

    available = sorted({(i.tag, getattr(i, "currency", None)) for i in summary if i.tag == tag})
    raise ValueError(
        f"アカウントサマリーに {tag}({currency}) が見つかりませんでした。"
        f"同じタグで存在した通貨: {available}"
    )


async def get_usd_to_base_rate_async(ib: IB, base_currency: str = "JPY") -> Optional[float]:
    """口座サマリーが持つUSD→基準通貨の為替レートを返す。取得できなければNone。

    確定申告向けの円換算レート（`TradeJournal` の usd_jpy_rate）を、為替の
    マーケットデータ購読が無い口座でも埋めるための経路である。IDEALPROの
    USD.JPY はマーケットデータの追加購読が要り、この口座では
    `Error 10089` / `Error 162 No market data permissions for IDEALPRO CASH`
    で3経路とも失敗する（2026-08-06に実測。ジャーナルの usd_jpy_rate が
    空のまま記録されていた）。口座サマリーのレートは購読なしで読める。

    **基準通貨が期待と違えばNoneを返す。** 基準通貨がドルの口座では
    ExchangeRate は 1.0 前後になり、それを円換算に使うと損益が約160分の1で
    記録される。通貨の取り違えは例外もログも出さずに桁だけを狂わせるため、
    分からない場合は記録しない側へ倒す（`usd_jpy_rate` は未記録を許容し、
    円換算の集計から除外される）。

    NetLiquidationの円建て額をこのレートで割ってはならない理由は
    `EQUITY_TAG` のコメントを参照。ここでの用途はレートそのものである。
    """
    summary = await ib.accountSummaryAsync()

    actual_base = next(
        (
            getattr(item, "currency", None)
            for item in summary
            if item.tag == BASE_CURRENCY_PROBE_TAG
        ),
        None,
    )
    if actual_base != base_currency:
        logger.warning(
            "口座の基準通貨が %s ではないため為替レートを記録しません（%s の通貨: %s）。",
            base_currency, BASE_CURRENCY_PROBE_TAG, actual_base,
        )
        return None

    for item in summary:
        if item.tag != EXCHANGE_RATE_TAG or getattr(item, "currency", None) != TRADING_CURRENCY:
            continue
        try:
            rate = float(item.value)
        except (TypeError, ValueError):
            logger.warning(
                "%s の値が数値として解釈できませんでした: %r", EXCHANGE_RATE_TAG, item.value,
            )
            return None
        if rate <= 0:
            logger.warning("%s が正の値ではありません: %s", EXCHANGE_RATE_TAG, rate)
            return None
        logger.info(
            "口座サマリーから為替レート(%s/%s)を取得しました: %.4f",
            TRADING_CURRENCY, base_currency, rate,
        )
        return rate

    logger.warning(
        "アカウントサマリーに %s(%s) が見つかりませんでした。存在したタグ: %s",
        EXCHANGE_RATE_TAG, TRADING_CURRENCY, sorted({item.tag for item in summary}),
    )
    return None


async def get_settled_cash_async(ib: IB) -> Optional[float]:
    """決済済み現金を返す。取得できなければNoneを返す。

    例外ではなくNoneを返すのは、資金の裏付けをどう扱うかの判断を呼び出し側に委ねるため。
    「取得できなかった」と「0ドルしかない」は意味が違い、前者を0として
    扱うと資金があるのに永久に建てられなくなる。

    タグが見つからない場合は、サマリーに実在したタグ名をログに出す。
    口座種別によってタグの有無が変わりうるため、接続先で何が返っているかを
    ログだけで切り分けられるようにしておく（この関数が黙ってNoneを返すと、
    新規エントリーが止まった理由に気付けない）。
    """
    summary = await ib.accountSummaryAsync()
    for item in summary:
        if item.tag == SETTLED_CASH_TAG:
            try:
                settled_cash = float(item.value)
            except (TypeError, ValueError):
                logger.warning(
                    "%s の値が数値として解釈できませんでした: %r",
                    SETTLED_CASH_TAG, item.value,
                )
                return None
            logger.info("決済済み現金(%s)を取得しました: %.2f", SETTLED_CASH_TAG, settled_cash)
            return settled_cash

    logger.warning(
        "アカウントサマリーに %s が見つかりませんでした。存在したタグ: %s",
        SETTLED_CASH_TAG, sorted({item.tag for item in summary}),
    )
    return None
