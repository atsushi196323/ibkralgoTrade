# 開発の指針

**このプロジェクトの設計判断・検証結果・不変条件は [`docs/DECISIONS.md`](docs/DECISIONS.md) にある。**
閾値をその値にした理由、検証して不採用にした仮説とその数字、実運用で壊れた
経緯から導いた不変条件を、いずれも日付と実測値つきで記録している。
**コードを変えるときは、同じコミットでそちらも更新すること**——放置された
記述は「古い」のではなく**嘘**になり、次に読む者を誤った前提で作業させる。

## このリポジトリは2つの言語でできている

| | 中身 | 指針 |
| --- | --- | --- |
| ルート（Python） | 売買ボット本体・戦略・バックテスト・運用スクリプト | [`docs/DECISIONS.md`](docs/DECISIONS.md) |
| [`web/`](web/) （TypeScript / Next.js） | 検証レポートを2つ突き合わせるビューア | [`web/README.md`](web/README.md) と `web/CLAUDE.md` |

**`web/` はボットの口座データ（`logs/`）を一切読まない。** 読むのは
`python -m backtest.run --report out.json` が書く検証レポートだけである。
口座番号・建値・資金を画面へ出す機能を足してはならない（公開リポジトリであり、
ビューアは静的書き出しで誰でも開ける）。

## 2つの言語をまたぐ唯一の契約 — `result_digest`

レポートの `result_digest` は `SHA-256(入力の指紋 + パラメータ + 結果)` で、
**Python が計算して書き、TypeScript が独立に計算して照合する**。同じ正規化を
2か所に実装しているので、**片方だけを変えると必ず食い違う**。

| 実装 | 場所 |
| --- | --- |
| 正規化と digest（Python） | `backtest/report.py` の `_canonical` / `compute_digest` |
| 正規化と digest（TypeScript） | `web/lib/canonical.ts` |
| 見本の生成 | `python -m scripts.make_web_fixtures` → `web/fixtures/` |
| 番人（Python側） | `tests/test_web_fixtures.py` |
| 番人（TypeScript側） | `web/lib/canonical.test.ts` |

**正規化に手を入れるときの手順は必ずこの順で行う。**

1. `backtest/report.py` を直す
2. `python -m scripts.make_web_fixtures` で見本を作り直す
3. `web/lib/canonical.ts` を同じ規則に揃える
4. **両方のテストを通す**（片方だけ緑にして先へ進まない）

digest に**入れてはならないもの**が3つある。実行時刻・実行環境
（Python / pandas / numpy / gitコミット）・出力の設定（`--verbose` / `--report`）。
理由は「検証の再現性」節（`docs/DECISIONS.md`）にある。

## 終える前に走らせるもの

CIと同じ検査を手元で回すこと。**どれか1つでも落ちている状態で報告しない。**

```bash
python -m ruff check . && python -m mypy && shellcheck scripts/*.sh && python -m pytest -q
cd web && npm run lint && npm run typecheck && npm test
```

@docs/DECISIONS.md
