"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { DiffTable } from "@/components/DiffTable";
import { ReportSlot } from "@/components/ReportSlot";
import { describeDiff, diffPayloads } from "@/lib/diff";
import { readReport, type Report } from "@/lib/report";
import { BASE_FILE, findSample, type SampleDefinition } from "@/lib/samples";

interface Slot {
  readonly report: Report | null;
  readonly error: string | null;
}

const EMPTY: Slot = { report: null, error: null };

export interface SampleSource extends SampleDefinition {
  /** 見本の中身。**ビルド時に埋め込む**ので、開いた時点で取得は要らない。 */
  readonly text: string;
}

interface Props {
  readonly samples: readonly SampleSource[];
  readonly defaultSampleId: string;
  /** 書き出し時に計算しておいた初期状態（開いた瞬間から差分が見えるようにする）。 */
  readonly initialLeft: Report;
  readonly initialRight: Report;
}

function sampleIdFromHash(): string | null {
  if (typeof window === "undefined") return null;
  return window.location.hash.replace(/^#/, "") || null;
}

export function Comparer({ samples, defaultSampleId, initialLeft, initialRight }: Props) {
  const [left, setLeft] = useState<Slot>({ report: initialLeft, error: null });
  const [right, setRight] = useState<Slot>({ report: initialRight, error: null });
  // どの見本を見ているかは、押した後の画面からは読み取れない
  // （左右のファイル名は出るが、5つのどれを押したのかは分からない）。
  const [sampleId, setSampleId] = useState<string | null>(defaultSampleId);

  const baseText = useMemo(
    () => samples.find((sample) => sample.file === BASE_FILE)?.text ?? null,
    [samples],
  );

  const selectSample = useCallback(
    async (id: string): Promise<void> => {
      const sample = samples.find((item) => item.id === id);
      if (!sample || baseText === null) return;
      setSampleId(id);
      // 押した状態をURLに残す。**履歴には積まない**——見本の切り替えは
      // 「戻る」で辿りたい操作ではなく、共有と撮影のための現在地である。
      window.history.replaceState(null, "", `#${id}`);
      const read = async (name: string, text: string): Promise<Slot> => {
        try {
          return { report: await readReport(name, text), error: null };
        } catch (error) {
          return { report: null, error: error instanceof Error ? error.message : String(error) };
        }
      };
      const [nextLeft, nextRight] = await Promise.all([
        read(BASE_FILE, baseText),
        read(sample.file, sample.text),
      ]);
      setLeft(nextLeft);
      setRight(nextRight);
    },
    [baseText, samples],
  );

  // **書き出し時に計算した digest を、そのまま信じない。** この画面の主張は
  // 「読む側が計算し直して初めて、その digest がその中身から出たものだと言える」
  // なので、開いた後に必ずブラウザ側で計算し直す（`crypto.subtle`）。
  // URLで見本が指定されていれば、そちらへ切り替える。
  useEffect(() => {
    const fromHash = findSample(sampleIdFromHash());
    void selectSample(fromHash ? fromHash.id : defaultSampleId);

    // **`#` だけが変わる移動では、この画面は作り直されない。** 開いたままの
    // ページに別の見本のURLを貼られた場合や、履歴を戻った場合がこれにあたり、
    // 拾わないと**URLと表示が食い違ったまま**になる（実測して見つけた）。
    const onHashChange = (): void => {
      const target = findSample(sampleIdFromHash());
      if (target) void selectSample(target.id);
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, [defaultSampleId, selectSample]);

  async function load(file: File, set: (slot: Slot) => void): Promise<void> {
    setSampleId(null);
    // 自分のファイルを読んだ時点で、URLの見本指定は現在地を表さなくなる。
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
    try {
      const report = await readReport(file.name, await file.text());
      set({ report, error: null });
    } catch (error) {
      // **例外を握り潰さない。** 利用者が渡すのは任意のファイルなので、
      // 何が悪かったのかを言わないと「反応しない画面」になる。
      set({ report: null, error: error instanceof Error ? error.message : String(error) });
    }
  }

  function clear(set: (slot: Slot) => void): void {
    setSampleId(null);
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
    set(EMPTY);
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

      <div className="samples">
        <span className="note">見本で試せる（左＝基準）:</span>
        {samples.map((item) => (
          <button
            key={item.id}
            type="button"
            className={sampleId === item.id ? "chosen" : undefined}
            aria-pressed={sampleId === item.id}
            onClick={() => void selectSample(item.id)}
          >
            {item.label}
          </button>
        ))}
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
            {sampleId !== null && (
              // **見本であることを隠さない。** 自動で埋めた画面は、
              // 「この人の実際の検証結果」と読まれうる。
              <p className="note sample-note">
                表示しているのは<strong>同梱の見本</strong>です（Python 側が生成したもの）。
                自分のレポートは下の受け口へ読み込ませてください。
              </p>
            )}
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

      <h2>突き合わせている2つのレポート</h2>
      <div className="slots">
        <ReportSlot
          label="左のレポート"
          report={left.report}
          error={left.error}
          onFile={(file) => void load(file, setLeft)}
          onClear={() => clear(setLeft)}
        />
        <ReportSlot
          label="右のレポート"
          report={right.report}
          error={right.error}
          onFile={(file) => void load(file, setRight)}
          onClear={() => clear(setRight)}
        />
      </div>

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
