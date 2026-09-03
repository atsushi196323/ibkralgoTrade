/**
 * 2つのレポートの本文を突き合わせ、**どこが動いたか**を葉の単位で並べる。
 *
 * 見せたいのは「違う」ではなく「**入力が変わったのか、パラメータが変わったのか、
 * 結果が変わったのか**」である。この3つは意味がまったく違う。
 *
 * - 入力だけが違う  … 別のデータで回した。結果が違うのは当然
 * - パラメータだけが違う … 設定を変えた。比較の前提が違う
 * - **入力もパラメータも同じなのに結果が違う** … コードが変わったか、
 *   実装に環境依存が入り込んだ。**いちばん重い知らせで、見逃してはならない**
 */

export type ChangeKind = "changed" | "added" | "removed";

export interface Change {
  /** `results.combined.profit_factor` のような、根からの道。 */
  readonly path: string;
  /** 先頭の要素（inputs / parameters / results / mode）。 */
  readonly section: string;
  readonly kind: ChangeKind;
  readonly left: unknown;
  readonly right: unknown;
}

export interface DiffSummary {
  readonly changes: readonly Change[];
  readonly sections: readonly string[];
  readonly identical: boolean;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function join(path: string, key: string | number): string {
  if (path === "") return String(key);
  return typeof key === "number" ? `${path}[${key}]` : `${path}.${key}`;
}

function walk(left: unknown, right: unknown, path: string, out: Change[]): void {
  const section = path === "" ? "" : path.split(/[.[]/)[0]!;

  if (isRecord(left) && isRecord(right)) {
    // 鍵は和集合で回す。**片側にしか無い鍵を落とすと、
    // 「項目が消えた」という最も分かりやすい変化が差分に現れない。**
    for (const key of [...new Set([...Object.keys(left), ...Object.keys(right)])].sort()) {
      walk(left[key], right[key], join(path, key), out);
    }
    return;
  }

  if (Array.isArray(left) && Array.isArray(right)) {
    for (let index = 0; index < Math.max(left.length, right.length); index += 1) {
      walk(left[index], right[index], join(path, index), out);
    }
    return;
  }

  if (Object.is(left, right)) return;

  const kind: ChangeKind =
    left === undefined ? "added" : right === undefined ? "removed" : "changed";
  out.push({ path, section, kind, left, right });
}

export function diffPayloads(
  left: Record<string, unknown>,
  right: Record<string, unknown>,
): DiffSummary {
  const changes: Change[] = [];
  walk(left, right, "", changes);
  const sections = [...new Set(changes.map((change) => change.section))].sort();
  return { changes, sections, identical: changes.length === 0 };
}

/**
 * 差分の読み方を1行で言う。**ここが画面でいちばん読まれる。**
 */
export function describeDiff(summary: DiffSummary): string {
  if (summary.identical) {
    return "本文は完全に一致しています（同じ入力・同じ設定・同じ結果）。";
  }

  const changedInputs = summary.sections.includes("inputs");
  const changedParameters = summary.sections.includes("parameters");
  const changedResults = summary.sections.includes("results");

  if (changedResults && !changedInputs && !changedParameters) {
    return (
      "入力もパラメータも同じなのに、結果だけが違います。" +
      "コードが変わったか、実装に環境依存が入り込んだ可能性があります。"
    );
  }
  if (changedInputs && !changedParameters) {
    return "入力データが違います。結果の差はデータの差で説明できます。";
  }
  if (changedParameters && !changedInputs) {
    return "設定（パラメータ）が違います。同じ条件の比較にはなっていません。";
  }
  return "入力とパラメータの両方が違います。比較の前提が揃っていません。";
}
