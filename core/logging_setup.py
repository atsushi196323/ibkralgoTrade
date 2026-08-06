"""稼働ログの出力先設定（コンソール + ファイル）。

ファイルへ残すのは、本プロジェクトの主要な故障モードが**例外を出さずに
静かに縮退する**ためである（CLAUDE.md「6. IBKR API利用上の制約」）。

- スキャナー/PER取得は購読権限が無いと空を返し、銘柄選定が無効化されたまま
  固定ウォッチリストで回り続ける
- ペーシング違反は例外ではなく空のバー列として返り、「データが無い銘柄」と
  区別がつかない
- `get_current_price_async` が3段のどの経路まで落ちたかは戻り値に現れない

いずれも異常終了しないので、後から「なぜ1件も建たなかったのか」を問うには
稼働中のログが残っている必要がある。標準出力だけではターミナルを閉じた
時点で消えるため、判断材料が失われる。
"""

import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Optional

DEFAULT_LOG_PATH: str = "logs/bot.log"

LOG_FORMAT: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# 自分が付けたコンソールハンドラの目印。他のライブラリやpytestが挿す
# StreamHandlerと取り違えないよう、名前で判別する。
CONSOLE_HANDLER_NAME: str = "ibkralgotrade-console"

# 1ファイル10MB × 10世代。300秒ポーリングで数ヶ月分が収まる一方、
# ディスクを無制限には食わない量として置いている。
MAX_BYTES: int = 10 * 1024 * 1024
BACKUP_COUNT: int = 10

# 連続する \uXXXX の並びだけを対象にする。1つずつ変換すると、サロゲートペアが
# 分断されて判別できなくなる。
_ESCAPE_RUN = re.compile(r"(?:\\u[0-9a-fA-F]{4})+")

# ib_insync.wrapper が出すIBKRのメッセージの先頭。
# 実体は `Warning 2104, reqId -1: ...` / `Error 1100, reqId -1: ...`。
_IBKR_MESSAGE_CODE = re.compile(r"^(?:Warning|Error) (\d+), reqId ")

# 定常運用で繰り返されるだけで、判断に使えない情報を持たないIBKRのコード。
#
# 2026-08-04時点の logs/bot.log（1560行）では、この後の ib_insync.client の
# INFO と合わせて全体の32%を占めていた。内訳は 2108 が131行、2104 が74行、
# 2119 が66行、10167 が51行。一方で「なぜ1件も建たなかったのか」の答えである
# 「時価総額スキャンの結果が0件でした」は1行しか無く、この量に埋もれていた。
#
# **データファームの障害側（2103/2105/2157）は落とさない。** バーが空で返る
# 原因になりうるため、ログを残している目的そのものに関わる（「6.1 ペーシング
# 制限」の空バーと同じく、例外を出さずにデータだけが来なくなる）。落とすのは
# 正常・接続中・非アクティブといった状態通知に限る。
_IBKR_STATUS_NOISE_CODES = frozenset(
    {
        2104,  # マーケットデータファームの接続には問題ありません
        2106,  # HMDSデータファーム・コネクションはOKです
        2107,  # HMDSデータファームの接続が非アクティブです
        2108,  # マーケットデータファームの接続状況は現在無効です
        2119,  # マーケットデータのファームに接続中です
        2158,  # Sec-defデータファームの接続に問題ありません
        10167,  # 購読が無いため遅延マーケットデータを表示しています
    }
)


def _decode_escape_run(match: "re.Match[str]") -> str:
    text = match.group(0)
    try:
        decoded = text.encode("ascii").decode("unicode_escape")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return text

    # サロゲートペアは unicode_escape では合成されず、単独サロゲートのまま残る。
    # それをUTF-8で書き出そうとするとログ出力自体が例外で落ちるため、
    # 読みやすさより安全側に倒して元の表記を保つ。
    if any(0xD800 <= ord(char) <= 0xDFFF for char in decoded):
        return text
    return decoded


class DecodeUnicodeEscapesFilter(logging.Filter):
    """メッセージ中の `\\uXXXX` を実際の文字へ戻すフィルター。

    TWS/IB GatewayはAPIへ非ASCII文字をエスケープした形で送るため、
    ib_insync経由のIBKRのメッセージがそのままでは読めない。実測では
    切断のエラーが `Error 1100, reqId -1: \\u30de\\u30fc\\u30b1...` と記録され、
    障害時に一番読みたいものが読めなかった。

    **ハンドラに付けること。** ロガーに付けたフィルターは、子ロガーから
    伝播してきたレコードには適用されない。IBKRのメッセージは
    `ib_insync.wrapper` が出すので、ルートロガーに付けても効かない。

    どんな入力でも例外を投げない。ログの整形が原因で稼働が止まるのは
    本末転倒であるため、変換できないものは元のまま通す。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str) and "\\u" in record.msg:
                record.msg = _ESCAPE_RUN.sub(_decode_escape_run, record.msg)
        except Exception:  # noqa: BLE001 - ログ整形で稼働を止めない
            pass
        return True


class DropIbkrNoiseFilter(logging.Filter):
    """IBKRの定型的な状態通知を稼働ログから落とすフィルター。

    ログを残す目的は、静かに縮退した原因を後から切り分けることである
    （モジュール冒頭）。その答えになる行は1サイクルに1行しか出ないのに対し、
    ib_insync が中継するデータファームの状態通知は接続のたびに数行ずつ増える。
    2026-08-04時点の実測で全体の32%がこれで、肝心の1行が埋もれていた。

    落とすのは次の2種類だけで、いずれも**同じ情報を別の行から読める**もの:

    - `_IBKR_STATUS_NOISE_CODES` の状態通知（障害側のコードは残す）
    - `ib_insync.client` の INFO（接続・切断の進行）。`core/connection.py` が
      試行回数とホストを添えて同じ出来事を記録しているため二重になる

    WARNING以上は名前空間によらず素通しする。IBKRの切断(1100/1101/1102)や
    注文の拒否はここで判断できる情報が無く、落とすと復旧の経緯が追えない。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True

        if record.name.startswith("ib_insync.client"):
            return False

        if record.name != "ib_insync.wrapper":
            return True

        try:
            match = _IBKR_MESSAGE_CODE.match(record.getMessage())
        except Exception:  # noqa: BLE001 - ログ整形で稼働を止めない
            return True

        if match is None:
            return True
        return int(match.group(1)) not in _IBKR_STATUS_NOISE_CODES


def configure_logging(
    log_path: str = DEFAULT_LOG_PATH,
    level: int = logging.INFO,
    console: Optional[bool] = None,
) -> None:
    """ルートロガーにコンソールとローテーティングファイルの出力を設定する。

    `console` を省略すると、標準エラーが端末に繋がっているときだけ
    コンソールへ出す。**常設運用ではコンソール出力が丸ごと無駄になるため。**
    launchd/systemd 配下では標準エラーがファイルへリダイレクトされ、
    `bot.log`（10MB×10世代でローテーション）と同じ内容が
    **ローテーションされないファイル**に積み上がる（実測で `launchd.err` が
    `bot.log` と同サイズ）。configure_logging が動く前の例外（依存関係の
    解決失敗など）はPython標準のstderrへ出るので、そちらは従来どおり残る。

    インポート時ではなくエントリーポイントから呼ぶこと。インポート時に
    設定すると、`main` を import するだけのテストが `logs/` を作ってしまう。

    再入可能。既に同じファイルへのハンドラが付いていれば何もしないため、
    複数回呼んでもログが二重に出ることはない。
    """
    root = logging.getLogger()
    root.setLevel(level)

    resolved = Path(log_path).resolve()
    for handler in root.handlers:
        if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == resolved:
            return

    resolved.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(LOG_FORMAT)
    escape_filter = DecodeUnicodeEscapesFilter()
    noise_filter = DropIbkrNoiseFilter()

    file_handler = logging.handlers.RotatingFileHandler(
        resolved,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(noise_filter)
    file_handler.addFilter(escape_filter)
    root.addHandler(file_handler)

    if console is None:
        console = sys.stderr.isatty()

    if console and not any(h.name == CONSOLE_HANDLER_NAME for h in root.handlers):
        stream_handler = logging.StreamHandler()
        stream_handler.name = CONSOLE_HANDLER_NAME
        stream_handler.setFormatter(formatter)
        stream_handler.addFilter(noise_filter)
        stream_handler.addFilter(escape_filter)
        root.addHandler(stream_handler)
