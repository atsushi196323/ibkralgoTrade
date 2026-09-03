"use client";

import type { Change } from "@/lib/diff";

function render(value: unknown): string {
  // **`String(value)` に丸投げしない。** レポートの葉には数値・文字列・真偽値
  // しか来ない想定だが、想定が外れたときに `[object Object]` と表示すると、
  // 「差分は出ているのに何が違うのか読めない」状態になる。
  if (value === undefined) return "—";
  if (value === null) return "null";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value) ?? "(表示できない値)";
}

const KIND_LABEL: Record<Change["kind"], string> = {
  changed: "変わった",
  added: "増えた",
  removed: "消えた",
};

/** 葉の単位の差分。**節（inputs / parameters / results）を必ず添える。** */
export function DiffTable({ changes }: { readonly changes: readonly Change[] }) {
  return (
    <div className="scroller">
      <table>
      <thead>
        <tr>
          <th>節</th>
          <th>場所</th>
          <th>左</th>
          <th>右</th>
          <th>種類</th>
        </tr>
      </thead>
      <tbody>
        {changes.map((change) => (
          <tr key={change.path}>
            <td>
              <span className="section-tag">{change.section}</span>
            </td>
            <td className="mono">{change.path}</td>
            <td className="mono">{render(change.left)}</td>
            <td className="mono">{render(change.right)}</td>
            <td className="kind">{KIND_LABEL[change.kind]}</td>
          </tr>
        ))}
        </tbody>
      </table>
    </div>
  );
}
