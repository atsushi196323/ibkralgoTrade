/** レポートの検証。**利用者が渡すのは任意のファイルである**という前提を守る。 */

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { ReportFormatError, readReport } from "./report";

function fixture(name: string): string {
  return readFileSync(join(__dirname, "..", "fixtures", name), "utf-8");
}

describe("読み取り", () => {
  it("digest を信じず、計算し直して照合する", async () => {
    // ここが「一致しました」の意味を決める。ファイルの文字列を読み上げる
    // だけなら、書き換えたレポートも一致と表示されてしまう。
    const report = await readReport("base", fixture("report_base.json"));

    expect(report.digestIsAuthentic).toBe(true);
    expect(report.recomputedDigest).toBe(report.declaredDigest);
  });

  it("digest を書き換えたレポートを見破る", async () => {
    const tampered = fixture("report_base.json").replace(
      /"result_digest": "[0-9a-f]{64}"/,
      '"result_digest": "0000000000000000000000000000000000000000000000000000000000000000"',
    );

    const report = await readReport("tampered", tampered);

    expect(report.digestIsAuthentic).toBe(false);
    expect(report.recomputedDigest).not.toBe(report.declaredDigest);
  });

  it("結果を書き換えたレポートを見破る", async () => {
    // digest はそのままに中身だけ差し替えた場合。計算し直していれば必ず割れる。
    const tampered = fixture("report_base.json").replace('"num_trades": 45', '"num_trades": 99');

    const report = await readReport("tampered", tampered);

    expect(report.digestIsAuthentic).toBe(false);
  });

  it("入力の指紋を読み出す", async () => {
    const report = await readReport("base", fixture("report_base.json"));

    expect(report.inputs.map((input) => input.symbol)).toEqual(["AAPL", "KO"]);
    expect(report.inputs[0]!.numBars).toBe(3);
    expect(report.inputs[0]!.barsSha256).toHaveLength(64);
  });
});

describe("受け付けないもの", () => {
  it("JSONでないファイル", async () => {
    await expect(readReport("x", "これはJSONではありません")).rejects.toThrow(ReportFormatError);
  });

  it("レポートではないJSON", async () => {
    // 「何も起きない」ではなく、何を渡せばよいかまで言う。
    await expect(readReport("x", '{"hello": 1}')).rejects.toThrow(/レポートではないようです/);
  });

  it("知らない schema_version", async () => {
    // 版が違うものを同じ形として扱うと、差分が「意味のある変化」ではなく
    // 「構造の違い」を並べ始める。
    const future = fixture("report_base.json").replace(
      '"schema_version": 1',
      '"schema_version": 99',
    );
    await expect(readReport("x", future)).rejects.toThrow(/schema_version=99/);
  });
});
