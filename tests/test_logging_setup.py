"""稼働ログの出力先設定のテスト。"""

import logging
from pathlib import Path

import pytest

from core.logging_setup import configure_logging


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


def test_configure_logging_keeps_console_output(tmp_path: Path, _restore_root_logger) -> None:
    """ファイルへ出すようになってもコンソールの出力は残ること。"""
    configure_logging(log_path=str(tmp_path / "bot.log"))

    root = logging.getLogger()
    assert any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in root.handlers
    )


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
