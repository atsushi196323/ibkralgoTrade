"""稼働ログの出力先設定のテスト。"""

import logging
import sys
from pathlib import Path

import pytest

from core.logging_setup import CONSOLE_HANDLER_NAME, configure_logging


@pytest.fixture
def _restore_root_logger():
    """ルートロガーをテスト前の状態へ戻す。

    configure_logging はルートロガーというプロセス全体の共有状態を書き換える。
    戻さないと、後続のテストがこのテストの張ったファイルハンドラへ書き込む。
    """
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in original_handlers:
            handler.close()
            root.removeHandler(handler)
    for handler in original_handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(original_level)


def test_configure_logging_writes_to_the_log_file(tmp_path: Path, _restore_root_logger) -> None:
    log_path = tmp_path / "logs" / "bot.log"

    configure_logging(log_path=str(log_path))
    logging.getLogger("test").info("スクリーニング結果が空でした")

    for handler in logging.getLogger().handlers:
        handler.flush()

    assert log_path.exists()
    assert "スクリーニング結果が空でした" in log_path.read_text(encoding="utf-8")


def test_configure_logging_creates_the_parent_directory(tmp_path: Path, _restore_root_logger) -> None:
    """logs/ が無い状態から起動しても失敗しないこと。"""
    log_path = tmp_path / "does" / "not" / "exist" / "bot.log"

    configure_logging(log_path=str(log_path))

    assert log_path.parent.is_dir()


def test_configure_logging_is_idempotent(tmp_path: Path, _restore_root_logger) -> None:
    """二重に呼んでも同じ行が2回出力されないこと。"""
    log_path = tmp_path / "bot.log"

    configure_logging(log_path=str(log_path))
    configure_logging(log_path=str(log_path))

    logging.getLogger("test").info("一度だけ")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert log_path.read_text(encoding="utf-8").count("一度だけ") == 1


def _console_handlers() -> list:
    # pytestのcaplogもStreamHandlerを挿すため、名前で自分のものだけを数える。
    return [h for h in logging.getLogger().handlers if h.name == CONSOLE_HANDLER_NAME]


def test_configure_logging_keeps_console_output_on_a_terminal(tmp_path: Path, _restore_root_logger) -> None:
    """端末から実行したときはコンソールへも出すこと。"""
    configure_logging(log_path=str(tmp_path / "bot.log"), console=True)

    assert len(_console_handlers()) == 1


def test_console_output_is_skipped_when_stderr_is_not_a_terminal(
    tmp_path: Path, _restore_root_logger, monkeypatch,
) -> None:
    """常設運用（launchd等）ではコンソールへ出さないこと。

    標準エラーはローテーションされないファイルへリダイレクトされ、
    ローテーション付きの bot.log と同じ内容が無制限に積み上がる
    （実測で launchd.err が bot.log と同サイズになっていた）。
    """
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False, raising=False)

    configure_logging(log_path=str(tmp_path / "bot.log"))

    assert _console_handlers() == []


def test_startup_failures_still_reach_stderr(tmp_path: Path, _restore_root_logger, monkeypatch) -> None:
    """コンソール出力を止めても、標準エラー自体は塞がないこと。

    configure_logging が動く前の例外（依存関係の解決失敗など）は
    Python標準のstderrへ出る。ここを塞ぐと起動失敗が完全に無音になる。
    """
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False, raising=False)

    configure_logging(log_path=str(tmp_path / "bot.log"))

    assert sys.stderr.closed is False


def test_importing_main_does_not_create_log_files(tmp_path: Path, monkeypatch) -> None:
    """インポートしただけでログファイルを作らないこと。

    インポート時に設定すると、main を import するだけのテストが作業ディレクトリへ
    logs/ を作る。設定はエントリーポイント（__main__）でのみ行う。
    """
    monkeypatch.chdir(tmp_path)

    import importlib

    import main

    importlib.reload(main)

    assert not (tmp_path / "logs").exists()


# --- IBKRのメッセージのエスケープ解除 ---------------------------------------------

# TWS/IB GatewayはAPIへ非ASCII文字をエスケープして送る。2026-08-01の実測で、
# 切断のエラーが `Error 1100, reqId -1: マーケ...` と記録され、
# 障害時に一番読みたいメッセージが読めない状態だった。


def test_ibkr_escaped_message_is_decoded(tmp_path: Path, _restore_root_logger) -> None:
    """実測どおりのエスケープ済みメッセージが日本語に戻ること。"""
    log_path = tmp_path / "bot.log"
    configure_logging(log_path=str(log_path))

    logging.getLogger("ib_insync.wrapper").error(
        "Error 1100, reqId -1: "
        "\\u63a5\\u7d9a\\u304c\\u5207\\u65ad\\u3055\\u308c\\u307e\\u3057\\u305f\\u3002"
    )
    for handler in logging.getLogger().handlers:
        handler.flush()

    written = log_path.read_text(encoding="utf-8")
    assert "Error 1100, reqId -1: 接続が切断されました。" in written
    assert "\\u63a5" not in written


def test_filter_applies_to_records_from_child_loggers(tmp_path: Path, _restore_root_logger) -> None:
    """伝播してきたレコードにも効くこと。

    ロガーに付けたフィルターは子ロガーからの伝播レコードに適用されないため、
    ハンドラ側に付けている。IBKRのメッセージは ib_insync.wrapper が出すので、
    ここを取り違えると本番でだけ効かない。
    """
    log_path = tmp_path / "bot.log"
    configure_logging(log_path=str(log_path))

    logging.getLogger("a.deeply.nested.logger").info("\\u5b8c\\u4e86")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert "完了" in log_path.read_text(encoding="utf-8")


def test_ordinary_messages_are_left_alone(tmp_path: Path, _restore_root_logger) -> None:
    """エスケープを含まないメッセージを壊さないこと。"""
    log_path = tmp_path / "bot.log"
    configure_logging(log_path=str(log_path))

    logging.getLogger("main").info("[%s] 乖離率=%.2f%% reason=%s", "AAPL", -5.25, "TAKE_PROFIT")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert "[AAPL] 乖離率=-5.25% reason=TAKE_PROFIT" in log_path.read_text(encoding="utf-8")


def test_malformed_escapes_do_not_break_logging(tmp_path: Path, _restore_root_logger) -> None:
    """壊れたエスケープでも例外を出さず、元の表記のまま通すこと。

    ログの整形が原因で稼働が止まるのは本末転倒である。
    """
    log_path = tmp_path / "bot.log"
    configure_logging(log_path=str(log_path))

    logging.getLogger("main").info("\\u30de\\uZZZZ \\u12 未完のエスケープ")
    for handler in logging.getLogger().handlers:
        handler.flush()

    written = log_path.read_text(encoding="utf-8")
    assert "未完のエスケープ" in written
    # 4桁揃っている先頭だけが変換され、壊れている側はそのまま残る。
    assert "\\uZZZZ" in written


def test_surrogate_pairs_are_left_escaped(tmp_path: Path, _restore_root_logger) -> None:
    """サロゲートペアは変換しないこと。

    unicode_escape では合成されず単独サロゲートのまま残り、UTF-8で
    書き出す段階でログ出力自体が例外で落ちる。
    """
    log_path = tmp_path / "bot.log"
    configure_logging(log_path=str(log_path))

    logging.getLogger("main").info("絵文字 \\ud83d\\ude00 を含む")
    for handler in logging.getLogger().handlers:
        handler.flush()

    written = log_path.read_text(encoding="utf-8")
    assert "\\ud83d\\ude00" in written


def test_non_string_messages_are_not_touched(tmp_path: Path, _restore_root_logger) -> None:
    """msgが文字列でなくても落ちないこと。"""
    log_path = tmp_path / "bot.log"
    configure_logging(log_path=str(log_path))

    logging.getLogger("main").info({"symbol": "AAPL", "qty": 10})
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert "AAPL" in log_path.read_text(encoding="utf-8")


# --- IBKRの定型通知の抑制 ---------------------------------------------------------

# 2026-08-04時点の logs/bot.log では、データファームの状態通知と ib_insync.client の
# 接続ログが全体の32%を占め、「なぜ1件も建たなかったのか」の答え（スキャン結果0件）は
# 1行しか無かった。以下はその1行が埋もれないための境界の固定。


def _write_and_read(log_path: Path, name: str, level: int, message: str) -> str:
    logging.getLogger(name).log(level, message)
    for handler in logging.getLogger().handlers:
        handler.flush()
    return log_path.read_text(encoding="utf-8")


def test_data_farm_status_notifications_are_dropped(tmp_path: Path, _restore_root_logger) -> None:
    """接続のたびに繰り返される正常系の状態通知を残さないこと。"""
    log_path = tmp_path / "bot.log"
    configure_logging(log_path=str(log_path))

    written = _write_and_read(
        log_path,
        "ib_insync.wrapper",
        logging.INFO,
        "Warning 2108, reqId -1: マーケットデータファームの接続状況は現在無効です。:usfarm",
    )

    assert written == ""


def test_data_farm_failures_are_kept(tmp_path: Path, _restore_root_logger) -> None:
    """データファームの障害は残すこと。

    バーが空で返る原因になりうる。空バーは例外にならないため（6.1節）、
    ここを落とすと「データが無い銘柄」と区別する材料が消える。
    """
    log_path = tmp_path / "bot.log"
    configure_logging(log_path=str(log_path))

    written = _write_and_read(
        log_path,
        "ib_insync.wrapper",
        logging.INFO,
        "Warning 2103, reqId -1: マーケットデータファームのコネクションが破損されています:usfarm",
    )

    assert "Warning 2103" in written


def test_ibkr_disconnection_errors_are_kept(tmp_path: Path, _restore_root_logger) -> None:
    """切断のエラーは抑制の対象にしないこと（WARNING以上は素通し）。"""
    log_path = tmp_path / "bot.log"
    configure_logging(log_path=str(log_path))

    written = _write_and_read(
        log_path,
        "ib_insync.wrapper",
        logging.ERROR,
        "Error 1100, reqId -1: IBKRとTrader Workstationの接続が切断されました。",
    )

    assert "Error 1100" in written


def test_ib_insync_connection_progress_is_dropped(tmp_path: Path, _restore_root_logger) -> None:
    """接続の進行ログは core/connection.py の記録と二重になるため残さないこと。"""
    log_path = tmp_path / "bot.log"
    configure_logging(log_path=str(log_path))

    written = _write_and_read(
        log_path, "ib_insync.client", logging.INFO, "Connecting to 127.0.0.1:4002 with clientId 1..."
    )

    assert written == ""


def test_ib_insync_client_errors_are_kept(tmp_path: Path, _restore_root_logger) -> None:
    log_path = tmp_path / "bot.log"
    configure_logging(log_path=str(log_path))

    written = _write_and_read(
        log_path, "ib_insync.client", logging.ERROR, "API connection failed: ConnectionRefusedError"
    )

    assert "API connection failed" in written


def test_the_screening_degradation_line_survives(tmp_path: Path, _restore_root_logger) -> None:
    """埋もれていた側の行を落とさないこと。

    スキャナーの購読権限が無いと0件が返り、固定ウォッチリストへ静かに
    縮退する（「5. 銘柄選定」）。この1行がログを残している理由そのものである。
    """
    log_path = tmp_path / "bot.log"
    configure_logging(log_path=str(log_path))

    written = _write_and_read(
        log_path,
        "data.fundamentals",
        logging.WARNING,
        "時価総額スキャンの結果が0件でした: scan_code=MOST_ACTIVE",
    )

    assert "時価総額スキャンの結果が0件でした" in written


def test_application_info_logs_are_not_affected(tmp_path: Path, _restore_root_logger) -> None:
    """抑制はib_insync由来に限ること。自前のINFOは判断の材料そのもの。"""
    log_path = tmp_path / "bot.log"
    configure_logging(log_path=str(log_path))

    written = _write_and_read(
        log_path, "strategy.pullback", logging.INFO, "[XOM] 乖離率=7.41% シグナル=NONE"
    )

    assert "シグナル=NONE" in written


def test_unparseable_ibkr_messages_are_kept(tmp_path: Path, _restore_root_logger) -> None:
    """コードを読み取れないメッセージは残す側に倒すこと。

    抑制の判定を外したときに、黙って行が消える方が危険である。
    """
    log_path = tmp_path / "bot.log"
    configure_logging(log_path=str(log_path))

    written = _write_and_read(log_path, "ib_insync.wrapper", logging.INFO, "position: Position(...)")

    assert "position: Position(...)" in written
