"use client";

import { useId, useState } from "react";

import type { Report } from "@/lib/report";

interface Props {
  readonly label: string;
  readonly report: Report | null;
  readonly error: string | null;
  readonly onFile: (file: File) => void;
  readonly onClear: () => void;
}

/** 1つ分のレポート受け口。**digest の真偽をここで必ず出す。** */
export function ReportSlot({ label, report, error, onFile, onClear }: Props) {
  // ドロップできることは、実際にドラッグしてみるまで分からない。
  // **枠の見た目を変えて、離してよい場所であることをその場で返す。**
  const [dragging, setDragging] = useState(false);
  const inputId = useId();

  const classes = ["slot"];
  if (report) classes.push("filled");
  if (dragging) classes.push("dragging");

  return (
    <section
      className={classes.join(" ")}
      aria-label={label}
      onDragOver={(event) => {
        event.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(event) => {
        event.preventDefault();
        setDragging(false);
        const file = event.dataTransfer.files[0];
        if (file) onFile(file);
      }}
    >
      <div className="slot-head">
        <h3>{label}</h3>
        {report && (
          <button type="button" className="ghost" onClick={onClear}>
            外す
          </button>
        )}
      </div>

      {report && (
        <p className="slot-id">
          <span className="mono">{report.name}</span>{" "}
          {report.digestIsAuthentic ? (
            <span className="pill ok">digest 照合 OK</span>
          ) : (
            <span className="pill bad">digest が中身と一致しない</span>
          )}
        </p>
      )}

      {/* **`<label>` にテキストを持たせる。** 素の `<input type="file">` だけだと、
          読み上げでは「ファイル選択」としか聞こえず、左右どちらの受け口かが分からない。
          入力自体は見た目から外すがフォーカスは受けるので、キーボードでも辿り着ける。 */}
      <label className="picker" htmlFor={inputId}>
        <span className="picker-main">
          {report ? "別のファイルに差し替える" : "JSONファイルを選ぶ"}
        </span>
        <span className="picker-sub">ここへドラッグしても読み込む</span>
      </label>
      <input
        id={inputId}
        className="visually-hidden"
        type="file"
        accept="application/json,.json"
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) onFile(file);
          // 同じファイルを選び直しても `change` が起きるようにする
          // （外した直後に同じものを読み込めないと、行き止まりに見える）。
          event.target.value = "";
        }}
      />

      {/* 読み込みの失敗は、その場で読み上げられないと「反応しない画面」になる。 */}
      <p className="error" role="status">
        {error}
      </p>

      {report && (
        // **既定では畳む。** digest 64桁・生成時刻・実行環境は、照合が失敗した
        // ときに初めて読む値であって、正常時に画面の一番広い面積を取る理由が無い。
        <details className="meta">
          <summary>詳細（digest・生成時刻・実行環境）</summary>
          <dl>
            <dt>digest</dt>
            <dd className="mono">{report.recomputedDigest}</dd>
            <dt>モード</dt>
            <dd>{report.mode}</dd>
            <dt>生成</dt>
            <dd>{report.generatedAt}</dd>
            <dt>入力</dt>
            <dd>
              {report.inputs.length}銘柄 /{" "}
              {/* ロケールを固定する。**書き出し時（Node）と閲覧時（ブラウザ）で
                  区切り文字が変わると、prerender した HTML と食い違う。** */}
              {report.inputs
                .reduce((total, input) => total + input.numBars, 0)
                .toLocaleString("ja-JP")}
              本
            </dd>
            <dt>環境</dt>
            <dd>
              {Object.entries(report.environment)
                .map(([key, value]) => `${key}=${String(value)}`)
                .join("  ")}
            </dd>
          </dl>
        </details>
      )}
    </section>
  );
}
