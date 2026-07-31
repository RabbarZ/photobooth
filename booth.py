#!/usr/bin/env python3
"""Photobooth loop: press the GPIO button or Enter to take a photo.

The button defaults to GPIO17; the keyboard stays live alongside it so a shot
can still be fired over SSH. Ctrl-C quits.

    python3 booth.py                 # button + keyboard
    python3 booth.py --no-gpio       # keyboard only
    python3 booth.py --once          # single shot, then exit
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from camera import Camera, CameraError, DEFAULT_OUTPUT_DIR, MockCamera
from trigger import DEFAULT_BOUNCE_S, DEFAULT_BUTTON_PIN, Event, Trigger

# Seconds between pressing the trigger and the shutter firing.
DEFAULT_COUNTDOWN_S = 5.0

# Shown on the booth screen when a shot fails. German, because the guests and
# the person running the booth read the screen, not the log. Two messages
# because the two failures need different things done to them: a camera that
# answers but will not fire has a dialog waiting on its own screen, whereas an
# absent one is switched off or out of PC Remote mode.
NOT_READY_MESSAGE = "Kamera nicht bereit - bitte Hinweis an der Kamera bestätigen"
NO_CAMERA_MESSAGE = "Keine Kamera gefunden - bitte Kamera einschalten"

# Shown from the moment the shutter fires until the photo lands on screen.
# Not a failure, so it is worded as progress rather than instruction -- the
# guest has nothing left to do at this point but wait a couple of seconds.
CAPTURE_MESSAGE = "Bitte lache!"

# How long either message stays up before the screen goes back to the last photo.
NOT_READY_MESSAGE_S = 5.0

# How long a shot may take before the screen stops promising a photo.
#
# Measured on this camera: a healthy capture costs about 3.7s, rising to a
# consistent 6.8s for the first shot after a connect, which pays for autofocus
# and session warm-up. 12s clears that with room for a slow autofocus or a
# RAW+JPEG pair, and is still far inside the 20-40s libgphoto2 needs to report
# a blocked shutter. Set it too low and every first shot flashes a warning it
# then has to take back.
CAPTURE_SLOW_S = 12.0

# The A7 III sleeps after 30 minutes at most and cannot be set to never
# sleep, so nudge it well inside that window while nobody is shooting.
KEEPALIVE_INTERVAL_S = 120.0

# After this many failures in a row, say plainly in the log that the camera
# itself needs looking at. The booth keeps running either way.
MAX_CONSECUTIVE_FAILURES = 3


def nonnegative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be 0 or greater")
    return number


def print_tick(remaining: int) -> None:
    """Default countdown display: '3... 2... 1... ' on one line."""
    print(f"{remaining}... ", end="", flush=True)


def countdown(seconds: float, tick=print_tick) -> None:
    """Pause before the shutter, ticking once per remaining whole second.

    Each sleep targets a fixed deadline rather than sleeping a flat second,
    so the total stays accurate instead of drifting by the tick cost. A
    fractional countdown spends the remainder on the first tick, keeping the
    final '1...' a full second long.

    Swap `tick` to drive an LED, a screen, or a buzzer instead of stdout.
    """
    if seconds <= 0:
        return
    deadline = time.monotonic() + seconds
    for remaining in range(math.ceil(seconds), 0, -1):
        tick(remaining)
        time.sleep(max(0.0, (deadline - (remaining - 1)) - time.monotonic()))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true",
                        help="take one photo and exit")
    parser.add_argument("--countdown", type=nonnegative_float,
                        default=DEFAULT_COUNTDOWN_S, metavar="SECONDS",
                        help=f"delay between trigger and shutter, 0 to disable "
                             f"(default: {DEFAULT_COUNTDOWN_S:g})")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR,
                        help=f"where to save photos (default: {DEFAULT_OUTPUT_DIR})")
    parser.add_argument("--delete-after-download", action="store_true",
                        help="remove each file from the camera card once saved")
    parser.add_argument("--mock-camera", action="store_true",
                        help="no camera: each shot copies a random existing "
                             "photo from the output directory under a new name")
    parser.add_argument("--display", action="store_true",
                        help="show each photo fullscreen until the next one")
    parser.add_argument("--capture-message", default=CAPTURE_MESSAGE,
                        metavar="TEXT",
                        help=f"shown while the shutter is working, empty "
                             f"string for none (default: {CAPTURE_MESSAGE!r})")
    # Forwarded verbatim to display.py. Defaults are deliberately None here so
    # display.py stays the single place they are defined -- booth.py only
    # passes on what was explicitly asked for.
    parser.add_argument("--qr", metavar="PNG",
                        help="Wi-Fi join QR to overlay (see display.py --help)")
    parser.add_argument("--no-qr", action="store_true",
                        help="do not overlay the join QR")
    parser.add_argument("--qr-height", type=int, metavar="PX",
                        help="QR size in pixels")
    parser.add_argument("--qr-gap", type=int, metavar="PX",
                        help="vertical gap between the QR plates in pixels")
    parser.add_argument("--qr-caption-size", type=int, metavar="PX",
                        help="caption text size in pixels")
    parser.add_argument("--qr-caption", metavar="TEXT",
                        help="text under the QR (empty string for none)")
    parser.add_argument("--camera-hint", metavar="TEXT",
                        help="look-at-the-lens line under the countdown "
                             "(empty string for none)")
    parser.add_argument("--camera-hint-size", type=int, metavar="PX",
                        help="size of that line in pixels")
    parser.add_argument("--portal-qr", metavar="PNG",
                        help="portal-URL QR to overlay (see display.py --help)")
    parser.add_argument("--no-portal-qr", action="store_true",
                        help="do not overlay the portal QR")
    parser.add_argument("--portal-qr-caption", metavar="TEXT",
                        help="text under the portal QR (empty string for none)")
    parser.add_argument("--gpio-pin", type=int, default=DEFAULT_BUTTON_PIN,
                        metavar="N",
                        help=f"BCM pin for the trigger button "
                             f"(default: {DEFAULT_BUTTON_PIN})")
    parser.add_argument("--no-gpio", action="store_true",
                        help="keyboard trigger only, ignore the button")
    parser.add_argument("--bounce-time", type=nonnegative_float,
                        default=DEFAULT_BOUNCE_S, metavar="SECONDS",
                        help=f"button debounce window "
                             f"(default: {DEFAULT_BOUNCE_S:g})")
    parser.add_argument("--keepalive-interval", type=float,
                        default=KEEPALIVE_INTERVAL_S, metavar="SECONDS",
                        help=f"idle keep-alive period (default: {KEEPALIVE_INTERVAL_S:.0f})")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="show libgphoto2 debug chatter")
    return parser.parse_args()


def report_not_ready(reason: str, notify,
                     message: str = NOT_READY_MESSAGE) -> None:
    """Say on the screen that no photo was taken, with the detail in the log."""
    print(f"  !! {reason}", file=sys.stderr)
    notify(f"WARNING {message}")
    time.sleep(NOT_READY_MESSAGE_S)
    notify("CLEAR")


def shoot(camera: Camera, countdown_s: float = 0.0, notify=None,
          capture_message: str = CAPTURE_MESSAGE) -> bool:
    """Count down, then take one photo. False if the camera needs reconnecting.

    `notify` mirrors the console countdown onto the screen, if a viewer is
    attached. `capture_message` is what that screen says while the shutter is
    working; empty leaves the countdown's last frame up instead.
    """
    notify = notify or (lambda _command: None)

    def tick(remaining: int) -> None:
        print_tick(remaining)
        notify(f"COUNTDOWN {remaining}")

    countdown(countdown_s, tick=tick)
    print("Capturing...")
    if capture_message:
        notify(f"MESSAGE {capture_message}")

    saved: list = []
    failure: list[CameraError] = []

    def work() -> None:
        try:
            saved.extend(camera.capture())
        except CameraError as err:
            failure.append(err)

    # The capture runs on a worker so this thread is free to update the
    # screen while it is stuck. The camera is touched from the worker alone,
    # and the worker is always joined below -- never abandoned. Tearing the
    # session down under an in-flight capture wedges the camera so thoroughly
    # that only a power cycle brings it back.
    worker = threading.Thread(target=work, name="capture", daemon=True)
    worker.start()
    worker.join(CAPTURE_SLOW_S)

    warned = worker.is_alive()
    if warned:
        # A shot that has not landed by now is not going to. libgphoto2 still
        # needs the best part of a minute to admit it, and leaving "Aufnahme..."
        # up for all of that tells the guest to keep smiling at a dead camera.
        print("Camera is not responding to the shutter.", file=sys.stderr)
        notify(f"WARNING {NOT_READY_MESSAGE}")
        worker.join()

    if failure:
        print(f"  !! {failure[0]}", file=sys.stderr)
        if not warned:
            notify(f"WARNING {NOT_READY_MESSAGE}")
            time.sleep(NOT_READY_MESSAGE_S)
        notify("CLEAR")
        return False

    for path in saved:
        print(f"  -> {path}")
    if warned:
        # It came good in the end: drop the warning rather than leaving it to
        # sit next to the photo that just proved it wrong.
        notify("CLEAR")
    return True


def viewer_options(args) -> list[str]:
    """Viewer flags to forward to display.py, omitting anything not asked for."""
    options: list[str] = []
    if args.no_qr:
        options.append("--no-qr")
    if args.qr:
        options += ["--qr", args.qr]
    if args.qr_height is not None:
        options += ["--qr-height", str(args.qr_height)]
    if args.qr_gap is not None:
        options += ["--qr-gap", str(args.qr_gap)]
    if args.qr_caption_size is not None:
        options += ["--qr-caption-size", str(args.qr_caption_size)]
    if args.qr_caption is not None:
        options += ["--qr-caption", args.qr_caption]
    if args.camera_hint is not None:
        options += ["--camera-hint", args.camera_hint]
    if args.camera_hint_size is not None:
        options += ["--camera-hint-size", str(args.camera_hint_size)]
    if args.no_portal_qr:
        options.append("--no-portal-qr")
    if args.portal_qr:
        options += ["--portal-qr", args.portal_qr]
    if args.portal_qr_caption is not None:
        options += ["--portal-qr-caption", args.portal_qr_caption]
    return options


def start_viewer(output_dir, extra_options=()) -> subprocess.Popen | None:
    """Launch display.py alongside the booth, watching the photo directory.

    A separate process by design: pygame needs its own event loop, and a
    display crash must not take the camera down with it.
    """
    script = Path(__file__).resolve().parent / "display.py"
    command = [sys.executable, str(script), "--dir", str(output_dir),
               *extra_options]

    # main.py runs as root (the access point needs it), but the viewer is a
    # GUI client of the desktop user's compositor and has no business being
    # root. Drop back to the invoking user, restoring the session environment
    # sudo stripped so SDL can still find Wayland.
    env = None
    sudo_uid = os.environ.get("SUDO_UID")
    if os.getuid() == 0 and sudo_uid:
        command = ["setpriv", "--reuid", sudo_uid,
                   "--regid", os.environ.get("SUDO_GID", sudo_uid),
                   "--init-groups", *command]
        env = {**os.environ,
               "HOME": os.path.expanduser(f"~{os.environ.get('SUDO_USER', 'pi')}"),
               "XDG_RUNTIME_DIR": f"/run/user/{sudo_uid}"}

    try:
        # stdin is the command channel: countdown digits go down this pipe.
        # Pinned to UTF-8 rather than the locale's encoding -- under systemd
        # or sudo that can still be ASCII, which would choke on the umlauts in
        # the German messages exactly when one needs to be shown.
        return subprocess.Popen(command, stdin=subprocess.PIPE, text=True,
                                encoding="utf-8", env=env)
    except OSError as err:
        print(f"Could not start the viewer: {err}", file=sys.stderr)
        return None


def viewer_notifier(viewer: subprocess.Popen | None):
    """A function that sends one command to the viewer, ignoring failures.

    The booth must keep shooting even if the screen has died, so every write
    error is swallowed rather than propagated.
    """
    if viewer is None or viewer.stdin is None:
        return lambda _command: None

    def send(command: str) -> None:
        try:
            viewer.stdin.write(command + "\n")
            viewer.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass

    return send


def stop_viewer(viewer: subprocess.Popen, notify) -> None:
    """Shut the viewer down, escalating only if it ignores us.

    Asking over the command pipe first is deterministic. SIGTERM is not:
    pygame installs SDL's signal handlers, which on this build swallow it
    without surfacing a quit event, so terminate() alone could stall until
    the timeout expired and we resorted to SIGKILL.
    """
    notify("QUIT")
    if viewer.stdin is not None:
        try:
            viewer.stdin.close()
        except OSError:
            pass
    for stage in (viewer.terminate, viewer.kill):
        try:
            viewer.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            stage()
    try:
        viewer.wait(timeout=3)
    except subprocess.TimeoutExpired:
        print("Viewer would not exit.", file=sys.stderr)


def reconnect(camera: Camera) -> bool:
    """Rebuild a dead session. False means the camera would not come back.

    Failing is not fatal -- the booth stays up and simply tries again on the
    next press, by which time the camera may have been sorted out.
    """
    print("Reconnecting...", file=sys.stderr)
    camera.disconnect()
    try:
        camera.connect()
        return True
    except CameraError as err:
        print(err, file=sys.stderr)
        return False


def main() -> int:
    return run(parse_args())


def run(args) -> int:
    """The booth loop, given already-parsed options.

    Split from main() so main.py can drive it with its own argument set
    without shelling out or re-parsing.
    """
    # No-op if the caller already configured logging (main.py does).
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    camera_class = MockCamera if args.mock_camera else Camera
    camera = camera_class(output_dir=args.output_dir,
                          delete_after_download=args.delete_after_download)
    try:
        camera.connect()
        connected = True
    except CameraError as err:
        # Not fatal outside --once. A camera that is asleep, switched off, or
        # in the wrong USB mode is fixed at the camera, and the booth exiting
        # would take the screen and the guest Wi-Fi down with it -- so it
        # starts anyway, says so on screen, and picks the camera up on the
        # first press after someone sorts it out.
        print(err, file=sys.stderr)
        connected = False
        if args.once:
            return 1

    viewer = (start_viewer(args.output_dir, viewer_options(args))
              if args.display else None)
    notify = viewer_notifier(viewer)

    try:
        if args.once:
            return 0 if shoot(camera, args.countdown, notify,
                              args.capture_message) else 1

        with Trigger(pin=None if args.no_gpio else args.gpio_pin,
                     bounce_time=args.bounce_time) as trigger:
            how = ("Press the button or Enter" if trigger.has_button
                   else "Press Enter")
            ready_msg = f"Ready. {how} to shoot, Ctrl-C to quit."
            if args.countdown:
                ready_msg += f" ({args.countdown:g}s countdown)"
            print(ready_msg)

            failures = 0
            while True:
                # One wait covers button, keyboard, and the keep-alive deadline.
                event = trigger.wait(args.keepalive_interval)
                if event is Event.QUIT:
                    return 0

                if event is Event.TRIGGER:
                    # With no session there is nothing to shoot through, so
                    # try to get one first -- this is the press right after
                    # someone woke the camera or fixed its USB mode.
                    if not connected:
                        connected = reconnect(camera)
                    if connected:
                        succeeded = shoot(camera, args.countdown, notify,
                                          args.capture_message)
                    else:
                        report_not_ready("No camera to shoot with.", notify,
                                         NO_CAMERA_MESSAGE)
                        succeeded = False
                    trigger.drain()  # drop triggers mashed during the countdown
                elif connected:
                    succeeded = camera.ping()
                else:
                    # Idle with no camera: keep trying quietly in the
                    # background so it is already up by the time anyone presses.
                    connected = reconnect(camera)
                    continue

                if succeeded:
                    failures = 0
                    continue

                # A failure usually means the session died (camera slept, cable
                # knocked, a dialog left the shutter unresponsive). Rebuild the
                # session so the next press has a clean one to work with.
                #
                # Never give up and exit, however long it stays broken: the
                # booth is unattended and the cure is at the camera, so quitting
                # would only mean nobody notices until the event is over. Once
                # the camera is dealt with, the next press just works.
                failures += 1
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    print(f"Camera has failed {failures} times in a row. Still "
                          f"listening -- check the camera is on, set to PC "
                          f"Remote, and has no dialog waiting on its screen.",
                          file=sys.stderr)
                connected = reconnect(camera)
    except KeyboardInterrupt:
        print()
        return 0
    finally:
        camera.disconnect()
        if viewer is not None:
            stop_viewer(viewer, notify)


if __name__ == "__main__":
    sys.exit(main())
