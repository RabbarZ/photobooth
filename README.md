# Photobooth

A self-contained event photobooth on a Raspberry Pi. A guest presses a button,
a countdown runs on the screen, a tethered Sony A7 III takes the photo, and the
guest downloads it from a gallery served over the Pi's own Wi-Fi.

## What a guest sees

1. Press the button. A countdown appears on the screen.
2. The photo is taken and stays on screen until the next one.
3. Two QR codes sit beside it: the first joins the Pi's Wi-Fi, the second opens
   the gallery at `http://10.42.0.1/`. Android phones open the gallery by
   themselves after joining.
4. In the gallery, tap a photo to save it.

The Wi-Fi has no internet. Its passphrase rotates, and that rotation is the
only thing limiting how long a guest stays connected.

## Layout

```
main.py        the one entry point: Wi-Fi up -> booth loop -> Wi-Fi down
config.yaml    every setting: booth, screen, Wi-Fi, gallery
pbcommon.py    reads config.yaml; shared by everything
photos/        captured photos (created at runtime, not in git)

booth/         camera, button and screen
  booth.py       the loop: countdown, capture, keep-alive, reconnect
  camera.py      Camera (libgphoto2) and MockCamera
  trigger.py     GPIO button + keyboard in one wait
  display.py     fullscreen viewer, run as its own process

wifi/          the Pi's own Wi-Fi network
  accesspoint.py      access point lifecycle (interface, hostapd, dnsmasq)
  rotate_password.py  passphrase rotation and the join QR
  wifiqr.py           QR rendering for both codes
  clientwatch.py      live report of guest devices (--watch-clients)

gallery/       the web page guests download photos from
  portal.py           Flask app: the gallery page
  ingest.py           gallery thumbnails
  templates/ static/  the page and its stylesheet

deploy/        installing onto the Pi
  setup.sh            packages, system config, systemd units
  render_configs.py   config.yaml -> hostapd / dnsmasq / nginx / units
  etc/                templates for the system config files
  systemd/            templates for the five units

tools/         diagnostics run by hand
  check-camera.sh     is the camera reachable?
  capture-ios.sh      what does a phone do when it joins?

docs/
  booth.md            camera settings, wiring, screen, triggers
  guest-wifi.md       access point, connectivity checks, rotation, limits
```

`booth/`, `wifi/` and `gallery/` do not import each other. The booth and the
gallery share only the `photos/` directory; the booth screen and the Wi-Fi
share only the two QR image files under `/var/lib/photobooth`. `main.py` is
the one place that ties the booth and the Wi-Fi together.

The Python files are flat modules, not packages. `main.py` puts `booth/` and
`wifi/` on the import path, and each script that needs `pbcommon` adds the
repo root itself, so every script runs directly from any directory.

## Install on a fresh Pi

1. Flash **Raspberry Pi OS with desktop** (not Lite) with Raspberry Pi Imager.
   The user you create there is the one the booth runs as; any name works.
2. Connect the Pi to the network by **Ethernet** if you can. After setup and a
   reboot, the onboard Wi-Fi becomes the guest access point.
3. Get the code onto the Pi **with git** and run the setup as that user:

   ```bash
   sudo apt install -y git
   git clone <repo-url> ~/photobooth
   cd ~/photobooth
   sudo deploy/setup.sh
   ```

4. Reboot. The desktop logs in automatically and the booth starts.

`setup.sh` installs every package (Wi-Fi, gallery, camera, button, screen),
configures USB for the camera, gives the user the groups it needs, sets desktop
autologin and turns off screen blanking, renders the system config from
`config.yaml`, and installs the systemd units with this checkout's path and
the user filled in. It does not touch the radio, so it is safe over SSH. Run
it again only if you move the repo; for everyday changes see
[Deploy a change](#deploy-a-change). Details of the camera-side changes are in
[docs/booth.md](docs/booth.md).

### Line endings

The code is edited on Windows but only runs on the Pi. `.gitattributes` makes
git store and check out every text file with LF line endings, whatever the
editing machine's `core.autocrlf` setting is, so a `git pull` on the Pi always
gets files Linux can run. Get files onto the Pi only through git.

## Run

`main.py` is the one program. Its flags only choose what to run; every setting
is in [config.yaml](config.yaml).

```bash
sudo python3 main.py                 # Wi-Fi + camera + screen
sudo python3 main.py --mock-camera   # no camera: each shot recycles a photo from photos/
python3 main.py --no-wifi            # booth only, no QR codes; no root needed
sudo python3 main.py --keep-wifi     # leave the Wi-Fi up after the booth exits
sudo python3 main.py --watch-clients # print what each guest device does
python3 main.py --help               # all flags
```

Root is needed because the access point reconfigures `wlan0`. The Pi has one
radio, so **an SSH session over Wi-Fi dies when the access point comes up**:
run it from the console or over Ethernet. The keyboard (Enter) always works as
a trigger alongside the button.

Normally systemd runs it at boot, with `--watch-clients` on, so its log shows
each guest joining, opening the gallery and saving photos (details in
[docs/guest-wifi.md](docs/guest-wifi.md#logs)). To restart it after changing
`config.yaml` or to watch that log:

```bash
sudo systemctl restart photobooth
journalctl -fu photobooth
```

A few parts also run on their own, for testing:

```bash
python3 booth/display.py --windowed         # the screen only, watching photos/
sudo python3 wifi/accesspoint.py start      # the Wi-Fi only
tools/check-camera.sh                       # camera diagnostics
```

## Configuration

Everything is in [config.yaml](config.yaml), in sections:

| Section | What |
| --- | --- |
| `booth` | countdown, button pin and debounce, camera keep-alive |
| `screen` | texts on the screen, QR captions and sizes |
| `access_point`, `password` | the guest Wi-Fi and its rotating passphrase |
| `gallery`, `limits`, `portal` | photo directory, thumbnails, download limits |

After editing, run `sudo deploy/deploy.sh` (see below). For `booth` and
`screen` alone, `sudo systemctl restart photobooth` is enough. Values under
`access_point`, `limits` and `portal` are baked into rendered system files and
need `deploy.sh`.

A scanned Wi-Fi code works for about `valid_passphrases x
rotate_interval_minutes`: 4 x 5 = 20 minutes as shipped.

## Deploy a change

`setup.sh` is the one-time install. After pulling code or editing
`config.yaml`:

```bash
git pull
sudo deploy/deploy.sh
```

It re-renders the system files and units from `config.yaml` and restarts the
three services:

| Unit | What |
| --- | --- |
| `photobooth` | `main.py`: camera, screen, guest Wi-Fi |
| `photobooth-portal` | the gallery page |
| `photobooth-ingest` | gallery thumbnails |

Restarting `photobooth` takes the access point down and back up, so run it over
Ethernet or from the console, not over the guest Wi-Fi.

## Hardware

- Raspberry Pi 5 with an HDMI screen, running Raspberry Pi OS with the labwc
  desktop (the viewer is a Wayland client). The guest Wi-Fi uses the onboard
  radio.
- Sony A7 III on USB, set to `PC Remote`. Other libgphoto2 cameras should work
  but are untested.
- A normally-open push button between GPIO17 and GND.
- Optional: an LIR2032 cell in a holder on the Pi 5's BAT connector. See
  [Clock](#clock).

## Clock

The screen and the gallery put the photo with the newest file time first, so
the clock must not jump backwards mid-event. Two things keep it right:

- **Internet on `wlan1`**, the USB Wi-Fi dongle (home Wi-Fi or a phone
  hotspot). The clock is set within seconds of the Pi getting online.
  `wlan0` cannot help: it is the guest access point and has no internet.
- **The RTC battery**, for a reboot or power cut at an event with no
  reception. While the Pi has no power, the Pi 5's real-time clock keeps
  time only on this battery.

The battery is an **LIR2032**: a rechargeable Li-ion coin cell. The firmware
ships with RTC charging off, so `setup.sh` adds this to
`/boot/firmware/config.txt` (active after a reboot):

```
dtparam=rtc_bbat_vchg=4200000
```

4.2 V is the LIR2032's full-charge voltage. The official Raspberry Pi battery
is a different cell (ML-2020, lithium manganese) that needs `3000000` instead.
Never fit a non-rechargeable CR2032: the Pi would try to charge it.

The cell is only charged while it is connected and the Pi is on. Unplugging it
between events is fine: it loses very little charge in storage, and `wlan1`
sets the clock at the next boot anyway. Check charging with
`cat /sys/class/rtc/rtc0/charging_voltage` (`4200000` when on).

## More

- [docs/booth.md](docs/booth.md): camera settings, power, wiring, the screen,
  triggers, troubleshooting.
- [docs/guest-wifi.md](docs/guest-wifi.md): how the access point, connectivity
  checks, passphrase rotation and download limits work, and why.
