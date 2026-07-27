# IBKR Automated Trading System - Project Requirements & Guidelines

## 1. プロジェクト概要 (Project Overview)

本プロジェクトは、Interactive Brokers (IBKR) のAPIを利用し、米国株のデイトレード・スイングトレードを軸とした自動取引を行うPythonベースのアルゴリズム取引システムである。
市場がパニックに陥った際（恐怖感が高まった際）のプルバックを狙う戦略など、短期〜中期の投資ロジックをシステム化することを目的とする。

**現在の開発フェーズ:** ドライラン（発注シミュレーション）。実発注 (`placeOrder`) は無効化されており、シグナル判定・ポジション管理・損益記録までを検証している段階である。

**スコープ外（将来検討）:** ハイグロース銘柄に対する長期コールオプション（LEAPS）の動的取得は、デイトレード・スイングトレードとは時間軸が根本的に異なるため、現フェーズの開発対象外とする。オプション関連のロジックを追加する場合は、別トラックとして扱うこと。

**スコープ外（将来検討）:** 日本株（東証）の自動取引は現フェーズの開発対象外だが、将来的に対応予定。`core/market_hours.py` の東証売買立会時間・休場日判定（`is_japan_regular_trading_hours`, `is_japan_market_holiday`等）は、その先行実装として既に用意されている（現時点ではmain.py等どこからも呼び出されていない未使用コードだが、意図的に残しているものであり削除しないこと）。

## 2. 技術スタック (Tech Stack)

- **言語:** Python 3.10+
- **ブローカーAPI:** `ib_insync` (公式 `ibapi` ではなく、非同期処理に最適化されたラッパーを使用)
- **データ処理:** `pandas`, `numpy`
- **休場日判定:** `holidays` (NYSE/東証の祝日カレンダー。移動祝日や振替休日を自前計算しない)
- **環境変数管理:** `python-dotenv`
- **テスト:** `pytest` (`requirements-dev.txt`)
- **接続先:** TWS (Trader Workstation) または IB Gateway のペーパートレード環境（`core/connection.py` はib_insyncの標準ソケットAPIのみに依存しており、TWS固有のGUI機能には依存しないため、どちらでも動作する）

## 3. 実行環境と設定 (Environment & Config)

設定はすべて `.env` から読み込む（`core/connection.py`）。`.env` はGit管理外。

| 環境変数 | 既定値 | 説明 |
| --- | --- | --- |
| `IBKR_HOST` | `127.0.0.1` | 接続先ホスト |
| `IBKR_PORT` | `7497` | ポート番号（下表参照） |
| `IBKR_CLIENT_ID` | `1` | クライアントID（他プロセスと重複させないこと） |
| `IBKR_MARKET_DATA_TYPE` | `3` | マーケットデータ種別。1=LIVE / 2=FROZEN / 3=DELAYED / 4=DELAYED_FROZEN |

**ポート番号:**

| | ペーパー | 本番 |
| --- | --- | --- |
| TWS | `7497` | `7496` |
| IB Gateway | `4002` | `4001` |

本番用ポート（`7496` / `4001`）への接続は「7. 開発時の禁止事項」により禁止。ペーパー環境の切り替えは `.env` の変更のみで完結し、コード修正は不要である。

`IBKR_MARKET_DATA_TYPE` の既定を遅延データ(3)にしているのは、ペーパー口座がリアルタイムデータの購読契約を持たないことが多いため。購読契約がある場合のみ `1` に切り替える。

**稼働時の出力（すべて `logs/` 配下、Git管理外）:**

- `logs/trade_journal.csv` — 決済ごとの実現損益・R倍率・為替レートの記録
- `logs/positions.json` — 保有ポジションの状態（再起動時に復元される）

## 4. ディレクトリ構成 (Architecture)

```
main.py                     エントリーポイント。非同期イベントループと全体のオーケストレーション
core/
  connection.py             TWS/IB Gatewayへの接続・切断・再接続（指数的バックオフ）
  market_hours.py           NYSE/東証のレギュラーセッション判定・休場日判定
  pacing.py                 IBKRのリクエスト制限を守るレートリミッター
data/
  market_data.py            ヒストリカルバー・現在値の取得（IBKR APIの唯一の入口）
  cache.py                  日足バー・コントラクトのキャッシュ（重複リクエストの排除）
  fundamentals.py           スキャナー・ファンダメンタルズ(PER)取得の低レベルラッパー
strategy/
  pullback.py               押し目買いシグナルの判定（移動平均からの下方乖離）
  screener.py               時価総額・PER・長期トレンドによる銘柄スクリーニング
  exit_signal.py            決済判定（利確・損切り・トレーリングストップ）
execution/
  order_manager.py          注文の組み立て・発注（現在はドライラン）
  position_manager.py       保有ポジションの状態管理・永続化・ブローカー同期
  position_sizing.py        リスクベースの発注数量計算
  account.py                口座資金（NetLiquidation）の取得
  trade_journal.py          実現損益・勝率・R倍率の記録と集計
  tax_export.py             確定申告向けCSVの出力（税理士へ渡す用）
backtest/
  engine.py                 ヒストリカルデータでの戦略シミュレーション
  metrics.py                勝率・プロフィットファクター・最大DD・シャープレシオ
  walk_forward.py           ウォークフォワード検証（過剰最適化の検出）
  run.py                    バックテスト/ウォークフォワードのCLI
scripts/
  check_market_data.py      実機でマーケットデータの取得経路を切り分ける診断CLI
  export_tax_report.py      確定申告用CSVを出力するCLI
tests/                      単体テスト。IBKRへの実接続は不要（すべてモック）
conftest.py                 pytest共通設定
```

**レイヤーの責務:**

- `data/` はIBKR APIとの境界。**IBKRのデータ取得APIを直接呼ぶのはこのパッケージのみ**とし、他パッケージは必ずここを経由する（理由は「6. IBKR API利用上の制約」）
- `strategy/` は純粋な判定ロジック。IBKRにもI/Oにも依存させず、DataFrameと数値だけを扱う（バックテストと本番で同じコードを使うため）
- `execution/` は注文・ポジション・記録
- `backtest/` は `strategy/` を再利用してヒストリカル検証を行う。ライブ運用の記録である `execution/trade_journal.py` とは別物

## 5. 取引ロジックの要件 (Trading Requirements)

パラメータの実体は `main.py` 冒頭の定数にある。**閾値を変更する場合はこの節も併せて更新すること。**

### エントリー

移動平均からの下方乖離（押し目）を検知して買う。日足で先に判定し、シグナルが無ければ短期足で判定する。

| | スイング | デイトレード |
| --- | --- | --- |
| 参照する足 | 日足（60日分） | 5分足（2日分） |
| 移動平均 | 20本 | 20本 |
| 買いシグナルの乖離率 | -5%以下 | -2%以下 |
| 利確 | +10% | +3% |
| 損切り | -5% | -1.5% |
| トレーリングストップ | 高値から-5% | 高値から-2% |

デイトレード判定で建てたポジションは、米国東部時間 **15:55** に強制決済する（オーバーナイトのギャップリスクを避けるため、大引け16:00より手前に置いている）。スイングは持ち越す。

ブローカー同期で発見した出所不明のポジションは、より安全側であるスイング基準で決済判定する。

### ブローカー同期の対象

`ib.reqPositionsAsync()` は全口座・全アセットクラスの建玉を返すため、取り込む対象を **米国株（`secType="STK"`）・USD建て・ロング（数量が正）** に限定する。シンボル文字列だけで突き合わせると、AAPLのコールオプションや他国上場の同名株を現物ポジションとして誤って取り込む。ショートは本Botがロング専用であるため対象外とする（取り込むと決済時のSELL数量が負になり発注処理が落ちる）。

### リスク管理

- **1トレードあたりのリスク:** 口座資金の **1%**。損切り幅から逆算して発注数量を決める（固定ロットではない）
- **最大同時保有ポジション数:** **5銘柄**。銘柄ごとの1%リスクが積み上がるのを防ぐ
- **日次サーキットブレーカー:** 当日の実現損益（**手数料控除後**）が口座資金の **-3%** に達したら新規エントリーを停止する。既存ポジションの決済判定（損切り等）は継続する
- **最大ロット数:** ドライラン検証中の安全弁として `execution/order_manager.MAX_POSITION_SIZE`（現在10株）でハードクランプする

### 銘柄選定

取引時間の最初のサイクルで1日1回スクリーニングし、ウォッチリストを入れ替える。

- IBKRスキャナーで時価総額 20億〜2000億ドルの母集団を抽出（`MOST_ACTIVE`、最大50銘柄）
- PER 15倍以下（赤字銘柄は除外）
- 200日移動平均を下回る明確な下降トレンドの銘柄を除外（弱い正の相関に基づく緩やかなフィルター）
- 最終的に**上位10銘柄まで**に絞る（理由は「6. IBKR API利用上の制約」）

スクリーニングに失敗した場合・結果が0件の場合は、既存のウォッチリスト（初回は `main.WATCHLIST` の固定リスト）を維持して稼働を継続する。

スクリーニングは過去時点のPER（point-in-time データ）をIBKRから遡って取得できないため、`backtest/` では検証できない。ドライラン運用の結果を見て閾値を調整すること。

## 6. IBKR API利用上の制約 (IBKR API Constraints)

**この節の制約は、破ってもエラーにならず静かに機能停止する。** 変更時は特に注意すること。

### 6.1 ヒストリカルデータのペーシング制限

IBKRは「**10分あたり60リクエスト**」を超えると pacing violation を返す。厄介なのは、ib_insyncの既定設定 (`IB.RaiseRequestErrors=False`) ではこれが例外にならず**空のバー列**として返る点で、呼び出し側からは「データが無い銘柄」と区別がつかない。結果、ボットはシグナルを出さないまま延々と動き続ける。

対策は3層:

1. **`core/pacing.py` のレートリミッター** — `data/market_data.get_historical_bars_async` が発行前に必ず枠を確保する（上限は安全マージンを引いた55件/10分）
2. **`data/cache.py` のキャッシュ** — 日足バーは1取引日に1本しか増えないため取引日単位でキャッシュ。コントラクトの `qualifyContractsAsync` 結果もシンボル単位でキャッシュ
3. **監視銘柄数とポーリング間隔** — 以下の不変条件を満たすこと

```
MAX_WATCHLIST_SIZE × (600 / POLL_INTERVAL_SECONDS) ≦ 60
```

現在は10銘柄・180秒（約33件/10分）。**この2つは連動しているため、片方だけを変更してはならない。** `tests/test_main.py` の `test_poll_interval_keeps_watchlist_within_ibkr_pacing_limit` が番人として機能している。

なお日中足（5分足）はデイトレードのシグナルそのものなので、キャッシュしてはならない。

### 6.2 マーケットデータの購読状況に依存しないこと

ペーパー口座はリアルタイムデータの購読契約を持たないことが多い。特に `ib.reqTickersAsync()` は内部で `snapshot=True` を使うが、**IBKRはスナップショット要求に遅延データを適用しない**ため、購読契約が無い口座では価格が取得できない。

そのため `data/market_data.get_current_price_async` は3段のフォールバック連鎖になっている:

1. ストリーミング (`reqMktData`, `snapshot=False`) — 遅延データでも配信される
2. スナップショット (`reqTickersAsync`) — 購読契約があれば1往復で速い
3. ヒストリカルバーの最終終値 — 購読権限が無くても取れることが多い

口座で実際にどの経路が使えているかは `python -m scripts.check_market_data` で確認できる。

### 6.3 必ず経由すべき入口

上記2つの対策を迂回しないため、以下を直接呼んではならない。

| 直接呼んではならないAPI | 代わりに使うもの |
| --- | --- |
| `ib.reqHistoricalDataAsync()` | `data.market_data.get_historical_bars_async()` |
| `ib.reqTickersAsync()` / `ib.reqMktData()` | `data.market_data.get_current_price_async()` |
| `ib.qualifyContractsAsync()` | `data.market_data.qualify_stock_async()` |

さらに、**メインループ内**では `data.cache` の `ContractCache` / `DailyBarCache` を経由すること（`main.MarketDataCaches` として `main()` で1つ生成し、サイクル間で共有する）。`backtest/run.py` や `scripts/` のような単発処理はキャッシュを挟まず直接呼んでよい。

### 6.4 その他

- ストリーミング購読 (`reqMktData`) を張ったら必ず `cancelMktData` で解除する。張りっぱなしにするとIBKRの同時購読数上限を食い潰す
- 為替 (`Forex`) には出来高を伴う取引が存在しないため、ヒストリカルバーの `whatToShow` に `TRADES` を指定してはならない（`MIDPOINT` を使う）
- IBKRはデータ未受信のフィールドをNaNや0で埋めてくる。価格として採用する前に必ず「NaNでない、かつ正の数」を検証する

## 7. コーディングガイドライン (Coding Guidelines)

Claude CodeおよびCursorは、以下のルールに従ってコードを生成・修正すること：

1. **非同期処理の徹底:**
   `ib_insync` のAPIコール（接続、データ取得、発注）はすべて `async/await` パターンを用いて非同期で記述すること。ブロッキング処理は避ける。
2. **堅牢なエラーハンドリング:**
   IBKRのサーバーは週末のメンテナンスや一時的な切断が発生しやすい。接続エラー時のリトライ処理（指数的バックオフなど）を必ず実装すること。銘柄単位の処理エラーは握り潰して次の銘柄へ進み、監視ループ全体を落とさない。
3. **型アノテーション (Type Hints):**
   関数の引数および戻り値には、Pythonの型ヒントを明記し、可読性と保守性を高めること。
4. **コントラクトの明確化:**
   株式(`Stock`)のコントラクトは、`data.market_data.qualify_stock_async()` で曖昧さを排除してから使うこと（内部で `ib.qualifyContractsAsync()` を呼んでいる）。メインループでは `ContractCache` を経由する。
5. **ログの出力:**
   `print` だけでなく、標準の `logging` モジュールを使用して、注文状況やエラーのトレースが後から確認できるようにすること。
6. **テスト:**
   ロジックの変更には単体テストを添えること。IBKRへの実接続に依存させず、`unittest.mock` でモック化する（既存テストはすべて実接続なしで数秒以内に完走する）。実時間の `sleep` をテストに持ち込まないこと。
7. **コメントは「なぜ」を書く:**
   本プロジェクトの制約（ペーシング制限、購読権限、ドライラン前提など）は自明でないものが多い。コードから読み取れる「何を」ではなく、その判断に至った理由を残すこと。

## 8. よく使うコマンド (Commands)

```bash
# パッケージインストール
pip install -r requirements.txt          # 実行用
pip install -r requirements-dev.txt      # 開発用（pytestを含む）

# 実行
python main.py

# テスト
python -m pytest -q

# バックテスト / ウォークフォワード検証
python -m backtest.run --symbol RIVN --duration "2 Y" --mode walk-forward
python -m backtest.run --symbol RIVN --duration "2 Y" --mode backtest

# マーケットデータ取得経路の診断（IB Gateway接続時、米国市場の取引時間内に実行）
python -m scripts.check_market_data
python -m scripts.check_market_data --symbol MSFT --wait 10

# 確定申告用CSVの出力（既定は前年分。IBKR接続不要）
python -m scripts.export_tax_report
python -m scripts.export_tax_report --year 2026
python -m scripts.export_tax_report --all-years
```

## 9. 開発時の禁止事項 (Constraints)

- 実資金の口座ポート (`7496` または `4001`) にハードコーディングで接続してはならない。必ず `.env` ファイル経由でポート番号を読み込むこと。
- ロジックの検証が完了するまでは、発注処理 (`placeOrder`) を無効化するか、厳格な最大ロット数制限（Max Position Size）をハードコードで設けること。
- **最大ロット数制限を決済 (SELL) に適用してはならない。** 呼び出し側は決済成立を前提にローカルのポジションを閉じるため、SELLの数量を丸めるとブローカー側に建玉が残ったまま追跡だけが消え、損切りもトレーリングストップも効かない未追跡ポジションが生まれる。制限は新規建て (BUY) のみに適用すること。
- 「6. IBKR API利用上の制約」で定めた入口を迂回して、IBKRのデータ取得APIを直接呼んではならない。
- `logs/` 配下（取引履歴・ポジション状態）をコミットしてはならない。実際の取引記録であり、`.gitignore` 済み。
- 実発注 (`placeOrder`) を有効化する際は、`Fill.commissionReport.commission` を `TradeJournal.record_trade()` に渡すこと。現在は実約定が無いため手数料を0.0固定で記録しており、そのままでは損益と日次サーキットブレーカーが手数料分だけ楽観的になる。
- Gitコミットを作成する際、コミットメッセージに `Co-Authored-By: Claude` などClaudeをAuthor/Co-Authorとして記載してはならない。コミットは常に開発者本人の作者情報のみとすること。
