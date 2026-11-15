# Guest Wi-Fi and gallery

The Pi runs its own Wi-Fi network and serves a photo gallery on it. Guests scan
one QR to join the Wi-Fi and a second to open the gallery. No sign-in, no
accounts, no internet. The network code is in [`wifi/`](../wifi/), the web page
in [`gallery/`](../gallery/), and the system config templates for both in
[`deploy/`](../deploy/). The camera side is covered in [booth.md](booth.md).

The two are separate programs that meet in the system config: dnsmasq and nginx
decide what a phone sees when it joins, and nginx is also what serves the
gallery. That is why they share this document.

The host is a Raspberry Pi 5, using its onboard radio (CYW43455, one antenna).
The access point runs on 2.4 GHz, 20 MHz, 802.11n only.

## Install, then run

Install once (safe over SSH — it never touches the radio):

```bash
sudo deploy/setup.sh
```

Then everything runs from one entry point at the repo root, which brings the
access point up, runs the booth, and takes the access point down on exit:

```bash
sudo python3 main.py                     # AP + camera + screen
sudo python3 main.py --mock-camera       # no camera: recycle photos/
python3 main.py --no-wifi                # booth only, leave the radio alone
sudo python3 main.py --keep-wifi         # leave the AP up after the booth exits
sudo python3 main.py --release-wifi      # on exit, hand wlan0 back to NetworkManager
sudo python3 main.py --watch-clients     # live report of what guest devices do
```

`setup.sh` also enables `photobooth-booth.service`, which runs `main.py` at
boot. It does not start it, so your SSH session survives the install.

`setup.sh` also renders a NetworkManager file that releases `wlan0`. It takes
effect at the next reboot: from then on the Pi has no normal Wi-Fi
connection, so manage it over Ethernet. `accesspoint.py stop --release`
removes the file and gives `wlan0` back.

The access point can also be driven on its own:

```bash
sudo python3 wifi/accesspoint.py start
sudo python3 wifi/accesspoint.py stop --release  # --release restores normal Wi-Fi
python3 wifi/accesspoint.py status
```

> **The Pi has one radio.** There is no upstream to share, so `wlan0` becomes
> AP-only and **any SSH session over Wi-Fi dies when the AP comes up**. Run it
> from the console, or manage the Pi over Ethernet.

### Guest access

How long a guest gets is set by the passphrase ring: `rotate_interval_minutes`
and `valid_passphrases` in the `password` section of `config.yaml`. Change
them there and restart the booth (`sudo systemctl restart photobooth-booth`),
which restarts the rotator when it brings the Wi-Fi up.

## Configuration

Everything lives in [`config.yaml`](../config.yaml) at the repo root — SSID,
channel, timers, limits, photo directory. The hostapd, dnsmasq, nginx and
NetworkManager files and the five systemd units are **generated artefacts**:

```bash
python3 deploy/render_configs.py --out-dir /tmp/review   # inspect first
sudo python3 deploy/render_configs.py --install          # then apply
```

The units are rendered because they need the absolute path of the checkout.
Changes under `access_point`, `limits` or `portal` need a re-render (`setup.sh`
does it). Everything else is read when a daemon starts —
`systemctl restart photobooth-*` is enough.

`gallery.photo_dir` may be relative, in which case it is relative to the repo
root. The shipped value `photos` is the same directory the booth saves into by
default.

## Services

| Unit | Runs as | Job | At boot |
| --- | --- | --- | --- |
| `photobooth-booth` | root | `main.py`: guest Wi-Fi, camera, screen | enabled |
| `photobooth-portal` | booth user | gunicorn + Flask: the gallery page | enabled |
| `photobooth-ingest` | booth user | gallery thumbnails, `Nice=10`, IO idle | enabled |
| `photobooth-rotate` | root | rotates the passphrase, rewrites the join QR | started by the AP |
| `photobooth-ap` | root | gives the interface its address (`accesspoint.py up`) | not enabled |

Portal and ingest never touch the radio, so the gallery stays reachable over
whatever network the Pi is on.

The AP stack (`hostapd`, `dnsmasq`, `photobooth-rotate`) is deliberately not
enabled at boot. `main.py` owns the guest Wi-Fi: `AccessPoint.start()` gives
the interface its address, starts hostapd and dnsmasq in that order, restarts
the rotator and writes the gallery QR; `stop()` reverses it. If the stack were
enabled by itself, the SSID would appear at boot with no booth behind it, and
`--no-wifi` would not mean anything.

`photobooth-ap` is for running the interface address under systemd without the
booth. `photobooth-booth` declares `Conflicts=photobooth-ap`, so the interface
never has two owners.

The order matters: hostapd running without the interface address gives an SSID
that accepts the passphrase and then hands out no DHCP lease, so every client
silently drops off. The order is enforced by `accesspoint.py`, not by systemd,
so starting hostapd or dnsmasq by hand with `systemctl` can produce that
half-up state. Use `accesspoint.py start`.

## Two QR codes, and a captive portal on Android only

Guests take two steps, each one a code on the booth screen:

1. **Join the Wi-Fi** — a `WIFI:` payload carrying the SSID and the current
   passphrase. Written by `rotate_password.py` on every rotation.
2. **Open the gallery** — a plain `http://10.42.0.1/` URL. Written once at AP
   bring-up by `AccessPoint.write_portal_qr()`; it depends only on the
   interface address, so unlike the join QR it never rotates.

Scanning a URL with the Camera app opens it in the real browser, where saving
a photo behaves normally.

Android phones skip the second step: after joining they offer *"Sign in to
network"* and open the gallery by themselves. iPhones and everything else are
deliberately left alone. Making iOS open the gallery reliably is not easy to
get right, and getting it half right means a sign-in sheet that gets in the
way, so iPhones use the second QR instead.

### Connectivity checks, answered per platform

Every OS fetches a known URL right after joining and decides from the reply
whether the network works. dnsmasq resolves those probe hostnames to the Pi —
by name, not by wildcard — and nginx answers each family differently:

| OS | Probe | Reply | Result |
| --- | --- | --- | --- |
| iOS / macOS | `captive.apple.com/hotspot-detect.html` | `200`, `<HTML>…<BODY>Success</BODY></HTML>` | no prompt |
| Windows | `www.msftconnecttest.com/connecttest.txt` | `200`, `Microsoft Connect Test` | no prompt |
| Firefox | `detectportal.mozilla.org/success.txt` | `200`, `success` | no prompt |
| Ubuntu | `connectivity-check.ubuntu.com/canonical.html` | `200`, `NetworkManager is online` | no prompt |
| **Android** | `connectivitycheck.gstatic.com/generate_204` and friends | **`302` → the gallery** | **captive portal** |

The bodies are exact, not stylistic: Apple checks for that precise document and
Windows compares byte for byte. They are served straight from nginx, never
proxied — phones re-probe every few tens of seconds and that has no business
waking gunicorn.

**Why iOS is told everything is fine.** With no reply, iOS puts up *"Stay
connected / Use mobile data"* over whatever the guest is doing, and guests
answer it wrongly and drop off the Wi-Fi mid-download.

**Why Android is answered the opposite way.** It trusts the network only on
exactly a `204`; anything else makes it declare a captive portal, which opens
the target in its own login browser. Three things produce that verdict:

- **The redirect.** All the Android probe hostnames get a `302` to the
  gallery. That includes `www.google.com` and `play.googleapis.com`, which
  Android uses as fallback HTTP probes (`http://www.google.com/gen_204`).
- **A failing HTTPS probe.** Android also probes
  `https://www.google.com/generate_204`; if that succeeds, it decides it has
  internet and shows no portal. It cannot succeed: nothing listens on port 443,
  and the Pi could not present a certificate for google.com anyway. Do not add
  an HTTPS server.
- **DHCP option 114, to Android only.** On Android 11+ the RFC 8910 portal URL
  is the primary signal and the HTTP probe only a fallback. dnsmasq tags leases
  whose vendor class contains `android-dhcp` and sends option 114 to that tag
  alone, pointing at `/captive-api`. The Flask app answers that with RFC 8908
  JSON (`"captive": true`, media type `application/captive+json`).

The two behaviours are split across two nginx server blocks because they are
inseparable from the hostnames: the success reply that quiets iOS would
suppress Android's portal, and the redirect that gives Android its portal
would make iOS nag.

Easy to break:

- **Never send option 114 untagged.** iOS acts on it too, and iPhones would get
  a sign-in sheet. `main.py` warns at start-up if the installed dnsmasq config
  does this.
- **Never resolve every name to the Pi** (`address=/#/`), for the same reason;
  `main.py` warns about that too. Only the probe names are hijacked, and
  Apple's are pinned to `captive.apple.com` and `www.apple.com` specifically. A
  bare `address=/apple.com/` would catch push, iCloud and the App Store too.
- **No default gateway is advertised** (`option:router` is not sent). A gateway
  that routes nowhere makes a phone treat this as its internet path and stall
  on every lookup.

> **The trade: while a guest is on this Wi-Fi they have no internet, and an
> iPhone does not warn them.** Nothing else resolves, so other apps simply
> fail. That is the price of silencing the prompt, and it is why the passphrase
> ring matters — a guest is returned to normal connectivity once the code they
> scanned ages out.

Probe requests are logged to `/var/log/nginx/photobooth-probes.log`, the only
access log nginx keeps here. It answers "did this phone probe, and what did we
tell it?".

## Sessions: none, by design

There are no session limits. Deauthenticating a phone does not keep it out: it
reassociates on its cached passphrase in about a second (measured, not
assumed).

### What bounds a session

The passphrase ring, and only that. Every rotation deauthenticates all stations
so they re-key, and a guest can return only while the code they scanned is
still in the ring:

    session length ≈ valid_passphrases × rotate_interval_minutes

As shipped (4 × 5 min) that is a cap of 20 minutes, measured from when the code
was *minted* rather than when it was scanned — so 15–20 minutes in practice. To
change how long guests get, change those two settings. Nothing else caps it.

The trade-offs this accepts, deliberately:

- **No idle/active distinction.** The cap is pure wall clock.
- **Session length is welded to reconnect forgiveness.** The same product also
  controls how long a phone can be away — behind a wall, in power save — and
  still rejoin on a cached key.
- **The cap is emergent, not declared.** Raise `valid_passphrases` and sessions
  silently get longer.
- **No per-device eviction at all.** Heavy downloading is metered by nginx
  (`per_device_downloads`, `global_downloads`), not by kicking anyone.

## Password rotation and the key ring

A new passphrase is minted every `rotate_interval_minutes` (5 as shipped) and
the QR PNG is rewritten in place, atomically, so the booth display never reads
a partial file. Passphrases are `length` characters (12) from an alphabet with
the ambiguous glyphs (0/O/1/l/I) removed, in case someone types one by hand.

The passphrase lives in a **`wpa_psk_file`** rather than inline as
`wpa_passphrase`. That file accepts many wildcard-MAC lines and hostapd will
authorise a station against **any** of them, which is what makes overlap
possible:

```
00:00:00:00:00:00 <newest -- this is what the QR encodes>
00:00:00:00:00:00 <previous>
00:00:00:00:00:00 <one before that>
```

`valid_passphrases` (4 as shipped) sets how many are kept.

**Why overlap matters.** With a single key, a guest who joins moments before a
rotation is stranded: the passphrase their phone cached no longer exists, so
any reconnect fails until they rescan, and they have no way of knowing that is
what happened. Set `valid_passphrases: 1` to get that single-key behaviour.

The ring is read back out of the key file on each rotation, not held in memory,
so restarting `photobooth-rotate` does not invalidate every guest's cached
passphrase.

The current passphrase is also written to
`/var/lib/photobooth/current-password.txt`, readable by root only. `main.py`
logs it at start-up.

> **Rotation is not an eviction mechanism.** It only affects devices that need
> to re-authenticate after their key has aged out of the ring. Treat the
> passphrase as a way to hand out access, not to take it away.

> **Every rotation briefly interrupts everyone.** On hostapd 2.10,
> `RELOAD_WPA_PSK` returns `OK` and rereads the key file, but the daemon
> deauthenticates stations so they re-key. A very short interval (say
> `rotate_interval_minutes: 0.5`) produces constant disconnects.

## The gallery

`portal.py` renders one page: a grid of thumbnails, newest first, where tapping
a photo saves it. nginx serves the photos, thumbnails and stylesheet directly;
the Flask app only produces the listing. Any unknown URL redirects to the
gallery.

The page lists at most `max_photos_per_user` photos (50). nginx serves the
files directly, so the app cannot meter per-file downloads — capping the
listing is the enforceable equivalent at this layer.

`GET /api/status` is a small diagnostic view: the caller's IP and MAC, the
photo count and the number of associated stations.

### Thumbnails

`ingest.py` checks the photo directory every `ingest_poll_seconds` (3) and
builds a thumbnail for each new photo, in the background, never during a
request. It uses PIL's `draft()`, so libjpeg downscales *during* decode
instead of unpacking 24 MP first, which is several times faster.

To collect the evening's photos, copy the photo directory off the Pi.

## Logs

The booth service runs `main.py --watch-clients`, so one log tells the story
of each guest:

```bash
journalctl -fu photobooth-booth
```

```
  20:14:03 [+] 3a:1f:..:9c associated
  20:14:04 [ip] 3a:1f:..:9c -> 10.42.0.73 (Pixel-7)
  20:14:06 [web] Android opened the gallery -> 200
  20:14:31 [dl] Android 10.42.0.73 saved 20251114-201102-417.JPG
```

When the booth stops it prints a summary: every device that got an address,
how many photos each saved, and which device types opened the gallery.

The raw sources, if you need more detail:

| What | Where |
| --- | --- |
| Joining and leaving the Wi-Fi | `journalctl -u hostapd` |
| Getting an address (DHCP) | `journalctl -u dnsmasq` |
| Opening the gallery page | `journalctl -u photobooth-portal` |
| Saving a photo | `/var/log/nginx/photobooth-downloads.log` |
| Connectivity checks, Android's `/captive-api` | `/var/log/nginx/photobooth-probes.log` |

DNS queries are not logged, so which other sites guests tried is not
recorded.

## Limits

| Setting | Shipped | Rationale |
| --- | --- | --- |
| `max_num_sta` (hostapd) | 10 | the radio degrades past a handful of *active* clients |
| `per_device_downloads` | 2 | 3+ makes every transfer slower without finishing sooner |
| `global_downloads` | 6 | keeps the radio from being oversubscribed |
| `portal_rate_per_second` | 10 (burst 20) | absorbs a page load, blocks a hammering client |
| `max_photos_per_user` | 50 | app-layer cap on the listing |

Stations older than 802.11n are refused (`require_ht=1`, no 802.11b rates):
one legacy client slows the whole network down.

## Tuning guidance

**Channel is the single biggest lever.** Only 1, 6 and 11 do not overlap.
Survey the venue and pick the quietest:

```bash
sudo iw dev wlan0 scan | grep -E 'freq:|SSID:' | paste - -   # before activating
```

`render_configs.py` warns if you choose anything else. 40 MHz width is
deliberately not enabled: on 2.4 GHz it swallows two of the three usable
channels and loses more to retries than the width wins back.

**If downloads crawl**, lower `global_downloads` before anything else. Six
guests each getting a slow trickle is worse than three getting a usable rate;
the radio is the bottleneck, not the CPU.

**If new photos appear without a thumbnail**, check
`journalctl -u photobooth-ingest` for decode errors.

## Gotchas found while bringing this up

**`hostapd.service` has a hard-coded config path.** The packaged unit carries
`ConditionFileNotEmpty=/etc/hostapd/hostapd.conf`, evaluated *before*
`ExecStart`. Setting `DAEMON_CONF` in `/etc/default/hostapd` is not enough:
with a differently-named config the condition fails and systemd **skips** the
unit, so `systemctl start hostapd` appears to do nothing. `setup.sh` installs
a drop-in retargeting the condition at `/etc/hostapd/photobooth.conf`.

**`ProtectSystem=strict` breaks `hostapd_cli`.** It mounts the entire hierarchy
read-only, including `/tmp`, where `wpa_ctrl` creates the client's reply
socket. Every call failed silently with `wpa_ctrl_open: Read-only file system`.
`photobooth-rotate` therefore lists `/tmp` in `ReadWritePaths`.

> `PrivateTmp=yes` is *not* the fix, though it looks like one. A private
> `/tmp` is invisible to hostapd, which then cannot deliver its reply and every
> command times out. It has to be the real, shared `/tmp`.

**nginx cannot read a home directory by default.** Raspberry Pi OS ships home
directories as `0700`, so every `/photos/` and `/static/` request returns 403
while thumbnails (under `/var/lib/photobooth`) work. `setup.sh`
adds traverse-only permission (`o+x`) on each directory leading to the photo
directory.

## Diagnostics

Run these from the repo root.

```bash
# Gallery reachable? (works with the AP down, over your normal network)
curl -s -H 'Host: 10.42.0.1' http://127.0.0.1/ | head -20
curl -s -H 'Host: 10.42.0.1' http://127.0.0.1/api/status

# The two QRs encode what they should
sudo python3 wifi/wifiqr.py --show                  # WIFI: join payload
python3 wifi/wifiqr.py --url http://10.42.0.1/ --show
ls -l /var/lib/photobooth/wifi-qr.png /var/lib/photobooth/portal-qr.png

# Portal for Android only: the first must not match, the second must mention tag:android
grep -E '^address=/#/' /etc/dnsmasq.d/photobooth.conf
grep -E '^dhcp-option.*114' /etc/dnsmasq.d/photobooth.conf

# iOS and friends must see success — this is what stops the "Stay connected?" prompt
curl -s  -H 'Host: captive.apple.com' http://127.0.0.1/hotspot-detect.html
curl -s  -H 'Host: www.msftconnecttest.com' http://127.0.0.1/connecttest.txt

# Android must see a redirect, NEVER a 204 — that is what opens its portal
curl -sI -H 'Host: connectivitycheck.gstatic.com' http://127.0.0.1/generate_204 | head -2
curl -s  -H 'Host: 10.42.0.1' http://127.0.0.1/captive-api

# Live
sudo hostapd_cli -i wlan0 list_sta
journalctl -fu photobooth-rotate -u hostapd
sudo cat /var/lib/photobooth/current-password.txt
tail -f /var/log/nginx/photobooth-probes.log

# What does one phone do when it joins? (forget the network on it first)
sudo tools/capture-ios.sh

# Everything at once, while the booth runs (see Logs above)
journalctl -fu photobooth-booth
```

## Files

```
main.py                  THE entry point: AP up -> booth loop -> AP down
config.yaml              all tunables — the single source of truth
pbcommon.py              config loading, IP->MAC, hostapd_cli

wifi/
  accesspoint.py         AP lifecycle: interface, hostapd, dnsmasq
  rotate_password.py     passphrase rotation; delegates the QR to wifiqr.py
  wifiqr.py              WIFI: join payload + URL QR rendering (both codes)
  clientwatch.py         --watch-clients report and the start-up config checks

gallery/
  portal.py              Flask: the gallery listing, /captive-api, /api/status
  ingest.py              gallery thumbnails
  templates/ static/     the gallery page and stylesheet

deploy/
  setup.sh               non-disruptive install
  render_configs.py      config.yaml -> system files and systemd units
  etc/*.tmpl             hostapd, dnsmasq, nginx, NetworkManager templates
  systemd/*.service      five unit templates

tools/
  capture-ios.sh         tcpdump of one phone joining
```

## Why it is built this way

| Constraint | Consequence |
| --- | --- |
| Single-antenna 2.4 GHz n | `max_num_sta=10`, download caps, 20 MHz only |
| One radio | AP-only; install never touches it; run from console or Ethernet |
| SD-card IO | thumbnails pre-generated; no DNS query logs |
| Large JPEGs, many guests | `draft()` decode; nginx `sendfile` straight from disk |

The radio, not the CPU or RAM, is what runs out first.
