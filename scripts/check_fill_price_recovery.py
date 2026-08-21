"""再接続後に約定価格を読めるかを、実データで確かめる（照会のみ）。

**発注も取り消しも行わない。** 読むだけである。

`_fill_price_of` は 2026-08-21 に、`orderStatus.avgFillPrice` が読めない場合は
`Fill` から計算し直すよう直した。直す前は、**ボットが止まっている間に約定した
待機注文を再起動後に読めず**、決済が `trade_journal.csv` に記録されないまま
建玉がローカルに残って同時保有枠(2)を占め続けた。

この修正は**通常の稼働では一度も通らない**。稼働中に約定した注文は
`orderStatus` が更新されるため `avgFillPrice` が埋まっており、フォールバックが
働く余地が無いからである。効いているかを確かめるには、**再起動直後と同じ
「まっさらな接続」から今日の約定を読み直す**しかない——それがこのスクリプトで、
`ib_async` の `connectAsync` が接続時に取り込む完了注文(`reqCompletedOrders`)と
約定(`reqExecutions`)を、ボットとまったく同じ関数で読む。

  * ib_async 2.1.0 の `Wrapper.completedOrder` は `OrderStatus(orderId, status)`
    しか組み立てないため、取り込んだ注文の `avgFillPrice` は 0.0 のままである
  * その後に走る `reqExecutions` は `trade.fills` を埋めるだけで `orderStatus`
    を更新しない。**約定価格はFill側にしか無い**

**Botを停止してから実行すること**（`.env` と同じクライアントIDで繋ぐため）。
引け後の締め(`scripts/after_close.sh`)がBotの停止後に自動で呼ぶ。

実行方法:
    python -m scripts.check_fill_price_recovery
    python -m scripts.check_fill_price_recovery --out logs/fill_price_recovery.jsonl
"""

import argparse
import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import List, Optional

from ib_async import IB

from core.connection import IBKRConnection
from execution.order_manager import PRICE_SOURCE_FILLS, _fill_price_with_source

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

DEFAULT_OUT_PATH = os.path.join("logs", "fill_price_recovery.jsonl")

_STATUS_FILLED = "Filled"


@dataclass
class FillReading:
    """約定した注文1件を、まっさらな接続から読んだ結果。"""

    symbol: str
    action: str
    order_type: str
    perm_id: int
    # ブローカーが返した avgFillPrice の生値。再接続で取り込んだ注文では 0.0。
    avg_fill_price: Optional[float]
    num_fills: int
    # `_fill_price_with_source` が返した値と経路。ボット本体と同じ関数を使う。
    price: Optional[float]
    source: str


def _read_fills(ib: IB) -> List[FillReading]:
    readings: List[FillReading] = []
    for trade in ib.trades():
        if getattr(trade.orderStatus, "status", None) != _STATUS_FILLED:
            continue
        order = trade.order
        price, source = _fill_price_with_source(trade)
        raw = getattr(trade.orderStatus, "avgFillPrice", None)
        readings.append(FillReading(
            symbol=getattr(getattr(trade, "contract", None), "symbol", ""),
            action=str(getattr(order, "action", "")),
            order_type=str(getattr(order, "orderType", "")),
            perm_id=int(getattr(order, "permId", 0) or 0),
            avg_fill_price=float(raw) if raw is not None else None,
            num_fills=len(getattr(trade, "fills", []) or []),
            price=price,
            source=source,
        ))
    return readings


def _verdict(readings: List[FillReading]) -> tuple:
    """(終了コード, 判定文) を返す。

    終了コードが1になるのは**約定価格をどうしても読めなかったとき**だけである。
    それは直した不具合がそのまま残っていることを意味する。「今日は約定が
    無かった」は判定材料が無いだけなので失敗にしない（毎晩自動で走るため、
    材料の無い日を失敗として積み上げると本当の失敗が埋もれる）。
    """
    if not readings:
        return 0, "今日の約定が1件もないため、判定材料がありません（建玉も決済も無かった日）。"

    unreadable = [r for r in readings if r.price is None]
    if unreadable:
        names = ", ".join(f"{r.symbol}/{r.order_type}" for r in unreadable)
        return 1, (
            f"**{len(unreadable)}件の約定価格が読めません（{names}）。**\n"
            "  再起動すると、この決済は記録されないまま建玉がローカルに残り、\n"
            "  同時保有枠を占め続けます。"
        )

    recovered = [r for r in readings if r.source == PRICE_SOURCE_FILLS]
    if recovered:
        names = ", ".join(f"{r.symbol}/{r.order_type}@{r.price:.2f}" for r in recovered)
        return 0, (
            f"**修正が効いています。** avgFillPrice が空の約定 {len(recovered)}件を "
            f"Fill から復元できました（{names}）。\n"
            "  これは再起動直後のボットが見るのと同じ状態です。"
        )
    return 0, (
        "今日の約定はすべて avgFillPrice から読めました。\n"
        "  この接続では再接続の状況（avgFillPriceが空）を再現できていないため、\n"
        "  修正が効くかどうかの判定にはなりません。"
    )


def _append_record(out_path: str, record: dict) -> None:
    """1行1件のJSONで追記する。**追記専用**（過去の記録を書き換えない）。"""
    directory = os.path.dirname(out_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(out_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


async def check_async(out_path: Optional[str]) -> int:
    connection = IBKRConnection()
    try:
        ib: IB = await connection.connect_async()
    except ConnectionError:
        print(
            f"\nIB Gatewayへ接続できません（{connection.host}:{connection.port}）。\n"
            "Botを停止し、Gatewayのログインが完了してから実行してください。"
        )
        return 1
    try:
        readings = _read_fills(ib)
    finally:
        await connection.disconnect_async()

    exit_code, verdict = _verdict(readings)

    print("\n===== 再接続後の約定価格の読み取り =====")
    print("（まっさらな接続から今日の約定を読む＝再起動直後のボットと同じ視点）\n")
    if not readings:
        print("今日の約定: なし")
    for reading in readings:
        raw = "空(0.0)" if not reading.avg_fill_price else f"{reading.avg_fill_price:.2f}"
        price = f"{reading.price:.2f}" if reading.price is not None else "**読めない**"
        print(
            f"  {reading.symbol} {reading.action} {reading.order_type}: "
            f"avgFillPrice={raw} / Fill {reading.num_fills}件 "
            f"-> 約定価格 {price}（経路 {reading.source}）"
        )
    print()
    print(verdict)

    if out_path:
        _append_record(out_path, {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "verdict": verdict.replace("\n", " ").replace("**", ""),
            "num_fills_read": len(readings),
            "num_recovered_from_fills": sum(
                1 for r in readings if r.source == PRICE_SOURCE_FILLS
            ),
            "num_unreadable": sum(1 for r in readings if r.price is None),
            "readings": [asdict(r) for r in readings],
        })
        print(f"\n記録しました: {out_path}")
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(
        description="再接続後に約定価格を読めるかを実データで確かめる（照会のみ）。",
    )
    parser.add_argument(
        "--out", default=DEFAULT_OUT_PATH,
        help=f"結果の追記先（既定 {DEFAULT_OUT_PATH}）。'' を渡すと記録しない。",
    )
    args = parser.parse_args()
    return asyncio.run(check_async(args.out or None))


if __name__ == "__main__":
    raise SystemExit(main())
