#!/usr/bin/env python3
"""Fullscreen viewer: shows the newest photo until a newer one appears.

    python3 display.py                  # watch the default photos/ directory
    python3 display.py --dir /some/path
    python3 display.py --fast           # quicker decode, slightly softer

Runs as its own process, so a display fault can never stop the camera and
pygame's event loop does not have to share a thread with the booth's wait.
Press Esc or Q to quit.
"""

from __future__ import annotations

import argparse
import os
import select
import signal
import sys
import time
from pathlib import Path

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")


def _discover_wayland() -> None:
    """Point SDL at the desktop session when the environment does not.

    Two things routinely strip this away:
      * sudo (env_reset) -- and main.py needs root for the access point, so
        the viewer it spawns inherits root's empty environment;
      * labwc itself, which does not export WAYLAND_DISPLAY into its own
        environment even though its clients have it.

    Without this SDL silently falls back to the 'offscreen' driver and renders
    to nowhere, so the only symptom is a black screen. Probing for the socket
    is more reliable than asking anyone to remember two exports.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        # Under sudo, HOME is /root and the runtime dir is gone; derive it from
        # the invoking user rather than root.
        uid = os.environ.get("SUDO_UID") or str(os.getuid())
        candidate = Path(f"/run/user/{uid}")
        if candidate.is_dir():
            runtime = str(candidate)
            os.environ["XDG_RUNTIME_DIR"] = runtime
    if not runtime:
        return

    if not os.environ.get("WAYLAND_DISPLAY"):
        sockets = sorted(Path(runtime).glob("wayland-[0-9]*"))
        sockets = [s for s in sockets if s.suffix != ".lock"]
        if sockets:
            os.environ["WAYLAND_DISPLAY"] = sockets[0].name

    # Running as root against another user's compositor needs this: without it
    # libwayland refuses the connection over the unix socket.
    if os.getuid() == 0 and os.environ.get("SUDO_UID"):
        os.environ.setdefault("XDG_SESSION_TYPE", "wayland")


_discover_wayland()

import pygame  # noqa: E402
from PIL import Image, ImageOps  # noqa: E402

DEFAULT_PHOTO_DIR = Path(__file__).resolve().parent / "photos"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}

# Written by the gallery's password rotator (gallery/rotate_password.py) and
# rewritten in place on every rotation. Overlaying this path means the screen
# always shows a QR that still works -- no coordination between the two
# systems beyond the filename.
DEFAULT_QR_PATH = Path("/var/lib/photobooth/wifi-qr.png")
DEFAULT_QR_HEIGHT = 260
DEFAULT_QR_CAPTION = "1. Mit WLAN verbinden"

# Written once at AP bring-up (gallery/accesspoint.py), never rotated: it
# encodes the portal URL, not credentials. Scanning it opens the gallery
# directly. Same size as the join QR and on the same plate: the two are one
# two-step instruction, and a smaller second code read as optional when it is
# not -- but it is useless before the phone is on the network, hence "2.".
DEFAULT_PORTAL_QR_PATH = Path("/var/lib/photobooth/portal-qr.png")
DEFAULT_PORTAL_QR_CAPTION = "2. Galerie öffnen"

POLL_INTERVAL_S = 0.3
BACKGROUND = (0, 0, 0)
TEXT_COLOUR = (110, 110, 110)
COUNTDOWN_COLOUR = (245, 245, 245)

# Commands accepted on stdin, one per line. booth.py sends these down the
# pipe it already owns, so the countdown appears on screen the instant the
# trigger fires rather than only in the console.
#   COUNTDOWN <n>   big number, centred
#   MESSAGE <text>  smaller text, centred
#   CLEAR           go back to showing the latest photo
#   QUIT            shut down
#
# Nothing here ever clears the screen on a timer: once a photo is shown it
# stays until the next one arrives, indefinitely.

# SDL silently falls back to these when it finds no display, rendering to
# nowhere and reporting success -- which looks exactly like a working viewer
# showing a black screen. Treat it as an error unless deliberately requested.
HEADLESS_DRIVERS = {"offscreen", "dummy"}


def newest_image(directory: Path) -> Path | None:
    """The most recently modified image in `directory`, or None."""
    newest: Path | None = None
    newest_mtime = -1.0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.is_file():
                    continue
                if Path(entry.name).suffix.lower() not in IMAGE_SUFFIXES:
                    continue
                mtime = entry.stat().st_mtime
                if mtime > newest_mtime:
                    newest, newest_mtime = Path(entry.path), mtime
    except FileNotFoundError:
        pass  # directory not created yet -- the booth makes it on first shot
    return newest


def is_complete(path: Path) -> bool:
    """True if `path` looks fully written.

    The booth may still be saving when the poll notices the file. A JPEG ends
    with the EOI marker 0xFFD9, which is a cheap and reliable completeness
    check; anything else we optimistically accept and let the loader judge.
    """
    if path.suffix.lower() not in {".jpg", ".jpeg"}:
        return True
    try:
        with open(path, "rb") as handle:
            if handle.seek(0, os.SEEK_END) < 4:
                return False
            handle.seek(-2, os.SEEK_END)
            return handle.read(2) == b"\xff\xd9"
    except OSError:
        return False


def fit_size(image_size: tuple[int, int],
             screen_size: tuple[int, int]) -> tuple[int, int]:
    """Largest size fitting inside the screen while keeping aspect ratio."""
    scale = min(screen_size[0] / image_size[0], screen_size[1] / image_size[1])
    return max(1, round(image_size[0] * scale)), max(1, round(image_size[1] * scale))


def load_scaled(path: Path, screen_size: tuple[int, int],
                fast: bool) -> pygame.Surface:
    """Decode `path` and scale it to fit the screen.

    draft() lets libjpeg downscale *during* decode using DCT scaling, which is
    far cheaper than decoding 24 MP and shrinking afterwards. Asking for half
    the screen size makes it pick a smaller scale factor -- quicker, at the
    cost of a slight upscale later.
    """
    image = Image.open(path)
    hint = ((screen_size[0] // 2, screen_size[1] // 2) if fast else screen_size)
    image.draft("RGB", hint)
    image = ImageOps.exif_transpose(image)  # honour camera rotation
    image = image.convert("RGB")

    # Resize in PIL and build the surface at its final size. Handing a
    # 6 MP surface to pygame.transform.smoothscale instead costs ~5s on a
    # Pi 3 -- several times the whole PIL path.
    target = fit_size(image.size, screen_size)
    if image.size != target:
        image = image.resize(target, Image.BILINEAR)
    return pygame.image.frombuffer(image.tobytes(), image.size, "RGB")


_stop_requested = False


def _request_stop(_signum, _frame) -> None:
    global _stop_requested
    _stop_requested = True


def install_signal_handlers() -> None:
    """Quit promptly on SIGTERM/SIGINT.

    SDL installs its own handlers during pygame.init() and, on this Wayland
    build, swallows SIGTERM without ever surfacing a QUIT event -- so
    Popen.terminate() would hang until the caller gave up and sent SIGKILL.
    Must be called *after* pygame.init(), or SDL overwrites these.
    """
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _request_stop)


class CommandReader:
    """Reads newline-delimited commands from a stream without blocking.

    Deliberately uses raw os.read rather than readline(). A buffered reader
    pulls every available byte into its own buffer on the first call, after
    which select() reports the fd as not-ready and any further lines sit
    stranded until more input happens to arrive.
    """

    def __init__(self, stream) -> None:
        self._buffer = ""
        # Hold the stream, not just its descriptor: dropping the last
        # reference would let the garbage collector close the fd underneath us.
        self._stream = stream
        try:
            self._fd = stream.fileno() if stream is not None else None
        except (AttributeError, ValueError, OSError):
            self._fd = None

    def poll(self) -> list[str]:
        """Every complete command waiting right now."""
        while self._fd is not None:
            try:
                ready, _, _ = select.select([self._fd], [], [], 0)
            except (ValueError, OSError):
                self._fd = None  # closed underneath us
                break
            if not ready:
                break
            try:
                chunk = os.read(self._fd, 4096)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                self._fd = None
                break
            if not chunk:
                # EOF: the booth exited. Stop watching, or select() would
                # report ready forever and spin the loop.
                self._fd = None
                break
            self._buffer += chunk.decode("utf-8", "replace")

        commands: list[str] = []
        while "\n" in self._buffer:
            line, _, self._buffer = self._buffer.partition("\n")
            if line.strip():
                commands.append(line.strip())
        return commands


class BadgePanel:
    """Both QR codes on one white plate: step 1 above step 2.

    One plate rather than two, because the codes are a single two-step
    instruction and two floating boxes read as two unrelated offers. Both codes
    are rendered at the same size for the same reason -- differing sizes imply
    one matters more, when in fact neither works without the other.

    The render loop only calls changed() and blit_onto(), so it does not care
    how many codes are on the plate.
    """

    def __init__(self, entries: "list[tuple[Path, str]]", height: int) -> None:
        self.entries = entries
        self.height = height
        self._mtimes: list[float | None] = [None] * len(entries)
        self._panel: pygame.Surface | None = None

    # -- composition ---------------------------------------------------------

    def _load(self, path: Path) -> pygame.Surface | None:
        try:
            image = Image.open(path).convert("RGB")
        except Exception as err:
            print(f"Cannot load QR {path}: {err}", file=sys.stderr)
            return None
        # NEAREST, deliberately. A QR is a binary bitmap: smooth interpolation
        # blurs the module edges and makes it noticeably harder for a phone to
        # lock on, especially from across a room.
        image = image.resize((self.height, self.height), Image.NEAREST)
        return pygame.image.frombuffer(image.tobytes(), image.size, "RGB")

    def _compose(self) -> pygame.Surface | None:
        pad = max(8, self.height // 20)
        font = _font(max(16, self.height // 9))

        # Load first: a code whose file is missing is skipped entirely rather
        # than leaving a white gap where a QR should be.
        blocks = []
        for path, caption in self.entries:
            qr = self._load(path)
            if qr is None:
                continue
            blocks.append((qr, font.render(caption, True, (20, 20, 20))
                           if caption else None))
        if not blocks:
            return None

        widths = [self.height] + [lbl.get_width() for _, lbl in blocks if lbl]
        width = max(widths) + pad * 2

        height = pad
        for index, (_, label) in enumerate(blocks):
            height += self.height
            if label:
                height += pad // 2 + label.get_height()
            # Gap between the two steps, but not after the last one.
            height += pad if index < len(blocks) - 1 else 0
        height += pad

        # Opaque white plate: the codes need their quiet zone and hard contrast
        # regardless of how dark or busy the photo behind them happens to be.
        panel = pygame.Surface((width, height))
        panel.fill((255, 255, 255))

        y = pad
        for index, (qr, label) in enumerate(blocks):
            panel.blit(qr, ((width - self.height) // 2, y))
            y += self.height
            if label:
                y += pad // 2
                panel.blit(label, ((width - label.get_width()) // 2, y))
                y += label.get_height()
            y += pad if index < len(blocks) - 1 else 0
        return panel

    # -- caching -------------------------------------------------------------

    def _current_mtimes(self) -> list[float | None]:
        stamps: list[float | None] = []
        for path, _ in self.entries:
            try:
                stamps.append(path.stat().st_mtime)
            except OSError:
                stamps.append(None)
        return stamps

    def changed(self) -> bool:
        """True if either PNG has been rewritten since the plate was built.

        Checked every poll, not just when something else redraws: the booth can
        sit idle for far longer than a rotation interval, and a stale join QR is
        worse than none -- a guest scans a passphrase that has aged out of the
        ring and simply cannot connect, with nothing to explain why.
        """
        return self._current_mtimes() != self._mtimes

    def surface(self) -> pygame.Surface | None:
        """Current plate, rebuilt only when one of the PNGs changes."""
        stamps = self._current_mtimes()
        if stamps != self._mtimes:
            self._mtimes = stamps
            self._panel = self._compose()
        return self._panel

    def blit_onto(self, screen: pygame.Surface, margin: int = 24) -> None:
        panel = self.surface()
        if panel is None:
            return
        screen.blit(panel, (screen.get_width() - panel.get_width() - margin,
                            screen.get_height() - panel.get_height() - margin))


_FONT_CACHE: dict[int, pygame.font.Font] = {}


def _font(size: int) -> pygame.font.Font:
    if size not in _FONT_CACHE:
        _FONT_CACHE[size] = pygame.font.Font(None, size)
    return _FONT_CACHE[size]


def draw_overlay(screen: pygame.Surface, kind: str, value: str,
                 badge: "BadgePanel | None" = None) -> None:
    """Draw a countdown digit or a short message, centred on black."""
    screen.fill(BACKGROUND)
    height = screen.get_height()
    if kind == "count":
        surface = _font(max(64, height * 2 // 3)).render(
            value, True, COUNTDOWN_COLOUR)
    else:
        surface = _font(max(32, height // 10)).render(value, True, TEXT_COLOUR)
    screen.blit(surface, surface.get_rect(center=screen.get_rect().center))
    # The QR stays up during the countdown too: a guest waiting for the shutter
    # is exactly who has a spare moment to scan it.
    if badge is not None:
        badge.blit_onto(screen)
    pygame.display.flip()


def draw(screen: pygame.Surface, surface: pygame.Surface | None,
         message: str | None = None, badge: "BadgePanel | None" = None) -> None:
    screen.fill(BACKGROUND)
    if surface is not None:
        screen.blit(surface, surface.get_rect(center=screen.get_rect().center))
    elif message:
        text = _font(48).render(message, True, TEXT_COLOUR)
        screen.blit(text, text.get_rect(center=screen.get_rect().center))
    if badge is not None:
        badge.blit_onto(screen)
    pygame.display.flip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", type=Path, default=DEFAULT_PHOTO_DIR,
                        help=f"directory to watch (default: {DEFAULT_PHOTO_DIR})")
    parser.add_argument("--fast", action="store_true",
                        help="decode at a lower scale: quicker, slightly softer")
    parser.add_argument("--windowed", action="store_true",
                        help="run in a window instead of fullscreen (for testing)")
    parser.add_argument("--qr", type=Path, default=DEFAULT_QR_PATH, metavar="PNG",
                        help=f"Wi-Fi join QR to overlay, rewritten by the "
                             f"gallery's password rotator "
                             f"(default: {DEFAULT_QR_PATH})")
    parser.add_argument("--no-qr", action="store_true",
                        help="do not overlay the join QR")
    parser.add_argument("--qr-height", type=int, default=DEFAULT_QR_HEIGHT,
                        metavar="PX",
                        help=f"QR size in pixels (default: {DEFAULT_QR_HEIGHT})")
    parser.add_argument("--qr-caption", default=DEFAULT_QR_CAPTION,
                        help="text under the QR (empty string for none)")
    parser.add_argument("--portal-qr", type=Path,
                        default=DEFAULT_PORTAL_QR_PATH, metavar="PNG",
                        help=f"portal-URL QR, written at AP bring-up; opens "
                             f"the gallery directly "
                             f"(default: {DEFAULT_PORTAL_QR_PATH})")
    parser.add_argument("--no-portal-qr", action="store_true",
                        help="do not overlay the portal QR")
    parser.add_argument("--portal-qr-caption", default=DEFAULT_PORTAL_QR_CAPTION,
                        help="text under the portal QR (empty string for none)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    pygame.init()
    pygame.display.set_caption("Photobooth")
    flags = 0 if args.windowed else pygame.FULLSCREEN
    size = (1280, 720) if args.windowed else (0, 0)
    try:
        screen = pygame.display.set_mode(size, flags)
    except pygame.error as err:
        print(f"Cannot open a display: {err}\n"
              "Run this inside the desktop session (WAYLAND_DISPLAY must be set).",
              file=sys.stderr)
        return 1

    driver = pygame.display.get_driver()
    if driver in HEADLESS_DRIVERS and "SDL_VIDEODRIVER" not in os.environ:
        print(f"SDL fell back to the '{driver}' driver, so nothing would appear "
              "on screen.\nRun this from the desktop session, or point it at "
              "one:\n"
              "  export XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0",
              file=sys.stderr)
        pygame.quit()
        return 1
    pygame.mouse.set_visible(False)
    install_signal_handlers()  # after pygame.init(), which sets SDL's own

    screen_size = screen.get_size()
    clock = pygame.time.Clock()
    commands = CommandReader(sys.stdin)
    # Both codes at args.qr_height, on one plate: they are two steps of one
    # instruction, so they are sized and framed alike.
    entries = []
    if not args.no_qr:
        entries.append((args.qr, args.qr_caption))
        if not args.qr.exists():
            print(f"QR not found at {args.qr} -- the overlay will appear as "
                  f"soon as the rotator writes it", file=sys.stderr)
    if not args.no_portal_qr:
        entries.append((args.portal_qr, args.portal_qr_caption))
        if not args.portal_qr.exists():
            print(f"Portal QR not found at {args.portal_qr} -- it is written "
                  f"when the AP starts (main.py, not display.py alone)",
                  file=sys.stderr)
    badge = BadgePanel(entries, args.qr_height) if entries else None
    shown: Path | None = None
    surface: pygame.Surface | None = None
    overlay: tuple[str, str] | None = None
    next_poll = 0.0

    draw(screen, None, "Warte auf das erste Foto...", badge)

    running = True
    while running and not _stop_requested:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN and event.key in (
                    pygame.K_ESCAPE, pygame.K_q):
                running = False

        # Commands are checked every frame (~33 ms), so the countdown appears
        # promptly rather than waiting on the file poll below.
        for command in commands.poll():
            verb, _, rest = command.partition(" ")
            verb = verb.upper()
            if verb == "COUNTDOWN":
                overlay = ("count", rest.strip())
            elif verb == "MESSAGE":
                overlay = ("message", rest.strip())
            elif verb == "CLEAR":
                overlay = None
                draw(screen, surface, "Warte auf das erste Foto..."
                     if surface is None else None, badge)
            elif verb == "QUIT":
                # Commanded shutdown. Deterministic, unlike SIGTERM: SDL
                # installs its own signal handlers and on this build does not
                # reliably surface them as a QUIT event.
                running = False
                break
            else:
                print(f"Unknown command: {command}", file=sys.stderr)
                continue
            if overlay is not None:
                draw_overlay(screen, *overlay, badge=badge)

        now = time.monotonic()
        if now >= next_poll:
            next_poll = now + POLL_INTERVAL_S
            latest = newest_image(args.dir)
            if latest is not None and latest != shown and is_complete(latest):
                try:
                    surface = load_scaled(latest, screen_size, args.fast)
                except Exception as err:
                    # Half-written or corrupt: keep the current photo up and
                    # retry on the next poll rather than blanking the screen.
                    print(f"Cannot show {latest.name}: {err}", file=sys.stderr)
                else:
                    shown = latest
                    overlay = None  # the new photo supersedes any countdown
                    draw(screen, surface, badge=badge)

            # A rotation rewrites the QR while nothing else is happening, and
            # an idle booth may go far longer than a rotation interval without
            # a redraw. Repaint whatever is currently up so the code on screen
            # is always one a phone can actually use.
            elif badge is not None and badge.changed():
                if overlay is not None:
                    draw_overlay(screen, *overlay, badge=badge)
                else:
                    draw(screen, surface, badge=badge,
                         message=None if surface else "Warte auf das erste Foto...")
                print("QR updated (passphrase rotated)", file=sys.stderr)

        clock.tick(30)

    pygame.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
