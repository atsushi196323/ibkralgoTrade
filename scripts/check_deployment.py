"""VPS移行の設定漏れを、稼働させる前に1回で洗い出す。

deploy/README.md が要求する手順のうち、**忘れても即座にはエラーにならず、
「毎日何も起きない」「時々つながらない」という形でしか現れないもの**を点検する。
症状が出てから原因に辿り着くのが難しい順に並べてある。

    1. loginctl enable-linger … 忘れるとSSHを切った時点でタイマーごと止まる。
       症状は「毎日何も起きない」で、翌朝のサマリの「ログがありません」警告が
       唯一の手掛かりになる（それも自分で叩かないと出ない）
    2. スワップ            … 2GB構成でJVMがGC中に膨らむとOOM killerがGatewayを
       落とす。**Bot側のログには「接続できない」としか出ない**
    3. macOS側のlaunchd    … 止め忘れると同じ認証情報で二重にログインし、
       後勝ちで一方のセッションが切られる。症状は断続的な切断
    4. systemdのバージョン … OnCalendar末尾のタイムゾーン指定は252以降でしか
       解釈されない。古いとタイマーの読み込み自体が失敗する
    5. APIポート           … Gatewayが応答するか。ここだけは失敗が即座に見える

**確かめられなかった項目は OK と数えない**（本プロジェクトの他の判定と同じく、
分からないものは安全側＝要確認に倒す）。判定できない理由そのものが、
たいてい設定漏れの兆候である。

実行方法:
    python -m scripts.check_deployment
"""

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

# Botと同じ設定を見るために .env を読む（core/connection.py と同じ経路）。
# 環境変数だけを見ると、.env に書いたポートと違う値を点検することになる。
load_dotenv()

BOT_UNIT = "ibkralgotrade.timer"
AFTERCLOSE_UNIT = "ibkralgotrade-afterclose.timer"
# launchdのラベルは逆DNS形式で、接頭辞に環境ごとの識別子（macOSのユーザー名など）が
# 入る。固定のラベルで照合すると、別のユーザーや別の名前で登録された残存ジョブを
# 見落とす——このチェックが防ぎたいのは「二重ログインで一方のセッションが切られる」
# ことなので、見落としはそのまま目的の失敗になる。この語を含むラベルを拾う。
LAUNCHD_LABEL_KEYWORD = "ibkralgotrade"

# OnCalendar末尾のタイムゾーン指定が使えるようになったバージョン。
SYSTEMD_TZ_SUFFIX_MIN_VERSION = 252

# ペーパー取引のポート（execution/order_manager.PAPER_TRADING_PORTS と同じ）。
# ここで再掲しているのは、このスクリプトが .env だけを見て動く点検であり、
# 発注経路をimportせずに済ませたいため。
PAPER_PORTS = frozenset({7497, 4002})

STATUS_OK = "OK"
STATUS_WARN = "要確認"
STATUS_FAIL = "NG"

_SUBPROCESS_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str
    remedy: str = ""


# --- 判定（純粋関数。実環境を読む処理とは分けてある） ------------------------


def evaluate_linger(linger: Optional[bool], user: str) -> CheckResult:
    if linger is None:
        return CheckResult(
            "loginctl enable-linger", STATUS_WARN,
            "状態を読めませんでした（loginctlが無い、またはsystemd以外の環境）。",
            f"loginctl show-user {user} --property=Linger",
        )
    if linger:
        return CheckResult("loginctl enable-linger", STATUS_OK, "有効。")
    return CheckResult(
        "loginctl enable-linger", STATUS_FAIL,
        "無効。SSHを切った時点でタイマーごと止まり、毎日何も起きなくなります。",
        f"sudo loginctl enable-linger {user}",
    )


def evaluate_swap(mem_total_kb: Optional[int], swap_total_kb: Optional[int]) -> CheckResult:
    if mem_total_kb is None or swap_total_kb is None:
        return CheckResult(
            "スワップ", STATUS_WARN,
            "/proc/meminfo を読めませんでした（Linux以外の環境）。",
        )
    mem_gb = mem_total_kb / 1024 / 1024
    if swap_total_kb > 0:
        return CheckResult(
            "スワップ", STATUS_OK,
            f"{swap_total_kb / 1024 / 1024:.1f}GB（メモリ {mem_gb:.1f}GB）。",
        )
    # メモリに余裕があればOOMの危険は小さいが、無いこと自体は記録する。
    status = STATUS_FAIL if mem_gb < 3.5 else STATUS_WARN
    return CheckResult(
        "スワップ", status,
        f"未設定（メモリ {mem_gb:.1f}GB）。GatewayのJVMがGC中に膨らむと"
        "OOM killerに落とされ、Bot側のログには「接続できない」としか出ません。",
        "sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && "
        "sudo mkswap /swapfile && sudo swapon /swapfile",
    )


def evaluate_launchd_jobs(registered: Optional[List[str]]) -> CheckResult:
    if registered is None:
        return CheckResult(
            "macOS側のlaunchd", STATUS_OK,
            "macOSではないため対象外（移行元での確認項目）。",
        )
    if not registered:
        return CheckResult("macOS側のlaunchd", STATUS_OK, "登録されていません。")
    return CheckResult(
        "macOS側のlaunchd", STATUS_FAIL,
        f"まだ登録されています: {', '.join(registered)}。"
        "同じ認証情報で二重にログインすると、後勝ちで一方のセッションが切られます。",
        "launchctl bootout gui/$(id -u)/" + registered[0],
    )


def evaluate_timers(enabled: Dict[str, Optional[bool]]) -> CheckResult:
    unknown = [u for u, v in enabled.items() if v is None]
    if unknown:
        return CheckResult(
            "systemdタイマー", STATUS_WARN,
            f"状態を読めませんでした: {', '.join(unknown)}。",
            "systemctl --user list-timers 'ibkralgotrade*'",
        )
    missing = [u for u, v in enabled.items() if not v]
    if missing:
        return CheckResult(
            "systemdタイマー", STATUS_FAIL,
            f"有効になっていません: {', '.join(missing)}。",
            "systemctl --user enable --now " + " ".join(missing),
        )
    return CheckResult("systemdタイマー", STATUS_OK, "2本とも有効。")


def evaluate_systemd_version(version: Optional[int], units_use_tz_suffix: bool) -> CheckResult:
    name = "systemdのバージョン"
    if not units_use_tz_suffix:
        return CheckResult(
            name, STATUS_OK,
            "OnCalendarにタイムゾーン指定が無いためバージョン要件はありません"
            "（システムのTZが Asia/Tokyo であることを確認すること）。",
        )
    if version is None:
        return CheckResult(
            name, STATUS_WARN,
            "バージョンを読めませんでした。OnCalendarのタイムゾーン指定は"
            f"{SYSTEMD_TZ_SUFFIX_MIN_VERSION}以降でしか解釈されません。",
            "systemctl --version",
        )
    if version < SYSTEMD_TZ_SUFFIX_MIN_VERSION:
        return CheckResult(
            name, STATUS_FAIL,
            f"{version} は OnCalendar のタイムゾーン指定に対応していません"
            f"（{SYSTEMD_TZ_SUFFIX_MIN_VERSION}以降が必要）。タイマーの読み込み自体が失敗します。",
            "Ubuntu 24.04以降を使うか、timerから ' Asia/Tokyo' を外して "
            "sudo timedatectl set-timezone Asia/Tokyo でシステム側を合わせること",
        )
    return CheckResult(name, STATUS_OK, f"{version}（タイムゾーン指定に対応）。")


def evaluate_api_port(host: str, port: int, reachable: Optional[bool]) -> CheckResult:
    name = f"IB Gateway API ({host}:{port})"
    if port not in PAPER_PORTS:
        return CheckResult(
            name, STATUS_FAIL,
            f"ポート {port} はペーパー口座のポート（{sorted(PAPER_PORTS)}）ではありません。"
            "main()は起動時に停止します。",
            ".env の IBKR_PORT を 4002（IB Gateway ペーパー）にすること",
        )
    if reachable is None:
        return CheckResult(name, STATUS_WARN, "接続可否を判定できませんでした。")
    if reachable:
        return CheckResult(name, STATUS_OK, "接続できました。")
    return CheckResult(
        name, STATUS_FAIL,
        "接続できません。Gatewayが起動していないか、ログインが完了していません。",
        "cd deploy/ib-gateway && docker compose logs -f",
    )


def format_results(results: List[CheckResult]) -> str:
    lines: List[str] = []
    for r in results:
        lines.append(f"[{r.status:^4}] {r.name}: {r.detail}")
        if r.remedy and r.status != STATUS_OK:
            lines.append(f"         → {r.remedy}")
    failed = [r for r in results if r.status == STATUS_FAIL]
    warned = [r for r in results if r.status == STATUS_WARN]
    lines.append("")
    if failed:
        lines.append(f"NG {len(failed)}件。稼働させる前に直すこと。")
    elif warned:
        lines.append(f"NGはありません（確認できなかった項目が{len(warned)}件）。")
    else:
        lines.append("すべて確認できました。")
    return "\n".join(lines)


def has_failures(results: List[CheckResult]) -> bool:
    return any(r.status == STATUS_FAIL for r in results)


# --- 実環境を読む -------------------------------------------------------------


def _run(args: List[str]) -> Optional[str]:
    """コマンドの標準出力を返す。実行できなければNone（＝判定不能）。"""
    if shutil.which(args[0]) is None:
        return None
    try:
        completed = subprocess.run(
            args, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout


def read_linger(user: str) -> Optional[bool]:
    out = _run(["loginctl", "show-user", user, "--property=Linger"])
    if out is None or "Linger=" not in out:
        return None
    return "Linger=yes" in out


def read_meminfo() -> tuple[Optional[int], Optional[int]]:
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        return None, None
    values: Dict[str, int] = {}
    for key in ("MemTotal", "SwapTotal"):
        match = re.search(rf"^{key}:\s+(\d+) kB", text, re.MULTILINE)
        if match:
            values[key] = int(match.group(1))
    return values.get("MemTotal"), values.get("SwapTotal")


def read_launchd_jobs() -> Optional[List[str]]:
    """macOSでまだ登録されているジョブ。macOS以外ではNone（対象外）。"""
    if sys.platform != "darwin":
        return None
    out = _run(["launchctl", "list"])
    if out is None:
        return []
    # `launchctl list` は「PID<TAB>終了コード<TAB>ラベル」。ラベル列だけを見る。
    labels = [line.split("\t")[-1].strip() for line in out.splitlines()]
    return [label for label in labels if LAUNCHD_LABEL_KEYWORD in label]


def read_timer_states() -> Dict[str, Optional[bool]]:
    states: Dict[str, Optional[bool]] = {}
    for unit in (BOT_UNIT, AFTERCLOSE_UNIT):
        out = _run(["systemctl", "--user", "is-enabled", unit])
        states[unit] = None if out is None else out.strip() == "enabled"
    return states


def read_systemd_version() -> Optional[int]:
    out = _run(["systemctl", "--version"])
    if out is None:
        return None
    match = re.search(r"systemd (\d+)", out)
    return int(match.group(1)) if match else None


def units_use_timezone_suffix(unit_dir: Path) -> bool:
    """OnCalendar行にタイムゾーン指定があるか（インストール済みのunitを見る）。"""
    for timer in sorted(unit_dir.glob("ibkralgotrade*.timer")):
        try:
            text = timer.read_text(encoding="utf-8")
        except OSError:
            continue
        if re.search(r"^OnCalendar=.*\s\w+/\w+\s*$", text, re.MULTILINE):
            return True
    return False


def probe_port(host: str, port: int, timeout: float = 3.0) -> Optional[bool]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def collect_results(unit_dir: Path, host: str, port: int) -> List[CheckResult]:
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "$USER"
    mem_total, swap_total = read_meminfo()
    installed = unit_dir if unit_dir.is_dir() else Path("deploy/systemd")
    return [
        evaluate_linger(read_linger(user), user),
        evaluate_swap(mem_total, swap_total),
        evaluate_launchd_jobs(read_launchd_jobs()),
        evaluate_timers(read_timer_states()),
        evaluate_systemd_version(read_systemd_version(), units_use_timezone_suffix(installed)),
        evaluate_api_port(host, port, probe_port(host, port)),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="VPS移行の設定漏れを点検する。")
    parser.add_argument("--host", default=os.environ.get("IBKR_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("IBKR_PORT", "4002")))
    parser.add_argument(
        "--unit-dir",
        type=Path,
        default=Path.home() / ".config/systemd/user",
        help="インストール済みのsystemd unitの置き場所",
    )
    args = parser.parse_args()

    results = collect_results(args.unit_dir, args.host, args.port)
    print(format_results(results))
    return 1 if has_failures(results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
