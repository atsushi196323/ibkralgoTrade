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
from pathlib import Path

DEFAULT_LOG_PATH: str = "logs/bot.log"

LOG_FORMAT: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# 1ファイル10MB × 10世代。180秒ポーリングで数ヶ月分が収まる一方、
# ディスクを無制限には食わない量として置いている。
MAX_BYTES: int = 10 * 1024 * 1024
BACKUP_COUNT: int = 10

# 連続する \uXXXX の並びだけを対象にする。1つずつ変換すると、サロゲートペアが
# 分断されて判別できなくなる。
_ESCAPE_RUN = re.compile(r"(?:\\u[0-9a-fA-F]{4})+")


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


def configure_logging(
    log_path: str = DEFAULT_LOG_PATH,
    level: int = logging.INFO,
) -> None:
    """ルートロガーにコンソールとローテーティングファイルの出力を設定する。

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

    file_handler = logging.handlers.RotatingFileHandler(
        resolved,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(escape_filter)
    root.addHandler(file_handler)

    if not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in root.handlers
    ):
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        stream_handler.addFilter(escape_filter)
        root.addHandler(stream_handler)
