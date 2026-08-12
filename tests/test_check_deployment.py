"""scripts/check_deployment.py の単体テスト（実環境は読まずすべて注入する）。"""

from pathlib import Path

from scripts.check_deployment import (
    STATUS_FAIL,
    STATUS_OK,
    STATUS_WARN,
    CheckResult,
    evaluate_api_port,
    evaluate_launchd_jobs,
    evaluate_linger,
    evaluate_swap,
    evaluate_systemd_version,
    evaluate_timers,
    format_results,
    has_failures,
    units_use_timezone_suffix,
)

_GB_IN_KB = 1024 * 1024


# --- linger ------------------------------------------------------------------


def test_linger_disabled_is_a_failure() -> None:
    result = evaluate_linger(False, "trader")

    assert result.status == STATUS_FAIL
    assert "trader" in result.remedy


def test_linger_that_could_not_be_read_is_not_counted_as_ok() -> None:
    """**「確かめられなかった」を「有効」として扱ってはならない。**

    linger未設定の症状は「毎日何も起きない」で、稼働してからでは
    設定漏れと休場日の区別がつかない。判定できないこと自体が兆候である。
    """
    result = evaluate_linger(None, "trader")

    assert result.status == STATUS_WARN


# --- スワップ ----------------------------------------------------------------


def test_missing_swap_on_a_2gb_server_is_a_failure() -> None:
    """2GB構成でスワップが無いと、GatewayがOOM killerに落とされる。

    落ちてもBot側のログには「接続できない」としか出ないため、
    稼働前に止めるだけの価値がある。
    """
    result = evaluate_swap(mem_total_kb=2 * _GB_IN_KB, swap_total_kb=0)

    assert result.status == STATUS_FAIL
    assert "swapfile" in result.remedy


def test_missing_swap_on_a_large_server_is_only_a_warning() -> None:
    """メモリに余裕があればOOMの危険は小さい。無いこと自体は記録する。"""
    result = evaluate_swap(mem_total_kb=8 * _GB_IN_KB, swap_total_kb=0)

    assert result.status == STATUS_WARN


def test_configured_swap_passes() -> None:
    result = evaluate_swap(mem_total_kb=2 * _GB_IN_KB, swap_total_kb=2 * _GB_IN_KB)

    assert result.status == STATUS_OK


def test_meminfo_that_could_not_be_read_is_not_counted_as_ok() -> None:
    result = evaluate_swap(mem_total_kb=None, swap_total_kb=None)

    assert result.status == STATUS_WARN


# --- macOS側のlaunchd --------------------------------------------------------


def test_launchd_jobs_still_registered_are_a_failure() -> None:
    """止め忘れると同じ認証情報で二重にログインし、一方が切られる。"""
    result = evaluate_launchd_jobs(["com.user.ibkralgotrade"])

    assert result.status == STATUS_FAIL
    assert "bootout" in result.remedy


def test_launchd_check_is_skipped_off_macos() -> None:
    result = evaluate_launchd_jobs(None)

    assert result.status == STATUS_OK


def test_no_launchd_jobs_passes() -> None:
    assert evaluate_launchd_jobs([]).status == STATUS_OK


# --- systemd -----------------------------------------------------------------


def test_disabled_timer_is_reported_with_the_unit_name() -> None:
    result = evaluate_timers(
        {"ibkralgotrade.timer": True, "ibkralgotrade-afterclose.timer": False}
    )

    assert result.status == STATUS_FAIL
    assert "ibkralgotrade-afterclose.timer" in result.detail


def test_timer_state_that_could_not_be_read_is_not_counted_as_ok() -> None:
    result = evaluate_timers({"ibkralgotrade.timer": None})

    assert result.status == STATUS_WARN


def test_old_systemd_rejects_the_timezone_suffix() -> None:
    """OnCalendar末尾のTZ指定は252以降でしか解釈されない。

    Ubuntu 22.04(systemd 249)では**タイマーの読み込み自体が失敗する**ため、
    「毎日何も起きない」になる。
    """
    result = evaluate_systemd_version(249, units_use_tz_suffix=True)

    assert result.status == STATUS_FAIL


def test_new_systemd_accepts_the_timezone_suffix() -> None:
    assert evaluate_systemd_version(255, units_use_tz_suffix=True).status == STATUS_OK


def test_old_systemd_is_fine_when_the_units_do_not_use_a_timezone_suffix() -> None:
    assert evaluate_systemd_version(249, units_use_tz_suffix=False).status == STATUS_OK


def test_the_shipped_timers_use_a_timezone_suffix() -> None:
    """リポジトリのtimerがTZ指定を使っている限り、バージョン要件は生きている。

    TZ指定を外したらこのテストが落ちる。そのときはREADMEの要件も
    併せて見直すこと。
    """
    assert units_use_timezone_suffix(Path("deploy/systemd")) is True


# --- APIポート ---------------------------------------------------------------


def test_a_live_port_is_rejected_before_anything_else() -> None:
    """実資金のポートは、到達できるかどうか以前に止める。"""
    result = evaluate_api_port("127.0.0.1", 4001, reachable=True)

    assert result.status == STATUS_FAIL
    assert "4001" in result.detail


def test_an_unreachable_paper_port_is_a_failure() -> None:
    result = evaluate_api_port("127.0.0.1", 4002, reachable=False)

    assert result.status == STATUS_FAIL
    assert "docker compose" in result.remedy


def test_a_reachable_paper_port_passes() -> None:
    assert evaluate_api_port("127.0.0.1", 4002, reachable=True).status == STATUS_OK


# --- 出力 --------------------------------------------------------------------


def test_failures_are_summarised_and_reported_through_the_exit_code() -> None:
    results = [
        CheckResult("A", STATUS_OK, "問題なし"),
        CheckResult("B", STATUS_FAIL, "壊れています", "直し方"),
    ]

    text = format_results(results)

    assert has_failures(results) is True
    assert "直し方" in text
    assert "NG 1件" in text


def test_remedies_are_not_printed_for_passing_checks() -> None:
    text = format_results([CheckResult("A", STATUS_OK, "問題なし", "不要な手順")])

    assert "不要な手順" not in text
    assert has_failures([CheckResult("A", STATUS_WARN, "不明")]) is False
