"""1取引日の稼働を1画面へ要約する（IBKR接続不要）。

`logs/bot.log` と `logs/trade_journal.csv` を読み、その日の
**「何が起きたか」ではなく「なぜそうなったか」** を出す。本プロジェクトの
主要な故障モードは例外を出さずに静かに縮退することなので（CLAUDE.md「3.」）、
知りたいのはほぼ常に「なぜ1件も建たなかったのか」である。その答えは
稼働ログに1行だけ埋もれているのが普通で、2026-08-04の bot.log では
1560行のうち再接続の再試行が大半を占め、答えである
`時価総額スキャンの結果が0件でした` は1行しか無かった。

**取引日は米国東部時間で区切る。** ログのタイムスタンプはプロセスの
ローカル時刻（日本時間）で記録されるため、1取引日は日本時間の2つの
カレンダー日にまたがる（22:30〜翌05:00）。ローカル日付で切ると、
寄り付きと引けが別の日の集計に入る。

実行方法:
    python -m scripts.daily_report                  # 直近の取引日
    python -m scripts.daily_report --date 2026-08-04
    python -m scripts.daily_report --log logs/bot.log --journal logs/trade_journal.csv
"""

import argparse
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, tzinfo
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

from core.market_hours import US_EASTERN
from execution.trade_journal import DEFAULT_JOURNAL_PATH, TradeJournal, TradeRecord

DEFAULT_LOG_PATH: str = "logs/bot.log"

# `core.logging_setup.LOG_FORMAT` が出す1行。この形に合わない行（例外の
# スタックトレース等）は本文の続きなので、直前の行の一部として無視する。
_LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} "
    r"\[(?P<level>[A-Z]+)\] (?P<name>[^:]+): (?P<message>.*)$"
)

_SIGNAL_RE = re.compile(
    r"^\[(?P<symbol>[^\]]+)\] 終値=.* 乖離率=(?P<deviation>-?[\d.]+)% シグナル=(?P<signal>\S+)"
)
_ENTRY_RE = re.compile(r"^\[(?P<symbol>[^\]]+)\] ポジションを新規建てしました: entry=(?P<price>[\d.]+)")
_EXIT_RE = re.compile(r"^\[(?P<symbol>[^\]]+)\] ポジションを決済しました")
_PRICE_BAND_RE = re.compile(
    r"^\[(?P<symbol>[^\]]+)\] 株価\((?P<price>[\d.]+) USD\)が(?P<bound>下限|上限)"
)
_EQUITY_RE = re.compile(r"口座資金.*USD\)?を取得しました: (?P<equity>[\d.]+)")

# 新規エントリーが見送られた理由。ログの原文をそのまま照合すると
# 書式変更で静かに数え漏れるため、判別に足りる最小の部分文字列だけを持つ。
_SKIP_REASONS: List[Tuple[str, str]] = [
    ("同時保有ポジション数の上限", "同時保有数の上限"),
    ("本日の新規建て回数", "1日の建て回数の上限"),
    ("本日すでに決済済み", "当日中の再エントリー禁止"),
    ("リスクベースの計算数量が0", "数量0（株価が資金に対して高い）"),
    ("サーキットブレーカー", "日次サーキットブレーカー"),
    ("鮮度", "価格が古い（新規建てのみ見送り）"),
    ("現在価格が取得できなかったため発注", "現在価格が取れない"),
    ("ヒストリカルデータが取得できなかった", "日足が取れない"),
    ("本数が揃うまでエントリーできません", "日足の本数不足（上場から日が浅い）"),
    ("決済済み現金", "決済済み現金の裏付け不足"),
]


@dataclass
class LogLine:
    """`bot.log` の1行を、取引日の判定まで済ませた形で持つ。"""

    trading_day: date
    level: str
    name: str
    message: str


@dataclass
class DayReport:
    trading_day: date
    account_equity: Optional[float] = None
    # 銘柄ごとのシグナル判定の回数。ログにサイクルの開始を示す行が無いため、
    # 「いちばん多く判定された銘柄の回数」を評価サイクル数の代用にする。
    signal_evaluations: Counter = field(default_factory=Counter)
    cycles_skipped_closed: int = 0
    entries: List[Tuple[str, float]] = field(default_factory=list)
    exits: List[str] = field(default_factory=list)
    # 銘柄ごとの「その日いちばん押した乖離率」。エントリーが0件だった日に
    # 「あと何%で閾値だったか」を見るための材料。
    lowest_deviation: Dict[str, float] = field(default_factory=dict)
    excluded_symbols: Dict[str, str] = field(default_factory=dict)
    skip_reasons: Counter = field(default_factory=Counter)
    screening_degraded: List[str] = field(default_factory=list)
    connection_failure_rounds: int = 0
    manual_login_hint: bool = False
    warnings: Counter = field(default_factory=Counter)
    errors: Counter = field(default_factory=Counter)
    trades: List[TradeRecord] = field(default_factory=list)


def parse_log_lines(
    lines: Iterable[str], local_tz: Optional[tzinfo] = None,
) -> Iterator[LogLine]:
    """ログ行を取引日(ET)付きで返す。書式に合わない行は読み飛ばす。

    `local_tz` を省略すると、タイムゾーンを持たないタイムスタンプは
    実行中のマシンのローカル時刻として解釈される（ログを書いたプロセスと
    同じマシンで読む通常の使い方）。テストと、別のタイムゾーンで書かれた
    ログを読む場合のために明示できるようにしてある。
    """
    for raw in lines:
        match = _LOG_LINE_RE.match(raw.rstrip("\n"))
        if match is None:
            continue

        local_dt = datetime.strptime(match.group("ts"), "%Y-%m-%d %H:%M:%S")
        if local_tz is not None:
            local_dt = local_dt.replace(tzinfo=local_tz)
        yield LogLine(
            trading_day=local_dt.astimezone(US_EASTERN).date(),
            level=match.group("level"),
            name=match.group("name"),
            message=match.group("message"),
        )


def latest_trading_day(log_lines: Iterable[LogLine]) -> Optional[date]:
    days = {line.trading_day for line in log_lines}
    return max(days) if days else None


def build_day_report(
    log_lines: Iterable[LogLine], trades: Iterable[TradeRecord], trading_day: date,
) -> DayReport:
    report = DayReport(trading_day=trading_day)

    for line in log_lines:
        if line.trading_day != trading_day:
            continue

        message = line.message
        if line.level == "WARNING":
            report.warnings[f"{line.name}: {_message_head(message)}"] += 1
        elif line.level == "ERROR":
            report.errors[f"{line.name}: {_message_head(message)}"] += 1

        signal = _SIGNAL_RE.match(message)
        if signal is not None:
            symbol = signal.group("symbol")
            deviation = float(signal.group("deviation"))
            report.signal_evaluations[symbol] += 1
            previous = report.lowest_deviation.get(symbol)
            if previous is None or deviation < previous:
                report.lowest_deviation[symbol] = deviation
            continue

        entry = _ENTRY_RE.match(message)
        if entry is not None:
            report.entries.append((entry.group("symbol"), float(entry.group("price"))))
            continue

        exit_match = _EXIT_RE.match(message)
        if exit_match is not None:
            report.exits.append(exit_match.group("symbol"))
            continue

        band = _PRICE_BAND_RE.match(message)
        if band is not None:
            report.excluded_symbols[band.group("symbol")] = (
                f"{band.group('bound')}外 (株価 {band.group('price')} USD)"
            )
            continue

        equity = _EQUITY_RE.search(message)
        if equity is not None:
            report.account_equity = float(equity.group("equity"))
            continue

        if "市場時間外のため" in message:
            report.cycles_skipped_closed += 1
            continue
        if "TWSへの再接続に失敗しました" in message:
            report.connection_failure_rounds += 1
        if "再ログインが必要な可能性があります" in message:
            report.manual_login_hint = True
        if "スキャンの結果が0件" in message or "スクリーニング結果が0件" in message:
            report.screening_degraded.append(message)

        for needle, label in _SKIP_REASONS:
            if needle in message:
                report.skip_reasons[label] += 1
                break

    report.trades = [
        trade for trade in trades
        if _closed_trading_day(trade) == trading_day
    ]
    return report


def _closed_trading_day(trade: TradeRecord) -> Optional[date]:
    try:
        return datetime.fromisoformat(trade.closed_at).astimezone(US_EASTERN).date()
    except ValueError:
        return None


def _message_head(message: str, limit: int = 60) -> str:
    """WARNING/ERRORを種類別に数えるための見出し。

    銘柄名・価格・秒数といった可変部分を含んだまま数えると、同じ種類の
    警告が全部バラバラの項目になって集計にならない。角括弧の銘柄名を
    落としたうえで先頭だけを見出しにする。
    """
    head = re.sub(r"^\[[^\]]+\] ", "", message)
    head = re.sub(r"[\d.]+", "N", head)
    return head[:limit]


def _counted_in_order(messages: Iterable[str]) -> List[Tuple[str, int]]:
    """重複を畳んで「メッセージ, 出現回数」を初出順で返す。

    初出順にするのは、縮退が起きた順序（何が先に落ちたか）が切り分けの
    材料になるため。回数の多い順に並べ替えるとその情報が消える。
    """
    counts = Counter(messages)
    seen: Dict[str, None] = {}
    for message in messages:
        seen.setdefault(message, None)
    return [(message, counts[message]) for message in seen]


def format_report(report: DayReport) -> str:
    lines: List[str] = []
    lines.append(f"===== {report.trading_day} (米国東部時間) の稼働サマリ =====")

    equity = f"{report.account_equity:.2f} USD" if report.account_equity is not None else "不明"
    lines.append(f"口座資金: {equity}")

    lines.append("")
    lines.append("--- 決済 ---")
    if report.trades:
        wins = [t for t in report.trades if t.net_pnl_usd > 0]
        net = sum(t.net_pnl_usd for t in report.trades)
        lines.append(
            f"{len(report.trades)}件 / 勝ち{len(wins)}件 / 純損益 {net:+.2f} USD（手数料控除後）"
        )
        for trade in report.trades:
            r_multiple = f"{trade.r_multiple:+.2f}R" if trade.r_multiple is not None else "R不明"
            lines.append(
                f"  {trade.symbol}: {trade.entry_price:.2f} -> {trade.exit_price:.2f} "
                f"({trade.pnl_pct:+.2f}%, {r_multiple}) reason={trade.reason}"
            )
        if all(trade.commission == 0.0 for trade in report.trades):
            lines.append(
                "  ※ 手数料が全件0のため、ドライラン（実発注が無効）の記録である可能性が高い。"
                "この損益を実発注の成績として読まないこと。"
            )
    else:
        lines.append("なし")

    lines.append("")
    lines.append("--- 新規建て ---")
    if report.entries:
        for symbol, price in report.entries:
            lines.append(f"  {symbol} @ {price:.2f} USD")
    else:
        lines.append("0件。以下は「なぜ建たなかったか」の材料。")
        if report.lowest_deviation:
            nearest = sorted(report.lowest_deviation.items(), key=lambda item: item[1])[:5]
            lines.append("  その日いちばん押した乖離率（買いシグナルは -5% 以下）:")
            for symbol, deviation in nearest:
                lines.append(f"    {symbol}: {deviation:+.2f}%")
        else:
            lines.append("  シグナル判定の行が1件も無い（＝監視サイクルが回っていない）。")
        for label, count in report.skip_reasons.most_common():
            lines.append(f"  見送り: {label} ({count}回)")

    lines.append("")
    lines.append("--- 監視銘柄 ---")
    lines.append(f"シグナル判定できた銘柄: {len(report.lowest_deviation)}件 "
                 f"{sorted(report.lowest_deviation)}")
    if report.excluded_symbols:
        lines.append("株価帯で除外:")
        for symbol, reason in sorted(report.excluded_symbols.items()):
            lines.append(f"  {symbol}: {reason}")
    if report.screening_degraded:
        lines.append("スクリーニングが縮退（固定ウォッチリストで稼働）:")
        # 再試行は SCREENING_RETRY_INTERVAL_SECONDS ごとに走るため、同じ2行が
        # 1日で20回以上並ぶ。全部出すと「1画面で読む」という本レポートの
        # 役目が壊れるので、回数を添えて1回にまとめる。
        for message, count in _counted_in_order(report.screening_degraded):
            suffix = f"（{count}回）" if count > 1 else ""
            lines.append(f"  {message}{suffix}")

    lines.append("")
    lines.append("--- 稼働 ---")
    evaluations = max(report.signal_evaluations.values()) if report.signal_evaluations else 0
    lines.append(f"シグナル判定サイクル: {evaluations}回（最も多く判定された銘柄の回数）")
    lines.append(f"市場時間外でスキップしたサイクル: {report.cycles_skipped_closed}回")
    lines.append(f"接続リトライを使い切ったラウンド: {report.connection_failure_rounds}回")
    if report.manual_login_hint:
        lines.append("  ** IB Gatewayへの再ログインが必要な可能性あり（ログに案内が出ている） **")
    if report.warnings:
        lines.append("WARNING（種類別）:")
        for head, count in report.warnings.most_common(10):
            lines.append(f"  {count:4d}  {head}")
    if report.errors:
        lines.append("ERROR（種類別）:")
        for head, count in report.errors.most_common(10):
            lines.append(f"  {count:4d}  {head}")

    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", default=DEFAULT_LOG_PATH)
    parser.add_argument("--journal", default=DEFAULT_JOURNAL_PATH)
    parser.add_argument("--date", help="対象の取引日(YYYY-MM-DD, 米国東部時間)。省略時はログ内の直近")
    args = parser.parse_args(argv)

    if not os.path.exists(args.log):
        print(f"稼働ログが見つかりません: {args.log}", file=sys.stderr)
        return 1

    with open(args.log, "r", encoding="utf-8", errors="replace") as f:
        log_lines = list(parse_log_lines(f))

    if args.date:
        trading_day = date.fromisoformat(args.date)
    else:
        trading_day = latest_trading_day(log_lines)
        if trading_day is None:
            print(f"解析できるログ行がありません: {args.log}", file=sys.stderr)
            return 1

    trades = TradeJournal(args.journal).load_trades()
    report = build_day_report(log_lines, trades, trading_day)
    print(format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
