"""Check that crypto_watch's alerts actually reach your screen.

Run this before leaving the watch running, so you find out now rather than
missing a real move later.

    python check_notifications.py              # try every route, report what works
    python check_notifications.py --route native
    python check_notifications.py --route popup
    python check_notifications.py --webhook    # also test $CRYPTO_WATCH_WEBHOOK

It calls the same functions crypto_watch.py calls, so a pass here means the
watch will notify you the same way. Exit code is 0 only if at least one route
put something on the screen.

Named check_ rather than test_ on purpose: a file called test_*.py gets
collected by pytest, and nobody wants popup windows opening during a test run.
"""
import argparse
import os
import shutil
import sys

import crypto_watch as cw

TITLE = "crypto_watch check"
BODY = ("BTCUSDT  +3.50%  z=4.2  rvol=6.1x  @ 12:35\n"
        "ETHUSDT  +2.10%  z=3.4  rvol=4.8x  @ 12:35")


def report_backends():
    """what this machine has, before anything is attempted"""
    print(f"platform      {sys.platform}")
    print(f"python        {sys.version.split()[0]}")

    rows = [
        ("osascript", "macOS notifications", shutil.which("osascript")),
        ("notify-send", "Linux notifications", shutil.which("notify-send")),
        ("powershell", "Windows toast", shutil.which("powershell")),
    ]
    try:
        import tkinter
        tk_status = f"available (Tk {tkinter.TkVersion})"
    except Exception as e:
        tk_status = f"MISSING - {type(e).__name__}"
    rows.append(("tkinter", "fallback popup", tk_status))

    print("\nbackends")
    for name, what, found in rows:
        print(f"  {name:<14} {what:<22} {found or 'absent'}")

    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        print("\n  note: DISPLAY is not set. Without a desktop session neither "
              "notify-send\n        nor tkinter can show anything - the webhook "
              "is your route on a\n        headless machine.")


def try_route(route):
    """one route, printing what happened"""
    print(f"\n--- {route} ---")
    if route == "native":
        ok = cw._native_notify(TITLE, BODY)
        print(f"  {'shown' if ok else 'not available'}")
        return ok
    if route == "popup":
        ok = cw._popup_notify(TITLE, BODY)
        if ok:
            print(f"  window spawned, it closes itself after "
                  f"{cw.NOTIFY_SECONDS}s")
        else:
            print("  not available")
        return ok
    if route == "auto":
        got = cw.ui_notify(TITLE, BODY, "auto")
        print(f"  went via {got}" if got else "  nothing reached the screen")
        return bool(got)
    raise ValueError(route)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--route", default="all",
                        choices=["all", "auto", "native", "popup"],
                        help="which route to try. Default: all")
    parser.add_argument("--webhook", action="store_true",
                        help=f"also post to ${cw.WEBHOOK_ENV}")
    args = parser.parse_args(argv)

    report_backends()

    routes = ["native", "popup"] if args.route == "all" else [args.route]
    worked = [r for r in routes if try_route(r)]

    if args.webhook:
        print(f"\n--- webhook ---")
        url = os.environ.get(cw.WEBHOOK_ENV)
        if not url:
            print(f"  ${cw.WEBHOOK_ENV} is not set")
        else:
            host = url.split("/")[2] if "//" in url else url[:30]
            print(f"  posting to {host} ...")
            cw.webhook_notify(f"*{TITLE}*\n{BODY}")   # logs a warning if it fails
            print("  posted, check that it arrived")

    print()
    if worked:
        print(f"OK - {', '.join(worked)} worked. If you did not SEE anything, "
              "your desktop\n     may be suppressing notifications; check its "
              "do-not-disturb setting.")
        return 0

    print(f"NOTHING reached the screen. The watch will still print alerts and "
          f"write\nthem to its log, but you will not be interrupted. On a "
          f"headless or remote\nmachine, set ${cw.WEBHOOK_ENV} to a slack or "
          "discord webhook instead.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
