"use client";

import type { Report } from "@/lib/report";

interface Props {
  readonly label: string;
  readonly report: Report | null;
  readonly error: string | null;
  readonly onFile: (file: File) => void;
}

/** 1つ分のレポート受け口。**digest の真偽をここで必ず出す。** */
export function ReportSlot({ label, report, error, onFile }: Props) {
  return (
    <div
      className={report ? "slot filled" : "slot"}
      onDragOver={(event) => event.preventDefault()}
      onDrop={(event) => {
        event.preventDefault();
        const file = event.dataTransfer.files[0];
        if (file) onFile(file);
      }}
    >
      <h3>{label}</h3>

      <label>
        <input
          type="file"
          accept="application/json,.json"
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) onFile(file);
          }}
        />
      </label>

      {error && <p className="error">{error}</p>}

      {report && (
        <>
          <p>
            {report.digestIsAuthentic ? (
              <span className="pill ok">digest 照合 OK</span>
            ) : (
              <span className="pill bad">digest が中身と一致しない</span>
            )}
          </p>
          <dl>
            <dt>ファイル</dt>
            <dd>{report.name}</dd>
            <dt>digest</dt>
            <dd className="mono">{report.recomputedDigest}</dd>
            <dt>モード</dt>
            <dd>{report.mode}</dd>
            <dt>生成</dt>
            <dd>{report.generatedAt}</dd>
            <dt>入力</dt>
            <dd>
              {report.inputs.length}銘柄 /{" "}
              {report.inputs.reduce((total, input) => total + input.numBars, 0).toLocaleString()}本
            </dd>
            <dt>環境</dt>
            <dd>
              {Object.entries(report.environment)
                .map(([key, value]) => `${key}=${String(value)}`)
                .join("  ")}
            </dd>
          </dl>
        </>
      )}
    </div>
  );
}
