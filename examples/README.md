# サンプルデータ

**IBKR の口座もAPIキーも無しに、README のコマンドをそのまま動かすためのもの。**

| | |
| --- | --- |
| `bars/` | 検証対象の日足（10年・6銘柄）。`--csv` / `--csv-dir` に渡す |
| `index/SPY.csv` | ベンチマーク。`scripts/measure_alpha --benchmark` に渡す |

**ベンチマークを `bars/` に置いていないのは、`--csv-dir` のグロブが直下の
`*.csv` を全部拾うためである。** 混ぜると SPY が検証対象の1銘柄として
成績に混入する。

出典は Yahoo Finance（`scripts/fetch_bars.py` が yfinance 経由で取得）。
**検証を再現するためのサンプルであり、データそのものの再配布を意図した
ものではない。** 全銘柄を取り直すには:

```bash
python -m scripts.fetch_bars --symbols-file universe.txt --period 10y
```

**本番の検証は 42銘柄で行っている。** この6銘柄は動作確認用で、
超過リターンの数値は 42銘柄の結果（+0.047%/trade）とは一致しない
（この6銘柄では -1.026%）。再現できるのは数値ではなく測り方である。
