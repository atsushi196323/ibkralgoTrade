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
