"""The booth loop: press the GPIO button or Enter to take a photo.

Run through main.py at the repo root, which also brings up the guest Wi-Fi.
Settings come from the `booth`, `screen` and `gallery` sections of
config.yaml; there is no command line here.

The keyboard stays live alongside the button so a shot can still be fired
over SSH. Ctrl-C quits.
"""

from __future__ import annotations

import math
import os
import pwd
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from camera import Camera, CameraError, MockCamera
from trigger import Event, Trigger

# How long a failure message stays up before the screen goes back to the
# last photo.
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

# After this many failures in a row, say plainly in the log that the camera
# itself needs looking at. The booth keeps running either way.
MAX_CONSECUTIVE_FAILURES = 3


@dataclass(frozen=True)
class Messages:
    """What the screen says around a shot (config.yaml `screen`).

    German, because the guests and the person running the booth read the
    screen, not the log.
    """

    # From the moment the shutter fires until the photo lands. Empty leaves
    # the countdown's last frame up instead.
    capture: str
    # The camera answers but will not fire: a dialog is waiting on its screen.
    not_ready: str
    # No camera at all: switched off or out of PC Remote mode.
    no_camera: str

    @classmethod
    def from_config(cls, screen: dict) -> "Messages":
        return cls(capture=screen.get("capture_message") or "",
                   not_ready=screen["not_ready_message"],
                   no_camera=screen["no_camera_message"])


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


def report_not_ready(reason: str, notify, message: str) -> None:
    """Say on the screen that no photo was taken, with the detail in the log."""
    print(f"  !! {reason}", file=sys.stderr)
    notify(f"WARNING {message}")
    time.sleep(NOT_READY_MESSAGE_S)
    notify("CLEAR")


def shoot(camera: Camera, countdown_s: float, notify, messages: Messages) -> bool:
    """Count down, then take one photo. False if the camera needs reconnecting.

    `notify` mirrors the console countdown onto the screen, if a viewer is
    attached.
    """
    def tick(remaining: int) -> None:
        print_tick(remaining)
        notify(f"COUNTDOWN {remaining}")

    countdown(countdown_s, tick=tick)
    print("Capturing...")
    if messages.capture:
        notify(f"MESSAGE {messages.capture}")

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
        # needs the best part of a minute to admit it, and leaving the capture
        # message up for all of that tells the guest to keep smiling at a dead
        # camera.
        print("Camera is not responding to the shutter.", file=sys.stderr)
        notify(f"WARNING {messages.not_ready}")
        worker.join()

    if failure:
        print(f"  !! {failure[0]}", file=sys.stderr)
        if not warned:
            notify(f"WARNING {messages.not_ready}")
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


def start_viewer(photo_dir: Path, show_qr: bool) -> subprocess.Popen | None:
    """Launch display.py alongside the booth, watching the photo directory.

    A separate process by design: pygame needs its own event loop, and a
    display crash must not take the camera down with it. display.py reads its
    texts and sizes from config.yaml itself.
    """
    script = Path(__file__).resolve().parent / "display.py"
    command = [sys.executable, str(script), "--dir", str(photo_dir)]
    if not show_qr:
        command.append("--no-qr")

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
               "HOME": pwd.getpwuid(int(sudo_uid)).pw_dir,
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


def run(config: dict, *, mock_camera: bool = False,
        display: bool = True, show_qr: bool = True) -> int:
    """The booth loop. Returns the process exit code.

    `config` is the whole of config.yaml (pbcommon.load_config()); the rest
    are main.py's run-mode flags.
    """
    settings = config["booth"]
    messages = Messages.from_config(config["screen"])
    photo_dir = Path(config["gallery"]["photo_dir"])
    countdown_s = max(0.0, float(settings["countdown_seconds"]))
    keepalive_s = float(settings["keepalive_seconds"])
    gpio_pin = settings.get("gpio_pin")  # null in config.yaml: keyboard only

    camera_class = MockCamera if mock_camera else Camera
    camera = camera_class(output_dir=photo_dir,
                          delete_after_download=bool(
                              settings.get("delete_after_download", False)))
    try:
        camera.connect()
        connected = True
    except CameraError as err:
        # Not fatal. A camera that is asleep, switched off, or in the wrong USB
        # mode is fixed at the camera, and the booth exiting would take the
        # screen and the guest Wi-Fi down with it -- so it starts anyway, says
        # so on screen, and picks the camera up on the first press after
        # someone sorts it out.
        print(err, file=sys.stderr)
        connected = False

    viewer = start_viewer(photo_dir, show_qr) if display else None
    notify = viewer_notifier(viewer)

    try:
        with Trigger(pin=gpio_pin,
                     bounce_time=float(settings["bounce_seconds"])) as trigger:
            how = ("Press the button or Enter" if trigger.has_button
                   else "Press Enter")
            ready_msg = f"Ready. {how} to shoot, Ctrl-C to quit."
            if countdown_s:
                ready_msg += f" ({countdown_s:g}s countdown)"
            print(ready_msg)

            failures = 0
            while True:
                # One wait covers button, keyboard, and the keep-alive deadline.
                event = trigger.wait(keepalive_s)
                if event is Event.QUIT:
                    return 0

                if event is Event.TRIGGER:
                    # With no session there is nothing to shoot through, so
                    # try to get one first -- this is the press right after
                    # someone woke the camera or fixed its USB mode.
                    if not connected:
                        connected = reconnect(camera)
                    if connected:
                        succeeded = shoot(camera, countdown_s, notify, messages)
                    else:
                        report_not_ready("No camera to shoot with.", notify,
                                         messages.no_camera)
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
