# VPS(Linux)への移行手順

macOSでの運用には**Macが起動していなければその日の取引が丸ごと落ちる**という制約がある。
launchdは睡眠中の予定時刻を飛ばすため、22:15 JSTにスリープしていると寄り付きに間に合わず、
`pmset repeat wakeorpoweron` による自動起床が要る（2026-08-10に実際に1取引日を落とした）。
VPSではこの制約ごと消える。

**リポジトリ側で必要な変更は済んでいる。** `scripts/start_bot.sh` は Linux では
`caffeinate` を挟まず、`scripts/after_close.sh` は systemd 配下なら `systemctl --user stop`
でBotを止める。Pythonのコードは無改修で動く（依存は `ib_async` / `pandas` / `numpy` /
`python-dotenv` / `holidays` のみで、時刻は `zoneinfo` で明示的にET/JSTを扱っている）。

## 1. サーバの前提

| | 要件 |
| --- | --- |
| メモリ | **2GB以上。** IB Gateway単体で1GB前後使い、これに Python + pandas が乗る |
| OS | **Ubuntu 24.04 LTS**（systemd 255）。`OnCalendar` 末尾のタイムゾーン指定は **systemd 252以降でしか解釈されず**、22.04(systemd 249)ではタイマーの読み込み自体が失敗する。22.04を使うなら `Asia/Tokyo` を外し `timedatectl` でシステム側を合わせること |
| タイムゾーン | `sudo timedatectl set-timezone Asia/Tokyo`（timerにTZを明示してあるので必須ではないが、ログが読みやすい） |
| パッケージ | `procps`（`pkill`/`pgrep`）。通常は導入済みだが、**無いと `after_close.sh` がBotを止められない**。ほかに `git` / `python3-venv` / `rsync`（`fetch_vps_logs.sh` の受け側） |

**2GBで足りるのは、常駐するプロセスが実質2つだけだからである。** `after_close.sh` は
ランキング記録(yfinance・639銘柄)の**前にBotを停止する**ため、ピークが重ならない。

| | 常駐する時間帯 | 概算 |
| --- | --- | --- |
| IB Gateway（Java + Xvfb） | 常時 | 約1GB |
| `main.py`（pandas/numpy・監視20銘柄） | 22:15〜06:05 JST | 数百MB |
| `scripts/rank_turnover`（yfinance） | 06:05に数十秒（Bot停止後） | — |

**ただし2GBちょうどで運用するならスワップを置くこと。** さくらのVPS等の
最小イメージはスワップ無しで配られることがあり、GatewayのJVMがGC中に一時的に
膨らむとOOM killerがGatewayを落とす。**落ちてもBot側のログには「接続できない」
としか出ない**ため、原因に辿り着けない。

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

**クラウド側のファイアウォールで開けるのはSSH(22)だけでよい。** APIポート(4002)も
VNC(5900)も外へ出さない。**ただしdockerのポート公開は、さくらのVPSのパケット
フィルタも ufw も迂回する**ため、それらは防御にならない。塞いでいるのは
compose側の `127.0.0.1` 束縛だけである（下記2.）。

## 2. IB Gateway の自動ログイン

**IB GatewayはGUIのJavaアプリで、しかも1日1回ログアウトする。** 素で置くと毎日
手でログインし直すことになり、「Macを起動する」手間が「VPSにログインする」手間に
置き換わるだけで無人化にならない。再認証を代行するのが
[IBC](https://github.com/IbcAlpha/IBC) で、これが**無人運用の必須条件**である。

`core/connection.py` のリトライ既定値（10回・1回あたり上限60秒＝総待ち約4分）は、
この日次再起動を待ち切れる長さとして決めてある。

**推奨は Docker 版**（[gnzsnz/ib-gateway-docker](https://github.com/gnzsnz/ib-gateway-docker)）。
IB Gateway・IBC・Xvfb・x11vnc が同梱されており、Javaの導入もXvfbの起動も要らない。
本プロジェクト向けの設定を `deploy/ib-gateway/` に置いてある。

```bash
cd ~/ibkralgoTrade/deploy/ib-gateway
cp .env.example .env && chmod 600 .env   # ペーパー口座の認証情報を書く
docker compose up -d
docker compose logs -f                   # ログイン成功まで見届ける
```

`deploy/ib-gateway/docker-compose.yml` で本プロジェクト向けに指定している値:

| | 値 | 理由 |
| --- | --- | --- |
| `TRADING_MODE` | `paper` | 注文層の検証中。実資金の口座へ発注してはならない |
| `READ_ONLY_API` | `no` | Botは実際に発注する。read-onlyだと注文が拒否される |
| `AUTO_RESTART_TIME` | `12:00 PM` | **既定の23:59は 10:59 ET＝ザラ場のど真ん中。** Botが停止している時間帯(06:05〜22:15 JST)に置く |
| ポート公開 | `127.0.0.1:4002:4004` | **APIポートには認証が無い。** 下記参照 |
| `TWS_ACCEPT_INCOMING` | `accept` | 既定(`manual`)はAPI接続の確認ダイアログを出し、人が押すまで通らない |
| 設定のvolume | `./tws_settings` → `/home/ibgateway/tws_settings` | 永続化しないと Order Presets の修正が再起動で消える。**`/home/ibgateway/Jts` へマウントしてはならない**——Gateway本体の導入先で、空のディレクトリを重ねると起動しなくなる |

**APIポートは必ず `127.0.0.1` に束ねること。** `"4002:4004"` と書くと 0.0.0.0 で
公開され、インターネットから誰でも発注できる状態になる。**dockerのポート公開は
ufw を迂回する**ため、ファイアウォールを設定してあっても塞げない。確認:

```bash
ss -tlnp | grep 4002        # 127.0.0.1:4002 になっていること（0.0.0.0 は危険）
```

### Docker を使わない場合

IBC を直接入れる。Java・IB Gateway・Xvfb を自分で用意する必要がある。

```bash
wget https://github.com/IbcAlpha/IBC/releases/download/3.24.1/IBCLinux-3.24.1.zip
sudo mkdir -p /opt/ibc && sudo unzip IBCLinux-3.24.1.zip -d /opt/ibc
sudo chmod o+x /opt/ibc/*.sh /opt/ibc/scripts/*.sh
```

`config.ini` で設定する主な項目（名称は IBC 3.24.1 のもの）:

| 設定 | 値 |
| --- | --- |
| `IbLoginId` / `IbPassword` | ペーパー口座の認証情報 |
| `TradingMode` | `paper` |
| `AutoRestartTime` | Botが停止している時間帯（例 `12:00 PM`） |
| `AcceptIncomingConnectionAction` | `accept`（API接続ダイアログで止まらないように） |
| `ExistingSessionDetectedAction` | `primary` |

### 共通の注意

**ペーパー口座は2要素認証を要求されない。** 自動ログインが成立するのはこのため
である。**実資金の口座へ移す際は IB Key による2FAが要る**ので、無人運用の設計を
その時点で見直すこと（ペーパー検証中は影響しない）。

**同じ認証情報でMacとVPSの同時ログインはできない。** 後からのログインが先の
セッションを切るため、並行稼働ではなく切り替えとして移行すること。

**IBCの設定ファイル(`.env` / `config.ini`)には平文のパスワードが入る。**
`chmod 600` とし、SSHは鍵認証のみにする。

## 3. セットアップ

```bash
git clone <repo> ~/ibkralgoTrade && cd ~/ibkralgoTrade
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q          # 実接続不要。ここで通ることを確認する

cp /path/to/.env ~/ibkralgoTrade/.env  # Git管理外。IBKR_PORT=4002 (ペーパー)
.venv/bin/python -m scripts.check_market_data   # Gateway起動後に経路を確認
```

`deploy/systemd/*` は `IBKRALGO_PYTHON=%h/ibkralgoTrade/.venv/bin/python` を前提にしている。
別の場所に置く場合は unit の `Environment=` と `WorkingDirectory=` を直すこと。

## 4. systemdへの登録

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ibkralgotrade.timer ibkralgotrade-afterclose.timer

# ログアウト中もタイマーを動かす（VPSでは必須）
sudo loginctl enable-linger "$USER"
```

確認:

```bash
systemctl --user list-timers ibkralgotrade\*   # 次回の発火時刻
journalctl --user -u ibkralgotrade.service -f  # 起動時の失敗はここに出る
tail -f ~/ibkralgoTrade/logs/bot.log           # 稼働ログ
```

**`loginctl enable-linger` を忘れると、SSHを切った時点でタイマーごと止まる。**
これはVPSで最も起こりやすい設定漏れで、症状は「毎日何も起きない」——
`scripts/daily_report.py` の「ログがありません」警告が唯一の手掛かりになる。

## 5. 動作確認

移行直後は**その日のサマリを必ず読むこと**。休場日・祝日の判定は
`scripts/is_us_trading_day.py` が起動直前に行うので、平日でも起動しない日がある。

```bash
.venv/bin/python -m scripts.check_deployment    # 下記の設定漏れを一括で点検する
.venv/bin/python -m scripts.check_market_data   # Gatewayへの接続と価格の取得経路
.venv/bin/python -m scripts.daily_report
bash scripts/after_close.sh    # 締め処理を手で1回通しておく
```

**`scripts/check_deployment.py` が見るのは「忘れても即座にはエラーにならない」項目である。**
linger・スワップ・タイマーの有効化・systemdのバージョン・APIポートの応答、そして
移行元のmacOSで実行したときは**launchdジョブが残っていないか**。いずれも症状が
「毎日何も起きない」「時々つながらない」という形でしか出ず、稼働してからでは
休場日や一時的な切断と区別できない。**確かめられなかった項目はOKと数えず「要確認」
として出す**（NGがあれば終了コード1）。

**Gatewayを新しく立てたら Order Presets を確認すること。** プリセットが注文の
有効期間(TIF)を書き換えると `Error 10349` が出るが、**上書きは注文を拒否しないので
気付けない**。子注文が `DAY` に落とされると、持ち越すスイングの建玉が引けで
無防備になる（docs/DECISIONS.md「決済の置き場所」）。VNCで繋いで
Global Configuration → Presets を見る:

```bash
ssh -L 5900:127.0.0.1:5900 <user>@<vps>   # 別端末で。VNCは外へ出さない
```

## 6. ログを手元へ持ってきて改善する

**VPSへ移すと、改善の材料になるログはすべて向こう側に出る。** `logs/` はGit管理外
（実際の取引記録なのでコミットしてはならない）なので、gitでは流れてこない。
「手元でログを読んで直す」という進め方を続けるには同期が要る。

```bash
IBKRALGO_VPS=user@vps.example.com bash scripts/fetch_vps_logs.sh
python -m scripts.daily_report --log logs_vps/bot.log --journal logs_vps/trade_journal.csv
```

**同期先を `logs/` にしない。** 手元の `logs/` にはmacOSで稼働していた期間の記録が
入っており、混ぜるとどちらの環境のものか区別できなくなる（`trade_journal.csv` は
追記型なので特に危険）。`logs_vps/` は `.gitignore` の `logs*/` に含まれる。

同期されるもの:

| ファイル | 用途 |
| --- | --- |
| `bot.log` | 稼働ログ。「なぜ1件も建たなかったのか」はここにしかない |
| `trade_journal.csv` | 決済ごとの実現損益・R倍率・為替レート |
| `positions.json` | 保有ポジションと待機注文の値段 |
| `after_close.log` | 引け後の締め処理の要約 |
| `systemd.out` / `systemd.err` | **起動しなかった日の切り分け**（祝日でスキップしたのか、失敗したのか） |

`systemd.out` / `systemd.err` は launchd の `launchd.out` / `launchd.err` に対応する。
**journalではなくファイルに出しているのは、`logs/` を同期するだけで揃うようにするため**
である（journalだと `journalctl` を別途取り出す必要がある）。2026-08-10にジョブが
動かなかった件は、この出力がファイルに残っていたから切り分けられた。

## 7. 移行後のmacOS側

**launchdのジョブを止めること。** 残すと同じ認証情報で二重にログインし、
一方のセッションが切られる。

```bash
launchctl bootout gui/$(id -u)/com.<ユーザー名>.ibkralgotrade
launchctl bootout gui/$(id -u)/com.<ユーザー名>.ibkralgotrade.afterclose

python -m scripts.check_deployment   # macOS側で実行し、登録が残っていないか確かめる
```

`logs/positions.json` は**移行先へ引き継ぐこと。** 建玉の状態（建値・待機注文の値段・
R倍率の分母）がここにあり、失うとブローカー同期で拾い直した建玉として
扱われて建値が手数料込みの `avgCost` に化ける。

**引き継いだら、稼働させる前にブローカー側と突き合わせること。**

```bash
.venv/bin/python -m scripts.check_positions   # 照会のみ。発注も取り消しもしない
```

移行の前後でBotが止まっている間に待機注文が約定していると、記録だけが残る。
`is_confirmed_by_broker` がERRORを出して決済は見送るので危険な売り建てには
ならないが、**その銘柄が監視枠を占め続ける**（`MAX_CONCURRENT_POSITIONS` は2しかない）。
待機注文が片側しか生きていない建玉も同時に分かる。
