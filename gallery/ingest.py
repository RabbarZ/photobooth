#!/usr/bin/env python3
"""Pre-generate thumbnails and the full-gallery zip whenever photos change.

Everything expensive happens here, once, in the background -- never during a
guest's request:

  * Thumbnails are built on ingest. Resizing a 24 MP JPEG takes seconds on a
    Pi 3, so doing it per request would be hopeless with several guests.
  * The zip is written to disk with NO COMPRESSION and served as a static file
    by nginx. Streaming or building it per request would burn the same CPU and
    IO repeatedly for every guest; building it in memory is impossible on 1 GB.

JPEGs are already compressed, so store-only costs nothing in size and saves all
the deflate CPU. PIL's draft() is used for thumbnails because it lets libjpeg
downscale during decode (DCT scaling) instead of unpacking 24 MP first --
several times faster on this hardware.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
import zipfile
from pathlib import Path

from PIL import Image, ImageOps

import pbcommon

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


def build_zip(config: dict, files: list[Path]) -> Path | None:
    """Write the full-gallery zip to disk, atomically.

    ZIP_STORED (no deflate) because JPEGs do not compress -- we would spend
    minutes of CPU to save perhaps 1%. Files are streamed in one at a time, so
    peak memory is a single buffer regardless of gallery size.
    """
    zip_path = Path(config["gallery"]["zip_path"])
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = zip_path.with_suffix(".zip.tmp")

    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED,
                             allowZip64=True) as archive:
            for path in files:
                if not _running:
                    log.info("zip build interrupted; discarding partial file")
                    tmp.unlink(missing_ok=True)
                    return None
                try:
                    archive.write(path, arcname=path.name)
                except OSError as err:
                    log.warning("skipped %s in zip: %s", path.name, err)
        os.chmod(tmp, 0o644)
        os.replace(tmp, zip_path)  # guests downloading the old zip are unaffected
    except OSError as err:
        log.error("zip build failed: %s", err)
        tmp.unlink(missing_ok=True)
        return None

    size = zip_path.stat().st_size
    log.info("zip rebuilt: %d photo(s), %.1f MB", len(files), size / 1e6)
    return zip_path


def rebuild(config: dict, files: list[Path]) -> None:
    made = sync_thumbnails(config, files)
    if made:
        log.info("generated %d thumbnail(s)", made)
    if _running:
        build_zip(config, files)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # Be the first thing the scheduler drops. Capturing photos and serving
    # guests both matter more than finishing a rebuild quickly.
    try:
        os.nice(10)
    except OSError:
        pass

    config = pbcommon.load_config()
    photo_dir = Path(config["gallery"]["photo_dir"])
    poll = float(config["gallery"]["ingest_poll_seconds"])
    debounce = float(config["gallery"]["ingest_debounce_seconds"])
    log.info("watching %s (poll %.0fs, debounce %.0fs)", photo_dir, poll, debounce)

    last_built: tuple | None = None
    pending: tuple | None = None
    pending_since = 0.0

    while _running:
        files = photo_files(photo_dir)
        current = signature(files)

        if current != last_built:
            if current != pending:
                # Directory changed (again) -- restart the debounce window so a
                # burst of shots produces one rebuild, not one per photo.
                pending, pending_since = current, time.time()
                log.debug("change detected; debouncing")
            elif time.time() - pending_since >= debounce:
                rebuild(config, files)
                if _running:
                    last_built, pending = current, None

        deadline = time.time() + poll
        while _running and time.time() < deadline:
            time.sleep(min(0.5, max(0.0, deadline - time.time())))

    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
