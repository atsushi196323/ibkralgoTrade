"""検証結果を、後から同一性を確かめられる形で残すレポート。

**なぜ要るのか。** バックテストの数字は、結果だけを見ると簡単に嘘をつく
（docs/DECISIONS.md「バックテストは簡単に嘘をつく」）。しかも嘘は、
過剰最適化のような分かりやすい形だけでなく **「前と数字が違う」** という
形でも現れる。そのとき切り分けるべきものは3つある——**データが変わったのか、
パラメータが変わったのか、コードが変わったのか**。標準出力へ数行流すだけの
出力では、この3つのどれも後から確かめられない。

そこでこのモジュールは、結果と一緒に**入力の指紋・パラメータ・実行環境**を
1ファイルに落とす。中心にあるのは `result_digest` で、

    digest = SHA-256(入力の指紋 + パラメータ + 結果)

として計算する。**同じデータ・同じパラメータで回した2回の実行は、この文字列が
一致する。** 一致しなければ、レポートの中の該当セクションを比べれば
どれが動いたのかがそのまま分かる。

**digest に含めないものが2つある。**

- **実行時刻** — 含めると、同じ入力の2回の実行が必ず別の digest になり、
  再現性の確認という目的そのものが消える
- **実行環境（Python / pandas / numpy の版、gitコミット）** — こちらは
  意図的である。**環境が変わっても数字が変わらないことを確かめたい**ので、
  環境を digest に混ぜてはならない。混ぜると「pandas を上げたら digest が
  変わった」が常に起こり、本当に結果が動いたのかを区別できなくなる。
  CIは pandas 2系と3系の両方でテストを回しており、そこで digest を固定して
  いるのはこの設計の帰結である（`tests/test_report.py`）

**浮動小数点は必ず桁を決めてから文字列化する。** `repr(float)` は環境と
版で揺れうるため、そのまま連結すると「数字は同じなのに digest が違う」が
起きる。ここでは全ての実数を `%.6f` に丸めてから並べる。
"""

import hashlib
import json
import logging
import platform
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# レポートの構造を変えたら上げること。古いレポートを読む側が、
# 「フィールドが無い」のか「構造が違う」のかを区別できるようにする。
SCHEMA_VERSION = 1

# 実数の桁。digest の安定性はこの丸めに乗っているので、
# 変えると過去のレポートと digest が一致しなくなる。
_FLOAT_FORMAT = "%.6f"


def _canonical(value: Any) -> Any:
    """digest を取る前に、環境で揺れうる表現をすべて潰す。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        # nan / inf は %f では環境依存の表記になりうるので名前で固定する。
        if value != value:
            return "nan"
        if value in (float("inf"), float("-inf")):
            return "inf" if value > 0 else "-inf"
        return _FLOAT_FORMAT % value
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        # キー順で digest が変わらないよう並べ替える。
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if value is None:
        return None
    return str(value)


def compute_digest(payload: Any) -> str:
    """辞書やリストから、環境に依存しない SHA-256 を作る。"""
    canonical = json.dumps(
        _canonical(payload), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class InputFingerprint:
    """検証に実際に使ったバーの指紋。

    **ファイルのハッシュではなく、読み込んで正規化した後のバーから取る。**
    同じ値が入っていれば、列の並び・改行コード・小数の書き方が違っても
    同じ指紋になってほしい——確かめたいのは「同じデータか」であって
    「同じファイルか」ではないため。ファイル側のハッシュは
    `file_sha256` に別途持ち、CSVを差し替えたことも追えるようにする。
    """

    symbol: str
    num_bars: int
    first_date: Optional[str]
    last_date: Optional[str]
    columns: List[str]
    bars_sha256: str
    file_sha256: Optional[str] = None
    path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "num_bars": self.num_bars,
            "first_date": self.first_date,
            "last_date": self.last_date,
            "columns": list(self.columns),
            "bars_sha256": self.bars_sha256,
            "file_sha256": self.file_sha256,
            "path": self.path,
        }


def _date_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        stamp = pd.Timestamp(value)
    except (ValueError, TypeError):
        return str(value)
    if stamp is pd.NaT or stamp != stamp:
        return None
    return stamp.isoformat()


def fingerprint_bars(
    symbol: str, df: pd.DataFrame, *, path: Optional[str] = None,
    file_sha256: Optional[str] = None,
) -> InputFingerprint:
    """バーのDataFrameから指紋を作る。"""
    # 列の順序はCSVの書き方で変わるので、名前で並べてから畳む。
    columns = sorted(str(c) for c in df.columns)
    rows: List[List[Any]] = []
    for _, row in df.iterrows():
        rows.append([
            _date_string(row[c]) if c == "date" else row[c]
            for c in columns
        ])

    dates = df["date"] if "date" in df.columns else None
    return InputFingerprint(
        symbol=symbol,
        num_bars=int(len(df)),
        first_date=_date_string(dates.iloc[0]) if dates is not None and len(df) else None,
        last_date=_date_string(dates.iloc[-1]) if dates is not None and len(df) else None,
        columns=columns,
        bars_sha256=compute_digest({"columns": columns, "rows": rows}),
        file_sha256=file_sha256,
        path=path,
    )


def sha256_of_file(path: str) -> Optional[str]:
    """CSVそのもののハッシュ。読めなければ None（レポートの生成は止めない）。"""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError:
        # 指紋はバー側(bars_sha256)で取れているので、ここは欠けてよい。
        # ただし黙って消さない（docs/DECISIONS.md「例外を握り潰すときは INFO 以上で残す」）。
        logger.info("CSVのハッシュを取れませんでした: %s", path, exc_info=True)
        return None
    return digest.hexdigest()


def describe_environment() -> Dict[str, Any]:
    """実行環境。**digest には含めない**（このモジュールの docstring を参照）。"""
    return {
        "python": sys.version.split()[0],
        "pandas": pd.__version__,
        "numpy": _version_of("numpy"),
        "platform": platform.platform(),
        "git_commit": _git_commit(),
    }


def _version_of(module_name: str) -> Optional[str]:
    try:
        module = __import__(module_name)
    except ImportError:
        logger.info("%s の版を読めませんでした。", module_name)
        return None
    return str(getattr(module, "__version__", "unknown"))


def _git_commit() -> Optional[str]:
    """検証したコードの位置。**リポジトリ外から実行されることもあるので必須にしない。**"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if out.returncode != 0:
            return None
        commit = out.stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if dirty.returncode == 0 and dirty.stdout.strip():
            # 未コミットの変更があるなら、そのコミットは結果の出所を指さない。
            return f"{commit}-dirty"
        return commit
    except (OSError, subprocess.SubprocessError):
        logger.info("gitのコミットを読めませんでした。", exc_info=True)
        return None


@dataclass
class RunReport:
    """1回の検証の全体。JSON / Markdown のどちらでも書き出せる。"""

    mode: str
    command: str
    parameters: Dict[str, Any]
    inputs: List[InputFingerprint]
    results: Dict[str, Any]
    generated_at: str
    environment: Dict[str, Any] = field(default_factory=describe_environment)
    schema_version: int = SCHEMA_VERSION

    @property
    def reproducible_payload(self) -> Dict[str, Any]:
        """digest の対象。**実行時刻と環境は入れない。**"""
        return {
            "mode": self.mode,
            "parameters": self.parameters,
            "inputs": [i.to_dict() for i in self.inputs],
            "results": self.results,
        }

    @property
    def result_digest(self) -> str:
        return compute_digest(self.reproducible_payload)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "result_digest": self.result_digest,
            "generated_at": self.generated_at,
            "command": self.command,
            "environment": self.environment,
            **self.reproducible_payload,
        }


def format_markdown(report: RunReport) -> str:
    """人が読む側。**digest を先頭に置く**——2回の実行を比べるときに最初に見る値である。"""
    lines: List[str] = [
        "# バックテストレポート",
        "",
        f"- **result_digest**: `{report.result_digest}`",
        f"- モード: {report.mode}",
        f"- 生成: {report.generated_at}",
        f"- コマンド: `{report.command}`",
        "",
        "同じデータ・同じパラメータで回した実行は、**digest が一致する**。",
        "一致しなければ、下の「入力データ」「パラメータ」を比べればどれが動いたのかが分かる。",
        "",
        "## 入力データ",
        "",
        "| 銘柄 | 本数 | 期間 | バーの指紋 |",
        "| --- | ---: | --- | --- |",
    ]
    for item in report.inputs:
        span = f"{(item.first_date or '?')[:10]} 〜 {(item.last_date or '?')[:10]}"
        lines.append(f"| {item.symbol} | {item.num_bars:,} | {span} | `{item.bars_sha256[:16]}` |")

    lines += ["", "## パラメータ", "", "| | |", "| --- | --- |"]
    for label, value in _flatten(sorted(report.parameters.items())):
        lines.append(f"| {label} | {_render(value)} |")

    lines += ["", "## 結果", "", "| | |", "| --- | --- |"]
    for label, value in _flatten(report.results.items()):
        lines.append(f"| {label} | {_render(value)} |")

    per_symbol = report.results.get("per_symbol")
    if isinstance(per_symbol, list) and per_symbol:
        lines += ["", "### 銘柄別", "", "| 銘柄 | trades | 勝率 | PF | 損益 |", "| --- | ---: | ---: | ---: | ---: |"]
        for row in per_symbol:
            lines.append(
                f"| {row['symbol']} | {row['num_trades']} | {row['win_rate_pct']:.1f}% "
                f"| {row['profit_factor']:.2f} | {row['total_pnl']:,.2f} |"
            )

    lines += [
        "",
        "## 実行環境",
        "",
        "**この節は digest に含めない。** 環境が変わっても数字が変わらないことを",
        "確かめたいので、環境を混ぜると比較そのものが成立しなくなる。",
        "",
        "| | |",
        "| --- | --- |",
    ]
    for key, value in sorted(report.environment.items()):
        lines.append(f"| {key} | {_render(value)} |")

    return "\n".join(lines) + "\n"


def _flatten(items: Any, prefix: str = "") -> List[Any]:
    """入れ子の辞書を「親.子」の行へ展開する。

    **辞書をそのまま1セルに流し込まない。** 見出しの数字（PFや勝率）が
    Pythonの辞書表記の中に埋もれると、レポートを開いた人が最初に見たい値が
    読めなくなる。銘柄別・ウィンドウ別のような辞書のリストはここでは畳まず、
    専用の表として別に出す。
    """
    flat: List[Any] = []
    for key, value in items:
        label = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.extend(_flatten(sorted(value.items()), prefix=f"{label}."))
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            flat.append((f"{label} (件数)", len(value)))
        else:
            flat.append((label, value))
    return flat


def _render(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    if value is None:
        return "—"
    return str(value)


def write_report(path: str, report: RunReport) -> str:
    """拡張子で書き分ける（`.md` なら人が読む形、それ以外はJSON）。"""
    if path.lower().endswith(".md"):
        payload = format_markdown(report)
    else:
        payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(payload)
    return report.result_digest
