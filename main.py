#!/usr/bin/env python3
"""Photobooth: the one entry point for the camera, the screen and the guest Wi-Fi.

Brings the access point up, runs the booth loop, and takes the AP down again
on the way out. Every setting (countdown, button pin, screen texts, Wi-Fi) is
in config.yaml; the flags below only choose what to run.

    sudo python3 main.py                  # AP + booth + screen
    sudo python3 main.py --mock-camera    # no camera: recycle photos/
    python3 main.py --no-wifi             # booth only, leave the radio alone

Root is required because the AP needs to reconfigure the interface and drive
hostapd. With --no-wifi it runs fine as an ordinary user.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The modules are flat (no packages): booth/ holds the camera, button and
# screen, wifi/ the access point. Both go on the path so the plain
# `import booth` / `from accesspoint import ...` below resolve. pbcommon sits
# beside this file, which is already on the path. gallery/ is not needed here:
# its two daemons run under systemd.
for _folder in ("wifi", "booth"):
    sys.path.insert(0, str(HERE / _folder))

import booth  # noqa: E402
import pbcommon  # noqa: E402

log = logging.getLogger("main")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

    run = parser.add_argument_group("what to run")
    run.add_argument("--no-wifi", action="store_true",
                     help="do not touch the radio; run the booth only")
    run.add_argument("--no-display", action="store_true",
                     help="do not open the fullscreen viewer")
    run.add_argument("--mock-camera", action="store_true",
                     help="no camera: each shot copies a random photo already "
                          "in the photo directory")

    wifi = parser.add_argument_group("guest Wi-Fi")
    wifi.add_argument("--keep-wifi", action="store_true",
                      help="leave the AP running after the booth exits")
    wifi.add_argument("--release-wifi", action="store_true",
                      help="on exit, also hand the interface back to "
                           "NetworkManager (restores normal Wi-Fi)")
    wifi.add_argument("--watch-clients", action="store_true",
                      help="live report of guests joining, getting a lease and "
                           "opening the gallery -- use this to see whether an "
                           "Android phone opens it on its own")

    parser.add_argument("-v", "--verbose", action="store_true",
                        help="debug logging, including libgphoto2")
    return parser


def start_wifi(config: dict, watch_clients: bool, verbose: bool):
    """Bring the guest Wi-Fi up. Returns (access_point, watcher) or None."""
    from accesspoint import AccessPoint
    from clientwatch import ClientWatch, preflight

    access_point = AccessPoint(config)
    if not access_point.start():
        log.error("access point failed to start; not running the booth")
        access_point.stop()
        return None
    status = access_point.status()
    log.info('guest Wi-Fi "%s" at http://%s/', status["ssid"], status["address"])

    # Cheap checks for the things that silently break captive detection.
    # These fail invisibly otherwise -- a malformed DHCP option produces no
    # log line anywhere, it just makes the portal not appear.
    for problem in preflight(config):
        log.warning("captive portal: %s", problem)

    try:
        passphrase = Path(config["password"]["password_path"]).read_text().strip()
        log.info("passphrase %s   QR %s", passphrase, config["password"]["qr_path"])
    except OSError:
        pass

    watcher = None
    if watch_clients:
        watcher = ClientWatch(config, verbose=verbose)
        watcher.start()
        print("\n  Watching guest devices. Join from a phone to see what "
              "it does.\n", flush=True)
    return access_point, watcher


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s")

    config = pbcommon.load_config()

    wanted_wifi = not args.no_wifi
    if wanted_wifi and os.geteuid() != 0:
        print("Starting the access point needs root: run with sudo, "
              "or pass --no-wifi to run the booth alone.", file=sys.stderr)
        return 1

    access_point = watcher = None
    if wanted_wifi:
        started = start_wifi(config, args.watch_clients, args.verbose)
        if started is None:
            return 1
        access_point, watcher = started

    try:
        return booth.run(
            config,
            mock_camera=args.mock_camera,
            display=not args.no_display,
            # Without the Wi-Fi the QR files on disk are left over from an
            # earlier run, and the join code may no longer work.
            show_qr=wanted_wifi,
        )
    finally:
        if watcher is not None:
            watcher.stop()
            watcher.summary()
        if access_point is not None and not args.keep_wifi:
            access_point.stop(release=args.release_wifi)
        elif access_point is not None:
            log.info("leaving the access point up (--keep-wifi)")


if __name__ == "__main__":
    sys.exit(main())
