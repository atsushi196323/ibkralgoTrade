/**
 * 見本レポートを `public/samples/` へ複製する（`prebuild` から呼ばれる）。
 *
 * **見本の正本は `fixtures/` である**（Python が
 * `python -m scripts.make_web_fixtures` で生成し、両言語のテストが読む）。
 * 公開した画面から取得できる場所へ置きたいだけなので、正本を移さずに複製する。
 * 移すと、テストが読むファイルと公開物が同じ場所を指し、
 * **ビルドの都合でテストの入力が変わりうる**状態になる。
 */

import { cpSync, mkdirSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const source = join(here, "..", "fixtures");
const target = join(here, "..", "public", "samples");

rmSync(target, { recursive: true, force: true });
mkdirSync(target, { recursive: true });
cpSync(source, target, { recursive: true });

console.log(`見本を複製しました: fixtures/ -> public/samples/`);
