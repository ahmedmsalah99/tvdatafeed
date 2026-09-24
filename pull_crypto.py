"""Download recent 5 minute bars for a list of crypto pairs.

The coins are the COINS list below, edit it to change what is pulled. One csv
per coin is written to exported_files/crypto/.

    python pull_crypto.py                       # last half day, 5 minute bars
    python pull_crypto.py --interval in_15_minute
    python pull_crypto.py --days 3
    python pull_crypto.py --coins BTCUSDT,ETHUSDT

Crypto trades around the clock, so the default half day is simply the last 144
five minute bars. Change it with --days, or pin an exact count with --n-bars.

LOGGING IN. This script never prompts, so it is safe to run unattended from
crypto_watch.py. It uses whatever tradingview token is already cached for
today, and otherwise reads as an anonymous user, which is usually enough for
the major pairs. To cache a token, run this once by hand:

    python pull_crypto.py --login manual        # opens a browser, asks you to
                                                # log in, then caches for the day
"""
import argparse
import logging
import os
import sys
import time

# tvDatafeed is imported lazily inside connect() and main(): it pulls in
# selenium, and crypto_watch.py imports this module just for COINS and csv_name.

# The pairs to download. These are tradingview symbols on the exchange below.
COINS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "DOTUSDT",
    "DASHUSDT",
    "SUIUSDT",
    "ARKUSDT",
    "PORTALUSDT",
    "PROMUSDT",
    "NEWTUSDT",
    "ZECUSDT",
    "LSKUSDT",
    "LTCUSDT",
    "NILUSDT"
]

EXCHANGE = "BINANCE"
DEFAULT_INTERVAL = "in_5_minute"
DEFAULT_DAYS = 0.5
OUTPUT_DIR = os.path.join(os.path.curdir, "exported_files", "crypto")

# the tradingview code for each interval, so csv_name works without importing
# tvDatafeed, and how many minutes each covers so --days can become a bar count
INTERVAL_CODES = {
    "in_1_minute": "1", "in_3_minute": "3", "in_5_minute": "5",
    "in_15_minute": "15", "in_30_minute": "30", "in_45_minute": "45",
    "in_1_hour": "1H", "in_2_hour": "2H", "in_3_hour": "3H",
    "in_4_hour": "4H", "in_daily": "1D",
}
INTERVAL_MINUTES = {
    "in_1_minute": 1, "in_3_minute": 3, "in_5_minute": 5, "in_15_minute": 15,
    "in_30_minute": 30, "in_45_minute": 45, "in_1_hour": 60, "in_2_hour": 120,
    "in_3_hour": 180, "in_4_hour": 240, "in_daily": 1440,
}

logger = logging.getLogger(__name__)


def bars_for(interval_name, days):
    """how many bars of this interval cover `days` of 24h trading"""
    minutes = INTERVAL_MINUTES.get(interval_name)
    if minutes is None:
        raise ValueError(f"no minute length known for {interval_name}")
    return max(2, int(round(days * 24 * 60 / minutes)))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--coins", help="comma separated pairs to download instead of the COINS list"
    )
    parser.add_argument(
        "--exchange",
        default=EXCHANGE,
        help=f"exchange the pairs trade on. Default: {EXCHANGE}",
    )
    parser.add_argument(
        "--interval",
        default=DEFAULT_INTERVAL,
        choices=sorted(INTERVAL_MINUTES),
        help=f"bar size. Default: {DEFAULT_INTERVAL}",
    )
    parser.add_argument(
        "--days",
        type=float,
        default=DEFAULT_DAYS,
        help=f"how much history to pull, in days. Default: {DEFAULT_DAYS}",
    )
    parser.add_argument(
        "--n-bars",
        type=int,
        help="exact number of bars, overrides --days. Max 5000",
    )
    parser.add_argument(
        "--output-dir",
        default=OUTPUT_DIR,
        help=f"directory the csv files are written to. Default: {OUTPUT_DIR}",
    )
    parser.add_argument(
        "--suffix",
        default="",
        help="text added to each csv name, e.g. --suffix _live",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=1.0,
        help="seconds to wait between coins. Default: 1.0",
    )
    parser.add_argument(
        "--login",
        default="none",
        choices=["none", "manual", "auto"],
        help="none: never prompt, use a cached token if there is one (the "
             "default, and the only safe one for a background run). "
             "manual: open a browser and wait for you to log in. "
             "auto: log in with --username and --password.",
    )
    parser.add_argument("--username", help="tradingview username, with --login auto")
    parser.add_argument("--password", help="tradingview password, with --login auto")
    parser.add_argument(
        "--no-login",
        action="store_true",
        help=argparse.SUPPRESS,      # kept so older commands still run
    )
    parser.add_argument(
        "--chromedriver-path", help="path of the chromedriver executable"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="show debug logging"
    )
    return parser.parse_args(argv)


def connect(args):
    """log in to tradingview the way the arguments ask for

    Only --login manual ever waits for a keypress. The default cannot block, so
    a background run fails loudly instead of hanging on a prompt nobody sees.
    """
    if args.login == "auto" and not (args.username and args.password):
        raise SystemExit("--login auto needs both --username and --password")

    from tvDatafeed import TvDatafeed

    if args.login == "auto":
        logger.info("logging in as %s", args.username)
        return TvDatafeed(
            username=args.username,
            password=args.password,
            chromedriver_path=args.chromedriver_path,
        )

    if args.login == "manual":
        logger.info("manual login, a browser will open")
        return TvDatafeed(manual_login=True, chromedriver_path=args.chromedriver_path)

    # "none": no browser and no prompt. TvDatafeed still picks up a token
    # cached earlier today; without one it reads as an anonymous user.
    logger.info("no login, using a cached token if there is one")
    return TvDatafeed(chromedriver_path=args.chromedriver_path)


def csv_name(exchange, coin, interval_name, suffix=""):
    """the file one coin is written to, also read by crypto_watch.py"""
    return f"{exchange}_{coin}_{INTERVAL_CODES[interval_name]}{suffix}.csv"


def download(tv, coin, exchange, interval, n_bars):
    """one coin, returns its dataframe or None if nothing came back"""
    try:
        data = tv.get_hist(
            symbol=coin, exchange=exchange, interval=interval, n_bars=n_bars
        )
    except AttributeError:
        # __create_df cannot parse a response that carries no bars
        logger.error(
            "no data for %s:%s, the pair may not exist on this exchange",
            exchange, coin,
        )
        return None
    except Exception as e:
        logger.error("failed to download %s:%s: %s", exchange, coin, e)
        return None

    if data is None or data.empty:
        logger.error("no data for %s:%s", exchange, coin)
        return None

    return data


def main(argv=None):
    from tvDatafeed import Interval

    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    if args.no_login:                      # old flag, same thing as the default
        args.login = "none"

    coins = ([c.strip().upper() for c in args.coins.split(",") if c.strip()]
             if args.coins else list(COINS))
    if not coins:
        logger.error("no coins to download")
        return 1

    interval = Interval[args.interval]
    n_bars = args.n_bars or bars_for(args.interval, args.days)

    tv = connect(args)
    os.makedirs(args.output_dir, exist_ok=True)

    downloaded, failed = [], []

    for i, coin in enumerate(coins, start=1):
        print(f"\n[{i}/{len(coins)}] {args.exchange}:{coin}  "
              f"{n_bars} x {args.interval}")

        data = download(tv, coin, args.exchange, interval, n_bars)
        if data is None:
            failed.append(coin)
        else:
            filename = os.path.join(
                args.output_dir,
                csv_name(args.exchange, coin, args.interval, args.suffix),
            )
            data.to_csv(filename)
            downloaded.append(coin)
            print(f"    {len(data)} bars, {data.index[0]} to {data.index[-1]}"
                  f" -> {filename}")

        if args.pause and i < len(coins):
            time.sleep(args.pause)

    print(f"\ndownloaded {len(downloaded)}/{len(coins)} coins")
    print(f"csv files are in {os.path.abspath(args.output_dir)}")
    if failed:
        print(f"no data for: {', '.join(failed)}")

    # only a run where nothing at all came back is worth failing on
    return 0 if downloaded else 1


if __name__ == "__main__":
    sys.exit(main())
