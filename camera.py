"""Capture stills from a USB-tethered camera (Sony A7 III) via libgphoto2."""

from __future__ import annotations

import logging
import os
import random
import shutil
import time
from datetime import datetime
from pathlib import Path

import gphoto2 as gp

log = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "photos"

# How long to keep listening for extra files after a shot. RAW+JPEG produces
# two GP_EVENT_FILE_ADDED events, and the second one lags the first.
EXTRA_FILE_WAIT_S = 2.0


class CameraError(RuntimeError):
    """A camera problem with an explanation the operator can act on."""


def _explain(err: gp.GPhoto2Error) -> str:
    """Turn a libgphoto2 error code into an actionable message."""
    hints = {
        gp.GP_ERROR_MODEL_NOT_FOUND: (
            "No camera detected. Check that it is powered on, the USB cable is "
            "in the camera's DATA port, and USB Connection is set to PC Remote."
        ),
        gp.GP_ERROR_IO_USB_CLAIM: (
            "Another process is holding the camera. Usually a gvfs volume "
            "monitor auto-mounted it -- see README troubleshooting."
        ),
        gp.GP_ERROR_IO: (
            "USB I/O error. Often a power or cable problem: use a powered hub "
            "and a known-good data cable."
        ),
    }
    hint = hints.get(err.code)
    base = f"{err} (code {err.code})"
    return f"{base}\n{hint}" if hint else base


class Camera:
    """A connected camera. Use as a context manager, or call connect/disconnect."""

    def __init__(self, output_dir: Path | str = DEFAULT_OUTPUT_DIR,
                 delete_after_download: bool = False) -> None:
        self.output_dir = Path(output_dir)
        self.delete_after_download = delete_after_download
        self._camera: gp.Camera | None = None

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "Camera":
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.disconnect()

    def connect(self) -> None:
        camera = gp.Camera()
        try:
            camera.init()
        except gp.GPhoto2Error as err:
            raise CameraError(_explain(err)) from err
        self._camera = camera
        log.info("Connected to %s", self.model())

    def disconnect(self) -> None:
        if self._camera is not None:
            self._camera.exit()
            self._camera = None
            log.info("Disconnected")

    def model(self) -> str:
        cam = self._require()
        try:
            config = cam.get_config()
            return config.get_child_by_name("cameramodel").get_value()
        except gp.GPhoto2Error:
            return "unknown model"

    def _require(self) -> gp.Camera:
        if self._camera is None:
            raise CameraError("Not connected -- call connect() first.")
        return self._camera

    def ping(self) -> bool:
        """Poke the camera to reset its idle timer. True if it answered.

        The A7 III caps Power Save Start Time at 30 minutes and offers no
        way to disable it, so an idle booth would eventually drop the USB
        session. Reading config is the cheapest PTP round-trip available.
        """
        try:
            self._require().get_config()
            return True
        except gp.GPhoto2Error as err:
            log.warning("Keep-alive failed: %s", _explain(err))
            return False

    # -- capture -----------------------------------------------------------

    def capture(self, basename: str | None = None) -> list[Path]:
        """Trigger the shutter and download whatever files it produced.

        Returns one path per file, so a RAW+JPEG shot returns two.
        """
        cam = self._require()
        basename = basename or datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]

        try:
            first = cam.capture(gp.GP_CAPTURE_IMAGE)
        except gp.GPhoto2Error as err:
            raise CameraError(_explain(err)) from err

        saved = [self._download(first, basename)]
        for extra in self._drain_new_files(EXTRA_FILE_WAIT_S):
            saved.append(self._download(extra, basename))
        return saved

    def _drain_new_files(self, seconds: float):
        """Yield CameraFilePaths announced within `seconds` of now."""
        cam = self._require()
        deadline = time.monotonic() + seconds
        while True:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                return
            event_type, data = cam.wait_for_event(remaining_ms)
            if event_type == gp.GP_EVENT_TIMEOUT:
                return
            if event_type == gp.GP_EVENT_FILE_ADDED:
                yield data

    def _download(self, path: gp.CameraFilePath, basename: str) -> Path:
        """Copy one file off the camera into output_dir."""
        cam = self._require()
        suffix = Path(path.name).suffix or ".jpg"
        target = self._unique_path(basename, suffix)
        target.parent.mkdir(parents=True, exist_ok=True)

        try:
            camera_file = cam.file_get(path.folder, path.name, gp.GP_FILE_TYPE_NORMAL)
            camera_file.save(str(target))
            if self.delete_after_download:
                cam.file_delete(path.folder, path.name)
        except gp.GPhoto2Error as err:
            raise CameraError(_explain(err)) from err

        log.info("Saved %s (%.1f MB)", target, target.stat().st_size / 1e6)
        return target

    def _unique_path(self, basename: str, suffix: str) -> Path:
        """Avoid clobbering when one shot yields several files (RAW+JPEG)."""
        return unique_path(self.output_dir, basename, suffix)


def unique_path(directory: Path, basename: str, suffix: str) -> Path:
    """A non-existent path in `directory`, suffixing -1, -2 ... if needed."""
    candidate = directory / f"{basename}{suffix}"
    counter = 1
    while candidate.exists():
        candidate = directory / f"{basename}-{counter}{suffix}"
        counter += 1
    return candidate


def timestamp_basename() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]


class MockCamera:
    """Stand-in for `Camera` that recycles photos already on disk.

    Same interface as `Camera`, so the booth, countdown, display, ingest and
    gallery can all be exercised with no camera attached. A "capture" copies a
    random existing photo under a fresh timestamped name, which means every
    downstream consumer -- the viewer's newest-file poll, thumbnail
    generation, the zip rebuild -- sees exactly what a real shot produces.
    """

    # Roughly what a real capture plus USB download costs on this hardware.
    # Without it the mock is unrealistically instant and hides timing bugs.
    CAPTURE_DELAY_S = 0.8

    def __init__(self, output_dir: Path | str = DEFAULT_OUTPUT_DIR,
                 delete_after_download: bool = False,
                 source_dir: Path | str | None = None) -> None:
        self.output_dir = Path(output_dir)
        self.delete_after_download = delete_after_download  # accepted, unused
        self.source_dir = Path(source_dir) if source_dir else self.output_dir
        self._connected = False

    def __enter__(self) -> "MockCamera":
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.disconnect()

    def _sources(self) -> list[Path]:
        try:
            return [p for p in self.source_dir.iterdir()
                    if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png"}]
        except OSError:
            return []

    def connect(self) -> None:
        sources = self._sources()
        if not sources:
            raise CameraError(
                f"Mock camera has no photos to recycle in {self.source_dir}. "
                f"Put at least one .jpg there, or drop --mock-camera.")
        self._connected = True
        log.info("Connected to mock camera (%d source photo(s) in %s)",
                 len(sources), self.source_dir)

    def disconnect(self) -> None:
        self._connected = False

    def model(self) -> str:
        return "mock camera"

    def ping(self) -> bool:
        return self._connected

    def capture(self, basename: str | None = None) -> list[Path]:
        """Copy a random source photo under a new timestamped name."""
        if not self._connected:
            raise CameraError("Not connected -- call connect() first.")
        sources = self._sources()
        if not sources:
            raise CameraError(f"No photos left to recycle in {self.source_dir}")

        time.sleep(self.CAPTURE_DELAY_S)
        source = random.choice(sources)
        target = unique_path(self.output_dir, basename or timestamp_basename(),
                             source.suffix)
        target.parent.mkdir(parents=True, exist_ok=True)

        # Copy via a temp file and rename, so nothing ever observes a partial
        # image. display.py polls for the newest file and ingest watches this
        # directory; both would otherwise race a half-copied JPEG.
        tmp = target.with_suffix(target.suffix + ".tmp")
        try:
            shutil.copyfile(source, tmp)
            os.replace(tmp, target)
        except OSError as err:
            tmp.unlink(missing_ok=True)
            raise CameraError(f"Mock capture failed: {err}") from err

        log.info("Mock captured %s (copy of %s)", target.name, source.name)
        return [target]
