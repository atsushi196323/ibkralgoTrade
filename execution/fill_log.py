"""約定を「想定した値段」と「実際に約定した値段」の乖離込みで記録する。

`trade_journal.csv` は**決済単位**の損益記録であり、建てたときに参照した価格が
実約定とどれだけずれていたかは残らない。ここが空白だと、注文層の不具合が
損益の分散に埋もれて見えなくなる。

**このずれは既に実害を出している。** 遅延データ(`IBKR_MARKET_DATA_TYPE=3`)の
参照価格は15分古く、2026-08-05のAMBQでは参照66.50に対し実約定67.44(+1.4%)
だった。待機注文はその参照価格から -5%/+10% として置かれたため、実際の建値から
見ると **-6.3%/+8.5%** の位置に並んだ。損切りが遠い側へずれるので1トレードの
リスクが設計値(1%)を超え、さらにBot側のポーリング判定(建値-5%)の方が先に
発動するようになる。結果として **2026-08-05〜08-18の14日間、ブラケット子注文の
約定が一度も観測できず**、現フェーズの主目的であるOCAの取消連動の検証が
止まっていた。**乖離を記録していれば初日に気付けた性質の故障である。**

### なぜ `trade_journal.csv` に列を足さないのか

あちらは追記専用で、控え(`scripts/backup_records.py`)が縮小を検知し、確定申告用
CSV(`execution/tax_export.py`)が全行を無条件に読む。粒度も違う——こちらは
**約定単位**（1往復で2行以上）である。別ファイルへ分けて、あちらの追記専用性と
列構成を触らない。

### なぜ決済ロジックへ渡さないのか

**これは観測専用であり、売買の判断材料にしてはならない。** 記録が読めない・
壊れている・欠けていることが、発注や決済の挙動を変えてはならないので、
書き込みの失敗は握り潰してログに残すだけにする（`FillLog.record_*`）。
`TradeJournal` を各処理へ引数で渡しているのは、あちらが日次損益の判定に
使われる**入力**だからであり、こちらは出力しかない。
"""

import json
import logging
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.market_hours import US_EASTERN

logger = logging.getLogger(__name__)

DEFAULT_FILL_LOG_PATH: str = "logs/fills.jsonl"

EVENT_ENTRY: str = "entry"
EVENT_EXIT: str = "exit"

ACTION_BUY: str = "BUY"
ACTION_SELL: str = "SELL"


@dataclass(frozen=True)
class FillRecord:
    """1回の約定と、その値段が想定からどれだけずれたか。"""

    recorded_at: str
    # 取引日(米国東部時間)。クールダウン・日次サーキットブレーカー・控えの
    # 日付と同じ区切りにする。日本時間で採るとザラ場の途中で日付が変わる。
    trading_day: str
    symbol: str
    event: str
    action: str
    order_type: str
    quantity: int
    # 発注時に「この値段で約定するはず」と見込んでいた価格。
    # 新規建ては参照価格（＝発注判断に使った現在値）、決済は待機注文の値段か、
    # 成行決済の判断に使った観測価格。
    intended_price: float
    # 実際の約定価格。ドライラン中と、約定価格が読めなかった場合はNone。
    fill_price: Optional[float]
    # (実約定 - 想定) / 想定 × 100。符号はそのまま（高く約定したら正）。
    deviation_pct: Optional[float]
    # 乖離のうち**不利な側**を金額にしたもの（正が不利）。買いは高く買った分、
    # 売りは安く売った分。符号を揃えてあるので、そのまま合計すると
    # 「乖離がいくら取り分を削ったか」になる。
    adverse_usd: Optional[float]
    commission: float = 0.0
    dry_run: bool = True
    # 約定価格の取得経路（`order_manager.PRICE_SOURCE_*`）。
    price_source: Optional[str] = None
    # 想定価格の取得経路と鮮度（`data.market_data.PriceQuote`）。**乖離の原因が
    # 遅延データなのかスリッページなのかは、これが無いと切り分けられない。**
    quote_source: Optional[str] = None
    is_stale: Optional[bool] = None
    # --- 新規建てのみ。実際にブローカーへ置かれた待機注文の位置 ---------------
    stop_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    # 実約定から見た待機注文の位置(%)。設計値(designed_*)と比べるための値で、
    # 上のAMBQなら effective_stop_pct=-6.3 に対し designed_stop_pct=-5.0 になる。
    effective_stop_pct: Optional[float] = None
    effective_take_profit_pct: Optional[float] = None
    designed_stop_pct: Optional[float] = None
    designed_take_profit_pct: Optional[float] = None
    # 逆指値までの距離 × 数量 が口座資金に占める割合。**1トレードのリスクが
    # 設計値(RISK_PER_TRADE_PCT = 1%)を超えていないかを、実際に置かれた注文から
    # 見る唯一の値である**（2026-08-10のUPSは1.16%だった）。
    risk_pct_of_equity: Optional[float] = None


def deviation_pct(intended_price: float, fill_price: Optional[float]) -> Optional[float]:
    """想定価格からの乖離率(%)。高く約定したら正。"""
    if fill_price is None or intended_price is None or intended_price <= 0:
        return None
    return (fill_price - intended_price) / intended_price * 100.0


def adverse_usd(
    action: str, intended_price: float, fill_price: Optional[float], quantity: int,
) -> Optional[float]:
    """乖離の不利な側を金額にする（正が不利）。

    買いと売りで不利の向きが逆なので、ここで符号を揃える。揃えずに記録すると、
    往復を合計したときに買いの不利と売りの不利が打ち消し合い、**乖離が無かった
    ように見える**。
    """
    if fill_price is None or quantity <= 0:
        return None
    if intended_price is None or intended_price <= 0:
        # 想定価格が分からない（ブローカー同期で取り込んだ建玉は待機注文の
        # 値段を持たない）。ここで0を基準に差を取ると、約定代金そのものを
        # 「乖離による損」として合計してしまう。
        return None
    difference = fill_price - intended_price
    if action == ACTION_SELL:
        difference = -difference
    return difference * quantity


def _percent_from(basis: Optional[float], price: Optional[float]) -> Optional[float]:
    if basis is None or price is None or basis <= 0 or price <= 0:
        return None
    return (price / basis - 1.0) * 100.0


def _positive(value: Optional[float]) -> Optional[float]:
    """NaN・0以下・数値でないものを弾く（「6.4」）。"""
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or value <= 0:
        return None
    return value


class FillLog:
    """約定記録の追記先（JSON Lines）。

    JSONLにしているのは、列が後から増えても既存の行を読めるまま保てるため。
    CSVだと列を足した時点で過去の行と食い違い、`trade_journal.csv` のように
    読み込み側にフォールバックを積むことになる。
    """

    def __init__(self, file_path: str = DEFAULT_FILL_LOG_PATH) -> None:
        self.file_path = file_path

    def record_entry(
        self,
        symbol: str,
        quantity: int,
        intended_price: float,
        fill_price: Optional[float],
        stop_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        designed_stop_pct: Optional[float] = None,
        designed_take_profit_pct: Optional[float] = None,
        account_equity: Optional[float] = None,
        commission: float = 0.0,
        dry_run: bool = True,
        price_source: Optional[str] = None,
        quote_source: Optional[str] = None,
        is_stale: Optional[bool] = None,
        now: Optional[datetime] = None,
    ) -> Optional[FillRecord]:
        """新規建ての約定と、その結果置かれた待機注文の位置を記録する。

        待機注文の位置は**実約定価格を基準に**測る。参照価格を基準にすると、
        まさに検出したいずれ（参照価格が実勢とずれていること）が定義上ゼロに
        なって消える。
        """
        basis = _positive(fill_price) or _positive(intended_price)
        stop_price = _positive(stop_price)
        take_profit_price = _positive(take_profit_price)

        risk_pct = None
        equity = _positive(account_equity)
        if basis is not None and stop_price is not None and equity is not None and quantity > 0:
            risk_pct = (basis - stop_price) * quantity / equity * 100.0

        return self._append(FillRecord(
            recorded_at=_now_iso(now),
            trading_day=_trading_day(now),
            symbol=symbol,
            event=EVENT_ENTRY,
            action=ACTION_BUY,
            order_type="MKT",
            quantity=quantity,
            intended_price=intended_price,
            fill_price=fill_price,
            deviation_pct=deviation_pct(intended_price, fill_price),
            adverse_usd=adverse_usd(ACTION_BUY, intended_price, fill_price, quantity),
            commission=commission,
            dry_run=dry_run,
            price_source=price_source,
            quote_source=quote_source,
            is_stale=is_stale,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            effective_stop_pct=_percent_from(basis, stop_price),
            effective_take_profit_pct=_percent_from(basis, take_profit_price),
            designed_stop_pct=-designed_stop_pct if designed_stop_pct is not None else None,
            designed_take_profit_pct=designed_take_profit_pct,
            risk_pct_of_equity=risk_pct,
        ))

    def record_exit(
        self,
        symbol: str,
        quantity: int,
        order_type: str,
        intended_price: float,
        fill_price: Optional[float],
        commission: float = 0.0,
        dry_run: bool = True,
        price_source: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Optional[FillRecord]:
        """決済の約定を記録する。

        `intended_price` は、待機注文の約定なら**置いてあった注文の値段**、
        Bot側の成行決済なら**判断に使った観測価格**を渡すこと。前者の乖離は
        逆指値がトリガー後に成行へ変わることによるもの（＝スリッページ）、
        後者は300秒のポーリング間隔と成行の両方を含むので、性質が違う。
        どちらも `order_type` で区別できる。
        """
        return self._append(FillRecord(
            recorded_at=_now_iso(now),
            trading_day=_trading_day(now),
            symbol=symbol,
            event=EVENT_EXIT,
            action=ACTION_SELL,
            order_type=order_type,
            quantity=quantity,
            intended_price=intended_price,
            fill_price=fill_price,
            deviation_pct=deviation_pct(intended_price, fill_price),
            adverse_usd=adverse_usd(ACTION_SELL, intended_price, fill_price, quantity),
            commission=commission,
            dry_run=dry_run,
            price_source=price_source,
        ))

    def _append(self, record: FillRecord) -> Optional[FillRecord]:
        """1行追記する。**失敗しても例外を上げない。**

        これは観測のための記録であり、書けなかったことで発注・決済の流れを
        止めてはならない（止めると、観測を足したせいで建玉が無防備になる、
        という本末転倒が起きる）。書けなかったこと自体はERRORで残る。
        """
        try:
            directory = os.path.dirname(self.file_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(self.file_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
        except Exception:
            logger.exception(
                "[%s] 約定記録の書き込みに失敗しました（売買は続行します）: %s",
                record.symbol, self.file_path,
            )
            return None

        logger.info(
            "[%s] 約定を記録しました: event=%s type=%s qty=%s 想定=%.2f 実約定=%s "
            "乖離=%s 不利=%s",
            record.symbol, record.event, record.order_type, record.quantity,
            record.intended_price,
            f"{record.fill_price:.2f}" if record.fill_price is not None else "なし",
            f"{record.deviation_pct:+.2f}%" if record.deviation_pct is not None else "N/A",
            f"{record.adverse_usd:+.2f} USD" if record.adverse_usd is not None else "N/A",
        )
        return record

    def load(self) -> List[FillRecord]:
        """記録を読み戻す。壊れた行は読み飛ばす。

        **1行が壊れていても残りを読めることがJSONLを選んだ理由である。**
        書き込み中に停止した場合、壊れうるのは最後の1行だけで済む。
        """
        if not os.path.exists(self.file_path):
            return []

        records: List[FillRecord] = []
        with open(self.file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload: Dict[str, Any] = json.loads(line)
                    records.append(_record_from(payload))
                except (ValueError, TypeError):
                    # 必須の列が欠けた行も同じ扱いにする。読めない行の存在を
                    # 理由に全体を捨てると、残りの正しい記録まで失う。
                    logger.warning("約定記録に読めない行があります: %s", line[:120])
        return records


def _record_from(payload: Dict[str, Any]) -> FillRecord:
    """辞書からレコードを組み立てる。

    列が後から増えても古い行を読めるよう、既知のフィールドだけを拾い、
    未知のキーは捨てる（`FillRecord` の既定値が埋める）。
    """
    known = {f: payload[f] for f in FillRecord.__dataclass_fields__ if f in payload}
    return FillRecord(**known)


def _now_iso(now: Optional[datetime]) -> str:
    return (now or datetime.now(timezone.utc)).isoformat()


def _trading_day(now: Optional[datetime]) -> str:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(US_EASTERN).date().isoformat()
