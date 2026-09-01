# 米国株 自動売買システム（Interactive Brokers API）

[![CI](https://github.com/atsushi196323/ibkralgoTrade/actions/workflows/ci.yml/badge.svg)](https://github.com/atsushi196323/ibkralgoTrade/actions/workflows/ci.yml)

Interactive Brokers の API を用い、米国株の押し目買い（日足の移動平均からの下方乖離）を検知して発注し、利確・損切りをブローカー側の待機注文（ブラケット注文）として置く自動売買システム。VPS 上で systemd により無人稼働する。

**現在はペーパー口座で注文層を検証している段階であり、実資金は投入していない。**

## このプロジェクトの結論

**このシステムは、稼げないという結論に到達している。それを自分で証明したことが成果物である。**

当初はプロフィットファクター 1.18 という数字を根拠に「エッジがある」と判断していた。しかしその指標は**ゼロを基準**にしており、「現金を持っているより良いか」しか答えていなかった。判断に必要なのは「**市場を持っているより良いか**」である。

同じトレードから「同じ日付区間で S&P500 (SPY) を持っていた場合」を差し引くと、こうなった。

| | 1トレードあたり |
| --- | --- |
| 戦略の純損益 | +0.492% |
| 同期間の SPY | +0.445% |
| **超過リターン（アルファ）** | **+0.047%（t = 0.09）** |

**+0.49% のうち 96% は、保有期間中に市場が上げたぶんだった。** 残りはゼロと区別がつかない。

その後、日足価格から作れる候補シグナルを 9 本、対照群つき・生存バイアスの感度つきで測ったが、採用基準（一貫性 8 項目）を通過したものは無い。最も強い横断モメンタムでも 7/8 で、生存バイアス耐性が不合格だった。

**この結論に至る過程で作った測定基盤が、このリポジトリの中身である。**

## 30秒で確かめる

サンプルの日足（10年・6銘柄）を同梱してあるので、IBKR の口座もAPIキーも不要で動く。

動作環境は **Python 3.11**（`pyproject.toml` の ruff / mypy もこの版に合わせてある）。

```bash
pip install -r requirements-dev.txt

# 番人テスト 938件（IBKRへの実接続なし・全てモック）
python -m pytest -q

# 単一銘柄のバックテスト（コスト込み・1秒未満）
python -m backtest.run --csv examples/bars/AAPL.csv --mode backtest --initial-equity 1220

# 銘柄横断のウォークフォワード（過剰最適化の検出。約3分）
python -m backtest.run --csv-dir examples/bars --initial-equity 1220
```

**「市場を持っていた場合」と比べる**——採否はこれで判断する。

同梱サンプルは6銘柄なので、上表（42銘柄で +0.047%）とは数字が違う（この6銘柄では **-1.026%** とさらに悪い）。**再現できるのは結論の数値ではなく測り方である。**

```bash
python -m scripts.measure_alpha --csv-dir examples/bars \
    --benchmark examples/index/SPY.csv --initial-equity 1220 --watchlist-size 0
```

## 規模

| | |
| --- | --- |
| 本体のコード | 14,756行 |
| テストコード | 14,455行 / **938件** — IBKRへの実接続なし（全てモック）・約2秒で完走 |
| カバレッジ | 87%（`main.py` 91% / `execution/order_manager.py` 96%） |
| 型注釈 | 戻り値 420関数中 413（98%）・mypy 通過 |
| 構成 | 6パッケージ（接続 / データ取得 / 戦略判定 / 執行 / バックテスト / 運用スクリプト） |
| 稼働 | VPS（Ubuntu）+ systemd による無人運用。平日22:15起動・翌06:05締め（日本時間） |
| CI | GitHub Actions で lint（ruff）・型検査（mypy）・テストを、稼働環境と同じ Linux・pandas 2系/3系で実行 |

## 構成

```mermaid
flowchart TD
    subgraph entry[エントリーポイント]
        M[main.py<br/>非同期の監視ループ]
    end
    subgraph core_[core/ 基盤]
        C[connection.py<br/>再接続・指数バックオフ]
        P[pacing.py<br/>10分60件の制限を守る]
        H[market_hours.py<br/>立会時間・休場日]
        L[logging_setup.py<br/>定型通知の抑制]
    end
    subgraph data_[data/ IBKR APIとの境界]
        D[market_data.py<br/>IBKR取得の唯一の入口]
        CA[cache.py<br/>日足は取引日単位]
    end
    subgraph strategy_[strategy/ 純粋な判定]
        S1[pullback.py 押し目]
        S2[exit_signal.py 決済]
        S3[momentum.py 横断ランク]
        S4[screener.py 銘柄選定]
    end
    subgraph exec_[execution/ 執行と記録]
        E1[order_manager.py<br/>ブラケット・OCA・約定の読み取り]
        E2[position_manager.py<br/>状態の永続化・ブローカー同期]
        E3[trade_journal.py / fill_log.py<br/>損益と乖離の記録]
    end
    subgraph bt[backtest/ 検証]
        B1[engine.py / walk_forward.py]
        B2[portfolio.py 口座水準]
        B3[benchmark.py 超過リターン]
        B4[robustness.py 採否の判定]
        B5[survivorship.py 生存バイアス]
    end

    M --> C & D & S1 & S2 & E1 & E2
    D --> CA
    S1 -.同じ判定ロジックを共有.-> B1
    S2 -.同上.-> B1
    B1 --> B2 --> B3 --> B4
    B5 --> B4
```

**`data/` が IBKR API との唯一の境界である。** ペーシング制限とキャッシュを迂回させないため、他パッケージから IBKR のデータ取得APIを直接呼ぶことを禁じている。`strategy/` は I/O に依存しない純粋関数で、**ライブとバックテストで同じコードが動く**。

### ここから読む

| 見たいもの | ファイル |
| --- | --- |
| **設計判断と検証結果のすべて** | [`docs/DECISIONS.md`](docs/DECISIONS.md) |
| 静かに壊れる注文層をどう固めたか | [`execution/order_manager.py`](execution/order_manager.py) |
| 採否の判定（一貫性8項目） | [`backtest/robustness.py`](backtest/robustness.py) |
| 生存バイアスを「消せないので上限で縛る」 | [`backtest/survivorship.py`](backtest/survivorship.py) |
| 「なぜ1件も建たなかったか」を1画面で | [`scripts/daily_report.py`](scripts/daily_report.py) |

## このシステムが難しい理由

- **主要な故障が例外を出さない。** データ購読の権限が無いとスクリーニングは空を返し、APIのレート制限違反は例外ではなく空のバー列として返り、注文の拒否は警告としてしか通知されない。いずれも異常終了しないため、「静かに縮退したまま動き続ける状態」をどう検知するかが設計の中心になる。
- **ドライランでは検証できない層がある。** 呼値の丸め、ブラケット注文の送信順序、OCA（一方が約定したら他方を取り消す）の連動、親子注文の関係の成立可否は、実際に発注しない限り1件も観測できない。実際に、取消要求の 1ミリ秒後に成行を出して 0.2秒で拒否される競合や、OCAグループ名がブローカー側で書き換わるために修正が拒否される挙動を、ペーパー発注で実測して不変条件に落としている。
- **バックテストは簡単に嘘をつく。** 過剰最適化・ルックアヘッド・生存バイアス・手数料の過小評価。銘柄横断・コスト込み・ウォークフォワードで検証し、エッジが無いと判定した仮説は、その根拠とともに無効化して記録している。

## 稼働時の出力

引け後に1取引日のサマリを出す（`python -m scripts.daily_report`）。**損益ではなく「なぜ建たなかったか」を読むための道具である。**

```
===== 2026-08-26 (米国東部時間) の稼働サマリ =====
口座資金: 1183.59 USD

--- 注文層の検証 ---
  残る未観測は利確LMTの約定1点。期限 2026-09-30 まで あと35日。
  逆指値側のOCA取消連動は2026-08-18 / 08-24に実測済み。

--- 新規建て ---
0件。以下は「なぜ建たなかったか」の材料。
  乖離率の判定まで進んだ銘柄が無い（監視サイクルは回っている）。
  見送り: 同時保有数の上限 (77回)
```

## 免責

- 本リポジトリは技術ポートフォリオであり、投資助言ではない。
- 本コードの利用によって生じたいかなる損失についても、作者は一切の責任を負わない。
