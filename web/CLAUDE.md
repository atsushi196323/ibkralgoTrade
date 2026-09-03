# `web/` の指針

レポートの突き合わせビューア（Next.js / TypeScript）。
**何をする画面で、なぜ digest を計算し直すのか**は [`README.md`](README.md) にある。

## ここで守ること

- **口座データを扱わない。** 読むのは検証レポート（`backtest/run.py --report`）だけ。
  `logs/` の建玉・損益・口座番号を読む機能を足してはならない。静的書き出しで
  公開される画面であり、**送信先を持たないことがその情報が漏れないことの保証**になっている
- **`lib/canonical.ts` は Python（`backtest/report.py`）の写しである。**
  片方だけ直すと `lib/canonical.test.ts` か `tests/test_web_fixtures.py` が落ちる。
  そのときテストを緩めてはならない——**落ちたこと自体が、契約が破れたという知らせ**である
- **`fixtures/` を手で編集しない。** Python 側が生成する
  （`python -m scripts.make_web_fixtures`）。手で直すと、2言語が同じ規則に
  合意していることを確かめる根拠が消える
- **例外を握り潰さない。** 利用者が渡すのは任意のファイルなので、読めなかった
  理由を画面に出さないと、症状は「何も起きない」になる（Python側と同じ方針）
- **`JSON.parse` でレポートを読まない。** 整数と実数の区別が消え、digest がずれる
  （`lib/json-source.ts` の冒頭に理由がある）

## コマンド

```bash
npm install
npm run dev        # http://localhost:3000
npm test           # Python が書いた見本の digest を再計算して照合する
npm run lint       # eslint（型情報つき）
npm run typecheck  # tsc --noEmit（strict + noUncheckedIndexedAccess）
npm run build      # 静的書き出し（out/）
```

## 依存

**追加は最小限にすること。** 現在の実行時依存は `next` / `react` / `react-dom` だけで、
JSONの読み取り・正規化・SHA-256・差分はすべて自前と標準API（`crypto.subtle`）で
書いてある。**この画面の中身は「他所のライブラリが正しく丸めてくれること」に
依存できない**——Python と桁単位で一致させる必要があるためである。
