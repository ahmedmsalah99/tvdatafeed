"""Download daily bars for a list of Egyptian Exchange (EGX) stocks.

The symbols to download are the SYMBOLS list right below, edit it to change
what is scraped. One csv per symbol is written to exported_files/.

By default it logs in manually: a browser is opened and it waits for you to
log in to tradingview yourself, no credentials are asked for. The session is
remembered, so you are only asked again once tradingview's session expires.

    python pull_egx_stocks.py                    # every symbol in SYMBOLS
    python pull_egx_stocks.py --n-bars 1000      # fewer bars per symbol
    python pull_egx_stocks.py --symbols COMI,HRHO    # only these two
    python pull_egx_stocks.py --username U --password P   # automatic login

EGX data is only available to logged in tradingview accounts, so --no-login
will most likely come back empty.
"""
import argparse
import logging
import os
import sys
import time

from tvDatafeed import TvDatafeed, Interval

# The stocks to download. These are tradingview symbols on the EGX exchange,
# add or remove lines to change what is scraped.
SYMBOLS = [
    "COMI",  # Commercial International Bank
    "HRHO",  # EFG Holding
    "CIEB",  # Credit Agricole Egypt
    "FWRY",  # Fawry
    "EFIH",  # e-finance
    "TMGH",  # Talaat Moustafa Group
    "PHDC",  # Palm Hills Developments
    "MNHD",  # Madinet Nasr Housing
    "HELI",  # Heliopolis Housing
    "OCDI",  # SODIC
    "ORAS",  # Orascom Construction
    "SWDY",  # Elsewedy Electric
    "ESRS",  # Ezz Steel
    "ABUK",  # Abu Qir Fertilizers
    "MFPC",  # Misr Fertilizers Production
    "SKPC",  # Sidi Kerir Petrochemicals
    "AMOC",  # Alexandria Mineral Oils
    "ETEL",  # Telecom Egypt
    "EAST",  # Eastern Company
    "ORWE",  # Oriental Weavers
    "JUFO",  # Juhayna Food Industries
    "ISPH",  # Ibnsina Pharma
    "CLHO",  # Cleopatra Hospital
    "NIPH",  # Nile Pharmaceuticals
]

EXCHANGE = "EGX"
INTERVAL = Interval.in_daily

logger = logging.getLogger(__name__)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--symbols",
        help="comma separated symbols to download instead of the SYMBOLS list",
    )
    parser.add_argument(
        "--exchange",
        default=EXCHANGE,
        help=f"exchange the symbols trade on. Default: {EXCHANGE}",
    )
    parser.add_argument(
        "--n-bars",
        type=int,
        default=5000,
        help="number of daily bars per symbol, max 5000. Default: 5000",
    )
    parser.add_argument(
        "--output-dir",
        default=os.path.join(os.path.curdir, "exported_files"),
        help="directory the csv files are written to. Default: ./exported_files",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=1.0,
        help="seconds to wait between symbols. Default: 1.0",
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
    return TvDatafeed(manual_login=True, chromedriver_path=args.chromedriver_path)


def download(tv, symbol, exchange, n_bars):
    """one symbol, returns its dataframe or None if nothing came back"""
    try:
        data = tv.get_hist(
            symbol=symbol, exchange=exchange, interval=INTERVAL, n_bars=n_bars
        )
    except AttributeError:
        # __create_df cannot parse a response that carries no bars
        logger.error(
            "no data for %s:%s, the symbol may not exist or your account "
            "may not have access to it",
            exchange,
            symbol,
        )
        return None
    except Exception as e:
        logger.error("failed to download %s:%s: %s", exchange, symbol, e)
        return None

    if data is None or data.empty:
        logger.error("no data for %s:%s", exchange, symbol)
        return None

    return data


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = list(SYMBOLS)

    if not symbols:
        logger.error("no symbols to download")
        return 1

    tv = connect(args)
    os.makedirs(args.output_dir, exist_ok=True)

    downloaded, failed = [], []

    for i, symbol in enumerate(symbols, start=1):
        print(f"\n[{i}/{len(symbols)}] {args.exchange}:{symbol}")

        data = download(tv, symbol, args.exchange, args.n_bars)
        if data is None:
            failed.append(symbol)
        else:
            filename = os.path.join(
                args.output_dir, f"{args.exchange}_{symbol}_{INTERVAL.value}.csv"
            )
            data.to_csv(filename)
            downloaded.append(symbol)
            print(
                f"    {len(data)} bars, {data.index[0]} to {data.index[-1]}"
                f" -> {filename}"
            )

        # tradingview is not keen on being hammered, and the last symbol does
        # not need to be waited on
        if args.pause and i < len(symbols):
            time.sleep(args.pause)

    print(f"\ndownloaded {len(downloaded)}/{len(symbols)} symbols")
    print(f"csv files are in {os.path.abspath(args.output_dir)}")
    if failed:
        print(f"no data for: {', '.join(failed)}")

    # only a run where nothing at all came back is worth failing on
    return 0 if downloaded else 1


if __name__ == "__main__":
    sys.exit(main())
