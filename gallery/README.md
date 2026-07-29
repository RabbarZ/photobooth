# Photobooth gallery — Pi-hosted Wi-Fi + photo gallery

Guests scan one QR to join the Pi's Wi-Fi and a second to open the gallery.
No sign-in, no accounts. The network has no internet: iOS and the rest are told
it is fine so they do not nag, while Android is told it is a captive portal so
the gallery opens by itself.

Target: **Raspberry Pi 3 Model B v1.2** (BCM2837, 1 GB RAM, onboard BCM43438 —
2.4 GHz only, 802.11n, single antenna). Every design choice below is driven by
that hardware; see [Why it is built this way](#why-it-is-built-this-way).

## Install, then run

Install once (safe over SSH — it never touches the radio):

```bash
sudo ./setup.sh
```

Then everything runs from **one entry point** at the repo root, which brings the
access point up, runs the booth, and takes the AP down again on exit:

```bash
sudo python3 ../main.py                     # AP + camera + screen
sudo python3 ../main.py --mock-camera       # no camera: recycle photos/
sudo python3 ../main.py --no-wifi           # booth only, leave the radio alone
sudo python3 ../main.py --keep-wifi         # leave the AP up after the booth exits
```

The AP can also be driven on its own:

```bash
sudo python3 accesspoint.py start
sudo python3 accesspoint.py stop --release  # --release restores normal Wi-Fi
python3 accesspoint.py status
```

> **The Pi has one radio.** `iw list` reports its only AP-capable combination as
> `#{managed} <= 1, #{AP} <= 1, #channels <= 1` — client and AP together only on
> a single shared channel. There is no upstream to share here, so `wlan0`
> becomes AP-only and **any SSH session over Wi-Fi dies when the AP comes up**.
> Run it from the console, or manage the Pi over Ethernet.

### Session limits at launch

The guest lockout timers live in `config.yaml`, but `main.py` can override them
for a single run without editing the file:

```bash
sudo python3 ../main.py --hard-limit 15 --soft-limit 2 --bulk-grace 5
sudo python3 ../main.py --rotate-interval 10
```

Overrides are written to `/run/photobooth/overrides.yaml` and merged over
`config.yaml` by every daemon. That path is tmpfs, so a timer you passed for one
evening disappears at reboot instead of quietly becoming the permanent default.
The merge is per-key, so setting `--hard-limit` alone leaves the others intact.

## Configuration

Everything lives in [`config.yaml`](config.yaml) — SSID, channel, timers, limits,
photo directory. The hostapd/dnsmasq/nginx files are **generated artefacts**:

```bash
python3 render_configs.py --out-dir /tmp/review   # inspect first
sudo python3 render_configs.py --install          # then apply
```

Changes under `access_point` or `limits` need a re-render (`setup.sh` does it).
Everything else is read at runtime — `systemctl restart photobooth-*` is enough.

## Services

| Unit | Runs as | Job |
| --- | --- | --- |
| `photobooth-portal` | pi | gunicorn + Flask: gallery, heartbeat, grace windows |
| `photobooth-ingest` | pi | thumbnails + prebuilt zip, `Nice=10`, IO idle |
| `photobooth-sessions` | root | enforces both limits via `hostapd_cli` |
| `photobooth-rotate` | root | rotates the passphrase, rewrites the QR |
| `photobooth-ap` | root | owns the interface address; hostapd/dnsmasq `BindsTo` it |

Portal and ingest are **enabled at boot** — they never touch the radio, and the
gallery stays reachable over whatever network the Pi is on.

The AP stack (`photobooth-ap`, `hostapd`, `dnsmasq`, `photobooth-sessions`,
`photobooth-rotate`) is deliberately **not enabled at boot**. `main.py` owns the
guest Wi-Fi: it comes up when the booth starts and goes down when it exits. If
these were enabled, the SSID would appear at boot with no booth behind it, and
`--no-wifi` would not mean anything.

`photobooth-ap` exists because the AP used to be configured imperatively by a
shell script, so a reboot brought hostapd back **without** the interface
address — the SSID broadcast, WPA handshakes succeeded, and then every client
silently failed to get a DHCP lease and dropped off. hostapd and dnsmasq are
now `BindsTo=photobooth-ap`, which makes that half-up state impossible whenever
the AP is started — by `main.py`, by `accesspoint.py start`, or manually.

## Two QR codes; a captive portal on Android only

Guests take two steps, each one a code on the booth screen:

1. **Join the Wi-Fi** — a `WIFI:` payload carrying SSID and the current
   rotated passphrase. Written by `rotate_password.py` on every rotation.
2. **Open the gallery** — a plain `http://10.42.0.1/` URL. Written once at AP
   bring-up by `accesspoint.write_portal_qr()`; it depends only on the
   interface address, so unlike the join QR it never rotates.

Scanning a URL with the Camera app opens it in the real browser, which is the
point: the gallery arrives in Safari or Chrome, where downloading and saving
behave normally.

**The general-purpose captive portal is gone** — what remains is one narrow,
Android-only case built from the connectivity checks below, not from the old
machinery. Removing the original meant taking out four separate mechanisms,
none of which should come back piecemeal:

| Removed | What it did |
| --- | --- |
| `address=/#/10.42.0.1` | resolved *every* name to the Pi, so any URL landed on the portal |
| `dhcp-option-force=114` | RFC 8910 URI — the primary signal that opens the sheet on iOS 14+ |
| `/captive-api` | RFC 8908 JSON endpoint answering `{"captive": true}` |
| nginx `default_server` 302 | answered the OS probes so no device concluded it had internet |

The last row is worth reading twice: the probes are answered again now, but
per platform — success for iOS and the rest, a redirect for Android alone. See
below. Restoring a blanket 302 would bring the sign-in sheet back on iPhones.

Also gone: `welcome.html` (the stripped-down page the iOS Captive Network
Assistant got instead of the thumbnail-heavy gallery), the `is_captive_browser()`
User-Agent sniffing, the `PROBE_PATHS` routes, and the `local=/#/` AAAA
workaround that existed only so iOS would reach a captive verdict rather than
deciding the resolver was broken.

**Correction to what this section first said.** It claimed a phone would
correctly conclude it had no internet and that this was fine. In practice iOS
does not just note it quietly — it puts up *"Stay connected / Use mobile
data"* over whatever the guest is doing, and guests answer it wrongly and drop
off the Wi-Fi mid-download. So the probes are now answered after all, in the
opposite direction from the old portal.

### Connectivity checks, answered per platform

Every OS fetches a known URL right after joining and decides from the reply
whether the network works. Those hostnames resolve to the Pi again — but by
name, not by wildcard — and nginx answers each family differently:

| OS | Probe | Reply | Result |
| --- | --- | --- | --- |
| iOS / macOS | `captive.apple.com/hotspot-detect.html` | `200`, `<HTML>…<BODY>Success</BODY></HTML>` | no prompt |
| Windows | `www.msftconnecttest.com/connecttest.txt` | `200`, `Microsoft Connect Test` | no prompt |
| Firefox | `detectportal.mozilla.org/success.txt` | `200`, `success` | no prompt |
| Ubuntu | `connectivity-check.ubuntu.com/canonical.html` | `200`, `NetworkManager is online` | no prompt |
| **Android** | `connectivitycheck.gstatic.com/generate_204` | **`302` → the gallery** | **captive portal** |

The bodies are exact, not stylistic: Apple checks for that precise document and
Windows compares byte for byte. Served straight from nginx, never proxied —
phones re-probe every few tens of seconds and that has no business waking
gunicorn.

**Android is answered the opposite way on purpose.** It trusts the network only
on exactly a `204`; anything else makes it declare a captive portal and offer
*"Sign in to network"*, which opens the target in its own login browser — so
Android guests get the gallery pushed at them, while iOS guests are left alone
and use the second QR.

The two are inseparable from the hostnames: the same success reply that quiets
iOS would suppress Android's portal, and the redirect that gives Android its
portal would make iOS nag. That is why the probe names are split across two
nginx server blocks rather than sharing one.

Two things are load-bearing and easy to break:

- **`www.google.com` is deliberately not in dnsmasq.** Android runs an HTTPS
  probe against it to tell a portal from a broken network. It has to *fail*:
  failing HTTPS plus a redirecting HTTP probe is exactly the combination read
  as "captive portal". Resolving it here would undo this.
- **Do not re-add DHCP option 114** to give Android a tidier signal. Option 114
  goes to every client, so iOS would pick it up too and the sign-in sheet would
  be back on iPhones.

Only the probe hostnames are hijacked, and Apple's are pinned to
`captive.apple.com` and `www.apple.com` specifically. A bare
`address=/apple.com/` would catch push, iCloud and the App Store too, so
instead of failing cleanly those would get a stray `204` and misbehave.

> **The trade: while a guest is on this Wi-Fi they have no internet and their
> phone no longer warns them.** Nothing else resolves, so other apps and sites
> simply fail rather than falling back to mobile data. That is the price of
> silencing the prompt, and it is why the session limits matter — a guest is
> kicked back to normal connectivity within `hard_limit_minutes`.

`dhcp-option:router` is deliberately **not** sent either. Advertising a default
gateway that routes nowhere makes a phone treat this as its internet path and
stall on every lookup; without one the OS keeps using mobile data for
everything else while `10.42.0.1` stays reachable.

## Sessions: two timers, no blocklist

| Limit | Default | Trigger |
| --- | --- | --- |
| Hard | 30 min | wall clock since first association |
| Soft | 5 min | no gallery request (page load or heartbeat) |
| Bulk grace | 10 min | suppresses the soft kick during a download |

Kicks are `hostapd_cli deauthenticate <MAC>`. **There is no MAC blocklist** —
deauth alone would let a device reassociate a second later, so the real
gatekeeper is password rotation: a kicked guest cannot rejoin until they rescan
the QR. A guest who deserves another turn simply scans again.

Sessions are keyed by MAC, resolved from the client IP via the dnsmasq lease
file, falling back to `ip neigh` (which catches a client using a static
address). Association alone starts the hard clock, so an idle squatter that
never opens the gallery is still visible and still gets kicked.

State is SQLite on `/run` (tmpfs). It is rewritten every few seconds and none of
it needs to survive a reboot, so keeping it off the SD card avoids flash wear.

### Why a server-side grace window

The page heartbeats every 30 s, which handles the ordinary idle case. But a
~500 MB download over this radio takes **minutes** and routinely outlives the
page that started it — the phone locks, the browser is backgrounded, heartbeats
stop. Relying on the heartbeat alone would kick guests mid-download. So pressing
*Download all* registers a grace window server-side **before** navigating, and
the session manager honours it even with no heartbeats at all.

## Password rotation and the key ring

A new passphrase is minted every `rotate_interval_minutes` (default 30) and the
QR PNG is rewritten in place, atomically, so the booth display never reads a
partial file.

The passphrase lives in a **`wpa_psk_file`** rather than inline as
`wpa_passphrase`. That file accepts many wildcard-MAC lines and hostapd will
authorise a station against **any** of them, which is what makes overlap
possible:

```
00:00:00:00:00:00 <newest -- this is what the QR encodes>
00:00:00:00:00:00 <previous>
00:00:00:00:00:00 <one before that>
```

`valid_passphrases` (default 3) sets how many are kept. A scanned code
therefore keeps working for roughly `valid_passphrases x rotate_interval` --
90 minutes at the defaults.

**Why overlap matters.** With a single key, a guest who joins moments before a
rotation is stranded: the passphrase their phone cached no longer exists, so any
reconnect -- power save, roaming, walking behind a wall -- fails until they
rescan, and they have no way of knowing that is what happened. Overlap makes
unlucky timing a non-event. Set `valid_passphrases: 1` to restore the old
single-key behaviour.

The ring is read back out of the key file on each rotation, not held in memory,
so restarting `photobooth-rotate` does not invalidate every guest's cached
passphrase.

> **Rotation is not an eviction mechanism.** It was originally described here as
> the thing that stops a kicked device rejoining. That was wrong, and testing
> disproved it: a deauthenticated phone reassociated **one second later** on its
> cached key. Rotation only ever affects devices that need to re-authenticate
> *after* their key ages out of the ring, which is unpredictable and hits
> innocent guests as readily as departing ones. Airtime is reclaimed by the
> session manager's deauth; treat the passphrase as a way to hand out access,
> not to take it away.

> **`RELOAD_WPA_PSK` does not preserve connections on hostapd 2.10.** The
> control command returns `OK` and rereads the key file, but the daemon
> deauthenticates stations so they re-key. Earlier revisions of this README
> claimed connected clients kept their sessions -- they do not. This is the
> reason to keep `rotate_interval_minutes` generously long: every rotation
> briefly interrupts everyone, so a short interval (say `--rotate-interval 0.5`)
> produces constant disconnects.

## Bulk download

The zip is **pre-generated on disk** by `ingest.py` whenever photos change, and
served by nginx as a static file. Not built per request, and never in memory:

- `ZIP_STORED` (no deflate) — JPEGs do not compress, so deflate would burn
  minutes of CPU to save ~1%.
- Written to `.tmp` then `os.replace()`, so guests downloading the old zip are
  unaffected by a rebuild.
- Rebuilds are debounced (5 s of quiet), so a burst of shots causes one rebuild
  rather than one per photo.
- On 1 GB with ~180 MB actually free, building a 500 MB zip in RAM is not
  merely wasteful — it is impossible.

Thumbnails are likewise built on ingest, never on request, using PIL's
`draft()` so libjpeg downscales *during* decode (DCT scaling) instead of
unpacking 24 MP first. Measured on this Pi: ~1.0 s with `draft()` versus ~5 s
without — per photo. Resizing on request with several guests would be hopeless.

## Limits

| Setting | Default | Rationale |
| --- | --- | --- |
| `max_num_sta` (hostapd) | 10 | chip collapses past a handful of *active* clients |
| `per_device_downloads` | 2 | 3+ makes every transfer slower without finishing sooner |
| `global_downloads` | 6 | keeps the radio from being oversubscribed |
| `portal_rate_per_second` | 10 (burst 20) | absorbs a page load, blocks a hammering client |
| `max_photos_per_user` | 50 | app-layer cap on the listing |

`max_photos_per_user` caps how many photos the gallery *lists* (newest first).
nginx serves the files directly, so the app cannot meter per-file downloads —
capping the listing is the enforceable equivalent at this layer.

## Tuning guidance

**Channel is the single biggest lever.** Only 1, 6 and 11 do not overlap.
Survey the venue and pick the quietest:

```bash
sudo iw dev wlan0 scan | grep -E 'freq:|SSID:' | paste - -   # before activating
```

`render_configs.py` warns if you choose anything else. 40 MHz width is
deliberately *not* enabled: on 2.4 GHz it swallows two of the three usable
channels and, on a single-antenna chip in a crowded room, loses more to retries
than the width wins back.

**RAM is the binding constraint.** This box currently shows ~905 MB total with
only ~180 MB free, because it is running the full labwc desktop — the spec
assumed Pi OS **Lite**. Booting to Lite (or `systemctl set-default
multi-user.target`) frees roughly 300 MB and is the cheapest performance win
available. Do that before raising `workers` above 2.

**If downloads crawl**, lower `global_downloads` before anything else.
Six guests each getting a slow trickle is worse than three getting a usable
rate; the radio is the bottleneck, not the CPU.

**If the portal is slow to open**, check `journalctl -u photobooth-ingest` — a
zip rebuild competing for the SD card is the usual cause. It runs at `Nice=10`
with idle IO priority, but the card is still shared.

## Gotchas found while bringing this up

Three things bit during the first activation. All are fixed in the scripts, but
they are the kind that reappear if the units are edited.

**`hostapd.service` has a hard-coded config path.** The packaged unit carries
`ConditionFileNotEmpty=/etc/hostapd/hostapd.conf`, evaluated *before*
`ExecStart`. Setting `DAEMON_CONF` in `/etc/default/hostapd` is not enough: with
a differently-named config the condition fails and systemd **skips** the unit,
reporting `skipped, unmet condition check` rather than an error — so
`systemctl start hostapd` appears to do nothing. `setup.sh` installs a drop-in
retargeting the condition.

**`ProtectSystem=strict` breaks `hostapd_cli`.** It mounts the entire hierarchy
read-only except `/dev`, `/proc`, `/sys` — including `/tmp`, where `wpa_ctrl`
creates the client's reply socket. Every call failed with
`wpa_ctrl_open: Read-only file system`, **silently**: `list_sta` returned empty,
so no session limit was ever enforced and rotation could not reload. Both
privileged units therefore list `/tmp` in `ReadWritePaths`.

> `PrivateTmp=yes` is *not* the fix, though it looks like one. It clears the
> permission error, but a private `/tmp` is invisible to hostapd, which then
> cannot deliver its reply and every command times out instead. It has to be
> the real, shared `/tmp`.

**A reboot left the AP half-up.** `activate.sh` configured the interface
imperatively and nothing reinstated it at boot, so hostapd came back alone:
SSID visible, passphrase accepted, no DHCP server, clients cycling on and off.
This is why `photobooth-ap.service` now owns the address and the other units
are bound to it.

## Diagnostics

```bash
# Gallery reachable? (works before activation, over your normal network)
curl -s -H 'Host: 10.42.0.1' http://127.0.0.1/ | head -20
curl -s -H 'Host: 10.42.0.1' http://127.0.0.1/api/status

# The two QRs actually encode what they should
python3 wifiqr.py --show                      # WIFI: join payload
python3 wifiqr.py --url http://10.42.0.1/ --show
ls -l /var/lib/photobooth/wifi-qr.png /var/lib/photobooth/portal-qr.png

# No captive machinery left: neither line should match
grep -E 'address=/#/|option-force=114' /etc/dnsmasq.d/photobooth.conf

# iOS and friends must see success — this is what stops the "Stay connected?" prompt
curl -s  -H 'Host: captive.apple.com' http://127.0.0.1/hotspot-detect.html
curl -s  -H 'Host: www.msftconnecttest.com' http://127.0.0.1/connecttest.txt

# Android must see a redirect, NEVER a 204 — that is what opens its portal
curl -sI -H 'Host: connectivitycheck.gstatic.com' http://127.0.0.1/generate_204 | head -2

# Live
hostapd_cli -i wlan0 list_sta
journalctl -fu photobooth-sessions -u photobooth-rotate -u hostapd
cat /var/lib/photobooth/current-password.txt
```

## Files

```
../main.py             THE entry point: AP up -> booth loop -> AP down
config.yaml            all tunables — the single source of truth
accesspoint.py         AP lifecycle: interface, hostapd, dnsmasq (was 3 .sh files)
wifiqr.py              WIFI: join payload + URL QR rendering (both codes)
pbcommon.py            config (+ runtime overrides), IP->MAC, state, hostapd_cli
portal.py              Flask: gallery, heartbeat, grace registration
session_manager.py     the two timers -> deauth
rotate_password.py     passphrase rotation, delegates the QR to wifiqr.py
ingest.py              thumbnails + prebuilt store-only zip
render_configs.py      config.yaml -> system files
etc/*.tmpl             hostapd, dnsmasq, nginx, NetworkManager templates
systemd/*.service      five units (incl. photobooth-ap, which owns the address)
setup.sh               non-disruptive install
```

`accesspoint.py` is the single implementation of AP bring-up, used both by
`main.py` and by `photobooth-ap.service` at boot. The old `activate.sh`,
`deactivate.sh` and `ap-interface.sh` are gone.

## Why it is built this way

| Constraint | Consequence |
| --- | --- |
| 1 GB RAM, ~180 MB free | zip on disk, never in memory; 2 gunicorn workers |
| Single-antenna 2.4 GHz n | `max_num_sta=10`, download caps, 20 MHz only |
| One radio | AP-only; two-stage install; SSH-safety check |
| SD-card IO | thumbnails/zip pre-generated; state on tmpfs; no query logs |
| Weak CPU | store-only zip; `draft()` decode; nginx `sendfile` |
