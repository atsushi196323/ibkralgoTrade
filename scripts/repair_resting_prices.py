"""既に建っている建玉の待機注文を、実約定価格を基準にした値段へ直す（単発の修復ツール）。

**なぜ単発の操作が要るのか。** 親の約定後の置き直しが `Error 10326`
（OCA group revision is not allowed）で拒否されると、待機注文は参照価格
（15分遅延）ベースの値段のまま板に残る。10326 は拒否しても元の注文を生かすため
生存確認では通り抜け、毎サイクルの突き合わせ(`main._adopt_broker_resting_prices`)
は**板の値を記録へ写すだけで、板を直しに行かない**（現に守られている建玉に対して
300秒ごとに無防備な窓を作らないため）。**したがって既に建っている建玉のずれは、
Botの通常経路では永久に直らない。** 直さないと、Bot側のポーリング判定（建値-5%）が
常に板の逆指値より先に効き、**ブラケット子注文の約定＝OCAの取消連動が観測できない**
（現フェーズの主目的がそれである）。

**このツールは10326を踏まない。** 10326の原因は、こちら側の `Order` が送信時の
OCAグループ名(`BRACKET_INTC_...`)を保持したまま修正を送り、IBKRがグループの変更と
解釈することにある。**前のセッションで発注した注文は、書き換え後の名前
（＝親のpermId）で読み直せる**（2026-08-18に UPS=1448732136 / INTC=470578601 と
して実測）。ここでは `reqAllOpenOrdersAsync()` が返した `Order` をそのまま使い、
値段のフィールドだけ差し替えて送るので、グループ名はIBKRが持っているものと一致する。

2026-08-18に UPS・INTC の4件へ実行し、4件とも板に反映されたことを読み直しで確認した
（INTC 損切り 95.44→97.29 / 利確 110.51→112.66、UPS 98.69→98.86 / 114.27→114.47）。

**既定はドライラン。** 実際に送るには `--apply` を付けること。

実行方法（IB Gatewayにログインできている時間帯に）:
    systemctl --user stop ibkralgotrade          # 先にBotを止める（下記）
    python -m scripts.repair_resting_prices      # 何をするか表示するだけ
    python -m scripts.repair_resting_prices --apply

**先にBotを停止すること。** 修正はBotと同じクライアントIDで繋ぐ必要があり
（理由は `--client-id` のコメント）、同じIDで同時に繋ぐと接続が弾かれる。
記録側(`positions.json`)は触らない——次のサイクルで
`main._adopt_broker_resting_prices` が板の値を正として書き戻し、R倍率の分母も
そこから取り直す。
"""

import argparse
import asyncio
import logging
from typing import Dict, List, Optional, Tuple

from ib_async import IB, Order, Trade

import main as bot
from core.connection import IBKRConnection
from execution.order_manager import round_to_tick
from execution.position_manager import DEFAULT_STATE_PATH, PositionManager

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# 修正が板に届いたかを読み直すまでの待ち時間。届かなかったこと（＝10326で
# 拒否されたこと）を「直った」として報告しないための検証であり、
# `_read_back_resting_prices_async` と同じ考え方。
READ_BACK_WAIT_SECONDS = 5.0

_LIVE_STATUSES = frozenset({"PreSubmitted", "Submitted"})


def _resting_exit_trades(trades: List[Trade], symbol: str) -> Dict[str, Trade]:
    """その銘柄の、生きている売りの待機注文を種類別に返す。

    OCAグループ名では突き合わせない（IBKRが親のpermIdへ書き換えるため）。
    銘柄と「売りのSTP/LMT」で絞る——`PositionManager` は1銘柄1建玉なので
    曖昧さは無く、新規建ての親(BUY)も混ざらない。
    """
    found: Dict[str, Trade] = {}
    for trade in trades:
        if getattr(trade.contract, "symbol", "") != symbol:
            continue
        order = trade.order
        if order.action != "SELL" or order.orderType not in ("STP", "LMT"):
            continue
        if trade.orderStatus.status not in _LIVE_STATUSES:
            continue
        found[order.orderType] = trade
    return found


def _intended_prices(entry_price: float) -> Tuple[float, float]:
    """実約定価格から、設計どおりの損切り・利確を出す（発注時と同じ丸め）。"""
    stop = round_to_tick(entry_price * (1 - bot.SWING_STOP_LOSS_PCT / 100))
    take_profit = round_to_tick(entry_price * (1 + bot.SWING_TAKE_PROFIT_PCT / 100))
    return stop, take_profit


def _current_price(order: Order) -> float:
    return float(order.auxPrice if order.orderType == "STP" else order.lmtPrice)


async def repair_async(state_path: str, apply: bool, client_id: Optional[int]) -> int:
    manager = PositionManager(state_path=state_path)
    positions = {symbol: manager.get_position(symbol) for symbol in manager.open_symbols()}
    if not positions:
        print("記録に建玉がありません。直すものはありません。")
        return 0

    connection = IBKRConnection()
    if client_id is not None:
        connection.client_id = client_id
    try:
        ib: IB = await connection.connect_async()
    except ConnectionError:
        print(
            f"\nIB Gatewayへ接続できません（{connection.host}:{connection.port}）。\n"
            "Gatewayにログインできている時間帯に実行してください。"
        )
        return 1

    try:
        trades = await ib.reqAllOpenOrdersAsync()
        planned: List[Tuple[str, Trade, float, float]] = []

        print(f"\n===== 待機注文の値段（記録: {manager.state_path}） =====")
        for symbol, position in sorted(positions.items()):
            intended_stop, intended_take_profit = _intended_prices(position.entry_price)
            resting = _resting_exit_trades(trades, symbol)
            print(f"\n[{symbol}] 実約定 {position.entry_price} × {position.quantity}株")
            if not resting:
                print("  板に生きている待機注文がありません（＝建玉が無防備）。")
                print("  この修復ツールでは直せません。Botの毎サイクルの突き合わせが置き直します。")
                continue
            for order_type, intended in (("STP", intended_stop), ("LMT", intended_take_profit)):
                trade = resting.get(order_type)
                label = "損切り" if order_type == "STP" else "利確"
                if trade is None:
                    print(f"  {label}: 板に無い（片側が無防備）。")
                    continue
                live = _current_price(trade.order)
                deviation = (live / position.entry_price - 1) * 100
                if abs(live - intended) < 1e-9:
                    print(f"  {label}: {live}（実約定比 {deviation:+.2f}%）… 設計どおり")
                    continue
                print(
                    f"  {label}: {live}（実約定比 {deviation:+.2f}%）"
                    f" → {intended} へ直す"
                )
                planned.append((symbol, trade, intended, live))

        if not planned:
            print("\n直すべきずれはありませんでした。")
            return 0

        if not apply:
            print(f"\nドライランです。実際に送るには --apply を付けてください（{len(planned)}件）。")
            return 0

        for symbol, trade, intended, _live in planned:
            order = trade.order
            # **ブローカーから読み直した Order をそのまま使う。** 特に ocaGroup は
            # IBKRが書き換えた名前（親のpermId）で入っており、こちらで作り直すと
            # グループの変更と解釈されて 10326 で拒否される。
            if order.orderType == "STP":
                order.auxPrice = intended
            else:
                order.lmtPrice = intended
            order.transmit = True
            ib.placeOrder(trade.contract, order)
            print(f"[{symbol}] {order.orderType} の修正を送信しました → {intended}")

        # **送ったことを直ったことと同一視しない。** 10326 は注文を拒否しても
        # 元の注文を生かすため、読み直さない限り拒否されたことに気付けない。
        await asyncio.sleep(READ_BACK_WAIT_SECONDS)
        after = await ib.reqAllOpenOrdersAsync()

        print("\n===== 読み直し =====")
        failures = 0
        for symbol, trade, intended, live in planned:
            order_type = trade.order.orderType
            resting = _resting_exit_trades(after, symbol).get(order_type)
            if resting is None:
                failures += 1
                print(f"[{symbol}] {order_type}: 読み直せませんでした（板から消えた可能性）。")
                continue
            now = _current_price(resting.order)
            if abs(now - intended) < 1e-9:
                print(f"[{symbol}] {order_type}: {live} → {now} … 板に反映されました")
            else:
                failures += 1
                print(
                    f"[{symbol}] {order_type}: 板は {now} のままです（要求 {intended}）。"
                    " 修正が拒否されました。上のログで 10326 等を確認してください。"
                )
        return 1 if failures else 0
    finally:
        await connection.disconnect_async()


def main_cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=DEFAULT_STATE_PATH, help="positions.json のパス")
    parser.add_argument("--apply", action="store_true", help="実際に修正を送る（既定はドライラン）")
    # **Botと同じクライアントIDで繋ぐこと。** 注文IDはクライアントごとの空間なので、
    # 別のIDから他人の注文IDを指定して修正を送ると、IBKRは新規注文の重複と見なして
    # `Error 103 Duplicate order id` で拒否する（2026-08-18に clientId=9 で実測。
    # 4件とも拒否され、板は元の値段のまま残った）。既定は .env の IBKR_CLIENT_ID。
    parser.add_argument(
        "--client-id", type=int, default=None,
        help="接続に使うクライアントID（既定は .env の IBKR_CLIENT_ID ＝ Botと同じ）。",
    )
    args = parser.parse_args()
    return asyncio.run(repair_async(args.state, args.apply, args.client_id))


if __name__ == "__main__":
    raise SystemExit(main_cli())
