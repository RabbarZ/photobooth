#!/usr/bin/env python3
"""Fullscreen viewer: shows the newest photo until a newer one appears.

Started by booth.py, but also runs on its own for testing the screen:

    python3 booth/display.py                # watch gallery.photo_dir
    python3 booth/display.py --windowed     # in a window, not fullscreen

Texts, sizes and the QR files come from the `screen` and `password` sections
of config.yaml. Runs as its own process, so a display fault can never stop the
camera and pygame's event loop does not have to share a thread with the
booth's wait. Press Esc or Q to quit.
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

# pbcommon (config loading) lives at the repo root, one level up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pbcommon  # noqa: E402

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}

POLL_INTERVAL_S = 0.3
BACKGROUND = (0, 0, 0)
TEXT_COLOUR = (110, 110, 110)
COUNTDOWN_COLOUR = (245, 245, 245)
# Warnings are for the person running the booth to catch across the room, so
# they get full brightness and a colour that reads as a fault rather than the
# muted grey of ordinary status text.
WARNING_COLOUR = (228, 82, 72)
# The camera hint is an instruction to act on within the next three seconds,
# not chrome, so it gets the countdown's brightness rather than the muted grey
# of ordinary status text -- which at across-the-room distance simply vanishes.
HINT_COLOUR = (245, 245, 245)

# Keeps wrapped text off the very edge of the panel.
MESSAGE_MARGIN = 48

# Gap between the QR plates and the screen edge. Shared with the message
# layout, which reserves their footprint so text is never drawn under them.
BADGE_MARGIN = 24

# Commands accepted on stdin, one per line. booth.py sends these down the
# pipe it already owns, so the countdown appears on screen the instant the
# trigger fires rather than only in the console.
#   COUNTDOWN <n>   big number, centred
#   MESSAGE <text>  smaller text, centred
#   WARNING <text>  the same, in red: something needs attention
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
    # 6 MP surface to pygame.transform.smoothscale instead is several times
    # slower than the whole PIL path.
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
    """One white plate per QR code, stacked down the right edge.

    A plate each rather than one shared box, so the codes can be spaced apart
    by `gap` without the padding of a common frame fighting it. Both codes are
    still rendered at the same size -- differing sizes imply one matters more,
    when in fact neither works without the other.

    The render loop only calls changed() and blit_onto(), so it does not care
    how many codes there are.
    """

    def __init__(self, entries: "list[tuple[Path, str]]", height: int,
                 gap: int, caption_size: int) -> None:
        self.entries = entries
        self.height = height
        self.gap = gap
        # Floored: pygame quietly accepts 0 and renders a caption two pixels
        # tall rather than failing, which looks like the text went missing.
        self.caption_size = max(16, caption_size)
        self._mtimes: list[float | None] = [None] * len(entries)
        self._plates: list[pygame.Surface] = []

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

    def _compose_one(self, qr: pygame.Surface,
                     label: pygame.Surface | None, pad: int) -> pygame.Surface:
        """One code and its caption on a plate of their own."""
        # Breathing room between code and caption, scaled to the caption: a
        # title set tight under the QR reads as part of the pattern above it.
        lead = max(pad // 2, self.caption_size // 4)
        width = max(self.height, label.get_width() if label else 0) + pad * 2
        height = pad + self.height
        if label:
            height += lead + label.get_height()
        height += pad

        # Opaque white plate: the codes need their quiet zone and hard contrast
        # regardless of how dark or busy the photo behind them happens to be.
        plate = pygame.Surface((width, height))
        plate.fill((255, 255, 255))

        y = pad
        plate.blit(qr, ((width - self.height) // 2, y))
        y += self.height
        if label:
            y += lead
            plate.blit(label, ((width - label.get_width()) // 2, y))
        return plate

    def _compose(self) -> list[pygame.Surface]:
        pad = max(8, self.height // 20)
        font = _font(self.caption_size)

        # A code whose file is missing is skipped entirely rather than leaving
        # a white gap where a QR should be.
        plates = []
        for path, caption in self.entries:
            qr = self._load(path)
            if qr is None:
                continue
            label = font.render(caption, True, (20, 20, 20)) if caption else None
            plates.append(self._compose_one(qr, label, pad))
        return plates

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

    def surfaces(self) -> list[pygame.Surface]:
        """Current plates, rebuilt only when one of the PNGs changes."""
        stamps = self._current_mtimes()
        if stamps != self._mtimes:
            self._mtimes = stamps
            self._plates = self._compose()
        return self._plates

    def footprint(self) -> tuple[int, int]:
        """Size of the whole stack, gaps included: (width, height).

        The one place the stack's extent is worked out, so what blit_onto
        draws and what text_area keeps clear can never drift apart.
        """
        plates = self.surfaces()
        if not plates:
            return 0, 0
        width = max(plate.get_width() for plate in plates)
        height = (sum(plate.get_height() for plate in plates)
                  + self.gap * (len(plates) - 1))
        return width, height

    def blit_onto(self, screen: pygame.Surface,
                  margin: int = BADGE_MARGIN) -> None:
        plates = self.surfaces()
        if not plates:
            return
        width, total = self.footprint()
        # One vertical axis for every plate: the captions differ in width, so
        # aligning their right edges instead would leave the codes themselves
        # visibly stepped.
        axis = screen.get_width() - margin - width // 2
        # Clamped so a stack taller than the screen runs off the bottom rather
        # than off the top, where it would lose the first step.
        y = max(margin, (screen.get_height() - total) // 2)
        for plate in plates:
            screen.blit(plate, (axis - plate.get_width() // 2, y))
            y += plate.get_height() + self.gap


_FONT_CACHE: dict[int, pygame.font.Font] = {}


def _font(size: int) -> pygame.font.Font:
    if size not in _FONT_CACHE:
        _FONT_CACHE[size] = pygame.font.Font(None, size)
    return _FONT_CACHE[size]


def _wrap(text: str, font: pygame.font.Font, max_width: int) -> list[str]:
    """Break `text` into lines that fit inside max_width."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and font.size(candidate)[0] > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    return lines + [current] if current else lines or [""]


def render_message(text: str, size: int, colour, max_width: int,
                   max_height: int | None = None) -> pygame.Surface:
    """Centred, wrapped text on a transparent plate.

    Messages are written for a person, not for a layout, so they are long
    enough to run off the side of the screen. Wrap first, then shrink until
    the block fits both the width and, where one is given, the height it has
    been allotted.
    """
    while True:
        font = _font(size)
        lines = _wrap(text, font, max_width)
        too_wide = max(font.size(line)[0] for line in lines) > max_width
        too_tall = (max_height is not None
                    and font.get_linesize() * len(lines) > max_height)
        if size <= 24 or not (too_wide or too_tall):
            break
        size = max(24, int(size * 0.8))

    surfaces = [font.render(line, True, colour) for line in lines]
    width = max(surface.get_width() for surface in surfaces)
    step = font.get_linesize()
    plate = pygame.Surface((width, step * len(surfaces)), pygame.SRCALPHA)
    for index, surface in enumerate(surfaces):
        plate.blit(surface, ((width - surface.get_width()) // 2, index * step))
    return plate


def text_area(screen: pygame.Surface,
              badge: "BadgePanel | None") -> pygame.Rect:
    """The region an overlay may use without hiding under the QRs.

    The plates are drawn last and so win every overlap; a warning nobody can
    read because a QR is sitting on top of it is worse than no warning.

    The region stays centred on the screen and is narrowed *symmetrically* --
    losing as much on the left as the plates take on the right. Text then grows
    outwards from the middle of the screen, where the eye already is, rather
    than sliding off-centre by half a QR plate whenever a code is on show.
    """
    area = screen.get_rect()
    width = badge.footprint()[0] if badge is not None else 0
    if width:
        # Left edge of the plates. Centred content may reach neither it nor
        # its mirror image on the far side of the screen's centre.
        clear = area.right - BADGE_MARGIN - width
        area.width = max(area.width // 3, 2 * (clear - area.centerx))
        area.centerx = screen.get_rect().centerx
    return area


def draw_overlay(screen: pygame.Surface, kind: str, value: str,
                 badge: "BadgePanel | None" = None,
                 hint: str = "",
                 hint_size: int = 128) -> None:
    """Draw a countdown digit or a short message, centred on black.

    `hint` is the look-at-the-lens line, drawn under the countdown -- the one
    stretch where the guest still has time to act on it.
    """
    screen.fill(BACKGROUND)
    height = screen.get_height()
    # Centred on the screen; only the width is clipped back by the plates.
    area = text_area(screen, badge)
    if kind == "count":
        surface = _font(max(64, height * 2 // 3)).render(
            value, True, COUNTDOWN_COLOUR)
    else:
        colour = WARNING_COLOUR if kind == "warning" else TEXT_COLOUR
        surface = render_message(value, max(32, height // 10), colour,
                                 area.width - 2 * MESSAGE_MARGIN,
                                 area.height - 2 * MESSAGE_MARGIN)

    # Countdown only. By the time the capture message is up the shutter has already
    # gone, so the hint would be telling people where to look a moment too
    # late -- and pulling their eyes off the lens just as the next frame lands.
    banner = None
    if hint and kind == "count":
        banner = render_message(hint, hint_size, HINT_COLOUR,
                                area.width - 2 * MESSAGE_MARGIN)

    if banner is None:
        screen.blit(surface, surface.get_rect(center=area.center))
    else:
        gap = hint_size // 2
        total = surface.get_height() + gap + banner.get_height()
        # Clamped so an oversized countdown digit loses its slack at the
        # bottom rather than pushing the hint off the screen.
        top = max(0, min(area.centery - total // 2, area.bottom - total))
        screen.blit(surface, surface.get_rect(midtop=(area.centerx, top)))
        screen.blit(banner, banner.get_rect(
            midtop=(area.centerx, top + surface.get_height() + gap)))
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
        area = text_area(screen, badge)
        text = render_message(message, 48, TEXT_COLOUR,
                              area.width - 2 * MESSAGE_MARGIN,
                              area.height - 2 * MESSAGE_MARGIN)
        screen.blit(text, text.get_rect(center=area.center))
    if badge is not None:
        badge.blit_onto(screen)
    pygame.display.flip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", type=Path,
                        help="directory to watch (default: gallery.photo_dir "
                             "in config.yaml)")
    parser.add_argument("--windowed", action="store_true",
                        help="run in a window instead of fullscreen (for testing)")
    parser.add_argument("--no-qr", action="store_true",
                        help="show neither QR code (booth.py passes this when "
                             "the guest Wi-Fi is off)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = pbcommon.load_config()
    settings = config["screen"]
    photo_dir = args.dir or Path(config["gallery"]["photo_dir"])
    hint = settings.get("camera_hint") or ""
    hint_size = int(settings["camera_hint_size"])
    fast = bool(settings.get("fast_decode", False))
    waiting = settings.get("waiting_message") or ""

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
              "  export XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=wayland-0",
              file=sys.stderr)
        pygame.quit()
        return 1
    pygame.mouse.set_visible(False)
    install_signal_handlers()  # after pygame.init(), which sets SDL's own

    screen_size = screen.get_size()
    clock = pygame.time.Clock()
    commands = CommandReader(sys.stdin)
    # Both codes at one size: they are two steps of one instruction, so they
    # are sized and framed alike even on separate plates. The join QR is
    # rewritten on every passphrase rotation, the gallery QR once when the
    # Wi-Fi comes up; a missing file is skipped until it appears.
    entries = []
    if not args.no_qr:
        files = config["password"]
        entries = [(Path(files["qr_path"]), settings.get("join_qr_caption") or ""),
                   (Path(files["portal_qr_path"]),
                    settings.get("gallery_qr_caption") or "")]
        for path, _ in entries:
            if not path.exists():
                print(f"QR not found at {path} -- it appears once the guest "
                      f"Wi-Fi writes it", file=sys.stderr)
    badge = (BadgePanel(entries, int(settings["qr_height"]),
                        int(settings["qr_gap"]),
                        int(settings["qr_caption_size"])) if entries else None)
    shown: Path | None = None
    surface: pygame.Surface | None = None
    overlay: tuple[str, str] | None = None
    next_poll = 0.0

    draw(screen, None, waiting, badge)

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
            elif verb == "WARNING":
                overlay = ("warning", rest.strip())
            elif verb == "CLEAR":
                overlay = None
                draw(screen, surface, waiting
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
                draw_overlay(screen, *overlay, badge=badge,
                             hint=hint,
                             hint_size=hint_size)

        now = time.monotonic()
        if now >= next_poll:
            next_poll = now + POLL_INTERVAL_S
            latest = newest_image(photo_dir)
            if latest is not None and latest != shown and is_complete(latest):
                try:
                    surface = load_scaled(latest, screen_size, fast)
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
                    draw_overlay(screen, *overlay, badge=badge,
                                 hint=hint,
                                 hint_size=hint_size)
                else:
                    draw(screen, surface, badge=badge,
                         message=None if surface else waiting)
                print("QR updated (passphrase rotated)", file=sys.stderr)

        clock.tick(30)

    pygame.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
