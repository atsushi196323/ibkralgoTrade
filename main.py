"""エントリーポイント: 非同期イベントループの起動と全体のオーケストレーション。"""

import asyncio
import logging
import math
import signal
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Set, Tuple

import pandas as pd
from ib_async import IB, Stock

from core.connection import IBKRConnection
from core.logging_setup import configure_logging
from core.market_hours import US_EASTERN, is_day_trade_flatten_time, is_regular_trading_hours
from data.cache import ContractCache, DailyBarCache
from data.fundamentals import run_turnover_scan_async
from data.rank_history import RankHistoryStore, resolve_store
from data.market_data import (
    get_current_price_async,
    get_current_price_quote_async,
    get_intraday_bars_async,
    get_usd_jpy_rate_async,
)
from execution.account import (
    get_account_equity_async,
    get_settled_cash_async,
    get_usd_to_base_rate_async,
)
from execution.order_manager import (
    ENABLE_REAL_ORDERS,
    MAX_ORDER_NOTIONAL_USD,
    MAX_POSITION_SIZE,
    RestingExitProtection,
    cancel_bracket_orders_async,
    ensure_account_is_paper,
    ensure_orders_are_paper_only,
    find_filled_resting_exit,
    find_resting_exit_protection_async,
    place_bracket_order_async,
    place_market_order_async,
    place_resting_exit_orders_async,
)
from execution.position_manager import (
    DEFAULT_STATE_PATH,
    Position,
    PositionManager,
    STRATEGY_TYPE_DAY,
    STRATEGY_TYPE_GROWTH,
    STRATEGY_TYPE_SWING,
)
from execution.position_sizing import calculate_position_size
from execution.fill_log import FillLog
from execution.trade_journal import TradeJournal
from strategy.exit_signal import (
    REASON_EOD_FLATTEN,
    REASON_STOP_LOSS,
    REASON_TAKE_PROFIT,
    detect_exit_signal,
    detect_resting_order_exit,
    resolve_stop_price,
    resolve_take_profit_price,
)
from strategy.attention import (
    AttentionConfig,
    build_rank_map,
    detect_rank_surges,
    has_enough_history,
)
from strategy.pullback import (
    MarketFilterConfig,
    SignalResult,
    compute_deviation_pct,
    detect_pullback_signal,
)
from strategy.screener import ScreenerConfig, is_in_long_term_uptrend, screen_value_stocks_async

logger = logging.getLogger(__name__)

# フォールバック用の固定ウォッチリスト。銘柄選定は本来スクリーニング
# （時価総額+PER）で毎日動的に決定するが、スキャナーの購読が無い現状では
# 稼働時間の100%がこのリストへのフォールバックである。
#
# **2026-08-06に運用者の指示で、押し目買いのエッジを確認した母集団そのもの
# （bars/ の42銘柄・10年）へ入れ替えた。** それまでは運用者が指定した15銘柄
# だったが、検証と運用の母集団が重複2銘柄(JOBY/RIVN)しか無く、実際に建てられる
# のは3銘柄まで落ちていた。入れ替えの根拠はCLAUDE.md「実際に売買している銘柄の
# 根拠」に固定してある。要点だけ:
#
# - 42銘柄は out-of-sample・コスト込みで PF 1.18 / t値 3.51（相関補正後2.92）。
#   旧15銘柄は PF 1.07 / t値 0.51 で有意ではなかった
# - 旧リストは6銘柄がウォークフォワードの窓（315本）すら作れず、うち2銘柄
#   (AMBQ/FIG)は検証実績ゼロのままライブで建っていた
# - 株価が $7.97〜$1,427 と179倍に散っており、幅40倍の株価帯にどの資金額でも
#   収まらなかった。**増資しても取引可能銘柄が増えず、むしろ減る**構造だった
#
# **銘柄別のプロフィットファクターで選び直してはならない。** 数十トレードの
# 実績で銘柄を選ぶのは、ウォークフォワードが検出しようとしている過剰最適化を
# 検証の外側でやることに等しい（CLAUDE.md「複数銘柄での判断」）。この42銘柄は
# 検証の**前に**決めた母集団であり、成績を見て選んだものではない。
#
# **このリストには生存バイアスがある。** 42銘柄は「2026年時点で上場していて
# 10年分の日足が取れる大型株」として選んだため、この10年で上場廃止・買収・破綻
# した銘柄が構造的に入っていない。押し目買いは「下がったものは戻る」に賭ける
# 戦略なので、戻らなかった銘柄が抜けた母集団は成績を甘く見せる（CLAUDE.md
# 「検証に使う銘柄数」）。PF 1.18 は上振れした値として読むこと。
#
# **件数は MAX_WATCHLIST_SIZE を超えてよい。** _refresh_watchlist_async の
# フォールバック経路が、株価帯で絞ったうえで記載順に切り詰める。帯を通る件数は
# 資金に比例して動く（$1,183で23件 / $3,142で38件）ので、**リストの長さでは
# ペーシングの不変条件を保証できない**。切り詰めた件数と落とした銘柄は
# 取引日1回のWARNINGで残る（`_log_watchlist_truncation`）。
# ただし**日足の初回取得はリスト全件に対して発生する**（1日1回・銘柄あたり1件。
# DailyBarCacheが以降を吸収する）ため、リストを大幅に伸ばすとその日の最初の
# サイクルがペーサーの待ちで長くなる。
WATCHLIST: List[str] = [
    "AAPL", "ABBV", "ADBE", "AVGO", "AXP", "BAC", "CAT", "COST",
    "CRM", "CSCO", "CVX", "DE", "DIS", "GS", "HD", "HON",
    "INTC", "JNJ", "JOBY", "JPM", "KO", "LMT", "MCD", "MRK",
    "MSFT", "NKE", "NVDA", "ORCL", "PEP", "PFE", "PG", "RIVN",
    "SBUX", "T", "TMO", "TXN", "UNH", "UPS", "VZ", "WFC",
    "WMT", "XOM",
]

# ファンダメンタルズスクリーニング（割安株抽出）のパラメータ。
# 1日1回（取引時間の最初のサイクル）だけ実行し、ウォッチリストを入れ替える。
SCREENER_MIN_MARKET_CAP: float = 2_000_000_000.0
SCREENER_MAX_MARKET_CAP: float = 200_000_000_000.0
SCREENER_MAX_PE_RATIO: float = 15.0
SCREENER_SCAN_CODE: str = "MOST_ACTIVE"
SCREENER_NUM_CANDIDATES: int = 50
# スクリーニング結果から実際に監視する銘柄数の上限。
# 監視銘柄1件につき毎サイクル1回のリクエストが発生するため、
# ここを絞ることがIBKRのペーシング制限(10分あたり60件)対策の要になる。
#
# **POLL_INTERVAL_SECONDS と必ず一緒に決めること。** 2026-08-04に監視銘柄を
# 増やすため 10 -> 17 -> 20 と上げ、同時にポーリングを180 -> 300秒へ延ばした
# （20 * 600/300 = 40件/10分）。片方だけ動かすと不変条件を割る。
#
# 2026-08-18に 20 -> 24 へ上げた（ポーリングは300秒のまま）。理由は、資金$1,220で
# 株価帯を通る銘柄がちょうど24件あり、**アルファベット順で末尾の4銘柄
# (VZ / WFC / WMT / XOM) が毎日必ず監視から外れていた**ためである。検証した
# 42銘柄の母集団のうち恒常的に4銘柄が使われず、頻度が律速の現フェーズでは
# そのぶん検証が遅くなる。24 * 600/300 = 48件/10分で不変条件は満たす。
#
# **2026-08-27に 24 -> 38 へ上げ、同時にポーリングを300 -> 450秒へ延ばした。**
# そのとき「24で足りる保証は無い（資金が動けば帯を通る件数も動く）」と
# 書いていたことが、運用資金を¥500,000($3,142)にする判断で現実になった:
#
#     資金 $1,183 → 株価帯 $5.92–$236.72 → 42銘柄中 23件が通る（枠24に収まる）
#     資金 $3,142 → 株価帯 $15.71–$628.38 → 42銘柄中 38件が通る（**14件が落ちる**）
#
# 帯を通る件数は資金に比例して増えるので、枠を据え置くと**増資したぶんだけ
# 監視から落ちる銘柄が増える**。これは切り詰めの順序を変えたのではない——
# 順序は記載順のままで、**成績を見て決め直してはならない**。
#
# 帯の上限($628)を超える4銘柄(CAT/COST/DE/GS)はそもそも1株も買えないので、
# 42全部を覆う必要は無い。38が¥500,000で実際に建てられる件数である。
#
# **最悪ケース（価格取得がヒストリカル経路へ落ち、銘柄あたり2リクエストになる場合）は
# 101件/10分で `core.pacing` の実効枠(55件/10分)を超える。ただしこれは 20銘柄でも
# 既に超えていた（80件/10分）ので、38 で新しく生まれた問題ではない。**
# リミッターは枠が尽きると違反せず待つので、症状は「空のバー列」ではなく
# サイクルが伸びることである。実際に落ちているかは `data.market_data` が毎回出す
# 取得経路のログで分かる（2026-08-18時点では全銘柄がstreamingで、この経路は
# 使われていない）。これ以上増やすならポーリング間隔を一緒に延ばすこと。
MAX_WATCHLIST_SIZE: int = 38

# スクリーニングに失敗（例外・0件）した後、次に再試行するまでの間隔（秒）。
#
# 銘柄選定は1取引日に1回で足りるが、**失敗をその日1日ぶん確定させてはならない。**
# スキャナーもPER取得も購読権限が無いと例外ではなく空を返し（「6.2」）、
# 起動直後・データファームの再接続中・IBKR側の一時的な不調のいずれでも同じ形で
# 空になる。1回目の結果で打ち切ると、その日は固定リストのまま回り続ける。
#
# 一方で毎サイクル(300秒)再試行すると、購読権限が無い口座では一日中
# スキャナー要求とログを吐き続ける（この失敗は復旧しない）。間隔を空けるのは
# 「一時的な不調なら復旧を拾い、恒久的な失敗なら安く諦める」ための妥協である。
# 900秒なら米国のレギュラーセッション(6.5時間)で最大26回。
SCREENING_RETRY_INTERVAL_SECONDS: float = 900.0

# 売買代金の急上昇銘柄をウォッチリストへ組み入れるか。
#
# **この軸は検証されていない。** 押し目買いのエッジは42銘柄・10年の日足で
# 確認したものだが、ここで入る銘柄はその母集団と無関係に決まる。しかも
# 過去時点の売買代金ランキングはIBKRから遡れないため、バックテストで
# 検証する方法が無い（PERと同じ制約。CLAUDE.md「銘柄選定」）。
#
# 急に売買代金の上位へ来る銘柄は、決算やニュースで**価格が再評価されている
# 最中**であることが多い。そこで出る-5%乖離は、この戦略が狙う「ノイズによる
# 一時的な下振れ」ではなく新しい価格への移動の初期段階でありうる。
# **したがって既定は無効（観測モード）である。** 有効化の前提が2つ揃っていない:
#
# 1. IBKRのスキャナーは購読権限が無いと空を返す（現状そうなっている）
# 2. 「急上昇と判定された銘柄がその後どう動くか」を誰も見ていない
#
# 2つ目は購読なしで進められる。`python -m scripts.rank_turnover` が
# yfinance経由で日次の売買代金順位を `logs/turnover_ranks.json` へ記録し、
# 同じ判定ロジック(strategy/attention.py)で急上昇銘柄をログに出す
# （監視リストは変更しない）。**ただしそれはユニバース内での順位でしかなく、
# 取引所全体からの発見はスキャナーにしかできない。**
#
# 有効にするのは、スキャナーの購読が通り、かつ観測で有用性が確認できてから。
ENABLE_ATTENTION_WATCHLIST: bool = False

# スキャンする取引所と件数。numberOfRowsの上限が50なので、上位100件を得るには
# 取引所を分けて2回呼ぶ必要がある（data.fundamentals.run_turnover_scan_async）。
ATTENTION_SCAN_LOCATIONS: Tuple[str, ...] = ("STK.NASDAQ", "STK.NYSE")
ATTENTION_SCAN_ROWS: int = 50

# 急上昇の判定条件。rank_ceiling=50 は運用者の指定（1〜50位に入ったもの）。
# min_rank_improvement は検証で決めた値ではなく、監視枠(MAX_WATCHLIST_SIZE)に収まる件数へ
# 絞るための足切りである。**成績を見てこの値を刻み直してはならない。**
ATTENTION_CONFIG: AttentionConfig = AttentionConfig(
    rank_ceiling=50, min_rank_improvement=20, history_window=10, absent_rank=101,
)

# 下降トレンドの銘柄をウォッチリストから外すか。**急上昇の組み入れとは
# 独立した機能である。** こちらは日足キャッシュだけで判定でき、追加の
# IBKRリクエストも購読権限も要らないため、観測モードでも有効にしている。
DROP_STRUGGLING_SYMBOLS: bool = True

# 建てられない銘柄（下降トレンド・本数不足）を監視対象に残すか。
#
# **既定でTrue。** 安全弁はエントリー側(ENTRY_REQUIRES_LONG_TERM_UPTREND /
# SWING_MIN_HISTORY_BARS)に移してあるので、監視に残しても建たない。残す利点は
# 乖離率と復帰までの距離が毎サイクル記録され、**トレンドが上向いた瞬間に
# その場でエントリー判定へ入れる**こと。外していると復帰の判定が1日1回の
# ウォッチリスト更新まで遅れる。
#
# 監視枠を食う点は「6.1 ペーシング制限」の不変条件
#     MAX_WATCHLIST_SIZE × (600 / POLL_INTERVAL_SECONDS) ≦ 60
# で頭打ちになる。現状は24銘柄・300秒＝48件/10分。
# **枠を超える場合はFalseにして落とすこと。**
KEEP_UNTRADEABLE_SYMBOLS_IN_WATCHLIST: bool = True

# 押し目シグナルが出ても、終値が長期移動平均を下回る銘柄は建てないか。
#
# もともとウォッチリストの出入りで表現していた条件を、エントリーの直前へ
# 移したもの。**判定の場所を変えただけで、建つ建たないの結果は変わらない。**
# 監視に残す（上記）ようにしたため、ここで止めないと下降トレンドの銘柄が
# そのまま建つ。
ENTRY_REQUIRES_LONG_TERM_UPTREND: bool = True

# 下降トレンドの銘柄をウォッチリストから外すときの移動平均日数。
# 銘柄選定のトレンドフィルター(SCREENER_TREND_MA_WINDOW)と同じ物差しを使う。
# 選定で通した条件と維持で使う条件が食い違うと、入れた翌日に外すことになる。
STRUGGLING_MA_WINDOW: int = 200
# IBKRのペーシング制限を避けるため、PER取得(reqFundamentalDataAsync)を
# 連続発行せずこの秒数だけ間隔を空ける
SCREENER_PE_REQUEST_INTERVAL_SECONDS: float = 1.0
# 長期トレンドフィルター: 明確な下降トレンド(200日移動平均割れ)の銘柄を除外する。
# 75銘柄・447トレードの実データ検証で、長期トレンドと平均回帰戦略の
# profit_factorに弱い正の相関(+0.16〜+0.18)が確認されたための追加条件。
SCREENER_ENABLE_TREND_FILTER: bool = True
SCREENER_TREND_MA_WINDOW: int = 200
SCREENER_TREND_LOOKBACK_DURATION: str = "300 D"

# 監視ループのポーリング間隔（秒）: 市場時間中/時間外で切り替える。
# 市場時間中の間隔は、IBKRのヒストリカルデータ制限(10分あたり60件)から逆算して
# 決めている。監視銘柄1件あたり毎サイクル1リクエストなので、
#     MAX_WATCHLIST_SIZE * (600 / POLL_INTERVAL_SECONDS) <= 60
# を満たす必要がある。38銘柄・450秒なら50.7件/10分で、日足の初回取得や
# スクリーニングの分の余裕も残る。
#
# 300秒にしたのは監視銘柄を20に増やしたため（2026-08-04）、450秒にしたのは
# 38に増やしたため（2026-08-27。運用資金を¥500,000にすると株価帯を通る銘柄が
# 23 -> 38件へ増える。MAX_WATCHLIST_SIZE の説明を参照）。**代償はボット側で
# 判定する決済（トレーリング）の検知が5分から7.5分に遅れること。**
# 利確・損切りはブローカー側の待機注文なので影響しないが、
# 「ライブのトレーリングはバックテストより遅れて発動する」既知の乖離
# （CLAUDE.md「決済の置き場所」）はその分だけ広がる。
#
# **大引け前の強制決済(15:55 ET)は「その時刻以降か」で判定するので取りこぼさない**
# （`core.market_hours.is_day_trade_flatten_time` は範囲ではなく閾値）。遅れるだけである。
POLL_INTERVAL_SECONDS: float = 450.0
CLOSED_MARKET_POLL_INTERVAL_SECONDS: float = 300.0

# 「再ログインが必要かもしれない」と1行だけ出すまでの、接続失敗の連続ラウンド数。
#
# ログだけでは**復帰する切断と復帰しない切断が区別できない。** どちらも
# 同じ `TWSへの再接続に失敗しました` が同じ間隔で並ぶだけで、実際に
# 2026-08-04のログでは同じ2行が9分おきに6回続いていた。
#
# 1ラウンド = connect_asyncのリトライ使い切り（10回・1回あたり上限60秒で約4分。
# core/connection.py）+ CLOSED_MARKET_POLL_INTERVAL_SECONDS の待機 ≒ 9分。
# IB GatewayのAuto restartはこの1ラウンドで復帰する長さなので、3ラウンド
# （約27分）続いたらソケットが閉じたままであり、再試行では解けない状態
# （2要素認証の期限切れ・セッション失効・Gatewayの停止）を疑う根拠になる。
#
# 短くすると通常のAuto restartで誤検知し、この行の意味が薄れる。
CONNECTION_FAILURE_ROUNDS_BEFORE_MANUAL_LOGIN: int = 3

# スイングトレード判定用（日足）のプルバックパラメータ。
# 移動平均30本は、42銘柄・10年の日足で移動平均期間だけを固定した
# ウォークフォワード検証（out-of-sample・コスト込み）で選んだ値。
# MA10/20/25/30/40/50/60 の比較では30が逆U字のピークで、profit_factorの
# 中央値1.42・プラスで終えた銘柄36/42(85.7%)といずれも最良だった
# （20は1.18・69.0%）。60まで伸ばすと中央値0.89まで崩れるため、
# 「長いほど良い」という単調な傾向を拾ったものではない。
SWING_MA_WINDOW: int = 30
SWING_THRESHOLD_PCT: float = 5.0

# スイングの新規建てに必要な日足の最低本数。**移動平均が確定する本数
# (SWING_MA_WINDOW)では足りない。**
#
# 長期トレンドフィルター(STRUGGLING_MA_WINDOW=200本)は本数が足りないと
# Noneを返し、_screen_watchlist_symbols_async はその銘柄を監視対象に残す。
# 残すこと自体は正しい（本数不足は「下降トレンドである」ことを意味しない）
# が、エントリー側が30本で通ると、**トレンド判定を一度も受けていない銘柄が
# そのまま建つ**。2026-08-04のペーパー検証で実際に起きており、上場から
# 35営業日のSPCXがMA(30)乖離-16.54%で建った。この乖離は上場直後の値付けの
# 途中経過であって、42銘柄・10年で検証した「平均回帰する押し目」ではない。
# 同じ日のウォッチリストにはCBRS(55本)・FRVO(56本)も居り、同時保有上限に
# 阻まれただけで条件は同じだった。
#
# したがってエントリーは「トレンド判定を実際に受けられる本数」を要求する。
# 分からないものを有利側に倒さない、という他の判定（価格の鮮度・株価帯）と
# 同じ向きに揃える。監視から外さないが建てもしない、が正しい扱いである。
SWING_MIN_HISTORY_BARS: int = STRUGGLING_MA_WINDOW

# 市場全体（指数）の状況によるエントリーの追加条件。日足＝スイング判定にのみ掛かる。
#
# **既定は無効（すべてNone）。** 有効化してよいのは、
#     python -m backtest.run --csv-dir bars --market-csv bars/SPY.csv \
#         --relative-threshold 2 3 4 --keep-unfiltered
# のような銘柄横断のウォークフォワードで、フィルター有りが選ばれ、かつ
# 合算PFだけでなくPFの中央値・プラスで終えた銘柄の割合まで改善したときだけ。
#
# デイトレード（5分足）分岐には掛けていない。5分足は外部データで数十日分しか
# 遡れず（CLAUDE.md「バックテストのデータ源」）、検証を経ていない条件を
# ライブにだけ入れることになるため。
MARKET_INDEX_SYMBOL: str = "SPY"
MARKET_INDEX_MA_WINDOW: int = 30
SWING_MARKET_FILTER: MarketFilterConfig = MarketFilterConfig()

# デイトレード（短期足）でのエントリーを行うか。**既定は無効。**
#
# 無効にしている理由は2つあり、それぞれ独立している:
#
# 1. **検証実績がゼロ。** 5分足のバックテストは1件も行っていない（外部データでは
#    60日程度しか遡れないため。CLAUDE.md「バックテストのデータ源」）。MA30の選定も
#    市場フィルターの不採用も小口座での成績も、すべて日足＝スイングの検証であり、
#    下のデイトレード用パラメータは誰も検証していない初期値のまま残っている。
# 2. **資金設計が成立しない。** 建玉金額 = 資金 × (リスク% ÷ 損切り%) なので、
#    損切り1.5%では1銘柄で資金の67%を使う。MAX_CONCURRENT_POSITIONS=2 でも
#    2銘柄目が入らない。現状これが表面化しないのはMAX_POSITION_SIZEの株数クランプが
#    効いているからで、それは検証用の安全弁であって資金設計ではない。
# かつて3つ目に挙げていた「キャッシュ口座の受渡し(T+1)によるGood Faith Violation」は
# 解消済み。GFVは米国のルールで日本居住者向けのIBSJ口座には適用されず、PDT規制も
# 掛からない（2026-07-31にIBKRサポートへ照会して確認）。同日往復そのものを
# 妨げるものは無い。受渡し前の資金で建てようとした場合は注文が拒否されるだけである。
#
# 再有効化するときは、(1) IBKR接続で5分足を取得しスイングと同じ基準（銘柄横断・
# コスト込み・ウォークフォワード）で検証してPFが1を超えること、(2) 建玉サイズの
# 問題を解決すること、の2つを揃えること。
#
# 無効でも、既存のデイトレードポジション（状態ファイルからの復元など）の決済判定と
# 大引け前の強制決済は動く。エントリーだけを止めている。
ENABLE_DAY_TRADING: bool = False

INTRADAY_BAR_SIZE: str = "5 mins"
INTRADAY_DURATION: str = "2 D"
INTRADAY_MA_WINDOW: int = 20
INTRADAY_THRESHOLD_PCT: float = 2.0

# 決済ロジックのパラメータ（利確・損切り・トレーリングストップ）。
# スイングは日足の押し目、デイトレードは5分足の押し目と値幅のスケールが
# 異なるため、種別ごとに別基準を設ける。
SWING_TAKE_PROFIT_PCT: float = 10.0
SWING_STOP_LOSS_PCT: float = 5.0
SWING_TRAILING_STOP_PCT: float = 5.0

DAY_TAKE_PROFIT_PCT: float = 3.0
DAY_STOP_LOSS_PCT: float = 1.5
DAY_TRAILING_STOP_PCT: float = 2.0

# --- グロース株トラック（日足・持ち越し。損切り-12%固定） -------------------
#
# **既定は無効。** 有効化の条件と検証結果は CLAUDE.md「グロース株トラック」節に
# ある。無効なのは思想の問題ではなく、42銘柄のスイングと同じ基準
# （銘柄横断・コスト込み・ウォークフォワード）で測った結果によるものである。
#
# 損切り-12%は運用者の指定値であり、検証で選んだ値ではない。**この値は
# 株数計算を通じて買える株価の上限まで変える**——
#   建玉金額 = 資金 × (リスク% ÷ 損切り%)
# なので、-5%なら資金の20%、-12%なら8.3%になる。$1,220では建玉予算が
# $244 -> $101 まで縮み、MRNA($138.89)は1株も買えない。この帯の判定は
# _growth_price_band() が別に持つ（スイングの帯をそのまま使ってはならない）。
ENABLE_GROWTH_SWING: bool = False

# グロース株トラックの監視銘柄。**スイングの WATCHLIST とは母集団が別**で、
# 混ぜてはならない（決済幅も株価帯も違う）。選定基準は「日次SDが高い成長株
# ／モメンタム株で、ウォークフォワードに必要な315本以上の日足が取れること」。
GROWTH_WATCHLIST: Tuple[str, ...] = (
    "AFRM", "ARM", "COIN", "CRWD", "DDOG", "MDB", "MRNA", "NET",
    "PLTR", "RBLX", "RIVN", "SHOP", "SMCI", "SNOW", "SOFI", "U",
)

# グロース株の押し目判定。スイングと同じ形（移動平均からの下方乖離）だが、
# **閾値はスイングの-5%では機能しない。** グロース株の日次SDは中央値3.86%で、
# 42銘柄の1.70%の2倍以上ある（2026-08-25実測）。-5%乖離はスイング銘柄では
# 2.9日ぶん＝3σ級の異常事態だが、グロース株では1日強の通常変動でしかなく、
# シグナルとしての情報が薄い。値はウォークフォワードに選ばせた（CLAUDE.md）。
GROWTH_MA_WINDOW: int = 30
GROWTH_THRESHOLD_PCT: float = 10.0

# 損切り-12%は指定値。利確とトレーリングはそれに対する比率で決める。
GROWTH_TAKE_PROFIT_PCT: float = 24.0
GROWTH_STOP_LOSS_PCT: float = 12.0
GROWTH_TRAILING_STOP_PCT: float = 12.0

# --- 単一銘柄への集中モード -------------------------------------------------
#
# Noneなら通常のウォッチリスト運用。銘柄コードを入れると、**スクリーニングも
# 固定リストも使わず、その1銘柄だけを監視する。**
#
# **これは分散を捨てる設定である。** 本プロジェクトの検証はすべて銘柄横断で
# 行っており（「複数銘柄での判断」節）、単一銘柄の成績は運である。集中させると
# 次の3つが同時に起きる:
#
#   1. トレード頻度が落ちる。1銘柄では年4〜10件程度で、同時保有枠(2)ではなく
#      シグナルの供給が律速になる
#   2. その銘柄固有のドローダウンをそのまま受ける（分散が効かない）
#   3. 銘柄選定の失敗が損益に直結する
#
# 2026-08-25に運用者の指定でMRNAを設定した。**この構成の実測値は下記のとおり
# マイナスである**（10年・out-of-sample・実測手数料$1.00・ライブと同じ
# MA30/-5%/+10%/-5%）:
#
#   資金 $1,220 : 101トレード 勝率32.7% PF 0.60 損益 -290.75 USD（口座の-24%）
#   資金 $3,300 : 105トレード 勝率40.0% PF 0.76 損益 -495.10 USD
#
# 解除は None に戻すだけでよい。
CONCENTRATED_SYMBOL: Optional[str] = None

# グロース株に割り当てる監視枠。MAX_WATCHLIST_SIZE の内数であり、
# **合計を超えさせてはならない**（「6.1」のペーシング不変条件は監視銘柄の
# 総数で決まる）。スイング側はこの分だけ枠が減る。
GROWTH_WATCHLIST_SLOTS: int = 6


@dataclass(frozen=True)
class ExitParams:
    take_profit_pct: float
    stop_loss_pct: float
    trailing_stop_pct: float


@dataclass(frozen=True)
class MarketDataCaches:
    """サイクルをまたいで使い回すIBKRデータのキャッシュ束。

    ペーシング制限対策の中心。main()で1つ生成してサイクル間で共有する。
    省略した場合は呼び出しごとに新規生成される（＝キャッシュが効かない）ため、
    単体テスト以外では必ず共有インスタンスを渡すこと。
    """

    contracts: ContractCache = field(default_factory=ContractCache)
    daily_bars: DailyBarCache = field(default_factory=DailyBarCache)


# strategy_type("swing"/"day")ごとの決済パラメータ。ブローカー同期で発見された
# STRATEGY_TYPE_UNKNOWNのポジションは、より安全側であるswing基準にフォールバックする
# （_process_exit_async参照）。
EXIT_PARAMS_BY_STRATEGY_TYPE: Dict[str, ExitParams] = {
    STRATEGY_TYPE_SWING: ExitParams(
        take_profit_pct=SWING_TAKE_PROFIT_PCT,
        stop_loss_pct=SWING_STOP_LOSS_PCT,
        trailing_stop_pct=SWING_TRAILING_STOP_PCT,
    ),
    STRATEGY_TYPE_DAY: ExitParams(
        take_profit_pct=DAY_TAKE_PROFIT_PCT,
        stop_loss_pct=DAY_STOP_LOSS_PCT,
        trailing_stop_pct=DAY_TRAILING_STOP_PCT,
    ),
    STRATEGY_TYPE_GROWTH: ExitParams(
        take_profit_pct=GROWTH_TAKE_PROFIT_PCT,
        stop_loss_pct=GROWTH_STOP_LOSS_PCT,
        trailing_stop_pct=GROWTH_TRAILING_STOP_PCT,
    ),
}


def is_growth_symbol(symbol: str) -> bool:
    """グロース株トラックの銘柄かを返す。

    トラックが無効なときは常にFalse。**判定を1か所に集めているのは、
    決済幅・株価帯・エントリー閾値の3つが同じ集合で切り替わらなければ
    ならないため**である。片方だけグロース基準になると、-12%の損切りを
    置いた建玉をスイングの株価帯（＝2.4倍の予算）で作ることになる。
    """
    return ENABLE_GROWTH_SWING and symbol in GROWTH_WATCHLIST

# 1トレードあたり口座資金の何%をリスクに晒すか（ポジションサイジングの基準）
RISK_PER_TRADE_PCT: float = 1.0

# 銘柄ごとの1%リスクは同時保有数が増えるほど積み上がるため、
# ウォッチリストが拡張されても青天井にならないよう独立して上限を設ける。
#
# 上限が2なのは、リスクベースのサイジングでは1ポジションが占める資金の割合が
# 株価によらず一定になるため。
#     数量     = (資金 × RISK_PER_TRADE_PCT%) ÷ (株価 × 損切り%)
#     建玉金額 = 数量 × 株価 = 資金 × (RISK_PER_TRADE_PCT% ÷ 損切り%)
# スイングの損切り5%なら資金の20%。5銘柄まで許すと資金を使い切り、
# 現金の裏付けが無い注文が並ぶ（キャッシュ口座では受渡し前の資金を
# 当てにすることになり、なおさら成立しない）。2銘柄なら40%に収まる。
# ここを増やす場合は、必ず上式で「同時保有数 × 建玉金額 ≦ 資金」を確認すること。
#
# なおデイトレード（損切り1.5%）は1銘柄で資金の67%を使うため、この上限が2でも
# 2銘柄同時には成立しない（MAX_POSITION_SIZEの株数クランプが効いて実際には
# もっと小さくなるが、それは資金設計ではなく検証用の安全弁に頼っている状態）。
# 損切り幅が狭いほど建玉が大きくなるという関係自体は正しく、
# デイトレード分岐を小口座で使う場合はこの点を別途詰める必要がある。
MAX_CONCURRENT_POSITIONS: int = 2
# 口座資金に対する1日の最大許容損失（%）。これを超えたら新規エントリーを停止する
# サーキットブレーカー。既存ポジションの決済判定（損切り等）は引き続き有効。
MAX_DAILY_LOSS_PCT: float = 3.0
# 1日に出してよい新規建ての回数。MAX_CONCURRENT_POSITIONSは「同時に何銘柄持つか」
# の制限であって、建てては決済を繰り返す回数は抑えない。同日中の再エントリー禁止
# (PositionManager.is_in_cooldown)により通常はウォッチリストの銘柄数(10)が事実上の
# 上限になるが、それはクールダウンが正しく効いている前提の話である。この上限は
# その前提が壊れたとき（状態ファイルの消失、日付判定のバグ等）に、損失の垂れ流しを
# 有限回で止めるための独立した歯止め。損失額ベースのサーキットブレーカーとは違い、
# 実現損益が確定する前の発注ラッシュにも効く。
MAX_DAILY_ENTRY_ORDERS: int = 10
# 新規建ての数量を「決済済み現金で買える株数」に制限するか。
#
# 目的は**資金不足による発注拒否を避けること**であって、Good Faith Violationの
# 回避ではない。GFVは米国のルールであり、日本居住者向けのIBSJ口座には適用されない
# （2026-07-31にIBKRサポートへ照会して確認。PDT規制も同様に適用されない）。
#
# 適用されないと分かった以上、残る実害は「受渡し(T+1)前の資金では買えない」ことだけで、
# その場合IBKRは約定させず**注文を拒否する**（同じ照会での回答。API経由でも手動でも
# 取扱いは同一）。拒否はペナルティを伴わないが、注文が通った前提で
# ローカルにポジションを記録すると実体の無い建玉を追跡することになるため、
# 入口で数量を現金の裏付けまで落としておく方が素直である。
#
# **既定は無効。** 検証に使っているペーパー口座がSettledCashタグを返さないため
# （実測: アカウントサマリー45タグ中に存在せず、BuyingPower/FullInitMarginReq/
# Cushionが並ぶマージン型口座だった）。ドライラン中は実約定が無く決済済み現金が
# 動かないので、有効にしても観察できるものが無い。
#
# 実口座へ移す際にTrueへ戻す価値はあるが、GFVのような不可逆な不利益は無いため、
# SettledCashが取得できない場合はエントリーを止めずに素通しする
# （_clamp_quantity_to_settled_cash_asyncを参照）。
ENFORCE_SETTLED_CASH_FUNDING: bool = False
# 当日のものでない価格を掴んだまま新規建てするのを止めるか。
#
# get_current_price_async のフォールバック連鎖は、下位の経路
# （ティッカーのclose・ヒストリカル最終終値）で**前営業日の終値**を返しうる。
# 購読権限の無い口座ほど下位に落ちやすいため、休場明けやギャップ後には
# 現実に起こる。この値は発注の参照価格になり、損切り・利確の指値もここから
# 算出されるので、古い値を掴むとブラケット一式が実勢からずれた値段で並ぶ。
# order_manager の値段の妥当性検証は参照価格を基準に測っている以上、
# 参照価格そのものがずれているケースは検出できない（CLAUDE.md該当節）。
#
# 決済側には掛けていない。古い価格で決済を見送ると、損切りが必要な場面で
# 何もしないことになり、新規建てを見送るのとは危険の向きが逆になる。
REJECT_STALE_ENTRY_PRICE: bool = True


async def _get_market_deviation_pct_async(
    ib: IB, caches: MarketDataCaches,
) -> Optional[float]:
    """指数の乖離率を返す。フィルターが無効なら取得もしない（=リクエスト0件）。

    指数の日足も `DailyBarCache` を通すため、追加のリクエストは
    「1取引日あたり1件」で済み、ペーシング制限(CLAUDE.md 6.1)には実質響かない。
    """
    if not SWING_MARKET_FILTER.is_enabled:
        return None

    try:
        contract = await caches.contracts.get_async(ib, MARKET_INDEX_SYMBOL)
        bars = await caches.daily_bars.get_async(ib, contract)
    except Exception:
        # 指数が取れないことで監視ループ全体を落とさない。Noneを返すと
        # フィルターは「条件を満たさない」＝エントリー見送りに倒れる。
        logger.exception("%s の日足を取得できませんでした。", MARKET_INDEX_SYMBOL)
        return None

    if len(bars) < MARKET_INDEX_MA_WINDOW:
        logger.warning(
            "%s の日足が%d本しかなく、移動平均(%d本)を計算できません。",
            MARKET_INDEX_SYMBOL, len(bars), MARKET_INDEX_MA_WINDOW,
        )
        return None

    return compute_deviation_pct(bars["close"], MARKET_INDEX_MA_WINDOW)


async def _detect_buy_signal_async(
    ib: IB, contract: Stock, symbol: str, caches: MarketDataCaches,
) -> Optional[Tuple[SignalResult, str]]:
    # 日足は1取引日に1本しか増えないためキャッシュから引く。日中足は
    # デイトレードのシグナルそのものなので毎回取得する。
    # デイトレードが無効なら日中足は使わないので、取得自体を行わない
    # （銘柄あたり毎サイクル1リクエストを丸ごと節約でき、ペーシング制限に効く）。
    daily_df = await caches.daily_bars.get_async(ib, contract)
    intraday_df = (
        await get_intraday_bars_async(
            ib, contract, duration=INTRADAY_DURATION, bar_size=INTRADAY_BAR_SIZE,
        )
        if ENABLE_DAY_TRADING else pd.DataFrame()
    )

    if daily_df.empty and intraday_df.empty:
        logger.warning("%s のヒストリカルデータが取得できなかったためスキップします。", symbol)
        return None

    if 0 < len(daily_df) < SWING_MIN_HISTORY_BARS:
        # 新規上場銘柄では現実に起きる。黙ってスキップすると「シグナルが
        # 出ない銘柄」と区別がつかず、監視枠を占めていることに気付けない。
        logger.warning(
            "[%s] 日足が%d本しかなく長期トレンド(%d本)を判定できないため、"
            "スイングの新規建てを見送ります（上場から日が浅い銘柄では"
            "本数が揃うまでエントリーできません）。",
            symbol, len(daily_df), SWING_MIN_HISTORY_BARS,
        )

    if len(daily_df) >= SWING_MIN_HISTORY_BARS:
        # グロース株は同じ日足・同じ判定ロジックで、閾値と決済幅だけが違う。
        # 本数要件(200本)と長期トレンドフィルターは共通で掛ける——上場直後の
        # 値付けの途中経過を「押し目」として建てないための歯止めであり、
        # グロース株でこそ効く（2026-08-04のSPCXが実例）。
        growth = is_growth_symbol(symbol)
        market_deviation_pct = await _get_market_deviation_pct_async(ib, caches)
        swing_signal = detect_pullback_signal(
            symbol, daily_df,
            ma_window=GROWTH_MA_WINDOW if growth else SWING_MA_WINDOW,
            threshold_pct=GROWTH_THRESHOLD_PCT if growth else SWING_THRESHOLD_PCT,
            market_deviation_pct=market_deviation_pct, market_filter=SWING_MARKET_FILTER,
        )
        if swing_signal.should_buy:
            # トレンド判定はここで行う。ウォッチリストから外して判定するより
            # 後段だが、**シグナルが出た銘柄にしかログが出ない**ぶん、
            # 「押し目は来たが下降トレンドなので見送った」という判断の記録が
            # 埋もれない。毎サイクル全銘柄について出すと1日数百行になる。
            if (
                ENTRY_REQUIRES_LONG_TERM_UPTREND
                and is_in_long_term_uptrend(daily_df, STRUGGLING_MA_WINDOW) is not True
            ):
                logger.info(
                    "[%s] 押し目シグナルは出ましたが、終値が%d日移動平均を下回るため"
                    "新規建てを見送ります。終値%.2f / MA%d %.2f（あと%+.1f%%で解除）。",
                    symbol, STRUGGLING_MA_WINDOW,
                    _latest_close_price(daily_df), STRUGGLING_MA_WINDOW,
                    _long_term_moving_average(daily_df), _pct_to_long_term_ma(daily_df),
                )
            else:
                if growth:
                    logger.info(
                        "[%s] グロース株(日足)のプルバックシグナルで買い判定しました"
                        "（損切り-%.1f%% / 利確+%.1f%%）。",
                        symbol, GROWTH_STOP_LOSS_PCT, GROWTH_TAKE_PROFIT_PCT,
                    )
                    return swing_signal, STRATEGY_TYPE_GROWTH
                logger.info("[%s] スイング(日足)のプルバックシグナルで買い判定しました。", symbol)
                return swing_signal, STRATEGY_TYPE_SWING

    if ENABLE_DAY_TRADING and len(intraday_df) >= INTRADAY_MA_WINDOW:
        intraday_signal = detect_pullback_signal(
            symbol, intraday_df, ma_window=INTRADAY_MA_WINDOW, threshold_pct=INTRADAY_THRESHOLD_PCT,
        )
        if intraday_signal.should_buy:
            logger.info(
                "[%s] デイトレード(%s足)のプルバックシグナルで買い判定しました。",
                symbol, INTRADAY_BAR_SIZE,
            )
            return intraday_signal, STRATEGY_TYPE_DAY

    return None


# 約定の記録先（想定価格と実約定価格の乖離）。**観測専用であり、売買の判断には
# 一切使わない。** `TradeJournal` のように引数で引き回していないのはそのためで、
# あちらは日次損益サーキットブレーカーの**入力**だが、こちらは出力しかない。
# 判断に使わないものを判断の経路へ通すと、記録が読めないことを理由に発注や
# 決済が止まりうる（それは観測を足したせいで建玉が無防備になることを意味する）。
#
# インスタンス生成ではファイルもディレクトリも作らない（追記時に作る）。
# import しただけで作業ディレクトリに logs/ を作らないためで、
# `core.logging_setup.configure_logging` をエントリーポイントから呼ぶのと同じ理由。
FILL_LOG = FillLog()


def _record_fill(record: Callable[[], object]) -> None:
    """約定記録の呼び出しを、売買の経路から隔離する。

    `FillLog` 自身も書き込み失敗を握り潰すが、**隔離は呼び出し側にも要る。**
    記録の組み立てで例外が出れば（値の取り違え・将来の改修）、それは
    `FillLog` の中まで届かずにここで発注・決済の流れを止める。止まる場所が
    悪く、新規建てなら「建玉はできたのにローカルへ記録されない」、決済なら
    「待機注文を取り消した直後に落ちる」——**どちらも建玉が無防備になる**。

    観測のために売買を止めないことがこの記録の前提なので、二重に囲う。
    """
    try:
        record()
    except Exception:
        logger.exception("約定の記録に失敗しました（売買は続行します）。")


async def _process_entry_async(
    ib: IB, symbol: str, position_manager: PositionManager, trade_journal: TradeJournal,
    caches: MarketDataCaches,
) -> None:
    if position_manager.count_open_positions() >= MAX_CONCURRENT_POSITIONS:
        # **銘柄ごとではなくサイクルごとに1行だけ出す。** 条件は口座全体のもので
        # 銘柄によらないのに、監視銘柄の数だけ同じ行が並ぶ。2026-08-17のVPSログでは
        # 18銘柄×77サイクル＝1386行で、その日の全ログ2773行のちょうど50%を
        # 占めていた（「3. 実行環境と設定」のログ方針）。サイクルごとに残すのは、
        # 枠が埋まった日に監視ループが回っていた証拠がこの行になるためである。
        global _position_limit_skip_logged_in_cycle
        if not _position_limit_skip_logged_in_cycle:
            _position_limit_skip_logged_in_cycle = True
            logger.info(
                "同時保有ポジション数の上限(%d)に達しているため、"
                "このサイクルの新規エントリーはすべて見送ります（保有中: %s）。",
                MAX_CONCURRENT_POSITIONS, ", ".join(sorted(position_manager.open_symbols())),
            )
        return

    # 発注回数の上限。データ取得より前に判定するのは、上限に達した後の
    # サイクルで無駄なヒストリカルリクエストを撃たないため（ペーシング制限）。
    entry_orders_today = position_manager.count_entry_orders_today()
    if entry_orders_today >= MAX_DAILY_ENTRY_ORDERS:
        logger.warning(
            "[%s] 本日の新規建て回数(%d)が上限(%d)に達したため、新規エントリーを停止します。",
            symbol, entry_orders_today, MAX_DAILY_ENTRY_ORDERS,
        )
        return

    # 決済した当日は同じ銘柄を買い直さない。日足の乖離率はその日の間ほぼ
    # 変わらないため、この判定が無いと損切り直後のサイクルで同じシグナルが
    # 再び成立し、下落トレンド中に損失を刻み続ける。
    if position_manager.is_in_cooldown(symbol):
        logger.info(
            "[%s] 本日すでに決済済みのため、新規エントリーをスキップします"
            "（当日中の再エントリー禁止）。",
            symbol,
        )
        return

    contract = await caches.contracts.get_async(ib, symbol)

    signal_result = await _detect_buy_signal_async(ib, contract, symbol, caches)
    if signal_result is None:
        return
    _signal, strategy_type = signal_result

    quote = await get_current_price_quote_async(ib, contract)
    if quote is None:
        logger.warning("%s の現在価格が取得できなかったため発注をスキップします。", symbol)
        return

    if quote.is_stale and REJECT_STALE_ENTRY_PRICE:
        logger.warning(
            "[%s] 現在価格 %.2f が当日のものではない可能性があるため（経路: %s）、"
            "新規エントリーを見送ります。この価格を参照価格にすると、"
            "損切り・利確の値段まで実勢からずれたブラケットが並ぶため。",
            symbol, quote.price, quote.source,
        )
        return

    price = quote.price

    account_equity = await get_account_equity_async(ib)

    daily_pnl = trade_journal.compute_daily_pnl()
    max_daily_loss = -account_equity * MAX_DAILY_LOSS_PCT / 100.0
    if daily_pnl <= max_daily_loss:
        logger.warning(
            "[%s] 本日の実現損益(%.2f)が最大許容損失(%.2f)に達したため、"
            "サーキットブレーカーが発動し新規エントリーをスキップします。",
            symbol, daily_pnl, max_daily_loss,
        )
        return

    exit_params = EXIT_PARAMS_BY_STRATEGY_TYPE[strategy_type]

    quantity = calculate_position_size(
        account_equity=account_equity,
        entry_price=price,
        stop_loss_pct=exit_params.stop_loss_pct,
        risk_per_trade_pct=RISK_PER_TRADE_PCT,
    )
    if quantity <= 0:
        logger.warning(
            "[%s] リスクベースの計算数量が0のため発注をスキップします"
            "（口座資金 %.2f に対して株価 %.2f が高すぎる可能性があります）。",
            symbol, account_equity, price,
        )
        return

    quantity = await _clamp_quantity_to_settled_cash_async(ib, symbol, quantity, price)
    if quantity <= 0:
        return

    # 損切りと利確はブローカー側に置く（ブラケット注文）。ボットのプロセスが
    # 落ちていても、TWSとの接続が切れていても、ポーリングを待たずに約定する。
    stop_price = resolve_stop_price(price, exit_params.stop_loss_pct)
    take_profit_price = resolve_take_profit_price(price, exit_params.take_profit_pct)

    # 発注は「出した時点」で数える。拒否された注文もブローカーへは届いており、
    # 約定だけを数えると全件拒否される状況で上限が効かない。
    attempt = position_manager.record_entry_order_attempt()
    logger.info("[%s] 本日%d回目の新規建てを発注します。", symbol, attempt)

    # 約定しなかった場合（拒否・取消・タイムアウト）は OrderNotFilledError が
    # 飛び、ここから先へは進まない。実体の無い建玉をローカルに記録しないため。
    order_result = await place_bracket_order_async(
        ib, contract, quantity=quantity,
        stop_price=stop_price, take_profit_price=take_profit_price,
        reference_price=price,
    )
    # 建値は実約定を優先する。参照価格で記録すると、損益・R倍率・トレーリングの
    # 基準がすべて実際の約定とずれる。
    entry_price = order_result.fill_price if order_result.fill_price is not None else price
    # 値段は実際にブローカーへ置いた待機注文のもの（呼値へ丸めた後）を記録する。
    # R倍率の分母も同じ値から取る。発注前の理論値で持つと、決済側の判定・記録が
    # ブローカーに置かれている注文とずれる。
    risk_per_share = max(entry_price - order_result.stop_price, 0.0)
    position_manager.open_position(
        symbol, entry_price=entry_price, quantity=order_result.quantity,
        risk_per_share=risk_per_share,
        strategy_type=strategy_type,
        stop_price=order_result.stop_price,
        take_profit_price=order_result.take_profit_price,
        oca_group=order_result.oca_group,
        entry_commission=order_result.commission,
        entry_price_is_fill=order_result.fill_price is not None,
    )

    # 参照価格と実約定のずれ、そしてその結果として待機注文が実際に置かれた
    # 位置を残す。**遅延データの参照価格は実約定と数%ずれるため、この記録が
    # 無いと「意図した -5%/+10% のはずの注文が、建値から見て -6.3%/+8.5% に
    # 並んでいる」ことに気付けない**（2026-08-05のAMBQ。`execution/fill_log.py`）。
    _record_fill(lambda: FILL_LOG.record_entry(
        symbol=symbol,
        quantity=order_result.quantity,
        intended_price=price,
        fill_price=order_result.fill_price,
        stop_price=order_result.stop_price,
        take_profit_price=order_result.take_profit_price,
        designed_stop_pct=exit_params.stop_loss_pct,
        designed_take_profit_pct=exit_params.take_profit_pct,
        account_equity=account_equity,
        commission=order_result.commission,
        dry_run=order_result.dry_run,
        quote_source=quote.source,
        is_stale=quote.is_stale,
    ))


async def _clamp_quantity_to_settled_cash_async(
    ib: IB, symbol: str, quantity: int, price: float,
) -> int:
    """新規建ての数量を、決済済み現金で実際に支払える株数まで切り下げる。

    リスクベースのサイジングはNetLiquidation（未受渡しの代金を含む評価額）を
    基準にしているため、「まだ手元に無い現金」を当てにした数量が出うる。
    その数量で発注してもIBKRは資金不足として拒否するだけなので、入口で
    現金の裏付けまで落としておく（ENFORCE_SETTLED_CASH_FUNDINGの説明を参照）。

    決済済み現金が取得できない場合は**数量を変えずに通す**。判定できないことの
    実害は注文が拒否されうることに留まり（GFVはIBSJ口座に適用されない）、
    ここで止めると口座やタグの都合だけで新規エントリーが全件停止するため、
    素通しの方が損失が小さい。
    """
    if not ENFORCE_SETTLED_CASH_FUNDING:
        return quantity

    settled_cash = await get_settled_cash_async(ib)
    if settled_cash is None:
        logger.warning(
            "[%s] 決済済み現金が取得できなかったため、資金の裏付けを確認せずに発注します。"
            "資金が不足していればIBKR側で注文が拒否されます。",
            symbol,
        )
        return quantity

    affordable_quantity = max(math.floor(settled_cash / price), 0)
    if affordable_quantity <= 0:
        logger.warning(
            "[%s] 決済済み現金 %.2f USD では1株(%.2f USD)も買えないため、"
            "新規エントリーを見送ります。",
            symbol, settled_cash, price,
        )
        return 0

    if affordable_quantity < quantity:
        logger.info(
            "[%s] 決済済み現金 %.2f USD の範囲に数量を切り下げます: %d株 -> %d株"
            "（残りは未受渡しの代金で、これで発注しても資金不足で拒否されうる）。",
            symbol, settled_cash, quantity, affordable_quantity,
        )
        return affordable_quantity

    return quantity


async def _resolve_usd_jpy_rate_async(ib: IB) -> Optional[float]:
    """円換算レートを、為替のマーケットデータ → 口座サマリーの順に取る。

    IDEALPROのUSD.JPYはマーケットデータの追加購読が要るため、購読の無い
    口座では3経路とも失敗し、ジャーナルの usd_jpy_rate が空のまま残る
    （2026-08-06に実測）。空欄でも稼働は続くが、確定申告用CSVの円換算が
    その年ぶんだけ埋まらない。口座サマリーのレートは購読なしで読める。

    どちらも取れなければNone。**推定値で埋めない**——記録が無いことは
    集計側が扱えるが（円換算合計から除外される）、間違ったレートは
    後から見分けられない。
    """
    rate = await get_usd_jpy_rate_async(ib)
    if rate is not None:
        return rate

    try:
        return await get_usd_to_base_rate_async(ib)
    except Exception:
        logger.exception("口座サマリーからの為替レート取得に失敗しました。")
        return None


async def _record_closed_trade(
    ib: IB, trade_journal: TradeJournal, closed_position: Position, exit_price: float, reason: str, pnl_pct: float,
    exit_commission: float = 0.0,
) -> None:
    pnl = (exit_price - closed_position.entry_price) * closed_position.quantity
    r_multiple = (
        (exit_price - closed_position.entry_price) / closed_position.risk_per_share
        if closed_position.risk_per_share > 0 else None
    )

    # 往復ぶんを記録する。建て側の手数料は決済時には分からないので、
    # 建玉と一緒に持ち越したものを使う。ドライラン中は実約定が無いため両方0。
    commission = closed_position.entry_commission + exit_commission
    usd_jpy_rate = await _resolve_usd_jpy_rate_async(ib)

    trade_journal.record_trade(
        symbol=closed_position.symbol,
        entry_price=closed_position.entry_price,
        exit_price=exit_price,
        quantity=closed_position.quantity,
        reason=reason,
        pnl=pnl,
        pnl_pct=pnl_pct,
        r_multiple=r_multiple,
        commission=commission,
        usd_jpy_rate=usd_jpy_rate,
        entry_date=closed_position.entry_date,
    )

    stats = trade_journal.compute_stats()
    logger.info(
        "トレード集計(累計): trades=%d win_rate=%.1f%% total_pnl=%.2f profit_factor=%.2f avg_R=%s",
        stats.num_trades, stats.win_rate_pct, stats.total_pnl, stats.profit_factor,
        f"{stats.avg_r_multiple:.2f}" if stats.avg_r_multiple is not None else "N/A",
    )


async def _process_exit_async(
    ib: IB, symbol: str, position_manager: PositionManager, trade_journal: TradeJournal,
    caches: MarketDataCaches,
) -> None:
    position = position_manager.get_position(symbol)
    if position is None:
        return

    contract = await caches.contracts.get_async(ib, symbol)

    # 1. ブローカー側に置いた待機注文（損切りの逆指値・利確の指値）が約定していないか。
    #    こちらはポーリングを待たずに市場で約定しているため、他の判定より先に確認する。
    #
    #    **現在価格の取得より前に見る。** この判定はブローカー側の約定だけを見るので
    #    現在価格を必要としない。価格取得の失敗の後ろに置くと、既に約定している決済が
    #    記録されないまま建玉がローカルに残り、同時保有枠(2)を占め続ける。
    #    実発注時はブローカー側の約定そのものを見る。ドライランの推定
    #    （観測した現在値が待機注文の値段に届いたか）は300秒ごとの1点しか
    #    見ないため、ザラ場で逆指値に触れて戻した動きを取りこぼす。
    if ENABLE_REAL_ORDERS:
        resting_fill = find_filled_resting_exit(ib, symbol)
        if resting_fill is not None:
            reason = (
                REASON_TAKE_PROFIT if resting_fill.order_type == "LMT" else REASON_STOP_LOSS
            )
            logger.info(
                # 約定価格の取得経路(source)まで残す。再接続で取り込んだ注文は
                # `avgFillPrice` が空で、Fillから復元できたかどうかがここにしか
                # 現れない（`_fill_price_with_source`）。
                "[%s] ブローカー側の待機注文が約定していました: reason=%s fill=%.2f "
                "commission=%.2f source=%s",
                symbol, reason, resting_fill.fill_price, resting_fill.commission,
                resting_fill.price_source,
            )
            pnl_pct = (
                (resting_fill.fill_price - position.entry_price) / position.entry_price * 100.0
            )
            # OCAグループの相方はIBKR側が自動で取り消すため、ここでの取り消しは不要。
            closed_position = position_manager.close_position(symbol)
            # 想定は「板に置いてあった注文の値段」。逆指値はトリガー後に成行へ
            # 変わるため値段が保証されず、ここの乖離がそのスリッページになる。
            # ブローカー同期で取り込んだ建玉は値段を持たない(0)ので、乖離は
            # 記録されず約定だけが残る。
            _record_fill(lambda: FILL_LOG.record_exit(
                symbol=symbol,
                quantity=closed_position.quantity,
                order_type=resting_fill.order_type,
                intended_price=(
                    closed_position.take_profit_price
                    if resting_fill.order_type == "LMT" else closed_position.stop_price
                ),
                fill_price=resting_fill.fill_price,
                commission=resting_fill.commission,
                dry_run=False,
                price_source=resting_fill.price_source,
            ))
            await _record_closed_trade(
                ib, trade_journal, closed_position, resting_fill.fill_price, reason, pnl_pct,
                exit_commission=resting_fill.commission,
            )
            return

    price = await get_current_price_async(ib, contract)
    if price is None:
        logger.warning("%s の現在価格が取得できなかったため決済判定をスキップします。", symbol)
        return

    position_manager.update_highest_price(symbol, price)

    # ブローカー同期で取り込んだ未追跡ポジションは待機注文を持たない(値段が0)ため対象外。
    if not ENABLE_REAL_ORDERS and position.stop_price > 0 and position.take_profit_price > 0:
        resting_exit = detect_resting_order_exit(
            stop_price=position.stop_price,
            take_profit_price=position.take_profit_price,
            # ポーリングではバー内の値動きが分からないため、観測した現在値だけで判定する。
            bar_low=price,
            bar_high=price,
        )
        if resting_exit is not None:
            logger.info(
                "[%s] ブローカー側の待機注文が約定しました: reason=%s fill=%.2f",
                symbol, resting_exit.reason, resting_exit.fill_price,
            )
            fill_price = resting_exit.fill_price
            pnl_pct = (fill_price - position.entry_price) / position.entry_price * 100.0
            # OCAグループの相方はIBKR側が自動で取り消すため、ここでの取り消しは不要。
            closed_position = position_manager.close_position(symbol)
            await _record_closed_trade(
                ib, trade_journal, closed_position, fill_price, resting_exit.reason, pnl_pct,
            )
            return

    # 2. ボット側で判定するもの（大引け前の強制決済・トレーリングストップ）。
    #    どちらも成行で出すため、先に待機注文を取り消さないと、決済済みの銘柄に
    #    売り注文だけが残る。
    #
    #    実発注時は、ブローカーが実際に持っていない建玉へSELLを出してはならない。
    #    ドライラン期間に作った想定ポジションが状態ファイルに残っていると、
    #    実発注を有効にした瞬間に「持っていない株の成行売り」＝売り建てになる。
    #    決済を見送る方向は安全側で、損切り・利確はブローカー側の待機注文が
    #    受け持っている（そもそも待機注文も無いので、守るべき建玉が無い）。
    if ENABLE_REAL_ORDERS and not position_manager.is_confirmed_by_broker(symbol):
        logger.error(
            "[%s] ローカルには建玉がありますが、ブローカー側に実在しません。"
            "成行決済を見送ります（持っていない株を売ると売り建てになるため）。"
            "ドライラン期間の想定ポジションが %s に残っている可能性があります。",
            symbol, DEFAULT_STATE_PATH,
        )
        return
    if position.strategy_type == STRATEGY_TYPE_DAY and is_day_trade_flatten_time():
        logger.info(
            "[%s] デイトレードポジションが大引け前の強制決済時刻に達したため決済します。", symbol,
        )
        exit_result = await _market_exit_async(ib, contract, position, price)
        exit_price = exit_result.fill_price if exit_result.fill_price is not None else price
        pnl_pct = (exit_price - position.entry_price) / position.entry_price * 100.0
        closed_position = position_manager.close_position(symbol)
        _record_market_exit_fill(closed_position, price, exit_result)
        await _record_closed_trade(
            ib, trade_journal, closed_position, exit_price, REASON_EOD_FLATTEN, pnl_pct,
            exit_commission=exit_result.commission,
        )
        return

    exit_params = EXIT_PARAMS_BY_STRATEGY_TYPE.get(
        position.strategy_type, EXIT_PARAMS_BY_STRATEGY_TYPE[STRATEGY_TYPE_SWING]
    )

    result = detect_exit_signal(
        symbol,
        entry_price=position.entry_price,
        current_price=price,
        highest_price_since_entry=position.highest_price,
        take_profit_pct=exit_params.take_profit_pct,
        stop_loss_pct=exit_params.stop_loss_pct,
        trailing_stop_pct=exit_params.trailing_stop_pct,
    )
    if not result.should_sell:
        return

    exit_result = await _market_exit_async(ib, contract, position, price)
    # 決済価格も実約定を優先する。判定に使った観測価格と実際の約定は
    # 成行のスリッページのぶんだけずれる。
    exit_price = exit_result.fill_price if exit_result.fill_price is not None else price
    pnl_pct = (exit_price - position.entry_price) / position.entry_price * 100.0
    closed_position = position_manager.close_position(symbol)
    _record_market_exit_fill(closed_position, price, exit_result)
    await _record_closed_trade(
        ib, trade_journal, closed_position, exit_price, result.reason, pnl_pct,
        exit_commission=exit_result.commission,
    )


def _record_market_exit_fill(closed_position, observed_price: float, exit_result) -> None:
    """Bot側の判断で出した成行決済の約定を記録する。

    想定価格には**判断に使った観測価格**を渡す。ここの乖離は成行のスリッページ
    だけでなく、300秒のポーリング間隔で価格を見ていることによる遅れも含む
    （待機注文の約定と性質が違うので、`order_type` で区別できるようにしてある）。
    """
    _record_fill(lambda: FILL_LOG.record_exit(
        symbol=closed_position.symbol,
        quantity=closed_position.quantity,
        order_type="MKT",
        intended_price=observed_price,
        fill_price=exit_result.fill_price,
        commission=exit_result.commission,
        dry_run=exit_result.dry_run,
    ))


async def _market_exit_async(ib: IB, contract, position, reference_price: float):
    """ボット側の判断で成行決済する（トレーリング・大引け）。

    **待機注文を先に取り消すのは不変条件だが、その直後に成行が失敗すると
    建玉だけが無防備で残る。** 2026-08-05のペーパー検証で実際に起きており、
    成行売りが60秒以内に約定せず取り消された後、次のサイクル（5分後）まで
    損切りの無い状態が続いた。約定しなかった理由が続くほどこの時間は伸びる。

    そのため失敗したら待機注文を置き直してから例外を上へ返す。呼び出し側
    （`run_watchlist_cycle_async`）は銘柄単位の例外を握り潰して次のサイクルで
    再試行するので、その間も建玉は保護されている。

    **取り消しは try の外に置く。** 取り消しが確定しなかった場合
    （`RestingOrderCancelTimeoutError`）は待機注文がまだ生きているので、
    置き直しに入ると同じ建玉に売り注文が二重に並ぶ。そのまま次のサイクルへ
    持ち越す方が安全である。
    """
    await cancel_bracket_orders_async(ib, contract.symbol)
    try:
        return await place_market_order_async(
            ib, contract, action="SELL", quantity=position.quantity,
        )
    except Exception:
        logger.exception(
            "[%s] 成行決済に失敗しました。取り消した待機注文を置き直します。", contract.symbol,
        )
        await _restore_resting_exit_orders_async(ib, contract, position, reference_price)
        raise


async def _restore_resting_exit_orders_async(
    ib: IB, contract, position, reference_price: float,
) -> None:
    """建玉に対する待機注文（損切り・利確）を置き直す。

    値段は建てたときの記録をそのまま使う。ここで現在値から計算し直すと、
    値下がりした局面で損切りが下へずれて、当初のリスク設計から離れる。

    **失敗しても例外を上げない。** これは復旧のための処理であり、ここで
    投げると呼び出し元の本来のエラー（決済の失敗）が置き換わって、何が
    起きたのか分からなくなる。無防備であることはログのERRORで残る。
    """
    if position.stop_price <= 0 or position.take_profit_price <= 0:
        # ブローカー同期で取り込んだ建玉には待機注文の値段が無い。
        logger.warning(
            "[%s] 待機注文の値段が記録されていないため置き直せません。", contract.symbol,
        )
        return
    try:
        await place_resting_exit_orders_async(
            ib, contract, quantity=position.quantity,
            stop_price=position.stop_price,
            take_profit_price=position.take_profit_price,
            reference_price=reference_price,
        )
    except Exception:
        logger.exception(
            "[%s] 待機注文の置き直しに失敗しました。建玉が無防備なまま残っています。",
            contract.symbol,
        )


async def _restore_missing_resting_orders_async(
    ib: IB, position_manager: PositionManager, caches: MarketDataCaches,
) -> None:
    """建玉があるのに待機注文が無い銘柄へ、待機注文を置き直す。

    待機注文は放っておいても消える。IB Gateway側のOrder Presetが有効期間を
    DAYへ上書きすると引けで失効し（2026-08-05に `Error 10349` として実測）、
    IBKR側の都合で取り消されることもある。**消えたことは通知されない**ので、
    毎サイクル突き合わせる以外に気付く方法が無い。

    **片方だけ生きている状態も「消えている」として扱う。** 同じ日に呼値違反で
    逆指値だけが不成立になり、利確だけが生きた建玉が残っている（＝下方向に
    無防備）。片方でもあれば保護ありと数えると、この状態を毎サイクル見逃す。

    置き直しは冪等である（両方が生きている銘柄は対象外になる）ため、
    通常のサイクルでは `reqAllOpenOrders` 1回で終わる。ヒストリカルデータの
    リクエストではないのでペーシング枠（「6.1」）も消費しない。
    """
    if not ENABLE_REAL_ORDERS or not position_manager.open_symbols():
        return

    protection = await find_resting_exit_protection_async(ib)
    for symbol in position_manager.open_symbols():
        state = protection.get(symbol)
        if state is not None and state.is_complete:
            _adopt_broker_resting_prices(position_manager, symbol, state)
            continue

        # 待機注文が約定した直後は、ブローカー側に建玉も待機注文も無い。
        # そこへ置き直すと**売り建てになる**。2026-08-18のINTCで実測した:
        # 22:49:37に逆指値が97.33で約定 → 22:51:30の突き合わせが「待機注文が
        # 無い」と判定してSTP/LMTを再送 → `Error 201` で拒否（この口座は
        # 評価額が証拠金取引の最低額に届かないため空売りが弾かれただけで、
        # 資金があれば2株の裸のショートが板に残っていた）。
        #
        # `RestingExitProtection.has_filled_exit` はこれを止められない。
        # 元になる `reqAllOpenOrdersAsync()` が返すのは**板に生きている注文**
        # だけで、約定済みの注文はそもそも含まれないためである（同日のログでは
        # 約定の1分53秒後の照会に逆指値が現れていない）。約定はローカルの
        # 取引ログを見る `find_filled_resting_exit` でしか読めない。
        if find_filled_resting_exit(ib, symbol) is not None:
            # 決済の記録はこの後の `_process_exit_async` が行う。
            logger.info(
                "[%s] 待機注文が約定済みのため置き直しません（建玉はもう閉じています）。", symbol,
            )
            continue

        # ブローカーが持っていない建玉へ売り注文を出すのも同じく売り建てになる。
        # 上の約定検知で拾えない経路（手動決済・ドライラン期間の想定ポジション）
        # に対する歯止めで、成行決済側と同じ判定を使う。
        if not position_manager.is_confirmed_by_broker(symbol):
            logger.error(
                "[%s] ローカルには建玉がありますが、ブローカー側に実在しません。"
                "待機注文の置き直しを見送ります（持っていない株に売り注文を出すと"
                "売り建てになるため）。", symbol,
            )
            continue

        position = position_manager.get_position(symbol)
        if state is not None and state.live_order_types:
            # 残っている片方を先に消す。消さずに両方を置き直すと、建玉を超える
            # 売り注文が並び、IBKRが超過分を空売りと見なして拒否する。
            logger.warning(
                "[%s] 待機注文が片方(%s)しか生きていません。取り消してから置き直します。",
                symbol, "/".join(sorted(state.live_order_types)),
            )
            await cancel_bracket_orders_async(ib, symbol)
        else:
            logger.warning(
                "[%s] 建玉があるのに待機注文がブローカー側にありません。置き直します。", symbol,
            )
        contract = await caches.contracts.get_async(ib, symbol)
        await _restore_resting_exit_orders_async(
            ib, contract, position, reference_price=position.entry_price,
        )


def _adopt_broker_resting_prices(
    position_manager: PositionManager, symbol: str, state: RestingExitProtection,
) -> None:
    """記録している待機注文の値段が板とずれていたら、板の値へ合わせて記録する。

    **生存確認だけでは値段のずれを検出できない。** 修正が拒否されても元の注文は
    生き続けるため（2026-08-06にINTCで `Error 10326` として実測。置き直しが
    両方とも拒否され、板は参照価格ベースの 93.38 / 108.12 のまま、
    `positions.json` には実約定ベースの 92.09 / 106.63 が残った）、
    両方が「生きている」ことは値段が意図どおりであることを意味しない。

    ずれを見つけたら板の値を正とする。実際に約定するのは板にある注文であり、
    ここで記録側に寄せると、R倍率が実際に負ったリスクと違う値で残る。
    """
    if state.has_filled_exit:
        # 約定済み＝建玉はもう閉じている。この後の決済処理が実約定を読むので、
        # ここで値段を触っても意味が無い。
        return

    changed = position_manager.adopt_broker_resting_prices(
        symbol, state.stop_price, state.take_profit_price,
    )
    for field, (recorded, live_price) in sorted(changed.items()):
        logger.warning(
            "[%s] 待機注文の %s が記録(%.2f)と板(%.2f)でずれています。"
            "実際に約定するのは板の注文なので、板の値段で記録し直しました。",
            symbol, field, recorded, live_price,
        )


async def process_symbol_async(
    ib: IB, symbol: str, position_manager: PositionManager, trade_journal: TradeJournal,
    caches: Optional[MarketDataCaches] = None,
) -> None:
    caches = caches if caches is not None else MarketDataCaches()

    if position_manager.has_position(symbol):
        await _process_exit_async(ib, symbol, position_manager, trade_journal, caches)
    else:
        await _process_entry_async(ib, symbol, position_manager, trade_journal, caches)


# 同時保有上限による見送りを、サイクルごとに1行へ絞るための印。
# `run_watchlist_cycle_async` の入口で毎サイクル落とす。
_position_limit_skip_logged_in_cycle: bool = False


async def run_watchlist_cycle_async(
    ib: IB, watchlist: List[str], position_manager: PositionManager, trade_journal: TradeJournal,
    caches: Optional[MarketDataCaches] = None,
) -> None:
    caches = caches if caches is not None else MarketDataCaches()

    global _position_limit_skip_logged_in_cycle
    _position_limit_skip_logged_in_cycle = False

    await position_manager.sync_with_broker_async(ib)

    # 建玉と待機注文の突き合わせは、決済判定より先に行う。待機注文が消えている
    # 間に決済判定へ入ると、その銘柄の処理が終わるまで無防備な時間が延びる。
    try:
        await _restore_missing_resting_orders_async(ib, position_manager, caches)
    except Exception:
        logger.exception("待機注文の突き合わせに失敗しました。決済判定は継続します。")

    # スクリーニング結果でウォッチリストが日次で入れ替わっても、既に保有中の
    # ポジションは（ウォッチリストから外れていても）決済判定を継続する必要が
    # あるため、ウォッチリストと保有中銘柄の和集合を処理対象にする。
    symbols_to_process = list(dict.fromkeys([*watchlist, *position_manager.open_symbols()]))

    for symbol in symbols_to_process:
        try:
            await process_symbol_async(ib, symbol, position_manager, trade_journal, caches)
        except Exception:
            logger.exception("%s の処理中にエラーが発生しました。", symbol)


def resolve_max_affordable_price(
    account_equity: float, settled_cash: Optional[float] = None,
    stop_loss_pct: float = SWING_STOP_LOSS_PCT,
) -> Optional[float]:
    """現在の口座資金で1株でも買える上限株価を返す。

    リスクベースのサイジングは
        数量 = floor((資金 × RISK_PER_TRADE_PCT%) ÷ (株価 × 損切り%))
    なので、数量が1株以上になる条件は
        株価 ≦ 資金 × (RISK_PER_TRADE_PCT% ÷ 損切り%)
    となる。

    損切り幅にはスイングの値(SWING_STOP_LOSS_PCT)を使う。損切りが広いほど
    上限株価は低くなるため、スイング・デイトレードのどちらの基準でも
    買える銘柄だけが残る。デイトレードの狭い損切り(1.5%)で計算すると、
    スイングでは数量0になる銘柄まで通してしまう。

    キャッシュ口座では、1株の値段が決済済み現金を超える銘柄も買えない
    （_clamp_quantity_to_settled_cash_asyncが数量0に切り下げる）。そのため
    settled_cashが渡された場合は、上式との**小さい方**を上限とする。
    これを無視すると、買えない銘柄が監視枠を占め続ける。

    資金が取得できない場合(0以下)はNoneを返して**フィルターを掛けない**。
    ここで0を返すと全銘柄が除外され、ウォッチリストが空になったまま
    稼働し続けることになる。決済済み現金が取れなかった場合(None)も同じ理由で
    上限を狭めない。実際に建てられるかどうかはエントリー時に必ず再判定するため、
    ここで絞り込みを外しても未受渡し資金で建ててしまうことはない。
    """
    if account_equity <= 0:
        logger.warning(
            "口座資金が取得できなかったため(%.2f)、株価上限フィルターを無効にします。",
            account_equity,
        )
        return None

    max_price = account_equity * (RISK_PER_TRADE_PCT / stop_loss_pct)

    if settled_cash is not None and 0 < settled_cash < max_price:
        logger.info(
            "決済済み現金 %.2f USD の方が小さいため、上限株価をこちらに合わせます"
            "（%.2f USD -> %.2f USD）。",
            settled_cash, max_price, settled_cash,
        )
        return settled_cash

    return max_price


def _log_watchlist_truncation(candidates: List[str], slots: int) -> None:
    """監視枠に入りきらなかった銘柄を、取引日1回だけ名指しで残す。

    **件数だけでは、どの銘柄が落ちたのかが分からない。** 切り詰めは記載順
    （＝アルファベット順）なので、落ちるのは常に末尾である。2026-08-18より前は
    VZ / WFC / WMT / XOM が毎日必ず外れていたが、それに気付くにはログを手で
    数えるしかなかった。

    **帯を通る件数は資金に比例して増える**ので、増資するたびにこの切り詰めは
    静かに悪化する（$1,183で23件・枠に収まる → $3,142で38件）。名前が出ていれば
    引け後のサマリで気付ける。

    INFOではなくWARNINGにするのは、これが**検証した母集団の一部を使っていない**
    という状態だからである。取引日1回に絞るのは、スクリーニングが失敗する日に
    900秒ごとの再試行で同じ行が26回並ぶため（「3. 実行環境と設定」のログ方針）。
    """
    dropped = candidates[slots:]
    if not _should_log_once_per_trading_day("watchlist_truncation", ",".join(dropped)):
        return
    logger.warning(
        "株価帯を通った%d件が監視枠(%d)に入りきらないため、記載順で末尾の%d件を"
        "監視対象から外します: %s。**検証した母集団の一部が使われていません。**"
        "枠を増やすなら POLL_INTERVAL_SECONDS も一緒に延ばすこと"
        "（MAX_WATCHLIST_SIZE × (600 / POLL_INTERVAL_SECONDS) ≦ 60）。",
        len(candidates), slots, len(dropped), ", ".join(dropped),
    )


def resolve_min_tradeable_price(
    account_equity: float, stop_loss_pct: float = SWING_STOP_LOSS_PCT,
) -> Optional[float]:
    """株数クランプが掛からずに済む下限株価を返す。

    リスクベースのサイジングは
        数量 = floor((資金 × RISK_PER_TRADE_PCT%) ÷ (株価 × 損切り%))
    なので、数量が MAX_POSITION_SIZE を超える条件は
        株価 < 資金 × (RISK_PER_TRADE_PCT% ÷ 損切り%) ÷ MAX_POSITION_SIZE
    となる。これを下回る銘柄は、シグナルが出ても株数クランプで建玉が
    小さくなり、**1トレードのリスクが RISK_PER_TRADE_PCT% に届かない**。

    除外するのは手数料比率が跳ね上がるため。1注文あたりの最低手数料(0.35 USD)は
    建玉の大きさによらず固定なので、クランプで建玉が縮むとその比率だけが上がる。
    JOBY(7.05 USD)の実測（当時の10株クランプ）では、本来34株($238.92)のところ
    10株($70.50)にクランプされ、往復手数料の約定代金比が0.29% -> 0.99%、
    リスクが1.00% -> 0.29% になっていた（CLAUDE.md「検証時の初期資金」節）。
    この条件下ではバックテストのPFが実運用に当てはまらない。

    上限株価と同じく損切り幅にはスイングの値を使い、資金が取得できない場合
    (0以下)はNoneを返してフィルターを掛けない（ウォッチリストが空になるため）。

    なお floor() があるため、連続量の数量が MAX_POSITION_SIZE.x 株になる帯
    （$1,220・40株なら $5.95〜$6.10）では実際にはクランプが効かない。この帯の
    上端を下限に置いているので、その分だけ安全側に余計に除外する。
    """
    if account_equity <= 0:
        return None

    return account_equity * (RISK_PER_TRADE_PCT / stop_loss_pct) / MAX_POSITION_SIZE


def growth_price_band(
    account_equity: float, settled_cash: Optional[float] = None,
) -> Tuple[Optional[float], Optional[float]]:
    """グロース株トラックの株価帯を返す（下限, 上限）。

    **スイングの帯を流用してはならない。** 帯は損切り幅から決まり
    （`建玉金額 = 資金 × (リスク% ÷ 損切り%)`）、-12%の損切りでは
    予算がスイングの 5/12 = 41.7% になる。$1,220 なら上限が $244 -> $101 で、
    スイングの帯で通した銘柄はグロース側では数量0になりうる——監視枠を
    消費したうえで毎サイクル必ずスキップされる状態である。
    """
    return (
        resolve_min_tradeable_price(account_equity, GROWTH_STOP_LOSS_PCT),
        resolve_max_affordable_price(account_equity, settled_cash, GROWTH_STOP_LOSS_PCT),
    )


# 株価帯で除外した銘柄を、その取引日に1度だけ記録するための印。
# スクリーニングが失敗した日はこのフィルターが900秒ごとに再実行され
# （`SCREENING_RETRY_INTERVAL_SECONDS`）、同じ除外が1日26回ぶん並ぶ。
# 2026-08-12〜14のVPSログでは18銘柄×26回＝468行/日がWARNINGを占めており、
# 「読むべき1行」を埋める側に回っていた（「3. 実行環境と設定」のログ方針）。
# 株価帯は資金と終値から1日1回決まるので、同じ日の2回目以降は情報が無い。
_once_per_day_logged: Tuple[Optional[str], Set[Tuple[str, str]]] = (None, set())


def _should_log_once_per_trading_day(
    kind: str, symbol: str, now: Optional[datetime] = None,
) -> bool:
    """その取引日にまだ記録していない (種類, 銘柄) なら True を返す。

    取引日が変わったら印を捨てる。持ち越すと翌日の1行目が出なくなり、
    条件が変わって監視候補が痩せたことに気付けなくなる。

    `kind` で種類を分けるのは、株価帯の除外と長期トレンドの見送りが同じ銘柄に
    同時に起きうるためである。まとめると片方しか出ない。
    """
    global _once_per_day_logged

    trading_day = (now or datetime.now(US_EASTERN)).astimezone(US_EASTERN).date().isoformat()
    logged_day, logged_keys = _once_per_day_logged
    if logged_day != trading_day:
        logged_keys = set()
        _once_per_day_logged = (trading_day, logged_keys)

    key = (kind, symbol)
    if key in logged_keys:
        return False
    logged_keys.add(key)
    return True


def _should_log_price_band_exclusion(symbol: str, now: Optional[datetime] = None) -> bool:
    return _should_log_once_per_trading_day("price_band", symbol, now)


async def _filter_symbols_by_price_band_async(
    ib: IB, symbols: List[str], min_price: Optional[float], max_price: Optional[float],
    caches: MarketDataCaches,
) -> List[str]:
    """取引可能な株価帯に入っている銘柄だけを返す。

    スクリーニングの結果には `ScreenerConfig` が同じ判定を掛けるが、
    **スクリーニングが失敗・0件のときに使うフォールバックのリストは
    その判定を通らない**。固定リスト(`WATCHLIST`)は資金額と無関係に
    書かれているため、素通しすると次のどちらかが起きる。

    - 上限超え: 毎サイクルのリクエスト枠を消費したうえで必ず数量0でスキップ
    - 下限割れ: `MAX_POSITION_SIZE` のクランプが掛かった建玉ができる

    2026-07-30〜07-31のドライランで実際に建った唯一のポジション(JOBY $7.05)は
    後者であり、当時の固定リスト8銘柄のうち取引可能なのは2銘柄だけだった。

    日足は `DailyBarCache` から取るので、**追加のIBKRリクエストは発生しない**
    （同じ銘柄の日足はこの後のシグナル判定でどのみち取得される）。

    株価が取得できない銘柄は除外に倒す。素通しすると、買えない銘柄が
    監視枠を占め続けても気付けない（スクリーナー側の判定と揃えている）。
    """
    if min_price is None and max_price is None:
        return symbols

    kept: List[str] = []
    for symbol in symbols:
        try:
            contract = await caches.contracts.get_async(ib, symbol)
            daily_df = await caches.daily_bars.get_async(ib, contract)
        except Exception:
            logger.exception(
                "[%s] 株価の判定に失敗したため、監視対象から外します。", symbol,
            )
            continue

        if daily_df.empty or "close" not in daily_df.columns:
            logger.warning(
                "[%s] 株価が取得できなかったため、監視対象から外します。", symbol,
            )
            continue

        price = float(daily_df["close"].iloc[-1])
        if max_price is not None and price > max_price:
            if _should_log_price_band_exclusion(symbol):
                logger.warning(
                    "[%s] 株価(%.2f USD)が上限(%.2f USD)を超えるため監視対象から外します"
                    "（現在の口座資金では数量が0株になる）。",
                    symbol, price, max_price,
                )
            continue
        if min_price is not None and price < min_price:
            if _should_log_price_band_exclusion(symbol):
                logger.warning(
                    "[%s] 株価(%.2f USD)が下限(%.2f USD)を下回るため監視対象から外します"
                    "（株数クランプでリスクベースのサイジングが効かない）。",
                    symbol, price, min_price,
                )
            continue
        kept.append(symbol)

    return kept


def _latest_close_price(daily_df: pd.DataFrame) -> float:
    return float(daily_df["close"].iloc[-1])


def _long_term_moving_average(daily_df: pd.DataFrame) -> float:
    return float(daily_df["close"].iloc[-STRUGGLING_MA_WINDOW:].mean())


def _pct_to_long_term_ma(daily_df: pd.DataFrame) -> float:
    """終値が長期移動平均まであと何%かを返す（正の値＝あとどれだけ上げれば復帰か）。

    除外の理由だけをログに残しても、その銘柄が復帰間近なのか遠いのかが
    分からない。**除外は永続化されず毎回やり直されるので**（`main()` は
    スクリーニングが成功した日以外 `fallback_watchlist` を入れ替えない）、
    この距離が縮めばその銘柄は自動的に監視へ戻り、押し目が出れば建つ。
    運用者が「待てば戻る銘柄」と「当面戻らない銘柄」を見分けるための値である。
    """
    close = _latest_close_price(daily_df)
    moving_average = _long_term_moving_average(daily_df)
    if close <= 0:
        return 0.0
    return (moving_average / close - 1.0) * 100.0


@dataclass
class WatchlistScreen:
    """トレンド・本数の判定結果。

    監視と売買可否を**別の集合として返す**。監視に残す銘柄でも建てられない
    ことがあるため（`KEEP_UNTRADEABLE_SYMBOLS_IN_WATCHLIST`）、1つのリストでは
    表現できない。注目銘柄の引き継ぎは `untradeable` を見て打ち切る——
    引き継ぎ続けると、下降トレンドの銘柄が記録に残ったまま毎日組み入れられる。
    """

    monitored: List[str]
    untradeable: Set[str] = field(default_factory=set)


async def _screen_watchlist_symbols_async(
    ib: IB, symbols: List[str], caches: MarketDataCaches, protected: Sequence[str] = (),
) -> WatchlistScreen:
    """明確な下降トレンドの銘柄と、本数が足りない銘柄を判定する。

    判定は銘柄選定と同じ「終値が長期移動平均(`STRUGGLING_MA_WINDOW`日)を
    下回っているか」で、`strategy.screener.is_in_long_term_uptrend` を共有する。
    別の物差しを新たに作らないのは、選定で通した条件と維持で使う条件が
    食い違うと、入れた翌日に外すような振る舞いになるため。

    日足は `DailyBarCache` から取るので追加リクエストは発生しない
    （キャッシュの取得期間を300日にしてあるのはこの判定のためでもある）。

    **既定では外さず、復帰までの距離を記録して監視に残す**
    (`KEEP_UNTRADEABLE_SYMBOLS_IN_WATCHLIST`)。建てさせない役目は
    エントリー側(`ENTRY_REQUIRES_LONG_TERM_UPTREND` /
    `SWING_MIN_HISTORY_BARS`)へ移してあるので、残しても建たない。
    残す利点は、乖離率と復帰までの距離が毎サイクル記録され、
    **トレンドが上向いた瞬間にその場でエントリー判定へ入れる**こと。
    外していると、復帰の判定が1日1回のウォッチリスト更新まで遅れる。

    枠を超える場合はフラグをFalseにして落とす。その場合も本数が揃うか
    トレンドが戻れば翌日の更新で戻ってくるので、締め出しにはならない。

    `protected` は保有中の銘柄。外してもポジションの決済判定は続く
    （`run_watchlist_cycle_async` が保有銘柄との和集合を処理する）が、
    再エントリーの判断ができなくなるため残す。
    """
    kept: List[str] = []
    untradeable: Set[str] = set()
    for symbol in symbols:
        if symbol in protected:
            kept.append(symbol)
            continue
        try:
            contract = await caches.contracts.get_async(ib, symbol)
            daily_df = await caches.daily_bars.get_async(ib, contract)
        except Exception:
            logger.exception("[%s] トレンド判定に失敗したため、監視対象に残します。", symbol)
            kept.append(symbol)
            continue

        # 監視に残す場合は「外します」と書けない。建てられない状態であることと
        # 復帰までの距離は、残す場合こそ読みたい情報である（ただし1取引日に1回。
        # スクリーニングが空を返す日はこの判定が900秒ごとに再実行され、同じ行が
        # 26回ぶん並ぶ。復帰までの距離は日足から決まるので2回目以降に情報は無い）。
        disposition = (
            "監視は継続します（新規建てはエントリー側で見送ります）"
            if KEEP_UNTRADEABLE_SYMBOLS_IN_WATCHLIST else "監視対象から外します"
        )

        if len(daily_df) < SWING_MIN_HISTORY_BARS:
            if _should_log_once_per_trading_day("history", symbol):
                logger.info(
                    "[%s] 日足が%d本しかなく長期トレンドを判定できないため、%s。"
                    "再エントリーまで残り%d営業日。",
                    symbol, len(daily_df), disposition,
                    SWING_MIN_HISTORY_BARS - len(daily_df),
                )
            untradeable.add(symbol)
            if KEEP_UNTRADEABLE_SYMBOLS_IN_WATCHLIST:
                kept.append(symbol)
            continue

        in_uptrend = is_in_long_term_uptrend(daily_df, STRUGGLING_MA_WINDOW)
        if in_uptrend is False:
            if _should_log_once_per_trading_day("downtrend", symbol):
                logger.info(
                    "[%s] 終値が%d日移動平均を下回る下降トレンドのため、%s。"
                    "終値%.2f / MA%d %.2f（あと%+.1f%%で復帰）。",
                    symbol, STRUGGLING_MA_WINDOW, disposition,
                    _latest_close_price(daily_df), STRUGGLING_MA_WINDOW,
                    _long_term_moving_average(daily_df),
                    _pct_to_long_term_ma(daily_df),
                )
            untradeable.add(symbol)
            if not KEEP_UNTRADEABLE_SYMBOLS_IN_WATCHLIST:
                continue
        kept.append(symbol)

    return WatchlistScreen(monitored=kept, untradeable=untradeable)


async def _scan_turnover_ranks_async(
    ib: IB, min_price: Optional[float], max_price: Optional[float],
) -> Dict[str, int]:
    """NASDAQとNYSEの売買代金上位を統合し、「銘柄 -> 順位」を返す。

    取引所ごとに分けて呼ぶのは、`numberOfRows` の上限が50だからである
    （上位100件を1回では取れない）。統合後の順位は
    `strategy.attention.build_rank_map` が決める。
    """
    ranked: List[str] = []
    for location in ATTENTION_SCAN_LOCATIONS:
        symbols = await run_turnover_scan_async(
            ib, location_code=location, number_of_rows=ATTENTION_SCAN_ROWS,
            above_price=min_price, below_price=max_price,
        )
        ranked.extend(symbols)
    return build_rank_map(ranked)


async def _apply_attention_watchlist_async(
    ib: IB, watchlist: List[str], account_equity: float,
    caches: MarketDataCaches, position_manager: PositionManager,
    store: RankHistoryStore, now: Optional[datetime] = None,
) -> List[str]:
    """売買代金の急上昇銘柄を組み入れ、下降トレンドの銘柄を落とす。

    1日1回だけ呼ぶこと。2つの機能は独立していて、それぞれ
    `DROP_STRUGGLING_SYMBOLS` / `ENABLE_ATTENTION_WATCHLIST` で切り替える。
    組み入れを有効にするとスキャナー2回ぶんのリクエストが増える（順位しか
    見ないので、銘柄ごとの追加取得は行わない）。

    **順序が重要である。** 先に下降トレンドの銘柄を落としてから急上昇銘柄を
    足す。逆にすると、枠が埋まっていて新しい銘柄が入らない。

    **前日までに組み入れた注目銘柄は引き継ぐ**（`store.load_attention_symbols`）。
    毎日ゼロから組み直すと、急上昇の翌日にランキングが落ち着いた時点で監視から
    外れ、押し目が出るまで持ち続けられない。引き継いだ銘柄も下降トレンドの
    判定と枠の上限は同じように受ける。

    枠は `MAX_WATCHLIST_SIZE` で頭打ちにし、**渡されたウォッチリストを優先する**。
    引き継ぎと急上昇は残った枠に入る。保有中の銘柄は落とさない。
    """
    protected = position_manager.open_symbols()
    min_price = resolve_min_tradeable_price(account_equity)
    max_price = resolve_max_affordable_price(account_equity)

    carried = store.load_attention_symbols() if ENABLE_ATTENTION_WATCHLIST else []
    combined = list(dict.fromkeys([*watchlist, *carried]))

    screen = (
        await _screen_watchlist_symbols_async(ib, combined, caches, protected)
        if DROP_STRUGGLING_SYMBOLS else WatchlistScreen(monitored=combined)
    )
    # 引き継ぎで枠を超えることがある。渡されたウォッチリストが先に並んでいるので、
    # 前から切ればそちらが優先される。
    kept = screen.monitored[:MAX_WATCHLIST_SIZE]
    if not ENABLE_ATTENTION_WATCHLIST:
        return kept

    # 引き継ぎの記録は、スキャンの成否より前に更新しておく。ここを後回しにすると、
    # スキャンが失敗した日に「下降トレンドで落とした銘柄」が記録に残り続け、
    # 翌日また組み入れては落とすことを繰り返す。
    #
    # **監視に残していても、建てられない銘柄は引き継がない。** 引き継ぎは
    # 「押し目が出るまで持ち続ける」ための仕組みなので、建てられない銘柄を
    # 残すと枠を占めたまま毎日組み入れ直すことになる。
    surviving_carried = [
        symbol for symbol in kept
        if symbol in carried and symbol not in screen.untradeable
    ]
    if carried:
        logger.info("前日までの注目銘柄を引き継ぎました: %s", surviving_carried)
    store.save_attention_symbols(surviving_carried)

    try:
        today_ranks = await _scan_turnover_ranks_async(ib, min_price, max_price)
    except Exception:
        logger.exception("売買代金スキャンに失敗しました。ウォッチリストの入れ替えのみ行います。")
        return kept

    if not today_ranks:
        return kept

    trading_day = (now or datetime.now(US_EASTERN)).astimezone(US_EASTERN).date().isoformat()
    history = store.load()
    surges = detect_rank_surges(today_ranks, history, ATTENTION_CONFIG)
    store.append(trading_day, today_ranks)

    if not has_enough_history(history, ATTENTION_CONFIG):
        # 履歴が浅いうちは全銘柄の基準がランク外になり、上位が軒並み
        # 「急上昇」になる。記録だけ進めて組み入れは見送る。
        logger.info(
            "売買代金ランキングの履歴が%d日ぶんしかないため、注目銘柄の組み入れは見送ります"
            "（基準順位が確定するまでは上位銘柄と区別できません）。",
            len(history),
        )
        return kept

    if surges:
        logger.info("売買代金が急上昇した銘柄: %s", surges)

    added: List[str] = []
    for symbol in surges:
        if len(kept) + len(added) >= MAX_WATCHLIST_SIZE:
            break
        if symbol in kept or symbol in added:
            continue
        added.append(symbol)

    if added:
        # 株価帯だけは掛け直す。スキャナー側にも渡しているが、通らなかった
        # 場合（フィルタ非対応の口座など）にそのまま入れてしまわないため。
        added = await _filter_symbols_by_price_band_async(ib, added, min_price, max_price, caches)

    if added:
        logger.info(
            "注目銘柄として監視対象に追加します: %s（監視%d -> %d銘柄）",
            added, len(kept), len(kept) + len(added),
        )
    store.save_attention_symbols(surviving_carried + added)
    return kept + added


class WatchlistRefresh(NamedTuple):
    """ウォッチリスト更新の結果と、それがスクリーニング由来かどうか。

    `screened=False`（フォールバック）を呼び出し側が区別できないと、
    「その日はもう選定済み」として扱われ、一時的な失敗が1日ぶん確定する。
    `symbols` は失敗時も使える監視対象（フォールバックの固定リストを
    株価帯で絞ったもの）で、取引可能な銘柄が無ければ空になる。
    """

    symbols: List[str]
    screened: bool


async def _concentrated_watchlist_async(
    ib: IB, symbol: str, account_equity: float, settled_cash: Optional[float],
    caches: MarketDataCaches,
) -> WatchlistRefresh:
    """単一銘柄への集中モードのウォッチリストを返す。

    スクリーニングも固定リストも使わない。**株価帯の判定だけは掛ける**——
    指定した銘柄が現在の資金で買えなければ、シグナルが出ても数量0で
    スキップされ続けるだけであり、それを「静かに何も起きない」状態にしない。

    帯を外れた場合は**空を返してERRORにする。42銘柄へフォールバックしない。**
    運用者が明示的に1銘柄を指定している以上、黙って別の銘柄を売買する方が
    危険である（保有中の建玉の決済判定は `run_watchlist_cycle_async` が
    保有銘柄との和集合を取るので継続する）。

    `screened=True` を返すのは、900秒ごとの再試行を止めるためである
    （集中モードでは再試行しても結果が変わらない）。
    """
    if is_growth_symbol(symbol):
        min_price, max_price = growth_price_band(account_equity, settled_cash)
        track = f"グロース株(損切り-{GROWTH_STOP_LOSS_PCT:.1f}%)"
    else:
        min_price = resolve_min_tradeable_price(account_equity)
        max_price = resolve_max_affordable_price(account_equity, settled_cash)
        track = f"スイング(損切り-{SWING_STOP_LOSS_PCT:.1f}%)"

    filtered = await _filter_symbols_by_price_band_async(
        ib, [symbol], min_price, max_price, caches,
    )
    if not filtered:
        logger.error(
            "集中モードの銘柄 %s が取引可能な株価帯(%s〜%s USD・%s)から外れています。"
            "新規エントリーは発生しません。CONCENTRATED_SYMBOL を見直すか、"
            "損切り幅に見合う資金を用意してください。",
            symbol,
            f"{min_price:.2f}" if min_price is not None else "下限なし",
            f"{max_price:.2f}" if max_price is not None else "上限なし",
            track,
        )
        return WatchlistRefresh([], screened=True)

    logger.warning(
        "単一銘柄への集中モードで稼働します: %s（%s）。"
        "**分散は効きません**——銘柄横断で検証したエッジはこの構成には当てはまりません。",
        symbol, track,
    )
    return WatchlistRefresh(filtered, screened=True)


async def _growth_watchlist_async(
    ib: IB, account_equity: float, settled_cash: Optional[float],
    caches: MarketDataCaches,
) -> List[str]:
    """グロース株トラックの監視銘柄を、専用の株価帯で絞って返す。

    トラックが無効なら空。枠は `GROWTH_WATCHLIST_SLOTS` で頭打ちにする
    （`MAX_WATCHLIST_SIZE` の内数。総数がペーシングの不変条件を決めるため）。
    切り詰めは記載順で、**成績を見て決めた順ではない**。
    """
    if not ENABLE_GROWTH_SWING:
        return []

    min_price, max_price = growth_price_band(account_equity, settled_cash)
    logger.info(
        "グロース株トラックの株価帯は %s〜%s USD です（損切り-%.1f%%のため"
        "建玉予算がスイングの%.0f%%になる）。",
        f"{min_price:.2f}" if min_price is not None else "下限なし",
        f"{max_price:.2f}" if max_price is not None else "上限なし",
        GROWTH_STOP_LOSS_PCT, SWING_STOP_LOSS_PCT / GROWTH_STOP_LOSS_PCT * 100.0,
    )
    filtered = await _filter_symbols_by_price_band_async(
        ib, list(GROWTH_WATCHLIST), min_price, max_price, caches,
    )
    if len(filtered) > GROWTH_WATCHLIST_SLOTS:
        logger.info(
            "グロース株%d件のうち、記載順で上位%d件のみを監視対象にします。",
            len(filtered), GROWTH_WATCHLIST_SLOTS,
        )
        filtered = filtered[:GROWTH_WATCHLIST_SLOTS]
    if filtered:
        logger.info("グロース株トラックの監視銘柄: %s", filtered)
    else:
        logger.warning(
            "グロース株トラックが有効ですが、株価帯を通る銘柄が1件もありません"
            "（損切り-%.1f%%では建玉予算が資金の%.1f%%しかないため、"
            "高株価の銘柄は1株も買えません）。",
            GROWTH_STOP_LOSS_PCT, RISK_PER_TRADE_PCT / GROWTH_STOP_LOSS_PCT * 100.0,
        )
    return filtered


async def _refresh_watchlist_async(
    ib: IB, fallback_watchlist: List[str], account_equity: float,
    settled_cash: Optional[float] = None, caches: Optional[MarketDataCaches] = None,
) -> WatchlistRefresh:
    max_price = resolve_max_affordable_price(account_equity, settled_cash)
    if max_price is not None:
        logger.info(
            "口座資金 %.2f USD で買える上限株価は %.2f USD です（これを超える銘柄は"
            "数量が0株になるため、監視対象から除外します）。",
            account_equity, max_price,
        )

    min_price = resolve_min_tradeable_price(account_equity)
    if min_price is not None:
        logger.info(
            "口座資金 %.2f USD で株数クランプ(%d株)が掛からない下限株価は %.2f USD です"
            "（これを下回る銘柄は1トレードのリスクが %.1f%% に届かず、手数料比率が"
            "跳ね上がるため監視対象から除外します）。",
            account_equity, MAX_POSITION_SIZE, min_price, RISK_PER_TRADE_PCT,
        )

    config = ScreenerConfig(
        max_price=max_price,
        min_price=min_price,
        market_cap_above=SCREENER_MIN_MARKET_CAP,
        market_cap_below=SCREENER_MAX_MARKET_CAP,
        max_pe_ratio=SCREENER_MAX_PE_RATIO,
        scan_code=SCREENER_SCAN_CODE,
        number_of_rows=SCREENER_NUM_CANDIDATES,
        pe_request_interval_seconds=SCREENER_PE_REQUEST_INTERVAL_SECONDS,
        enable_trend_filter=SCREENER_ENABLE_TREND_FILTER,
        trend_ma_window=SCREENER_TREND_MA_WINDOW,
        trend_lookback_duration=SCREENER_TREND_LOOKBACK_DURATION,
    )

    caches = caches if caches is not None else MarketDataCaches()

    if CONCENTRATED_SYMBOL is not None:
        return await _concentrated_watchlist_async(
            ib, CONCENTRATED_SYMBOL, account_equity, settled_cash, caches,
        )

    # グロース株はスイングとは別の株価帯で絞り、専用枠を先に確保する。
    # 後ろに足して全体を切り詰めると、記載順の都合でグロース株だけが
    # 毎日落ちる（2026-08-18に末尾4銘柄で実際に起きた形と同じ）。
    growth_symbols = await _growth_watchlist_async(
        ib, account_equity, settled_cash, caches,
    )
    swing_slots = max(0, MAX_WATCHLIST_SIZE - len(growth_symbols))

    async def _fallback(cause: str) -> WatchlistRefresh:
        # フォールバックはスクリーニングの判定を通っていないため、株価帯だけは
        # ここで掛け直す。掛けないと固定リストの買えない銘柄がそのまま監視枠に入る。
        filtered = await _filter_symbols_by_price_band_async(
            ib, fallback_watchlist, min_price, max_price, caches,
        )
        if not filtered:
            # ここに落ちるのは「スクリーニングも効かず、固定リストにも取引可能な
            # 銘柄が無い」状態。新規エントリーは一切起きない（保有中の決済判定は
            # run_watchlist_cycle_asyncが保有銘柄との和集合を取るので継続する）。
            # 静かに空で回り続けると気付けないため、ここだけはERRORで出す。
            logger.error(
                "%s、フォールバックの固定ウォッチリスト%sにも取引可能な株価"
                "(%s〜%s USD)の銘柄がありません。新規エントリーは発生しません。"
                "スクリーニングの購読権限を `python -m scripts.check_screener` で"
                "確認し、固定リストを現在の資金額に合わせて見直してください。",
                cause, fallback_watchlist,
                f"{min_price:.2f}" if min_price is not None else "下限なし",
                f"{max_price:.2f}" if max_price is not None else "上限なし",
            )
            return WatchlistRefresh([], screened=False)
        excluded_by_price = len(fallback_watchlist) - len(filtered)
        # 件数の切り詰めはここでも行う。株価帯を通る件数は株価しだいで日々変わる
        # ため、固定リストの長さだけでは「6.1」の不変条件
        # (MAX_WATCHLIST_SIZE × (600 / POLL_INTERVAL_SECONDS) ≦ 60) を保証できない。
        # 切り詰める順序はリストの記載順（＝銘柄選定の結果を見て決めた順ではない）。
        if len(filtered) > swing_slots:
            _log_watchlist_truncation(filtered, swing_slots)
            filtered = filtered[:swing_slots]
        logger.warning(
            "%s、フォールバックの固定ウォッチリストで継続します: %s"
            "（株価帯の判定で %d 件を除外）。",
            cause, filtered, excluded_by_price,
        )
        return WatchlistRefresh(filtered + growth_symbols, screened=False)

    try:
        screened = await screen_value_stocks_async(ib, config)
    except Exception:
        logger.exception("銘柄スクリーニングに失敗しました。")
        return await _fallback("銘柄スクリーニングに失敗したため")

    if not screened:
        return await _fallback("スクリーニング結果が0件のため")

    # 監視銘柄1件につき毎サイクル1回の日中足リクエストが発生するため、
    # スクリーニングが何件返しても監視対象は上限で頭打ちにする。
    # これを外すとIBKRのペーシング制限に張り付き、全銘柄の処理が遅延する。
    if len(screened) > swing_slots:
        logger.info(
            "スクリーニング結果%d件のうち、上位%d件のみを監視対象にします"
            "（IBKRのペーシング制限対策）。",
            len(screened), swing_slots,
        )
        screened = screened[:swing_slots]

    logger.info("スクリーニング結果でウォッチリストを更新しました: %s", screened)
    return WatchlistRefresh(screened + growth_symbols, screened=True)


async def main() -> None:
    connection = IBKRConnection()
    # 接続する前に判定する。実発注が有効なまま本番ポートを向いていたら、
    # 1件も注文を出す前にここで止める。
    ensure_orders_are_paper_only(connection.port)
    if ENABLE_REAL_ORDERS:
        logger.warning(
            "実発注が有効です（接続先ポート %s = ペーパー取引）。"
            "株数上限%d株・1注文%.0f USDのクランプは有効のままです。",
            connection.port, MAX_POSITION_SIZE, MAX_ORDER_NOTIONAL_USD,
        )
    # 状態ファイルを指定して、再起動しても保有ポジションと
    # トレーリングストップの基準（高値）を引き継げるようにする。
    position_manager = PositionManager(state_path=DEFAULT_STATE_PATH)
    trade_journal = TradeJournal()
    # キャッシュはサイクル間で共有する。ここで毎サイクル作り直すと
    # ペーシング制限対策の意味が無くなる。
    caches = MarketDataCaches()
    watchlist: List[str] = list(WATCHLIST)
    # フォールバックの元になるリストは、スクリーニングが成功したときだけ入れ替える。
    # watchlist をそのまま渡すと、株価帯で絞られた結果が次回のフォールバック元に
    # なり（さらに空で返った日にはフォールバック先が消え）、失敗が重なるほど
    # 監視候補が痩せていく。
    fallback_watchlist: List[str] = list(WATCHLIST)
    # 売買代金ランキングの履歴。急上昇の判定には昨日までの順位が要る。
    rank_history: RankHistoryStore = resolve_store()
    last_screened_date: Optional[date] = None
    next_screening_attempt_at: Optional[datetime] = None
    # 接続の「一時的な瞬断」と「人手が要る状態」を区別するためのラウンド数。
    # 接続に成功した時点で0へ戻す。
    consecutive_connection_failures: int = 0

    ib: Optional[IB] = None
    try:
        while True:
            try:
                # TWSとの接続はメンテナンスやネットワーク瞬断で稼働中に切れうるため、
                # サイクルの先頭で毎回接続状態を確認し、切れていれば再接続する
                # （connect_async自体は指数的バックオフ付きリトライを内包している）。
                if ib is None or not ib.isConnected():
                    ib = await connection.connect_async()
                    # **接続のたびに口座を確かめる。** ポートによる判定
                    # (ensure_orders_are_paper_only) はGatewayが既定のポート割り当てを
                    # 使っている前提でしか成立せず、IBCの OverrideTwsApiPort は
                    # モードと無関係にポートを決める。実口座が4002番で待ち受けていても
                    # ポートだけでは見抜けないため、ブローカーが返す口座番号で二重化する。
                    ensure_account_is_paper(ib.managedAccounts())
                    consecutive_connection_failures = 0

                if is_regular_trading_hours():
                    now_et = datetime.now(US_EASTERN)
                    today = now_et.date()
                    # 成功した日だけ選定済みとして扱い、失敗した日は間隔を空けて
                    # 再試行する。スキャナーの空応答は購読権限が無い場合だけでなく
                    # 一時的な不調でも起きるため、1回目の結果で1日を確定させない。
                    if today != last_screened_date and (
                        next_screening_attempt_at is None or now_et >= next_screening_attempt_at
                    ):
                        # 銘柄選定は口座資金に依存する（買えない株価の銘柄を除外する）。
                        # スクリーニングは1日1回なので、資金の取得もこのタイミングだけで足りる。
                        account_equity = await get_account_equity_async(ib)
                        # キャッシュ口座では1株の値段が決済済み現金を超える銘柄も
                        # 買えないため、株価上限の判定に併せて渡す。
                        settled_cash = (
                            await get_settled_cash_async(ib)
                            if ENFORCE_SETTLED_CASH_FUNDING else None
                        )
                        # cachesを渡すのは、フォールバック時の株価帯の判定で
                        # 日足を取り直さないため（同じ銘柄の日足はこの後の
                        # シグナル判定でどのみち取得される）。
                        refresh = await _refresh_watchlist_async(
                            ib, fallback_watchlist, account_equity, settled_cash, caches,
                        )
                        watchlist = refresh.symbols
                        if refresh.screened:
                            fallback_watchlist = refresh.symbols
                            last_screened_date = today
                            next_screening_attempt_at = None
                        else:
                            next_screening_attempt_at = now_et + timedelta(
                                seconds=SCREENING_RETRY_INTERVAL_SECONDS,
                            )
                            logger.info(
                                "銘柄スクリーニングは %.0f 秒後に再試行します"
                                "（それまではフォールバックのウォッチリストで継続）。",
                                SCREENING_RETRY_INTERVAL_SECONDS,
                            )

                        if ENABLE_ATTENTION_WATCHLIST or DROP_STRUGGLING_SYMBOLS:
                            # 売買代金の急上昇銘柄の組み入れと、下降トレンド銘柄の
                            # 除外。スクリーニングの成否によらず掛ける（フォールバックの
                            # 固定リストにも同じ手入れが要る）。失敗しても稼働は続ける。
                            try:
                                watchlist = await _apply_attention_watchlist_async(
                                    ib, watchlist, account_equity, caches,
                                    position_manager, rank_history, now=now_et,
                                )
                            except Exception:
                                logger.exception(
                                    "注目銘柄の組み入れに失敗しました。"
                                    "既存のウォッチリストで継続します。",
                                )

                    await run_watchlist_cycle_async(
                        ib, watchlist, position_manager, trade_journal, caches,
                    )
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
                else:
                    logger.info("市場時間外のため、シグナル評価をスキップします。")
                    await asyncio.sleep(CLOSED_MARKET_POLL_INTERVAL_SECONDS)
            except ConnectionError:
                # connect_asyncのリトライを使い果たした場合（延長メンテナンス等）。
                # プロセスを落とさず、時間外ポーリング間隔でリトライし続ける。
                if not is_regular_trading_hours():
                    # **市場時間外の接続失敗は数えない。** IB Gatewayを日次で
                    # 落とす運用（検証中は日本時間8:00にログアウトし22:30に再ログイン）
                    # では、閉場中に何十ラウンドも失敗するのが正常な状態である。
                    # ここで数えると再ログインの案内が毎日出て、本当に人手が要る
                    # ときと見分けがつかなくなる。取れなかったデータも無い。
                    logger.info(
                        "TWSへ接続できません（市場時間外）。%.0f秒後に再試行します。",
                        CLOSED_MARKET_POLL_INTERVAL_SECONDS,
                    )
                    await asyncio.sleep(CLOSED_MARKET_POLL_INTERVAL_SECONDS)
                    continue

                consecutive_connection_failures += 1
                if consecutive_connection_failures == 1:
                    # 1回目だけスタックトレースを出す。以降は同じ例外が同じ経路で
                    # 繰り返されるだけで、行数が増える以上の情報が無い。
                    logger.exception(
                        "TWSへの再接続に失敗しました。%.0f秒後に再試行します。",
                        CLOSED_MARKET_POLL_INTERVAL_SECONDS,
                    )
                else:
                    logger.error(
                        "TWSへの再接続に失敗しました（連続%d回）。%.0f秒後に再試行します。",
                        consecutive_connection_failures, CLOSED_MARKET_POLL_INTERVAL_SECONDS,
                    )
                if consecutive_connection_failures == CONNECTION_FAILURE_ROUNDS_BEFORE_MANUAL_LOGIN:
                    # 等号で1回だけ出す。以降のラウンドで繰り返すと、この行自体が
                    # 上の再試行ログに埋もれて「読むべき1行」でなくなる。
                    logger.error(
                        "%d ラウンド連続で接続できません。IB GatewayのAuto restartは"
                        "数分で復帰するため、この長さは瞬断や再起動では説明できません。"
                        "**IB Gatewayへの再ログインが必要な可能性があります**"
                        "（2要素認証の期限切れ・セッション失効・Gatewayのプロセス停止）。"
                        "再試行自体はこのまま継続します。",
                        consecutive_connection_failures,
                    )
                await asyncio.sleep(CLOSED_MARKET_POLL_INTERVAL_SECONDS)
            except Exception:
                # サイクル処理中の予期しないエラー（サイクル途中の切断等を含む）で
                # プロセス全体を落とさない。次のループ先頭でisConnected()により
                # 再接続要否を判定する。
                logger.exception("監視サイクルの処理中にエラーが発生しました。次のサイクルで再試行します。")
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        logger.info("ユーザーの割り込みにより停止します。")
    finally:
        await connection.disconnect_async()


def _raise_keyboard_interrupt_on_sigterm(_signum: int, _frame: object) -> None:
    """SIGTERMを KeyboardInterrupt に変換する（`main()` の停止経路へ合流させる）。

    引け後の停止(`scripts/after_close.sh`)はシグナルでBotを止めるが、
    SIGTERMの既定動作はプロセスの即時終了であり、`main()` の
    `finally: disconnect_async()` を通らない。ポジション状態は変更のたびに
    保存されるので取りこぼしは無いものの、IBKRとのソケットは明示的に閉じた方が
    次回接続で同じclientIdを取り合わずに済む。

    SIGINTで済ませないのは、シェルがバックグラウンドで起動した子プロセスの
    SIGINTを SIG_IGN にする場合があり（POSIXのジョブ制御）、届いても無視され
    うるためである。
    """
    raise KeyboardInterrupt


if __name__ == "__main__":
    configure_logging()
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt_on_sigterm)
    asyncio.run(main())
