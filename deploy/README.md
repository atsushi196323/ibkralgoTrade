# VPS(Linux)への移行手順

macOSでの運用には**Macが起動していなければその日の取引が丸ごと落ちる**という制約がある。
launchdは睡眠中の予定時刻を飛ばすため、22:15 JSTにスリープしていると寄り付きに間に合わず、
`pmset repeat wakeorpoweron` による自動起床が要る（2026-08-10に実際に1取引日を落とした）。
VPSではこの制約ごと消える。

**リポジトリ側で必要な変更は済んでいる。** `scripts/start_bot.sh` は Linux では
`caffeinate` を挟まず、`scripts/after_close.sh` は systemd 配下なら `systemctl --user stop`
でBotを止める。Pythonのコードは無改修で動く（依存は `ib_insync` / `pandas` / `numpy` /
`python-dotenv` / `holidays` のみで、時刻は `zoneinfo` で明示的にET/JSTを扱っている）。

## 1. サーバの前提

| | 要件 |
| --- | --- |
| メモリ | **2GB以上。** IB Gateway単体で1GB前後使い、これに Python + pandas が乗る |
| OS | Ubuntu LTS 等。`OnCalendar` のタイムゾーン指定を使うので **systemd 252以降**が望ましい |
| タイムゾーン | `sudo timedatectl set-timezone Asia/Tokyo`（timerにTZを明示してあるので必須ではないが、ログが読みやすい） |

## 2. IB Gateway

**IB GatewayはGUIのJavaアプリなので、ヘッドレスでは Xvfb と組み合わせる。**
日次の自動ログイン・自動再起動には [IBC](https://github.com/IbcAlpha/IBC) を使う
（Gatewayは1日1回再起動する。`core/connection.py` のリトライ既定値は
その再起動を待ち切れる長さとして決めてある）。

**APIポート(4002)を外部に晒さないこと。認証が無い。** Gatewayのリスニングを
localhostに限定し、ファイアウォールでも塞ぐ。IBCの設定ファイルには
パスワードが平文で入るので `chmod 600` とし、SSHは鍵認証のみにする。

**同じ認証情報でMacとVPSの同時ログインはできない。** 後からのログインが先の
セッションを切るため、並行稼働ではなく切り替えとして移行すること。

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
.venv/bin/python -m scripts.daily_report
bash scripts/after_close.sh    # 締め処理を手で1回通しておく
```

## 6. 移行後のmacOS側

**launchdのジョブを止めること。** 残すと同じ認証情報で二重にログインし、
一方のセッションが切られる。

```bash
launchctl bootout gui/$(id -u)/com.user.ibkralgotrade
launchctl bootout gui/$(id -u)/com.user.ibkralgotrade.afterclose
```

`logs/positions.json` は**移行先へ引き継ぐこと。** 建玉の状態（建値・待機注文の値段・
R倍率の分母）がここにあり、失うとブローカー同期で拾い直した建玉として
扱われて建値が手数料込みの `avgCost` に化ける。
