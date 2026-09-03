/** 整数と実数の区別を保って JSON を読めることの番人。 */

import { describe, expect, it } from "vitest";

import { JsonParseError, parseJsonPreservingNumberKind, toPlain } from "./json-source";

function kindOf(text: string): string {
  const node = parseJsonPreservingNumberKind(text);
  return node.kind;
}

describe("数値の種別", () => {
  it("小数点があれば実数", () => {
    expect(kindOf("1220.0")).toBe("float");
    expect(kindOf("-0.0")).toBe("float");
  });

  it("小数点が無ければ整数", () => {
    // **ここが JSON.parse では復元できない情報である。** Python 側は
    // 整数をそのまま、実数を "%.6f" の文字列にするため、取り違えると
    // 同じレポートから違う digest が出る。
    expect(kindOf("45")).toBe("int");
    expect(kindOf("-7")).toBe("int");
  });

  it("指数表記は実数として扱う", () => {
    expect(kindOf("1e3")).toBe("float");
  });
});

describe("読み取り", () => {
  it("入れ子とエスケープを読む", () => {
    const node = parseJsonPreservingNumberKind('{"a":{"b":["x\\"y",true,null]}}');
    expect(toPlain(node)).toEqual({ a: { b: ['x"y', true, null] } });
  });

  it("空のオブジェクトと配列を読む", () => {
    expect(toPlain(parseJsonPreservingNumberKind("{}"))).toEqual({});
    expect(toPlain(parseJsonPreservingNumberKind("[]"))).toEqual([]);
  });

  it("鍵の並びを読んだ順のまま保つ", () => {
    const node = parseJsonPreservingNumberKind('{"b":1,"a":2}');
    if (node.kind !== "object") throw new Error("object のはず");
    expect(node.entries.map(([key]) => key)).toEqual(["b", "a"]);
  });

  it("壊れた JSON は位置つきで知らせる", () => {
    // 「何も起きない」で終わらせない。利用者が渡すファイルは任意なので、
    // どこで読めなくなったかが唯一の手掛かりになる。
    expect(() => parseJsonPreservingNumberKind('{"a":}')).toThrow(JsonParseError);
    expect(() => parseJsonPreservingNumberKind('{"a":1}x')).toThrow(JsonParseError);
    expect(() => parseJsonPreservingNumberKind('"閉じていない')).toThrow(JsonParseError);
  });

  it("Infinity は受け付けない（標準JSONに無い表記）", () => {
    // Python の json は既定でこれを書くので、書き出し側で潰してある
    // （backtest/report.py の _json_safe）。読む側でも通さない。
    expect(() => parseJsonPreservingNumberKind('{"pf":Infinity}')).toThrow(JsonParseError);
  });
});
