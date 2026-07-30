#!/usr/bin/env python3
"""Photobooth: one entry point for the camera, the screen and the guest Wi-Fi.

Brings the access point up, runs the booth loop, and takes the AP down again
on the way out. Replaces the old activate.sh / deactivate.sh pair -- the AP is
now started and stopped by the same command that runs the booth.

    sudo python3 main.py                       # AP + booth + screen
    sudo python3 main.py --mock-camera         # no camera: recycle photos/
    sudo python3 main.py --no-wifi             # booth only, leave the radio alone
    sudo python3 main.py --rotate-interval 15 --valid-passphrases 2

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
sys.path.insert(0, str(HERE / "gallery"))

import booth  # noqa: E402
from camera import DEFAULT_OUTPUT_DIR  # noqa: E402

log = logging.getLogger("main")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

    wifi = parser.add_argument_group("guest Wi-Fi")
    wifi.add_argument("--no-wifi", action="store_true",
                      help="do not touch the radio; run the booth only")
    wifi.add_argument("--keep-wifi", action="store_true",
                      help="leave the AP running after the booth exits")
    wifi.add_argument("--watch-clients", action="store_true",
                      help="live report of guests joining, getting a lease and "
                           "probing the captive portal -- use this to see "
                           "whether a phone opens its login sheet on its own")
    wifi.add_argument("--release-wifi", action="store_true",
                      help="on exit, also hand the interface back to "
                           "NetworkManager (restores normal Wi-Fi)")

    # These two are the only thing bounding a guest's session: a rotation
    # deauthenticates everyone, and a guest can only return while the
    # passphrase they scanned is still in the ring. There is no separate
    # session limit any more -- see the note in gallery/config.yaml.
    limits = parser.add_argument_group(
        "guest access (override gallery/config.yaml for this run)")
    limits.add_argument("--rotate-interval", type=float, metavar="MIN",
                        help="how often a new Wi-Fi passphrase is minted")
    limits.add_argument("--valid-passphrases", type=int, metavar="N",
                        help="how many passphrases stay valid at once; a "
                             "scanned QR keeps working for about "
                             "N x rotate-interval (1 = no overlap)")

    booth_group = parser.add_argument_group("booth")
    booth_group.add_argument("--once", action="store_true",
                             help="take one photo and exit")
    booth_group.add_argument("--countdown", type=booth.nonnegative_float,
                             default=booth.DEFAULT_COUNTDOWN_S, metavar="SECONDS",
                             help="delay between trigger and shutter, 0 to disable")
    booth_group.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR,
                             help="where to save photos")
    booth_group.add_argument("--delete-after-download", action="store_true",
                             help="remove each file from the camera card once saved")
    booth_group.add_argument("--mock-camera", action="store_true",
                             help="no camera: copy a random existing photo per shot")
    booth_group.add_argument("--no-display", action="store_true",
                             help="do not open the fullscreen viewer")
    booth_group.add_argument("--gpio-pin", type=int,
                             default=booth.DEFAULT_BUTTON_PIN, metavar="N",
                             help="BCM pin for the trigger button")
    booth_group.add_argument("--no-gpio", action="store_true",
                             help="keyboard trigger only, ignore the button")
    booth_group.add_argument("--bounce-time", type=booth.nonnegative_float,
                             default=booth.DEFAULT_BOUNCE_S, metavar="SECONDS",
                             help="button debounce window")
    booth_group.add_argument("--keepalive-interval", type=float,
                             default=booth.KEEPALIVE_INTERVAL_S, metavar="SECONDS",
                             help="camera keep-alive period")

    screen = parser.add_argument_group("screen")
    screen.add_argument("--qr", metavar="PNG", help="join QR to overlay")
    screen.add_argument("--no-qr", action="store_true",
                        help="do not overlay the join QR")
    screen.add_argument("--qr-height", type=int, metavar="PX",
                        help="QR size in pixels")
    screen.add_argument("--qr-gap", type=int, metavar="PX",
                        help="vertical gap between the QR plates in pixels")
    screen.add_argument("--qr-caption-size", type=int, metavar="PX",
                        help="caption text size in pixels")
    screen.add_argument("--qr-caption", metavar="TEXT", help="text under the QR")
    screen.add_argument("--capture-message", metavar="TEXT",
                        default=booth.CAPTURE_MESSAGE,
                        help="shown while the shutter is working "
                             "(empty string for none)")
    screen.add_argument("--camera-hint", metavar="TEXT",
                        help="look-at-the-lens line under the countdown "
                             "(empty string for none)")
    screen.add_argument("--camera-hint-size", type=int, metavar="PX",
                        help="size of that line in pixels")
    screen.add_argument("--portal-qr", metavar="PNG",
                        help="portal-URL QR to overlay; opens the gallery "
                             "directly, without waiting for the captive sheet")
    screen.add_argument("--no-portal-qr", action="store_true",
                        help="do not overlay the portal QR")
    screen.add_argument("--portal-qr-caption", metavar="TEXT",
                        help="text under the portal QR")

    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def session_overrides(args) -> dict:
    """Turn the access flags into a config overlay, or {} if none were given."""
    overlay: dict = {}
    password = {}
    if args.rotate_interval is not None:
        password["rotate_interval_minutes"] = args.rotate_interval
    if args.valid_passphrases is not None:
        password["valid_passphrases"] = max(1, args.valid_passphrases)
    if password:
        overlay["password"] = password
    return overlay


def to_booth_args(args) -> argparse.Namespace:
    """Adapt our namespace to the one booth.main() expects.

    booth.py keeps its own CLI so it stays runnable on its own; this just maps
    the shared options across, including the --no-display inversion.
    """
    return argparse.Namespace(
        once=args.once,
        countdown=args.countdown,
        output_dir=args.output_dir,
        delete_after_download=args.delete_after_download,
        mock_camera=args.mock_camera,
        display=not args.no_display,
        capture_message=args.capture_message,
        qr=args.qr, no_qr=args.no_qr,
        qr_height=args.qr_height, qr_gap=args.qr_gap,
        qr_caption_size=args.qr_caption_size, qr_caption=args.qr_caption,
        camera_hint=args.camera_hint, camera_hint_size=args.camera_hint_size,
        portal_qr=args.portal_qr, no_portal_qr=args.no_portal_qr,
        portal_qr_caption=args.portal_qr_caption,
        gpio_pin=args.gpio_pin, no_gpio=args.no_gpio,
        bounce_time=args.bounce_time,
        keepalive_interval=args.keepalive_interval,
        verbose=args.verbose,
    )


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s")

    wanted_wifi = not args.no_wifi
    if wanted_wifi and os.geteuid() != 0:
        print("Starting the access point needs root: run with sudo, "
              "or pass --no-wifi to run the booth alone.", file=sys.stderr)
        return 1

    access_point = None
    watcher = None
    if wanted_wifi:
        import pbcommon
        from accesspoint import AccessPoint

        overlay = session_overrides(args)
        if overlay:
            # Written before the daemons start so they read the new values,
            # and left on tmpfs so it does not become a permanent default.
            path = pbcommon.write_overrides(overlay)
            log.info("session overrides -> %s: %s", path, overlay)
        else:
            pbcommon.clear_overrides()

        from clientwatch import ClientWatch, preflight

        access_point = AccessPoint()
        if not access_point.start():
            log.error("access point failed to start; not running the booth")
            access_point.stop()
            return 1
        status = access_point.status()
        log.info('guest Wi-Fi "%s" at http://%s/',
                 status["ssid"], status["address"])

        # Cheap checks for the things that silently break captive detection.
        # These fail invisibly otherwise -- a malformed DHCP option produces no
        # log line anywhere, it just makes iOS ignore the portal.
        for problem in preflight(pbcommon.load_config()):
            log.warning("captive portal: %s", problem)

        try:
            passphrase = Path(
                pbcommon.load_config()["password"]["password_path"]
            ).read_text().strip()
            log.info("passphrase %s   QR %s", passphrase,
                     pbcommon.load_config()["password"]["qr_path"])
        except OSError:
            pass

        if args.watch_clients:
            watcher = ClientWatch(pbcommon.load_config(), verbose=args.verbose)
            watcher.start()
            print("\n  Watching guest devices. Join from a phone to see what "
                  "it does.\n", flush=True)
    elif session_overrides(args):
        log.warning("passphrase flags ignored: they configure the guest "
                    "Wi-Fi, which --no-wifi disables")

    try:
        return booth.run(to_booth_args(args))
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
