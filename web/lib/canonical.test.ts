/**
 * **Python と同じ digest を出せることの番人。**
 *
 * 見本（`web/fixtures/`）は Python 側が `python -m scripts.make_web_fixtures` で
 * 生成し、`tests/test_web_fixtures.py` が「再生成しても同じ中身か」を見ている。
 * こちらは同じファイルから **独立に digest を計算し直して**、向こうが書いた
 * 値と一致するかを見る。**片方の正規化だけを変えると、必ずどちらかが落ちる。**
 */

import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  CanonicalisationError,
  computeDigest,
  formatFloat,
  serialiseCanonical,
} from "./canonical";
import { parseJsonPreservingNumberKind } from "./json-source";
import { PAYLOAD_KEYS } from "./report";

const FIXTURE_DIR = join(__dirname, "..", "fixtures");

function fixture(name: string): string {
  return readFileSync(join(FIXTURE_DIR, name), "utf-8");
}

function payloadNodeOf(text: string) {
  const root = parseJsonPreservingNumberKind(text);
  if (root.kind !== "object") throw new Error("オブジェクトではありません");
  const entries = new Map(root.entries);
  return {
    kind: "object" as const,
    entries: PAYLOAD_KEYS.map((key) => [key, entries.get(key)!] as const),
  };
}

function declaredDigestOf(text: string): string {
  return (JSON.parse(text) as { result_digest: string }).result_digest;
}

describe("Python が書いた digest を計算し直す", () => {
  const names = readdirSync(FIXTURE_DIR).filter((name) => name.endsWith(".json"));

  it("見本が1件も無いまま緑にならない", () => {
    // 見本を読めていないのに全テストが通ると、
    // 「一致を確かめている」つもりで何も確かめない状態になる。
    expect(names.length).toBeGreaterThanOrEqual(5);
  });

  it.each(names)("%s の digest が一致する", async (name) => {
    const text = fixture(name);
    await expect(computeDigest(payloadNodeOf(text))).resolves.toBe(declaredDigestOf(text));
  });

  it("実行環境と時刻が違っても digest は同じ", async () => {
    const base = fixture("report_base.json");
    const other = fixture("report_same_digest_other_environment.json");

    expect(declaredDigestOf(base)).toBe(declaredDigestOf(other));
    await expect(computeDigest(payloadNodeOf(other))).resolves.toBe(declaredDigestOf(base));
  });

  it.each([
    "report_changed_input.json",
    "report_changed_parameters.json",
    "report_changed_results.json",
  ])("%s は base と違う digest になる", async (name) => {
    const base = await computeDigest(payloadNodeOf(fixture("report_base.json")));
    await expect(computeDigest(payloadNodeOf(fixture(name)))).resolves.not.toBe(base);
  });
});

describe("実数の文字列化（Python の %.6f と一致させる）", () => {
  it("小数点以下6桁へ丸める", () => {
    expect(formatFloat(1.164321)).toBe("1.164321");
    expect(formatFloat(0.1)).toBe("0.100000");
    expect(formatFloat(37.777777)).toBe("37.777777");
  });

  it("整数値の実数も6桁で書く", () => {
    // Python は 1220.0 を "1220.000000" にする。ここが揃わないと
    // --initial-equity を含むレポートの digest が全部ずれる。
    expect(formatFloat(1220)).toBe("1220.000000");
  });

  it("負のゼロの符号を落とさない", () => {
    // JS の toFixed は符号を落とす（"0.000000"）が Python は "-0.000000"。
    // 損益がちょうど 0 のときに実際に現れる。
    expect(formatFloat(-0)).toBe("-0.000000");
    expect(formatFloat(0)).toBe("0.000000");
  });

  it("nan と inf を名前で書く", () => {
    // プロフィットファクターは負けトレードが0件だと inf になる。
    expect(formatFloat(Number.POSITIVE_INFINITY)).toBe("inf");
    expect(formatFloat(Number.NEGATIVE_INFINITY)).toBe("-inf");
    expect(formatFloat(Number.NaN)).toBe("nan");
  });

  it("桁を展開できない大きさでは黙らずに止まる", () => {
    // toFixed が指数表記へ切り替わる領域。ここで例外にせず値を返すと、
    // Python と違う digest を静かに出すことになる。
    expect(() => formatFloat(1e21)).toThrow(CanonicalisationError);
  });
});

describe("直列化", () => {
  it("鍵をコードポイント順に並べる", () => {
    expect(serialiseCanonical({ b: 1, a: 2 })).toBe('{"a":2,"b":1}');
  });

  it("整数に見える鍵を数値順に並べ替えない", () => {
    // JS のオブジェクトは "2" を "10" より前に置く。Python の sorted() は
    // 文字列比較なので "10" が先。JSON.stringify に任せるとここでずれる。
    expect(serialiseCanonical({ "10": 1, "2": 2 })).toBe('{"10":1,"2":2}');
  });

  it("区切りに空白を入れない", () => {
    expect(serialiseCanonical({ a: [1, 2] })).toBe('{"a":[1,2]}');
  });

  it("非ASCIIをそのまま書く（ensure_ascii=False と揃える）", () => {
    expect(serialiseCanonical({ note: "日本語" })).toBe('{"note":"日本語"}');
  });
});
