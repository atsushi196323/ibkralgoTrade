"""記録している建玉と、ブローカーが実際に持っている建玉を突き合わせる（照会のみ）。

**発注も取り消しも行わない。** 読むだけである。

再起動・環境の移行をまたいだ直後は、`logs/positions.json` の内容が
ブローカー側の実体とずれうる。ずれる典型は次の2つで、どちらも
起動しただけでは気付けない。

    1. **記録にあるがブローカーに無い** … Botが停止していた間に待機注文が
       約定した場合。`is_confirmed_by_broker` がERRORを出して決済は見送るので
       危険な売り建てにはならないが、**その銘柄が監視枠を占め続ける**
       （`MAX_CONCURRENT_POSITIONS` は2しかない）
    2. **ブローカーにあるが記録に無い** … 状態ファイルを持ち込み忘れた場合。
       ブローカー同期が拾い直すものの、建値が**手数料込みの `avgCost`** に
       化け、損切り判定・R倍率・待機注文の置き直しの基準がすべてずれる

待機注文の生存も併せて見る。**片方だけ生きている建玉は守られていない**
（呼値違反で逆指値だけが不成立になり、利確だけが残った実例が2026-08-05にある）。

実行方法:
    python -m scripts.check_positions
"""

import argparse
import asyncio
import logging
from typing import Dict, List, Optional

from ib_async import IB

from core.connection import IBKRConnection
from execution.order_manager import RestingExitProtection, find_resting_exit_protection_async
from execution.position_manager import DEFAULT_STATE_PATH, PositionManager

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

TRACKED_SEC_TYPE = "STK"
TRACKED_CURRENCY = "USD"


def _broker_positions(raw: List[object]) -> Dict[str, float]:
    """米国株・USD建て・ロングの建玉だけを拾う。

    `reqPositionsAsync` は全口座・全アセットクラスを返すため、シンボル文字列
    だけで突き合わせるとオプションや他国上場の同名株を混同する
    （`PositionManager._is_tracked_position` と同じ絞り込み）。
    """
    positions: Dict[str, float] = {}
    for item in raw:
        contract = getattr(item, "contract", None)
        quantity = float(getattr(item, "position", 0) or 0)
        if contract is None or quantity <= 0:
            continue
        if getattr(contract, "secType", None) != TRACKED_SEC_TYPE:
            continue
        if getattr(contract, "currency", None) != TRACKED_CURRENCY:
            continue
        positions[getattr(contract, "symbol", "")] = quantity
    return positions


def _format_protection(protection: Optional[RestingExitProtection]) -> str:
    if protection is None:
        return "待機注文: **無し（建玉が無防備）**"
    if protection.has_filled_exit:
        return "待機注文: 約定済み（＝この建玉はもう閉じている）"
    types = sorted(protection.live_order_types)
    prices = []
    if protection.stop_price is not None:
        prices.append(f"損切り {protection.stop_price}")
    if protection.take_profit_price is not None:
        prices.append(f"利確 {protection.take_profit_price}")
    detail = "（板の値段: " + " / ".join(prices) + "）" if prices else ""
    if protection.is_complete:
        return f"待機注文: 損切り・利確とも生存 {detail}"
    return f"待機注文: **{'/'.join(types) or '無し'} のみ生存＝片側が無防備** {detail}"


async def check_async(state_path: str) -> int:
    # **状態ファイルのパスを必ず渡す。** 省略するとインメモリ動作になり
    # （単体テスト用の既定）、記録が常に空＝ブローカー側だけが見える。
    manager = PositionManager(state_path=state_path)
    recorded = {symbol: manager.get_position(symbol) for symbol in manager.open_symbols()}

    connection = IBKRConnection()
    try:
        ib: IB = await connection.connect_async()
    except ConnectionError:
        # 既定のリトライ（10回・約4分）を使い切ってからここへ来る。
        # 突き合わせは照会だけなので、繋がらないことを1行で伝えて終える方が
        # スタックトレースより読みやすい。
        print(
            f"\nIB Gatewayへ接続できません（{connection.host}:{connection.port}）。\n"
            "Gatewayを起動してログインが完了してから、もう一度実行してください。"
        )
        return 1
    try:
        broker = _broker_positions(await ib.reqPositionsAsync())
        protections = await find_resting_exit_protection_async(ib)
    finally:
        await connection.disconnect_async()

    print(f"\n===== 建玉の突き合わせ =====")
    print(f"記録({manager.state_path}): {sorted(recorded) or 'なし'}")
    print(f"ブローカー: {sorted(broker) or 'なし'}")
    print()

    problems = 0
    for symbol in sorted(set(recorded) | set(broker)):
        position = recorded.get(symbol)
        broker_qty = broker.get(symbol)

        if position is not None and broker_qty is None:
            problems += 1
            print(f"[NG] {symbol}: 記録にあるがブローカーに無い（記録 {position.quantity}株）。")
            print("      Botが停止していた間に決済された可能性があります。")
            print("      放置すると監視枠を占め続けるため、記録から消してから起動すること。")
            print(f"      {_format_protection(protections.get(symbol))}")
            continue

        if position is None:
            problems += 1
            print(f"[NG] {symbol}: ブローカーにあるが記録に無い（{broker_qty}株）。")
            print("      同期で拾い直せますが、建値が手数料込みのavgCostになり、")
            print("      損切り判定・R倍率・置き直しの基準がずれます。")
            print(f"      {_format_protection(protections.get(symbol))}")
            continue

        protection = protections.get(symbol)
        quantity_matches = float(position.quantity) == broker_qty
        protected = protection is not None and protection.is_complete and not protection.has_filled_exit
        status = "OK" if quantity_matches and protected else "要確認"
        if status != "OK":
            problems += 1
        print(f"[{status}] {symbol}: 記録 {position.quantity}株 / ブローカー {broker_qty:g}株"
              f"（建値 {position.entry_price} / 損切り {position.stop_price} / 利確 {position.take_profit_price}）")
        if not quantity_matches:
            print("      **数量が一致しません。** 部分約定の可能性があります（ブローカー側が正）。")
        print(f"      {_format_protection(protection)}")

    print()
    if problems:
        print(f"要対応 {problems}件。稼働させる前に解消すること。")
    else:
        print("記録とブローカーは一致しています。")
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="記録とブローカーの建玉を突き合わせる（照会のみ）。")
    parser.add_argument("--state", default=DEFAULT_STATE_PATH, help="状態ファイル")
    args = parser.parse_args()
    return asyncio.run(check_async(args.state))


if __name__ == "__main__":
    raise SystemExit(main())
