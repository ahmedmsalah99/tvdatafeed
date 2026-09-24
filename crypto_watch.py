"""Watch the crypto pairs for an unusual move up on heavy volume.

Runs pull_crypto.py as a subprocess every WATCH_MINUTES, scores the newest bar
of each coin, and notifies when one moves up much more than it usually does
while trading much more than it usually does.

The first cycle runs immediately on start, then it waits between cycles.

    python crypto_watch.py                      # run in the foreground
    nohup python crypto_watch.py > watch.log 2>&1 &     # ... or in the background
    python crypto_watch.py --once               # one cycle, for testing

A bar has to clear all three of these at once, so a quiet drift with a big
z score, or a fat green candle on no volume, will not fire:

    return  >= MIN_RETURN_PCT      a real move, not a rounding blip
    z score >= MIN_RETURN_Z        big against THIS coin's own recent moves
    rvol    >= MIN_RVOL            volume against its own trailing median

Each bar is alerted at most once - the last alerted timestamp per coin is kept
in the state file, so a restart does not replay old alerts.

Notifications go to the console and the log, to a desktop pop up where one is
available, and to $CRYPTO_WATCH_WEBHOOK if that is set (any Slack or Discord
style endpoint that takes a json body).
"""
import argparse
import datetime
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

import numpy as np
import pandas as pd

from pull_crypto import COINS, EXCHANGE, DEFAULT_INTERVAL, OUTPUT_DIR, csv_name

# ---- how often, and how unusual is unusual -----------------------------------
WATCH_MINUTES = 5.0         # how long to wait between cycles
LOOKBACK_BARS = 144         # trailing bars the "usual" is measured over,
                            # half a day of 5 minute bars - keep this <= the
                            # bars --days pulls, or the window is never full
MIN_RETURN_PCT = 1.0        # the bar must be up at least this much
MIN_RETURN_Z = 3.0          # ... and this many sd above its own recent moves
MIN_RVOL = 3.0              # ... on this many times its trailing median volume

STATE_FILE = os.path.join(os.path.curdir, ".crypto_watch_state.json")
LOG_FILE = os.path.join(os.path.curdir, "crypto_watch.log")
WEBHOOK_ENV = "CRYPTO_WATCH_WEBHOOK"

logger = logging.getLogger("crypto_watch")


# ---- notifications -----------------------------------------------------------

NOTIFY_SECONDS = 20         # how long the fallback popup stays on screen

# a real Windows toast, not a console beep. WinRT through PowerShell, which is
# on every Win10/11 box with nothing to install.
_WINDOWS_TOAST = """
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$text = $xml.GetElementsByTagName('text')
$text[0].AppendChild($xml.CreateTextNode($env:CW_TITLE)) > $null
$text[1].AppendChild($xml.CreateTextNode($env:CW_BODY)) > $null
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('crypto_watch').Show($toast)
"""

# the fallback: a tkinter window in its own process. tkinter is in the standard
# library and works on all three platforms, and running it detached means the
# window can never block the watch or be killed with it.
_POPUP_CODE = r"""
import sys, tkinter as tk

title, body, seconds = sys.argv[1], sys.argv[2], float(sys.argv[3])
root = tk.Tk()
root.title(title)
root.configure(bg="#fcfcfb")
root.attributes("-topmost", True)
try:
    root.eval("tk::PlaceWindow . center")
except tk.TclError:
    pass

tk.Label(root, text=title, font=("TkDefaultFont", 14, "bold"),
         fg="#b3261e", bg="#fcfcfb").pack(padx=24, pady=(20, 8))
tk.Label(root, text=body, font=("TkFixedFont", 11), justify="left",
         fg="#0b0b0b", bg="#fcfcfb").pack(padx=24, pady=(0, 12))
tk.Button(root, text="Dismiss", command=root.destroy).pack(pady=(0, 18))

root.after(int(seconds * 1000), root.destroy)
root.bell()
root.mainloop()
"""


def _native_notify(title, body):
    """the operating system's own notification. True if one was really shown."""
    try:
        if sys.platform == "darwin" and shutil.which("osascript"):
            def esc(text):
                return text.replace("\\", "\\\\").replace('"', '\\"')

            script = (f'display notification "{esc(body)}" '
                      f'with title "{esc(title)}" sound name "Glass"')
            done = subprocess.run(["osascript", "-e", script], timeout=15,
                                  stdin=subprocess.DEVNULL, capture_output=True)
            return done.returncode == 0

        if shutil.which("notify-send"):
            done = subprocess.run(
                ["notify-send", "--urgency=critical", "--app-name=crypto_watch",
                 title, body],
                timeout=15, stdin=subprocess.DEVNULL, capture_output=True)
            return done.returncode == 0

        if sys.platform == "win32" and shutil.which("powershell"):
            env = dict(os.environ, CW_TITLE=title, CW_BODY=body)
            done = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-Command", _WINDOWS_TOAST],
                timeout=30, stdin=subprocess.DEVNULL, capture_output=True, env=env)
            return done.returncode == 0
    except Exception as e:
        logger.debug("native notification failed: %s", e)
    return False


def _popup_notify(title, body, seconds=NOTIFY_SECONDS):
    """a tkinter window, spawned detached so it never blocks the watch"""
    try:
        import tkinter                          # noqa: F401  just a probe
    except Exception as e:
        logger.debug("no tkinter for the popup: %s", e)
        return False

    try:
        subprocess.Popen(
            [sys.executable, "-c", _POPUP_CODE, title, body, str(seconds)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as e:
        logger.debug("popup failed: %s", e)
        return False


def ui_notify(title, body, mode="auto"):
    """put `body` on the screen, by whichever route this machine supports

    auto   try the operating system's notification, fall back to a popup
    native only the operating system's own notification
    popup  only the tkinter window
    none   nothing on screen, console and log only
    """
    if mode == "none":
        return None
    if mode in ("auto", "native") and _native_notify(title, body):
        return "native"
    if mode in ("auto", "popup") and _popup_notify(title, body):
        return "popup"

    logger.warning(
        "no on screen notification available (mode %s). The alert is still in "
        "the console and the log - set $CRYPTO_WATCH_WEBHOOK to have it pushed "
        "somewhere you will see it.", mode)
    return None


def webhook_notify(text, url=None):
    """post to $CRYPTO_WATCH_WEBHOOK if it is set"""
    url = url or os.environ.get(WEBHOOK_ENV)
    if not url:
        return
    payload = json.dumps({"text": text, "content": text}).encode()
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            logger.debug("webhook returned %s", response.status)
    except (urllib.error.URLError, OSError) as e:
        logger.warning("webhook failed: %s", e)


def notify(alerts, mode="auto"):
    """one notification per cycle, however many coins fired"""
    lines = [
        f"{a['coin']}  +{a['return_pct']:.2f}%  z={a['return_z']:.1f}  "
        f"rvol={a['rvol']:.1f}x  @ {a['bar_time']}"
        for a in alerts
    ]
    title = f"{len(alerts)} unusual move(s) up"
    body = "\n".join(lines)

    print("\n" + "!" * 62)
    print(title)
    print(body)
    print("!" * 62 + "\n", flush=True)
    logger.warning("%s\n%s", title, body)

    route = ui_notify(title, body, mode)
    if route:
        logger.debug("on screen notification via %s", route)
    webhook_notify(f"*{title}*\n{body}")


# ---- the detector ------------------------------------------------------------

def score_bars(df, lookback=LOOKBACK_BARS):
    """add the three numbers the rule needs, each against the coin's own past

    Everything is shifted by one bar, so a bar is never judged against itself.
    """
    df = df.sort_index().copy()
    floor = max(20, lookback // 10)

    ret = df["close"].pct_change() * 100.0
    df["return_pct"] = ret
    df["return_z"] = (
        (ret - ret.rolling(lookback, min_periods=floor).mean().shift(1))
        / ret.rolling(lookback, min_periods=floor).std().shift(1)
    )

    baseline = df["volume"].rolling(lookback, min_periods=floor).median().shift(1)
    df["rvol"] = df["volume"] / baseline.replace(0, np.nan)
    return df


def check_coin(coin, path, cfg):
    """the newest bar of one coin, as an alert dict or None"""
    df = pd.read_csv(path)
    date_col = "datetime" if "datetime" in df.columns else df.columns[0]
    df[date_col] = pd.to_datetime(df[date_col])
    df = df.rename(columns={date_col: "datetime"}).set_index("datetime")

    if len(df) < max(20, cfg.lookback // 10) + 2:
        logger.debug("%s: only %d bars, not enough to judge", coin, len(df))
        return None

    scored = score_bars(df, cfg.lookback)
    last = scored.iloc[-1]

    if not np.isfinite([last["return_pct"], last["return_z"], last["rvol"]]).all():
        return None

    hit = (last["return_pct"] >= cfg.min_return_pct
           and last["return_z"] >= cfg.min_return_z
           and last["rvol"] >= cfg.min_rvol)
    if not hit:
        logger.debug("%s: +%.2f%% z=%.1f rvol=%.1f - quiet",
                     coin, last["return_pct"], last["return_z"], last["rvol"])
        return None

    return {
        "coin": coin,
        "bar_time": str(scored.index[-1]),
        "close": float(last["close"]),
        "return_pct": float(last["return_pct"]),
        "return_z": float(last["return_z"]),
        "rvol": float(last["rvol"]),
        "volume": float(last["volume"]),
    }


# ---- state, so the same bar is not alerted twice -----------------------------

def load_state(path=STATE_FILE):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state, path=STATE_FILE):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)          # atomic, so a kill mid write cannot corrupt it


# ---- one cycle ---------------------------------------------------------------

def run_puller(cfg):
    """pull_crypto.py as a child process, so a crash in it cannot kill the watch"""
    cmd = [sys.executable, cfg.puller,
           "--exchange", cfg.exchange,
           "--interval", cfg.interval,
           "--days", str(cfg.days),
           "--output-dir", cfg.output_dir,
           "--pause", str(cfg.pause),
           "--login", cfg.login]
    if cfg.coins:
        cmd += ["--coins", ",".join(cfg.coins)]

    logger.info("pulling: %s", " ".join(cmd))
    try:
        done = subprocess.run(cmd, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL,
                              timeout=cfg.pull_timeout)
    except subprocess.TimeoutExpired:
        logger.error("the puller did not finish in %ss, skipping this cycle",
                     cfg.pull_timeout)
        return False

    if done.returncode != 0:
        logger.error("the puller exited %s\n%s", done.returncode,
                     (done.stderr or done.stdout or "").strip()[-2000:])
        return False

    logger.debug(done.stdout.strip()[-2000:])
    return True


def cycle(cfg, state):
    """pull, score every coin, notify about the ones that are new"""
    if not run_puller(cfg):
        return []

    coins = cfg.coins or list(COINS)
    alerts, missing = [], []

    for coin in coins:
        path = os.path.join(cfg.output_dir,
                            csv_name(cfg.exchange, coin, cfg.interval))
        if not os.path.exists(path):
            missing.append(coin)
            continue

        try:
            alert = check_coin(coin, path, cfg)
        except Exception as e:
            logger.error("%s: could not be checked - %s", coin, e)
            continue

        if alert is None:
            continue
        if state.get(coin) == alert["bar_time"]:
            logger.debug("%s: already alerted for %s", coin, alert["bar_time"])
            continue

        state[coin] = alert["bar_time"]
        alerts.append(alert)

    if missing:
        logger.warning("no csv for: %s", ", ".join(missing))

    if alerts:
        notify(alerts, cfg.notify)
        save_state(state, cfg.state_file)
    else:
        logger.info("%s - nothing unusual across %d coin(s)",
                    datetime.datetime.now().strftime("%H:%M:%S"),
                    len(coins) - len(missing))
    return alerts


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--coins", type=lambda s: [c.strip().upper()
                                                   for c in s.split(",") if c.strip()],
                        help="comma separated pairs instead of the COINS list")
    parser.add_argument("--exchange", default=EXCHANGE)
    parser.add_argument("--interval", default=DEFAULT_INTERVAL)
    parser.add_argument("--days", type=float, default=0.5,
                        help="history pulled each cycle, in days. Default: 0.5")
    parser.add_argument("--every", type=float, default=WATCH_MINUTES,
                        dest="every_minutes",
                        help=f"minutes between cycles. Default: {WATCH_MINUTES}")
    parser.add_argument("--once", action="store_true",
                        help="run a single cycle and exit")
    parser.add_argument("--lookback", type=int, default=LOOKBACK_BARS,
                        help=f"bars the 'usual' is measured over. Default: {LOOKBACK_BARS}")
    parser.add_argument("--min-return-pct", type=float, default=MIN_RETURN_PCT)
    parser.add_argument("--min-return-z", type=float, default=MIN_RETURN_Z)
    parser.add_argument("--min-rvol", type=float, default=MIN_RVOL)
    parser.add_argument("--output-dir", default=OUTPUT_DIR)
    parser.add_argument("--state-file", default=STATE_FILE)
    parser.add_argument("--log-file", default=LOG_FILE)
    parser.add_argument("--puller", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "pull_crypto.py"))
    parser.add_argument("--pull-timeout", type=float, default=600.0,
                        help="seconds to give the puller. Default: 600")
    parser.add_argument("--pause", type=float, default=1.0,
                        help="seconds the puller waits between coins")
    parser.add_argument("--login", default="none",
                        choices=["none", "manual", "auto"],
                        help="passed to the puller. Leave it at none: a "
                             "background watch cannot answer a login prompt. "
                             "Run 'python pull_crypto.py --login manual' once "
                             "by hand to cache a token for the day.")
    parser.add_argument("--notify", default="auto",
                        choices=["auto", "native", "popup", "none"],
                        help="how alerts appear on screen. auto tries the "
                             "OS notification, then a tkinter popup. "
                             "Default: auto")
    parser.add_argument("--test-notify", action="store_true",
                        help="send one fake alert and exit, to check that "
                             "notifications work on this machine")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    cfg = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if cfg.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        handlers=[logging.FileHandler(cfg.log_file), logging.StreamHandler()],
    )

    coins = cfg.coins or list(COINS)
    print(f"watching {len(coins)} coin(s) on {cfg.exchange}, {cfg.interval}")
    print(f"alert when a bar is up >= {cfg.min_return_pct}% AND "
          f"z >= {cfg.min_return_z} AND rvol >= {cfg.min_rvol}x")
    print(f"every {cfg.every_minutes} min, first cycle now, "
          f"logging to {cfg.log_file}")
    if not os.environ.get(WEBHOOK_ENV):
        print(f"({WEBHOOK_ENV} is not set, so no webhook notifications)")
    print()

    if cfg.test_notify:
        route = ui_notify("crypto_watch test",
                          "BTCUSDT  +3.50%  z=4.2  rvol=6.1x  @ now",
                          cfg.notify)
        print(f"on screen notification: "
              f"{route or 'NONE - console and log only'}")
        return 0 if route else 1

    state = load_state(cfg.state_file)

    while True:
        started = time.perf_counter()
        try:
            cycle(cfg, state)
        except KeyboardInterrupt:
            print("\nstopped")
            return 0
        except Exception as e:                  # a bad cycle must not end the watch
            logger.exception("cycle failed: %s", e)

        if cfg.once:
            return 0

        wait = max(5.0, cfg.every_minutes * 60 - (time.perf_counter() - started))
        logger.debug("sleeping %.0fs", wait)
        try:
            time.sleep(wait)
        except KeyboardInterrupt:
            print("\nstopped")
            return 0


if __name__ == "__main__":
    sys.exit(main())
