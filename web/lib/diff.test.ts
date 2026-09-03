/** 差分の意味づけ。**「違う」ではなく「どこが違うか」を出せること。** */

import { describe, expect, it } from "vitest";

import { describeDiff, diffPayloads } from "./diff";

const base = {
  mode: "backtest",
  inputs: [{ symbol: "AAPL", bars_sha256: "aaa" }],
  parameters: { initial_equity: 1220.0, costs: { slippage_pct: 0.05 } },
  results: { combined: { num_trades: 45, profit_factor: 1.16 } },
};

function withChange(patch: Record<string, unknown>) {
  return { ...structuredClone(base), ...patch };
}

describe("差分の抽出", () => {
  it("同じ本文なら変化なし", () => {
    const summary = diffPayloads(base, structuredClone(base));

    expect(summary.identical).toBe(true);
    expect(summary.changes).toHaveLength(0);
  });

  it("葉までの道を出す", () => {
    const right = withChange({ results: { combined: { num_trades: 45, profit_factor: 1.2 } } });

    const summary = diffPayloads(base, right);

    expect(summary.changes).toHaveLength(1);
    expect(summary.changes[0]!.path).toBe("results.combined.profit_factor");
    expect(summary.changes[0]!.left).toBe(1.16);
    expect(summary.changes[0]!.right).toBe(1.2);
  });

  it("配列は位置つきで出す", () => {
    const right = withChange({ inputs: [{ symbol: "AAPL", bars_sha256: "bbb" }] });

    expect(diffPayloads(base, right).changes[0]!.path).toBe("inputs[0].bars_sha256");
  });

  it("片側にしか無い鍵を落とさない", () => {
    // 落とすと「項目が消えた」という最も分かりやすい変化が差分に現れない。
    const right = withChange({
      parameters: { initial_equity: 1220.0, costs: {} },
    });

    const summary = diffPayloads(base, right);

    expect(summary.changes.map((c) => c.kind)).toContain("removed");
    expect(summary.changes[0]!.path).toBe("parameters.costs.slippage_pct");
  });

  it("丸ごと消えた枝は、葉に展開せず1行で出す", () => {
    // **意図的にここで止める。** 50個の鍵を持つ枝が消えたときに50行並べると、
    // 「枝ごと無くなった」という1つの事実が読み取りにくくなる。
    const right = withChange({ parameters: { initial_equity: 1220.0 } });

    const summary = diffPayloads(base, right);

    expect(summary.changes).toHaveLength(1);
    expect(summary.changes[0]!.path).toBe("parameters.costs");
    expect(summary.changes[0]!.kind).toBe("removed");
  });
});

describe("読み方の1行", () => {
  it("入力だけが違うとき", () => {
    const right = withChange({ inputs: [{ symbol: "AAPL", bars_sha256: "bbb" }] });

    expect(describeDiff(diffPayloads(base, right))).toMatch(/入力データが違います/);
  });

  it("パラメータだけが違うとき", () => {
    const right = withChange({
      parameters: { initial_equity: 3142.0, costs: { slippage_pct: 0.05 } },
    });

    expect(describeDiff(diffPayloads(base, right))).toMatch(/設定（パラメータ）が違います/);
  });

  it("入力もパラメータも同じなのに結果が違うとき", () => {
    // **いちばん重い知らせ。** コードが変わったか、実装に環境依存が
    // 入り込んだことを意味する。他の文言に混ぜてはならない。
    const right = withChange({ results: { combined: { num_trades: 46, profit_factor: 1.16 } } });

    expect(describeDiff(diffPayloads(base, right))).toMatch(/結果だけが違います/);
  });

  it("完全に一致するとき", () => {
    expect(describeDiff(diffPayloads(base, structuredClone(base)))).toMatch(/完全に一致/);
  });
});
