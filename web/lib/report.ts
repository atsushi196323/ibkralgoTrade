/**
 * レポートJSONの検証と読み取り。
 *
 * **中身を信用しないこと。** ここへ来るのは利用者がドロップした任意のファイルで、
 * 「バックテストのレポートである」という保証はどこにも無い。鍵が欠けている・
 * 型が違う・そもそもJSONではない、をすべて**画面に出せる日本語のエラー**に
 * して返す（例外を投げっぱなしにすると、利用者から見た症状は「何も起きない」になる）。
 */

import { computeDigest } from "./canonical";
import { parseJsonPreservingNumberKind, toPlain } from "./json-source";
import type { JsonNode } from "./json-source";

/** Python 側 `RunReport.reproducible_payload` の鍵。**順序ではなく集合が契約である。** */
export const PAYLOAD_KEYS = ["mode", "parameters", "inputs", "results"] as const;

/** このビューアが読めるレポートの版。Python 側の `SCHEMA_VERSION` と対応する。 */
export const SUPPORTED_SCHEMA_VERSION = 1;

export interface InputFingerprint {
  readonly symbol: string;
  readonly numBars: number;
  readonly firstDate: string | null;
  readonly lastDate: string | null;
  readonly barsSha256: string;
  readonly fileSha256: string | null;
  readonly path: string | null;
}

export interface Report {
  readonly name: string;
  readonly schemaVersion: number;
  /** ファイルに書いてある digest。 */
  readonly declaredDigest: string;
  /** 中身から計算し直した digest。**この2つが違うレポートは信用できない。** */
  readonly recomputedDigest: string;
  readonly digestIsAuthentic: boolean;
  readonly mode: string;
  readonly command: string;
  readonly generatedAt: string;
  readonly environment: Readonly<Record<string, unknown>>;
  readonly inputs: readonly InputFingerprint[];
  /** 差分を取る対象（mode / parameters / inputs / results）。 */
  readonly payload: Readonly<Record<string, unknown>>;
}

export class ReportFormatError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ReportFormatError";
  }
}

function entriesOf(node: JsonNode, where: string): Map<string, JsonNode> {
  if (node.kind !== "object") {
    throw new ReportFormatError(`${where} がオブジェクトではありません。`);
  }
  return new Map(node.entries.map(([key, value]) => [key, value]));
}

function requireString(entries: Map<string, JsonNode>, key: string): string {
  const node = entries.get(key);
  if (!node || node.kind !== "string") {
    throw new ReportFormatError(`"${key}" が文字列として入っていません。`);
  }
  return node.value;
}

function optionalString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function readInputs(node: JsonNode | undefined): readonly InputFingerprint[] {
  if (!node || node.kind !== "array") {
    throw new ReportFormatError('"inputs" が配列として入っていません。');
  }
  return node.items.map((item, index) => {
    const plain = toPlain(item);
    if (typeof plain !== "object" || plain === null) {
      throw new ReportFormatError(`inputs[${index}] がオブジェクトではありません。`);
    }
    const record = plain as Record<string, unknown>;
    return {
      symbol: typeof record.symbol === "string" ? record.symbol : `#${index}`,
      numBars: typeof record.num_bars === "number" ? record.num_bars : 0,
      firstDate: optionalString(record.first_date),
      lastDate: optionalString(record.last_date),
      barsSha256: typeof record.bars_sha256 === "string" ? record.bars_sha256 : "",
      fileSha256: optionalString(record.file_sha256),
      path: optionalString(record.path),
    };
  });
}

/**
 * レポートを読み、**digest を計算し直してから**返す。
 *
 * 計算し直した値がファイルの `result_digest` と違う場合でも例外にはしない。
 * それ自体が利用者へ伝えるべき結果（＝レポートが編集されている、あるいは
 * 書き出した実装とこのビューアの正規化がずれている）だからである。
 */
export async function readReport(name: string, text: string): Promise<Report> {
  let root: JsonNode;
  try {
    root = parseJsonPreservingNumberKind(text);
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    throw new ReportFormatError(`JSONとして読めません: ${detail}`);
  }

  const entries = entriesOf(root, "レポート");

  const missing = PAYLOAD_KEYS.filter((key) => !entries.has(key));
  if (missing.length > 0) {
    throw new ReportFormatError(
      `バックテストのレポートではないようです（${missing.join(" / ")} がありません）。` +
        "`python -m backtest.run --report out.json` で作ったファイルを渡してください。",
    );
  }

  const schemaVersionNode = entries.get("schema_version");
  const schemaVersion =
    schemaVersionNode && schemaVersionNode.kind === "int" ? schemaVersionNode.value : 0;
  if (schemaVersion !== SUPPORTED_SCHEMA_VERSION) {
    // **黙って読み進めない。** 版が違うレポートを同じ形として扱うと、
    // 差分が「意味のある変化」ではなく「構造の違い」を並べ始める。
    throw new ReportFormatError(
      `schema_version=${schemaVersion} は、このビューア（対応=${SUPPORTED_SCHEMA_VERSION}）では読めません。`,
    );
  }

  const payloadNode: JsonNode = {
    kind: "object",
    entries: PAYLOAD_KEYS.map((key) => [key, entries.get(key)!] as const),
  };

  const declaredDigest = requireString(entries, "result_digest");
  const recomputedDigest = await computeDigest(payloadNode);

  return {
    name,
    schemaVersion,
    declaredDigest,
    recomputedDigest,
    digestIsAuthentic: declaredDigest === recomputedDigest,
    mode: requireString(entries, "mode"),
    command: entries.has("command") ? requireString(entries, "command") : "",
    generatedAt: entries.has("generated_at") ? requireString(entries, "generated_at") : "",
    environment: (toPlain(entries.get("environment") ?? { kind: "object", entries: [] }) ??
      {}) as Record<string, unknown>,
    inputs: readInputs(entries.get("inputs")),
    payload: toPlain(payloadNode) as Record<string, unknown>,
  };
}
