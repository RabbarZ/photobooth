# Photobooth — Sony A7 III over USB

Tethered capture from a Raspberry Pi using libgphoto2.

## What is installed

Both from Debian trixie's repos (no venv needed — `python3-gphoto2` is a system package):

| Package | Version | Purpose |
| --- | --- | --- |
| `gphoto2` | 2.5.28 | CLI, used for diagnostics |
| `python3-gphoto2` | 2.5.1 | Python bindings used by `camera.py` |
| `libgphoto2-6t64` | 2.5.31 | the actual driver library |

`gvfs-gphoto2-volume-monitor` has been **masked**. The desktop's gvfs
auto-mounts any PTP camera and then libgphoto2 cannot claim it
(`-53 Could not claim the USB device`). Undo with:

```bash
systemctl --user unmask gvfs-gphoto2-volume-monitor.service
```

Device permissions come from `/lib/udev/rules.d/60-libgphoto2-6t64.rules`,
which grants `plugdev` access to any USB PTP interface. `pi` is already in
`plugdev`, so no sudo is needed.

## USB tuning applied to the host

The Pi's USB host controller needs no configuration — libgphoto2 reaches the
camera through libusb, so there is no kernel driver to bind and no device-tree
overlay to enable. Two kernel defaults were unfriendly to tethering, though.
`usbcore` is built into this kernel (not a module), so `modprobe.d` does not
apply; these avoid editing `cmdline.txt`, where a typo costs you a boot.

| File | What it does |
| --- | --- |
| `/etc/udev/rules.d/70-photobooth-camera.rules` | Sets `power/control=on` for any libgphoto2-recognised camera, disabling USB autosuspend |
| `/etc/tmpfiles.d/photobooth-usbfs.conf` | Raises `usbfs_memory_mb` from 16 to 1000 at boot |

**Autosuspend** defaulted to 2 seconds. A booth idles between guests, and a
suspended camera can drop the PTP session. The rule keys off `ID_GPHOTO2`,
which `60-libgphoto2-6t64.rules` already sets, so it covers any supported body.

**usbfs memory** defaulted to 16 MB, which is the buffer ceiling for in-flight
USB transfers. This is insurance rather than a confirmed fix — it is the
standard recommendation for tethered shooting with high-resolution bodies.

Remove either file and reboot (or `sudo udevadm control --reload-rules`) to undo.

`max_usb_current` in `config.txt` was deliberately **not** set. It raises the
Pi 3B's total USB budget from 600 mA to 1.2 A, which is still not enough for an
A7 III and encourages exactly the setup the next section warns against.

## Camera settings (Sony A7 III)

Set these on the camera before plugging in — most connection failures are
one of these, not a software problem.

Press `MENU` and move right along the tab row to the **Setup** tab — the yellow
toolbox icon, after the two camera tabs, Network, and Playback. Page down
through its sub-pages to the group containing `HDMI Settings`, `USB Connection`,
`USB LUN Setting`, `USB Power Supply`, and `PC Remote Setting`. (Page numbers
moved between firmware 2.x and 3.x, so look for the cluster, not a page.)

1. **`USB Connection` → `PC Remote`**
   Not Auto, not Mass Storage, not MTP. Only PC Remote lets the Pi fire the
   shutter — in the other modes the camera still appears in
   `gphoto2 --auto-detect` but `--capture-image` fails, which is a confusing
   way to lose an hour.
2. **`PC Remote Setting` → Still Img. Save Dest.** (a separate item from
   `USB Connection`, in the same group)
   - `PC+Camera` — writes to the SD card *and* sends to the Pi. Safest: you
     keep an on-camera backup.
   - `PC Only` — no card writes, nothing to clean up later.
3. **`Power Save Start Time` → `30 Min`.**
   An earlier page of the same Setup tab, grouped with `Display Quality`.
   The default 1 minute puts the camera to sleep between guests and the
   USB session dies with it. 30 Min is the maximum the A7 III offers — the
   full list is 30 Min / 5 Min / 2 Min / 1 Min / 10 Sec, with no `Off`.
   Because you cannot disable it outright, `booth.py` sends a keep-alive
   while idle; see below.
4. **Shoot JPEG-only** unless you want RAW. RAW+JPEG produces two files per
   shot and roughly triples the download time over USB 2.0.
5. Use the camera's **DATA** USB port (the Type-C / micro-B data port), not a
   charge-only port, and a cable known to carry data.

## Power

This Pi is a USB 2.0 model — all ports share one bus with the Ethernet
controller and a modest total current budget. Do **not** rely on the Pi to
power the A7 III. Run the camera on its own battery or a dummy-battery mains
adapter, and put a **powered USB hub** between the Pi and the camera. Brownouts
show up as `-7 I/O error` mid-transfer, which looks like a software bug.

## Usage

```bash
./check-camera.sh                # diagnostics — run first when something breaks
python3 booth.py                 # press Enter to shoot, Ctrl-C to quit
python3 booth.py --once          # single shot
python3 booth.py --countdown 5   # longer countdown
python3 booth.py --countdown 0   # fire immediately
python3 booth.py --no-gpio       # keyboard only, ignore the button
python3 booth.py --gpio-pin 27   # button on a different pin
python3 booth.py --display       # also show each photo fullscreen
python3 display.py               # viewer on its own, watching photos/
```

Photos land in `photos/`, named `YYYYMMDD-HHMMSS-mmm.JPG`. A RAW+JPEG shot
saves both files under the same timestamp, the second suffixed `-1`.

### Countdown

`--countdown SECONDS` (default 3, `0` disables) sets the delay between the
trigger and the shutter. It prints `3... 2... 1... ` and then fires.

Each tick sleeps toward a fixed deadline rather than sleeping a flat second,
so the total stays accurate even if the display work is slow — a 3s countdown
takes 3.00s. A fractional value spends the remainder on the first tick, so
`--countdown 2.5` gives a short `3...` and full-length `2...` and `1...`,
keeping the final second honest.

Triggers pressed *during* the countdown or capture are discarded
(`drain_stdin`). Without that, someone impatiently mashing the button queues
a burst of shots that all fire back to back once the current one finishes.

To drive an LED ring, a screen, or a buzzer instead of stdout, pass a
different `tick` callable to `countdown()` — it receives the number of
seconds remaining:

```python
countdown(3.0, tick=lambda n: gpio_flash(n))
```

### Focus

Focus is entirely the camera's job — there is no focus logic in `camera.py`.
But `capture()` is not a bare shutter release. For Sony bodies the libgphoto2
PTP driver:

1. sets `PTP_DPC_SONY_AutoFocus` (the PTP equivalent of a half-press),
2. waits for `PTP_DPC_SONY_FocusFound`,
3. and only then completes the press.

So the camera chooses *how* to focus, while the driver makes the shot
*conditional* on focus succeeding. If AF never locks — dim lighting, a
low-contrast backdrop, a guest inside the lens's minimum focus distance —
the driver hits `Capture timed out` and you get **no photo at all**, surfaced
as a `CameraError`. A dead trigger with a guest waiting is worse than a
slightly soft frame, so configure to avoid it:

- **Preferred: manual focus, prefocused.** Mark the spot guests stand on,
  focus once, flick the lens to MF, tape the ring. Deterministic, fastest
  (no AF round-trip per shot), and the shutter always fires. Shoot f/5.6-f/8
  so small differences in where people stand stay sharp.
- **If you want AF:** set `Priority Set in AF-S` to `Release` rather than `AF`
  or `Balanced Emphasis`. The camera then fires without a lock — an occasional
  soft frame instead of an occasional missing one.

The driver exposes focus mode as a settable config (`AF-S`, `AF-C`, `AF-A`,
`Manual`), so the Pi can enforce it rather than trusting that nobody bumped a
dial:

```bash
gphoto2 --get-config focusmode          # what is it now
gphoto2 --set-config focusmode=Manual   # force it
```

Note this sets the *body's* focus mode; a lens with a physical AF/MF switch
set to MF overrides it regardless.

### Staying awake

Because the A7 III cannot be told never to sleep (30 minutes is the maximum),
`booth.py` pings the camera every 120 seconds while idle — a cheap PTP config
read that should reset its idle timer. Tune with `--keepalive-interval`.

If a shot or a ping fails, the booth reconnects and carries on, since the usual
cause is a dropped session rather than a missing camera. After
`MAX_CONSECUTIVE_FAILURES` (3) failures in a row it exits non-zero instead of
reconnecting forever — that pattern means the camera is genuinely unwell, and
a supervisor restarting the process is better than a silent retry loop.

## Files

- `camera.py` — `Camera` class: connect, capture, download. Hardware only.
- `trigger.py` — `Trigger` class: GPIO button and/or keyboard, unified wait.
- `display.py` — standalone fullscreen viewer of the newest photo.
- `booth.py` — the loop: countdown, capture, keep-alive, reconnect.
- `check-camera.sh` — connection diagnostics.

## Wiring

Nothing on this Pi claims the GPIO header — no I2C, SPI, 1-Wire, or HAT — so
the whole header is free. `gpiozero` runs on the `lgpio` backend and `pi` is in
the `gpio` group, so no sudo and nothing to install.

| Function | GPIO | Physical pin | GND pin |
| --- | --- | --- | --- |
| Trigger button | 17 | 11 | 9 |
| Green "ready" LED | 27 | 13 | 14 |
| Red "error" LED | 22 | 15 | 14 |

GPIO17/GND are **adjacent** on the header, so the button is one 2-pin
connector with no off-by-one risk. GPIO17 has no alternate function, and
`Button(17)` already defaults to the internal pull-up this wiring needs.

A button's contact block is a **dry contact** — an isolated switch with no
ground terminal and no polarity. Wire one terminal to GND and the other to
GPIO17; which is which does not matter. Pressing shorts the pin to ground and
it reads LOW.

- Arcade microswitch (COM / NO / NC spades): use **COM** and **NO**, leave NC.
- Eaton M22 block (e.g. `M22-KC10`): terminals `3` and `4`, printed `13`/`14`
  on some variants. Per IEC 60947-5-1 a `3`/`4` pair is normally-open and a
  `1`/`2` pair is normally-closed.

Confirm the contact is **normally open** before wiring — a N/C block
(`...01` suffix) inverts the logic and leaves the booth permanently triggered.
Multimeter on continuity: N/O is open when released and beeps when pressed.

**Never wire a button to 5V (pins 2 or 4).** GPIO is 3.3V and not 5V tolerant;
there is no protection diode. Every button goes between a GPIO and GND.

Pins to avoid on this board:

| Pin | GPIO | Why |
| --- | --- | --- |
| 3, 5 | 2, 3 | Fixed 1.8k hardware pull-ups; cannot be pulled down |
| 8, 10 | 14, 15 | Serial console is enabled in `cmdline.txt` |
| 27, 28 | 0, 1 | Reserved for HAT EEPROM detection |
| 12 | 18 | PWM, and `dtparam=audio=on` is set |

Test the wiring before writing code — press the button and watch for an edge:

```bash
gpiomon -b pull-up --banner GPIO17
```

### Booth-specific gotchas

- **A button with a built-in LED** is usually 12V or 5V and cannot run from a
  GPIO (~16 mA per pin, ~50 mA total). It needs a MOSFET and its own supply.
- **Long cable runs cause phantom triggers.** The internal pull-up is weak
  (~50k), so 1-2 m of unshielded wire acts as an antenna. If `gpiomon` shows
  edges you did not cause, add a 10k pull-up to 3.3V and a 100nF capacitor
  across the switch terminals.
- **Industrial contacts need wetting current.** M22-class blocks use silver
  contacts rated for amps; the internal pull-up passes only ~66 uA, too little
  to break down the oxide film that forms on them, which reads as presses that
  intermittently do nothing. The same external 10k pull-up fixes this (~330 uA);
  1k (~3.3 mA) if dropouts persist. Gold-plated blocks avoid it entirely.
- **Always debounce — but `bounce_time` means something different on a Pi 5.**
  This advice used to read "pass `bounce_time=0.1`", which was correct on the
  Pi 3 and is wrong here.

  On the Pi 3, gpiozero used the RPi.GPIO backend, where `bounce_time` is a
  **lockout**: the first edge fires immediately, further edges are ignored for
  that long. The Pi 5 has no RPi.GPIO support, so gpiozero falls back to
  `LGPIOFactory`, which implements it with `lgpio.gpio_set_debounce_micros` —
  *"an alert will only be issued if the edge has been stable for at least
  debounce microseconds"*. That is a **minimum hold time**, so 0.1 silently
  dropped every press shorter than 100 ms and a quick tap is only 40-80 ms.
  Same value, same library, opposite behaviour.

  The default is now **25 ms**: past real contact bounce (a microswitch settles
  in under 10 ms, a large industrial block in ~20 ms) while leaving the
  quickest deliberate tap well inside the window. Raising `--bounce-time`
  lengthens the press the booth demands, one for one.
- A 330R-1k resistor in series with the GPIO leg limits current if the pin is
  ever misconfigured as an output — cheap insurance for public hardware.

## Display

`--display` shows each photo fullscreen until the next one is taken.
`display.py` also runs standalone against any directory, which is handy for
testing without a camera.

It runs as a **separate process**, spawned by `booth.py` and terminated with
it. That is deliberate: pygame wants its own event loop, which would fight the
booth's `select()` wait, and a display fault must never stop the camera. The
viewer simply watches the photo directory for the newest file — nothing is
piped between the two, so either can be restarted alone.

### How it reaches the screen

`display.py` is not a system image viewer — none is installed — and it does
not write to HDMI directly. It is an ordinary fullscreen Wayland client:

```
display.py
  PIL          decode JPEG -> RGB buffer
  pygame/SDL2  buffer -> surface, "wayland" video driver
  labwc        composites the fullscreen surface
  DRM/KMS      /dev/dri/card0, vc4 driver
  HDMI-A-1     -> the monitor
```

Writing straight to the hardware is not possible while the desktop is up:
**labwc holds DRM master** on `/dev/dri/card0`, and only one process can.
`/dev/fb0` exists (`vc4drmfb`, fbdev emulation over the same KMS device, and
it only appears once a monitor is attached) but drawing into it while a
compositor owns the display gets overwritten.

Going direct would mean SDL's `kmsdrm` driver on a bare TTY with labwc
stopped — marginally less copying, no desktop, more to go wrong. Not worth it
here; the compositor path costs almost nothing for a still image.

### Connecting the monitor

Plug an HDMI monitor or TV into the Pi. **A laptop cannot be used as the
screen** — laptop HDMI ports are outputs, so Pi-out to laptop-out moves no
data in either direction. (Only the rare laptop with a port explicitly
labelled `HDMI IN` can act as a sink.)

If the Pi booted with nothing attached, labwc invents a synthetic headless
output and will keep using it. After plugging in, check that a real one
appeared:

```bash
cat /sys/class/drm/card0-HDMI-A-1/status      # want: connected
export XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0
wlr-randr                                     # want: HDMI-A-1, not NOOP-1
```

If it still reports `NOOP-1 "Headless output"`, reboot with the monitor
attached and powered on — hotplug into an already-headless session is the
unreliable case.

**On a 4K display**, `display.py` scales to the full panel, and `draft()` can
no longer decode at a reduced scale — expect several seconds per photo. Use
`--fast`, or set the output to 1080p, unless the extra sharpness matters.

### Countdown on screen

With `--display`, the countdown appears on the monitor as well as the console,
starting the instant the trigger fires. `booth.py` sends commands down the
viewer's **stdin pipe** — the one it already owns by spawning it — so there is
no socket, no state file, and no extra lifecycle to manage.

| Command | Effect |
| --- | --- |
| `COUNTDOWN <n>` | big digit, centred on black |
| `MESSAGE <text>` | smaller text, centred |
| `CLEAR` | back to the latest photo |
| `QUIT` | shut down |

The viewer checks for commands every frame (~33 ms), so digits appear
promptly rather than waiting for the 0.3 s file poll. A new photo clears any
overlay automatically.

**A photo stays on screen indefinitely.** Nothing clears it on a timer — it
remains until the next photo replaces it. Nothing on this Pi is configured to
blank the screen either (no idle daemon, `consoleblank=0`).

Two details worth knowing if you extend this:

- **Startup is slow on a Pi 3** — roughly 7 s to import pygame and PIL, open
  the Wayland surface, and decode the first photo. Commands sent before then
  wait in the pipe and are processed once the loop starts.
- **Shutdown is commanded, not signalled.** pygame installs SDL's signal
  handlers, which on this build swallow `SIGTERM` without surfacing a quit
  event, so `terminate()` alone stalls until the caller resorts to `SIGKILL`.
  `stop_viewer()` sends `QUIT` first (~1 s, clean exit 0) and only escalates
  to signals if that is ignored.

### Speed

Decoding a 6000x4000 JPEG on a Pi 3 is the slow part, so the viewer leans on
`Image.draft()`, which lets libjpeg downscale *during* decode via DCT scaling
rather than unpacking 24 MP and shrinking afterwards.

| Path | Time |
| --- | --- |
| Naive full decode + LANCZOS | ~5.2 s |
| Handing a 6 MP surface to `pygame.transform.smoothscale` | ~6.2 s |
| draft + PIL resize (default) | ~1.9 s |
| draft + PIL resize, `--fast` | ~1.0 s |

All scaling happens in PIL and the pygame surface is built at its final size;
letting pygame scale a large surface is several times slower than the entire
PIL path. `--fast` decodes at a smaller scale and upscales slightly — quicker,
marginally softer.

### Gotchas handled

- **Half-written files.** The viewer may poll while the booth is still saving.
  JPEGs are checked for the `0xFFD9` end-of-image marker before loading; an
  incomplete file is skipped and retried, so the screen never flashes a
  partial image.
- **No display.** With no Wayland session, SDL silently falls back to its
  `offscreen` driver and reports success — a viewer that appears to run while
  showing nothing. `display.py` detects that and exits with instructions.
  Setting `SDL_VIDEODRIVER` yourself overrides the check, which is how the
  tests run headless.
- **Rotation.** EXIF orientation is applied, so portrait shots are upright.

Over SSH you must point it at the session explicitly:

```bash
export XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0
```

## Triggers

`trigger.py` accepts a shutter trigger from **the GPIO button and the keyboard
at the same time**. The button is GPIO17 by default (see Wiring); `--gpio-pin`
moves it, `--no-gpio` disables it. Keeping the keyboard live means you can
still fire a shot over SSH when the button is unwired or suspect.

Both sources feed a single `select()` call, so the idle keep-alive shares the
same wait and neither source starves the other. gpiozero delivers presses on
its own thread, which cannot be handed to `select()`, so `Trigger` uses the
**self-pipe trick**: the button callback writes one byte to a pipe that
`select()` can watch. No polling, no race.

`Trigger.wait(timeout)` returns an `Event`:

| Event | Meaning |
| --- | --- |
| `TRIGGER` | button pressed, or Enter |
| `TIMEOUT` | deadline reached — the loop sends a keep-alive |
| `QUIT` | keyboard input ended *and* no button is configured |

After each shot the loop calls `Trigger.drain()` to discard presses that piled
up during the countdown, so a guest mashing the button gets one photo rather
than a burst.

### Running headless

Under systemd, stdin is `/dev/null`, which `select()` reports as permanently
readable. Left alone that would either spin or immediately "quit" a perfectly
healthy booth. `Trigger` detects the EOF and **stops watching stdin** instead,
provided a button exists — so a headless service keeps running on the button
alone. With no button configured, EOF still means quit, which is what you want
interactively.

If the GPIO pin cannot be claimed — `gpiozero` missing, the line already held
by another process — `Trigger` logs a warning and falls back to keyboard-only
rather than refusing to start. Note that only one process can hold a GPIO line:
a second `booth.py` will report `GPIO busy` and run keyboard-only.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `No camera detected` | Camera off/asleep, USB Connection not `PC Remote`, or charge-only cable |
| `Could not claim the USB device` (-53) | A gvfs monitor grabbed it — see masking above, or another `gphoto2` process is still running |
| `I/O error` (-7) mid-download | Power. Use a powered hub and camera battery |
| First shot works, later ones fail | Camera power-save timed out — set Power Save Start Time to 30 Min/Off |
| Capture succeeds, no file arrives | `Still Img. Save Dest.` is set to `Camera Only` |
