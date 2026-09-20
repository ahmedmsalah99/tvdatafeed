"""Download NIPH daily bars from the Egyptian Exchange (EGX) via TradingView.

By default it logs in manually: a browser is opened and it waits for you to
log in to tradingview yourself, no credentials are asked for. The session is
remembered, so you are only asked again once tradingview's session expires.

    python pull_niph.py                     # daily bars, saved as csv
    python pull_niph.py --n-bars 1000       # fewer bars
    python pull_niph.py --no-login          # skip logging in altogether
    python pull_niph.py --username U --password P   # automatic login

EGX data is only available to logged in tradingview accounts, so --no-login
will most likely come back empty.
"""
import argparse
import logging
import os
import sys

from tvDatafeed import TvDatafeed, Interval

SYMBOL = "NIPH"
EXCHANGE = "EGX"
INTERVAL = Interval.in_daily

logger = logging.getLogger(__name__)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--symbol", default=SYMBOL, help=f"symbol to download. Default: {SYMBOL}"
    )
    parser.add_argument(
        "--exchange",
        default=EXCHANGE,
        help=f"exchange the symbol trades on. Default: {EXCHANGE}",
    )
    parser.add_argument(
        "--n-bars",
        type=int,
        default=5000,
        help="number of daily bars to download, max 5000. Default: 5000",
    )
    parser.add_argument(
        "--output-dir",
        default=os.path.join(os.path.curdir, "exported_files"),
        help="directory the csv is written to. Default: ./exported_files",
    )
    parser.add_argument("--username", help="tradingview username, for automatic login")
    parser.add_argument("--password", help="tradingview password, for automatic login")
    parser.add_argument(
        "--no-login",
        action="store_true",
        help="do not log in at all, tradingview then limits the symbols you can read",
    )
    parser.add_argument(
        "--chromedriver-path", help="path of the chromedriver executable"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="show debug logging"
    )
    return parser.parse_args(argv)


def connect(args):
    """log in to tradingview the way the arguments ask for"""
    if args.no_login:
        logger.info("connecting without logging in")
        return TvDatafeed(chromedriver_path=args.chromedriver_path)

    if args.username and args.password:
        logger.info("logging in as %s", args.username)
        return TvDatafeed(
            username=args.username,
            password=args.password,
            chromedriver_path=args.chromedriver_path,
        )

    logger.info("logging in manually")
    return TvDatafeed(
        manual_login=True, chromedriver_path=args.chromedriver_path
    )


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    tv = connect(args)

    print(f"\ndownloading {args.n_bars} daily bars of {args.exchange}:{args.symbol}")
    try:
        data = tv.get_hist(
            symbol=args.symbol,
            exchange=args.exchange,
            interval=INTERVAL,
            n_bars=args.n_bars,
        )
    except AttributeError:
        # __create_df cannot parse a response that carries no bars
        logger.error(
            "no data came back for %s:%s. The symbol may not exist, or your "
            "tradingview account may not have access to it",
            args.exchange,
            args.symbol,
        )
        return 1

    if data is None or data.empty:
        logger.error("no data came back for %s:%s", args.exchange, args.symbol)
        return 1

    os.makedirs(args.output_dir, exist_ok=True)
    filename = os.path.join(
        args.output_dir, f"{args.exchange}_{args.symbol}_{INTERVAL.value}.csv"
    )
    data.to_csv(filename)

    print(f"\n{len(data)} bars, {data.index[0]} to {data.index[-1]}")
    print(data.tail())
    print(f"\nsaved to {os.path.abspath(filename)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
