"""Shutter triggers: a GPIO button and/or the keyboard.

Both sources feed a single `select()` call so the idle keep-alive can share
the same wait without either starving the other.
"""

from __future__ import annotations

import logging
import os
import select
import sys
import time
from enum import Enum, auto

log = logging.getLogger(__name__)

DEFAULT_BUTTON_PIN = 17
DEFAULT_BOUNCE_S = 0.1


class Event(Enum):
    """Why a wait() returned."""

    TRIGGER = auto()  # button pressed, or Enter on the keyboard
    TIMEOUT = auto()  # nothing happened before the deadline
    QUIT = auto()     # keyboard input ended and there is no button


class Trigger:
    """Waits for a shutter trigger from a GPIO button and/or the keyboard.

    gpiozero fires its callback on its own thread, which cannot be handed to
    select(). The button therefore uses the self-pipe trick: the callback
    writes a byte to a pipe that select() *can* watch, so one wait covers
    both sources with no polling and no race.
    """

    def __init__(self, pin: int | None = DEFAULT_BUTTON_PIN,
                 bounce_time: float = DEFAULT_BOUNCE_S) -> None:
        self._sources: list = [sys.stdin]
        self._button = None
        self._pipe_r: int | None = None
        self._pipe_w: int | None = None
        if pin is not None:
            self._attach_button(pin, bounce_time)

    def _attach_button(self, pin: int, bounce_time: float) -> None:
        """Wire up the GPIO button, degrading to keyboard-only on failure."""
        try:
            from gpiozero import Button
        except ImportError:
            log.warning("gpiozero not installed -- keyboard trigger only")
            return
        try:
            button = Button(pin, bounce_time=bounce_time)
        except Exception as err:  # gpiozero raises a zoo of pin/lib errors
            log.warning("GPIO %d unavailable (%s) -- keyboard trigger only",
                        pin, err)
            return

        self._pipe_r, self._pipe_w = os.pipe()
        os.set_blocking(self._pipe_w, False)
        button.when_pressed = self._on_press
        self._button = button
        self._sources.append(self._pipe_r)
        log.info("Button on GPIO %d (bounce %.0f ms)", pin, bounce_time * 1000)

    @property
    def has_button(self) -> bool:
        return self._button is not None

    def _on_press(self) -> None:
        """Runs on gpiozero's thread -- keep it to one non-blocking write."""
        try:
            os.write(self._pipe_w, b"\x01")
        except BlockingIOError:
            pass  # pipe full: a trigger is already pending, which is enough

    def wait(self, timeout: float) -> Event:
        """Block until a trigger arrives, the timeout expires, or input ends."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return Event.TIMEOUT
            ready, _, _ = select.select(self._sources, [], [], remaining)
            if not ready:
                return Event.TIMEOUT

            triggered = False
            for source in ready:
                if source is sys.stdin:
                    if sys.stdin.readline() != "":
                        triggered = True
                    elif self._button is None:
                        return Event.QUIT
                    else:
                        # Headless (stdin is /dev/null under systemd): it
                        # reads ready forever, so stop watching it rather
                        # than spinning -- or quitting a working booth.
                        self._sources.remove(sys.stdin)
                        log.info("Keyboard input ended -- button only")
                else:
                    os.read(self._pipe_r, 4096)
                    triggered = True

            if triggered:
                return Event.TRIGGER
            # stdin was dropped and nothing else fired: keep waiting.

    def drain(self) -> None:
        """Discard triggers that piled up during the countdown or capture."""
        while True:
            ready, _, _ = select.select(self._sources, [], [], 0)
            if not ready:
                return
            for source in ready:
                if source is sys.stdin:
                    if sys.stdin.readline() == "":
                        return  # EOF: readline would spin forever
                else:
                    os.read(self._pipe_r, 4096)

    def close(self) -> None:
        if self._button is not None:
            self._button.close()
            self._button = None
        for fd in (self._pipe_r, self._pipe_w):
            if fd is not None:
                os.close(fd)
        self._pipe_r = self._pipe_w = None

    def __enter__(self) -> "Trigger":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
