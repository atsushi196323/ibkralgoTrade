"""米国東部時間の「今日」が取引日かを終了コードで返す（0=取引日 / 1=休場）。

launchd/systemd は祝日を知らない。`StartCalendarInterval` が持つのは
Minute/Hour/Day/Weekday/Month だけで、除外の仕組みも祝日カレンダーも無い。
そのため「祝日は起動しない」はジョブ定義では表現できず、起動直前に
判定するしかない。この判定を各シェルスクリプトへ書くとカレンダーの実装が
分散するので、`core/market_hours` の1か所へ寄せるための薄い入口である。

**基準は米国東部時間の今日である。** 呼び出し元はどちらも日本時間の
夜〜早朝（Bot起動 22:15 JST / 引け後の締め 06:05 JST）に走るが、
22:15 JST は同日の 09:15 ET（冬時間 08:15 ET）、06:05 JST は前日の
17:05 ET（冬時間 16:05 ET）にあたり、いずれも「対象の取引日」と
東部時間の日付が一致する。日本時間の日付で判定すると、後者が翌日に
なってずれる。

IBKRへは接続しない（`holidays` パッケージだけで判定する）ので、
Gatewayが落ちていても答えが出る。
"""

import argparse
import sys
from datetime import date, datetime
from typing import List, Optional

from core.market_hours import US_EASTERN, is_us_market_holiday, is_us_trading_day


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        help="判定する日付(YYYY-MM-DD)。省略時は米国東部時間の今日。",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="終了コードだけを返し、何も出力しない。"
    )
    args = parser.parse_args(argv)

    target = (
        date.fromisoformat(args.date)
        if args.date
        else datetime.now(US_EASTERN).date()
    )

    if is_us_trading_day(target):
        if not args.quiet:
            print(f"{target} は取引日です。")
        return 0

    reason = "祝日" if is_us_market_holiday(target) else "週末"
    if not args.quiet:
        print(f"{target} は米国市場の休場日です（{reason}）。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
