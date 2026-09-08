import { readFile } from "node:fs/promises";
import path from "node:path";

import { Comparer, type SampleSource } from "@/components/Comparer";
import { readReport } from "@/lib/report";
import { BASE_FILE, DEFAULT_SAMPLE_ID, findSample, SAMPLES } from "@/lib/samples";

/**
 * **開いた瞬間に、この画面の答えが見えている状態にする。**
 *
 * 空の受け口を先に見せると、読み手は「JSONを選べと言われた」で離脱する。
 * この画面の値打ちは差分が並んだ結果の側にあるので、書き出しの時点で既定の
 * 見本を突き合わせておき、静的HTMLにその結果ごと入れる。
 *
 * **見本は取得しに行かず、ビルド時に埋め込む**（1件あたり2KB弱）。開いた
 * 直後に空の画面が一瞬出ることが無く、経路も1本で済む——`fetch` にすると
 * 「既定の見本」と「押して切り替えた見本」で読み込み方が二手に分かれる。
 *
 * **ただしここで計算した digest は、そのまま結論にしない。** ブラウザ側で
 * 必ず計算し直す（`components/Comparer.tsx`）。読む側が計算し直して初めて
 * 「その digest がその中身から出た」と言える、というのがこの画面の主張である。
 */
async function loadSamples(): Promise<readonly SampleSource[]> {
  const directory = path.join(process.cwd(), "fixtures");
  return Promise.all(
    SAMPLES.map(async (sample) => ({
      ...sample,
      text: await readFile(path.join(directory, sample.file), "utf8"),
    })),
  );
}

export default async function Page() {
  const samples = await loadSamples();

  const initial = findSample(DEFAULT_SAMPLE_ID);
  if (!initial) throw new Error(`既定の見本 ${DEFAULT_SAMPLE_ID} が SAMPLES にありません。`);

  const textOf = (file: string): string => {
    const found = samples.find((sample) => sample.file === file);
    if (!found) throw new Error(`見本 ${file} を読み込めませんでした。`);
    return found.text;
  };

  const [initialLeft, initialRight] = await Promise.all([
    readReport(BASE_FILE, textOf(BASE_FILE)),
    readReport(initial.file, textOf(initial.file)),
  ]);

  return (
    <Comparer
      samples={samples}
      defaultSampleId={DEFAULT_SAMPLE_ID}
      initialLeft={initialLeft}
      initialRight={initialRight}
    />
  );
}
