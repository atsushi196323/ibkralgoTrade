/**
 * 数値が「整数として書かれたか、実数として書かれたか」を保ったまま JSON を読む。
 *
 * **`JSON.parse` は使えない。** Python 側は digest を取る前に、整数はそのまま・
 * 実数は `%.6f` の文字列へ落とす（`backtest/report.py` の `_canonical`）。
 * ところが `JSON.parse` は `45` と `45.0` をどちらも `number` の `45` にして
 * しまうため、**元がどちらだったかを復元できない**。
 *
 *     Python:  {"num_trades": 45, "initial_equity": 1220.0}
 *     正規化:  {"num_trades": 45, "initial_equity": "1220.000000"}
 *
 * つまり整数と実数の区別を落とした時点で、同じレポートから違う digest が出る。
 * JSONのテキストには `.` の有無としてこの情報が残っているので、
 * ここでは自前で読んで種別を持ち回る。
 */

export type JsonNode =
  | { readonly kind: "null" }
  | { readonly kind: "boolean"; readonly value: boolean }
  | { readonly kind: "int"; readonly value: number; readonly raw: string }
  | { readonly kind: "float"; readonly value: number; readonly raw: string }
  | { readonly kind: "string"; readonly value: string }
  | { readonly kind: "array"; readonly items: readonly JsonNode[] }
  | { readonly kind: "object"; readonly entries: readonly (readonly [string, JsonNode])[] };

export class JsonParseError extends Error {
  constructor(message: string, readonly position: number) {
    super(`${message}（${position}文字目）`);
    this.name = "JsonParseError";
  }
}

const WHITESPACE = new Set([" ", "\t", "\n", "\r"]);
// JSONの数値の文法（RFC 8259）。指数表記も受ける。
const NUMBER = /^-?(?:0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?/;

class Reader {
  private index = 0;

  constructor(private readonly text: string) {}

  parse(): JsonNode {
    this.skipWhitespace();
    const node = this.readValue();
    this.skipWhitespace();
    if (this.index < this.text.length) {
      throw new JsonParseError("JSONの後ろに余分な文字があります", this.index);
    }
    return node;
  }

  private skipWhitespace(): void {
    while (this.index < this.text.length && WHITESPACE.has(this.text[this.index]!)) {
      this.index += 1;
    }
  }

  private expect(character: string): void {
    if (this.text[this.index] !== character) {
      throw new JsonParseError(`${character} が必要です`, this.index);
    }
    this.index += 1;
  }

  private readValue(): JsonNode {
    const character = this.text[this.index];
    if (character === undefined) throw new JsonParseError("値がありません", this.index);
    if (character === "{") return this.readObject();
    if (character === "[") return this.readArray();
    if (character === '"') return { kind: "string", value: this.readString() };
    if (character === "t" || character === "f") return this.readBoolean();
    if (character === "n") return this.readNull();
    return this.readNumber();
  }

  private readObject(): JsonNode {
    this.expect("{");
    const entries: (readonly [string, JsonNode])[] = [];
    this.skipWhitespace();
    if (this.text[this.index] === "}") {
      this.index += 1;
      return { kind: "object", entries };
    }
    for (;;) {
      this.skipWhitespace();
      const key = this.readString();
      this.skipWhitespace();
      this.expect(":");
      this.skipWhitespace();
      entries.push([key, this.readValue()] as const);
      this.skipWhitespace();
      const next = this.text[this.index];
      if (next === ",") {
        this.index += 1;
        continue;
      }
      this.expect("}");
      return { kind: "object", entries };
    }
  }

  private readArray(): JsonNode {
    this.expect("[");
    const items: JsonNode[] = [];
    this.skipWhitespace();
    if (this.text[this.index] === "]") {
      this.index += 1;
      return { kind: "array", items };
    }
    for (;;) {
      this.skipWhitespace();
      items.push(this.readValue());
      this.skipWhitespace();
      const next = this.text[this.index];
      if (next === ",") {
        this.index += 1;
        continue;
      }
      this.expect("]");
      return { kind: "array", items };
    }
  }

  private readString(): string {
    // 文字列の中身のエスケープは JSON と JS で同じなので、
    // 範囲を切り出して `JSON.parse` に任せる（自前で書き直す理由が無い）。
    const start = this.index;
    this.expect('"');
    while (this.index < this.text.length) {
      const character = this.text[this.index]!;
      if (character === "\\") {
        this.index += 2;
        continue;
      }
      this.index += 1;
      if (character === '"') {
        return JSON.parse(this.text.slice(start, this.index)) as string;
      }
    }
    throw new JsonParseError("文字列が閉じていません", start);
  }

  private readBoolean(): JsonNode {
    if (this.text.startsWith("true", this.index)) {
      this.index += 4;
      return { kind: "boolean", value: true };
    }
    if (this.text.startsWith("false", this.index)) {
      this.index += 5;
      return { kind: "boolean", value: false };
    }
    throw new JsonParseError("true / false ではありません", this.index);
  }

  private readNull(): JsonNode {
    if (!this.text.startsWith("null", this.index)) {
      throw new JsonParseError("null ではありません", this.index);
    }
    this.index += 4;
    return { kind: "null" };
  }

  private readNumber(): JsonNode {
    const match = NUMBER.exec(this.text.slice(this.index));
    if (!match) throw new JsonParseError("数値として読めません", this.index);

    const raw = match[0];
    this.index += raw.length;
    // **`.` か指数があれば実数。** Python の json はこの形でしか実数を書かない
    // （`json.dumps(1220.0)` は `1220.0`）ので、テキストだけで判別できる。
    const isFloat = match[1] !== undefined || match[2] !== undefined;
    return { kind: isFloat ? "float" : "int", value: Number(raw), raw };
  }
}

export function parseJsonPreservingNumberKind(text: string): JsonNode {
  return new Reader(text).parse();
}

/** 種別を落として、ふつうの JS の値へ戻す（表示や差分で使う）。 */
export function toPlain(node: JsonNode): unknown {
  switch (node.kind) {
    case "null":
      return null;
    case "boolean":
    case "int":
    case "float":
    case "string":
      return node.value;
    case "array":
      return node.items.map(toPlain);
    case "object":
      return Object.fromEntries(node.entries.map(([key, value]) => [key, toPlain(value)]));
  }
}
