/**
 * 画面が最初から見せる見本の定義。
 *
 * **見本は Python 側が生成したものである**（`python -m scripts.make_web_fixtures`）。
 * ここで作った偽物ではない——偽物を置くと、この画面が確かめていること
 * （**Python が書いた digest を計算し直して一致させる**）を、自分で作った値で
 * 確かめることになる。
 *
 * `id` は URL の `#` に出る。**押した状態をそのまま共有・撮影できるようにする**
 * ためで、これが無いと README の画像と公開先の実物が別の状態を指す。
 */
export interface SampleDefinition {
  readonly id: string;
  readonly label: string;
  readonly file: string;
}

/** 左（基準）に置くレポート。どの見本を選んでも左はこれで固定する。 */
export const BASE_FILE = "report_base.json";

export const SAMPLES: readonly SampleDefinition[] = [
  { id: "same", label: "同じ入力・同じ設定", file: BASE_FILE },
  { id: "input", label: "入力だけ違う", file: "report_changed_input.json" },
  { id: "parameters", label: "設定だけ違う", file: "report_changed_parameters.json" },
  { id: "results", label: "結果だけ違う", file: "report_changed_results.json" },
  {
    id: "environment",
    label: "環境だけ違う（digest は一致）",
    file: "report_same_digest_other_environment.json",
  },
];

/**
 * 何も指定されなかったときに開く見本。
 *
 * **「結果だけ違う」を既定にするのは、この画面が存在する理由そのものだからである**
 * ——入力もパラメータも同じなのに結果が違う、という一件だけが「コードが変わったか、
 * 実装に環境依存が入り込んだ」を意味する。「入力だけ違う」は、結果が違って当然の
 * ケースなので、開いた人に何も教えない。
 */
export const DEFAULT_SAMPLE_ID = "results";

export function findSample(id: string | null): SampleDefinition | null {
  if (id === null) return null;
  return SAMPLES.find((sample) => sample.id === id) ?? null;
}
