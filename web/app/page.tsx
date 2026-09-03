"use client";

import { useMemo, useState } from "react";

import { DiffTable } from "@/components/DiffTable";
import { ReportSlot } from "@/components/ReportSlot";
import { describeDiff, diffPayloads } from "@/lib/diff";
import { readReport, type Report } from "@/lib/report";

interface Slot {
  readonly report: Report | null;
  readonly error: string | null;
}

const EMPTY: Slot = { report: null, error: null };

export default function Page() {
  const [left, setLeft] = useState<Slot>(EMPTY);
  const [right, setRight] = useState<Slot>(EMPTY);

  async function load(file: File, set: (slot: Slot) => void): Promise<void> {
    try {
      const report = await readReport(file.name, await file.text());
      set({ report, error: null });
    } catch (error) {
      // **例外を握り潰さない。** 利用者が渡すのは任意のファイルなので、
      // 何が悪かったのかを言わないと「反応しない画面」になる。
      set({ report: null, error: error instanceof Error ? error.message : String(error) });
    }
  }

  const comparison = useMemo(() => {
    if (!left.report || !right.report) return null;
    const summary = diffPayloads(left.report.payload, right.report.payload);
    return { summary, headline: describeDiff(summary) };
  }, [left.report, right.report]);

  const digestsMatch =
    left.report && right.report
      ? left.report.recomputedDigest === right.report.recomputedDigest
      : null;

  const tampered =
    (left.report && !left.report.digestIsAuthentic) ||
    (right.report && !right.report.digestIsAuthentic);

  return (
    <main>
      <h1>レポートの突き合わせ</h1>
      <p className="lede">
        <code>python -m backtest.run --report out.json</code> が書いたレポートを2つ読み込み、
        <code>result_digest</code> を<strong>計算し直して</strong>照合する。一致しなければ、
        入力・パラメータ・結果のどこが動いたのかを葉の単位で並べる。
        <br />
        ファイルはブラウザの中だけで処理し、どこにも送らない。
      </p>

      <div className="slots">
        <ReportSlot
          label="左のレポート"
          report={left.report}
          error={left.error}
          onFile={(file) => void load(file, setLeft)}
        />
        <ReportSlot
          label="右のレポート"
          report={right.report}
          error={right.error}
          onFile={(file) => void load(file, setRight)}
        />
      </div>

      {tampered && (
        <div className="verdict tampered">
          <strong>digest が中身と一致しないレポートがあります。</strong>
          <p className="note">
            書かれている <code>result_digest</code> と、中身から計算し直した値が違う。
            レポートが編集されたか、書き出した実装とこのビューアの正規化がずれている。
            <strong>どちらであれ、この比較の結果は当てにできない。</strong>
          </p>
        </div>
      )}

      {comparison && digestsMatch !== null && (
        <>
          <div className={digestsMatch ? "verdict match" : "verdict differ"}>
            <strong>
              {digestsMatch
                ? "digest が一致：同じ入力・同じ設定・同じ結果"
                : "digest が不一致：本文のどこかが違う"}
            </strong>
            <p className="note">{comparison.headline}</p>
          </div>

          {!comparison.summary.identical && (
            <>
              <h2>変化した箇所（{comparison.summary.changes.length}件）</h2>
              <DiffTable changes={comparison.summary.changes} />
            </>
          )}

          {comparison.summary.identical && !digestsMatch && (
            <p className="note">
              本文に差が無いのに digest が違う。
              <strong>正規化の実装がずれている疑いがある。</strong>
            </p>
          )}
        </>
      )}

      <h2>この画面が答えること</h2>
      <table>
        <tbody>
          <tr>
            <td>入力だけが違う</td>
            <td>別のデータで回した。結果が違うのは当然</td>
          </tr>
          <tr>
            <td>パラメータだけが違う</td>
            <td>設定を変えた。同じ条件の比較になっていない</td>
          </tr>
          <tr>
            <td>
              <strong>入力もパラメータも同じなのに結果が違う</strong>
            </td>
            <td>
              <strong>コードが変わったか、実装に環境依存が入り込んだ</strong>
            </td>
          </tr>
        </tbody>
      </table>
      <p className="note">
        実行時刻・実行環境・コマンドは digest に含まれない。確かめたいのは
        「環境が変わっても数字が変わらないこと」なので、環境を混ぜると比較そのものが成立しない。
      </p>
    </main>
  );
}
