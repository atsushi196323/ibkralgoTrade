/**
 * Python 側（`backtest/report.py`）と**同じ digest** を、独立に計算し直す。
 *
 * ビューアが digest を信じてしまうと、「一致しました」はレポートの中に
 * 書いてある文字列を読み上げているだけになる。**計算し直して初めて、
 * その digest がその中身から出たものだと言える。**
 *
 * Python 側の手順（`_canonical` → `json.dumps(sort_keys=True,
 * ensure_ascii=False, separators=(",", ":"))` → SHA-256）を、
 * 同じ順序でなぞる。ずれると `web/lib/canonical.test.ts` と
 * `tests/test_web_fixtures.py` の両方が落ちる。
 */

import { parseJsonPreservingNumberKind } from "./json-source";
import type { JsonNode } from "./json-source";

/** 実数の桁。**Python 側の `_FLOAT_FORMAT` と必ず一致させること。** */
const FLOAT_DIGITS = 6;

// `toFixed` はこの大きさを超えると指数表記へ切り替わり、
// Python の `%f`（常に展開する）と食い違う。**黙って違う digest を出すより、
// 止めて知らせる方がよい**（このプロジェクトが避けたい「静かな縮退」そのもの）。
const TO_FIXED_LIMIT = 1e21;

export class CanonicalisationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CanonicalisationError";
  }
}

/**
 * Python の `"%.6f" % value` と同じ文字列を作る。
 *
 * 差が出る点が2つあるので、どちらも明示的に潰す。
 *
 * - **負のゼロ**: Python は `-0.000000`、JS の `toFixed` は `0.000000` を返す
 *   （符号が消える）。損益が 0 ちょうどのとき実際に現れる
 * - **`nan` / `inf`**: Python 側は名前の文字列にしている。プロフィット
 *   ファクターは負けトレードが0件だと `inf` になるので、例外的ではない
 *
 * なお「ちょうど半分」の丸めは考えなくてよい。7桁目が5でそこで終わる10進数は
 * 分母に 5^7 を含むため、倍精度では厳密に表現できない——つまり Python の
 * 偶数丸めと JS の切り上げが分かれる入力が存在しない。
 */
export function formatFloat(value: number): string {
  if (Number.isNaN(value)) return "nan";
  if (value === Infinity) return "inf";
  if (value === -Infinity) return "-inf";
  if (Math.abs(value) >= TO_FIXED_LIMIT) {
    throw new CanonicalisationError(
      `${value} は大きすぎて Python と同じ桁展開ができません（1e21未満であること）。`,
    );
  }
  const text = value.toFixed(FLOAT_DIGITS);
  return Object.is(value, -0) ? `-${text}` : text;
}

/** 正規化した後の形。実数は文字列になっている。 */
export type CanonicalValue =
  | null
  | boolean
  | number
  | string
  | readonly CanonicalValue[]
  | { readonly [key: string]: CanonicalValue };

export function canonicalise(node: JsonNode): CanonicalValue {
  switch (node.kind) {
    case "null":
      return null;
    case "boolean":
      return node.value;
    case "int":
      return node.value;
    case "float":
      return formatFloat(node.value);
    case "string":
      return node.value;
    case "array":
      return node.items.map(canonicalise);
    case "object":
      return Object.fromEntries(
        [...node.entries]
          .sort(([a], [b]) => compareKeys(a, b))
          .map(([key, value]) => [key, canonicalise(value)] as const),
      );
  }
}

/**
 * Python の `sorted()` と同じ並び＝**UTF-16ではなくコードポイント順**。
 *
 * JS の `<` はサロゲートペアを含む文字列で Python と食い違う。
 * 鍵が ASCII の間は同じ結果になるが、**そこに依存すると、日本語の鍵を
 * 1つ足した日に digest だけが静かにずれる。**
 */
function compareKeys(a: string, b: string): number {
  const left = [...a];
  const right = [...b];
  for (let i = 0; i < Math.min(left.length, right.length); i += 1) {
    const difference = left[i]!.codePointAt(0)! - right[i]!.codePointAt(0)!;
    if (difference !== 0) return difference;
  }
  return left.length - right.length;
}

/**
 * `json.dumps(..., sort_keys=True, ensure_ascii=False, separators=(",", ":"))`
 * と同じ1行を作る。
 *
 * **`JSON.stringify` に鍵の並びを任せてはならない。** JSの実行時は
 * 「整数に見える鍵」を数値順で先に並べるため、`{"10": …, "2": …}` のような
 * 鍵があると Python の並びと変わる。文字列のエスケープだけは JSON と JS で
 * 同じなので、そこは `JSON.stringify` に任せる。
 */
export function serialiseCanonical(value: CanonicalValue): string {
  if (value === null) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return String(value);
  if (typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(serialiseCanonical).join(",")}]`;

  const entries = Object.entries(value as { readonly [key: string]: CanonicalValue }).sort(
    ([a], [b]) => compareKeys(a, b),
  );
  const body = entries
    .map(([key, item]) => `${JSON.stringify(key)}:${serialiseCanonical(item)}`)
    .join(",");
  return `{${body}}`;
}

async function sha256Hex(text: string): Promise<string> {
  const bytes = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** レポートの本文（mode / parameters / inputs / results）から digest を計算する。 */
export async function computeDigest(node: JsonNode): Promise<string> {
  return sha256Hex(serialiseCanonical(canonicalise(node)));
}

export function parseAndCanonicalise(text: string): CanonicalValue {
  return canonicalise(parseJsonPreservingNumberKind(text));
}
