#!/usr/bin/env python3
"""Pre-generate gallery thumbnails whenever photos change.

Resizing a 24 MP JPEG is far too slow to do per request with several guests
loading the page at once, so it happens here, once, in the background. PIL's
draft() lets libjpeg downscale during decode (DCT scaling) instead of
unpacking 24 MP first -- several times faster.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path

from PIL import Image, ImageOps

# pbcommon is shared with wifi/ and deploy/ and lives at the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pbcommon  # noqa: E402

log = logging.getLogger("ingest")
_running = True

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}


def _stop(_signum, _frame) -> None:
    global _running
    _running = False


def photo_files(photo_dir: Path) -> list[Path]:
    try:
        return sorted((e for e in photo_dir.iterdir()
                       if e.is_file() and e.suffix.lower() in IMAGE_SUFFIXES),
                      key=lambda p: p.name)
    except OSError:
        return []


def signature(files: list[Path]) -> tuple:
    """Cheap fingerprint of the directory: names, sizes and mtimes.

    Compared between polls to detect change without reading any image data.
    """
    out = []
    for path in files:
        try:
            st = path.stat()
        except OSError:
            continue
        out.append((path.name, st.st_size, int(st.st_mtime)))
    return tuple(out)


def build_thumbnail(source: Path, target: Path, width: int) -> bool:
    """Write one downscaled thumbnail. False if the source was unreadable."""
    try:
        image = Image.open(source)
        # Ask libjpeg for roughly the size we need; it decodes at a reduced
        # DCT scale, which is where nearly all the saving comes from.
        image.draft("RGB", (width, width))
        image = ImageOps.exif_transpose(image)  # honour camera rotation
        image = image.convert("RGB")
        if image.width > width:
            height = max(1, round(image.height * width / image.width))
            image = image.resize((width, height), Image.BILINEAR)

        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        # progressive=False keeps decode cheap on the phone as well.
        image.save(tmp, "JPEG", quality=78, optimize=False, progressive=False)
        os.replace(tmp, target)  # atomic: nginx never serves a partial file
        return True
    except Exception as err:
        log.warning("thumbnail failed for %s: %s", source.name, err)
        return False


def sync_thumbnails(config: dict, files: list[Path]) -> int:
    """Create missing or stale thumbnails. Returns how many were written."""
    thumb_dir = Path(config["gallery"]["thumb_dir"])
    width = int(config["gallery"]["thumb_width"])
    thumb_dir.mkdir(parents=True, exist_ok=True)

    wanted = set()
    written = 0
    for source in files:
        target = thumb_dir / source.name
        wanted.add(target.name)
        try:
            fresh = (target.exists()
                     and target.stat().st_mtime >= source.stat().st_mtime)
        except OSError:
            fresh = False
        if fresh:
            continue
        if not _running:
            break
        if build_thumbnail(source, target, width):
            written += 1

    # Remove orphans so deleted photos stop appearing in the grid.
    for existing in thumb_dir.iterdir():
        if existing.is_file() and existing.name not in wanted \
                and not existing.name.endswith(".tmp"):
            existing.unlink(missing_ok=True)

    return written


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # Be the first thing the scheduler drops. Capturing photos and serving
    # guests both matter more than a thumbnail appearing a second sooner.
    try:
        os.nice(10)
    except OSError:
        pass

    config = pbcommon.load_config()
    photo_dir = Path(config["gallery"]["photo_dir"])
    poll = float(config["gallery"]["ingest_poll_seconds"])
    log.info("watching %s (poll %.0fs)", photo_dir, poll)

    last_built: tuple | None = None

    while _running:
        files = photo_files(photo_dir)
        current = signature(files)

        # Only new or changed photos get a thumbnail, so this is cheap even
        # with a full evening on disk. A photo caught half-written fails to
        # decode, changes size by the next poll, and is retried then.
        if current != last_built:
            made = sync_thumbnails(config, files)
            if made:
                log.info("generated %d thumbnail(s)", made)
            if _running:
                last_built = current

        deadline = time.time() + poll
        while _running and time.time() < deadline:
            time.sleep(min(0.5, max(0.0, deadline - time.time())))

    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
