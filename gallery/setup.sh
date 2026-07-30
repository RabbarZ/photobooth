#!/usr/bin/env bash
# Photobooth gallery — non-disruptive setup.
#
# Installs packages, renders config from config.yaml, installs systemd units and
# builds the first thumbnails/zip. Does NOT touch the radio: wlan0 keeps its
# current Wi-Fi connection and your SSH session survives. Bringing the AP up
# is main.py's job (or accesspoint.py, for the AP alone).
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR=/var/lib/photobooth

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo $0" >&2
    exit 1
fi

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

say "Reading config"
python3 - <<'PY'
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
PY
cd "$APP_DIR"
python3 -c "import pbcommon; c=pbcommon.load_config(); print('  SSID:', c['access_point']['ssid']); print('  AP address:', c['access_point']['address']); print('  channel:', c['access_point']['channel'])"

say "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# nginx-light is enough: no fancy modules are used.
apt-get install -y --no-install-recommends \
    hostapd dnsmasq nginx-light \
    python3-flask python3-yaml python3-qrcode python3-pil gunicorn iw

# hostapd ships masked and disabled on Debian; we drive it via our own config.
systemctl unmask hostapd 2>/dev/null || true
# Reset to a known state; the enables further down set the boot behaviour.
systemctl disable --now hostapd 2>/dev/null || true
systemctl disable --now dnsmasq 2>/dev/null || true

say "Creating state directories"
install -d -m 0755 -o pi -g pi "$STATE_DIR" "$STATE_DIR/thumbs"

# /run/photobooth holds main.py's runtime overrides, written as root and read
# by the daemons running as pi. Declaring RuntimeDirectory= in a unit instead
# would make systemd chown the directory to that unit's user, locking the
# others out.
cat > /etc/tmpfiles.d/photobooth.conf <<'TMPFILES'
# Installed by photobooth setup.sh
d /run/photobooth 0775 pi pi -
TMPFILES
systemd-tmpfiles --create /etc/tmpfiles.d/photobooth.conf
echo "  /run/photobooth (runtime overrides)"
install -d -m 0755 /etc/hostapd
echo "  $STATE_DIR (thumbs, zip, QR, current password)"

say "Granting nginx traversal to the photo directory"
# nginx runs as www-data. Raspberry Pi OS ships /home/pi as 0700, so www-data
# cannot even traverse into it and every /photos/ and /static/ request 403s --
# with the confusing symptom that /thumbs/ and the zip work fine, because those
# live under /var/lib/photobooth.
#
# o+x is traverse-only: www-data can open a file whose path it is given, but
# cannot list the directory. sshd's StrictModes is unaffected -- it rejects
# group/other WRITE, and there is none here.
#
# Everything below /home/pi is already world-readable (0775/0664), so this
# exposes nothing that was not already intended to be readable. If you would
# rather not open the home directory at all, the precise alternative is an ACL:
#   setfacl -m u:www-data:x /home/pi
PHOTO_PARENT=$(python3 -c "import pbcommon,pathlib; print(pathlib.Path(pbcommon.load_config()['gallery']['photo_dir']).resolve())")
node="$PHOTO_PARENT"
while [[ "$node" != "/" ]]; do
    if ! sudo -u www-data test -x "$node"; then
        chmod o+x "$node"
        echo "  chmod o+x $node"
    fi
    node=$(dirname "$node")
done
if sudo -u www-data test -r "$PHOTO_PARENT"; then
    echo "  www-data can reach $PHOTO_PARENT"
else
    echo "  WARNING: www-data still cannot read $PHOTO_PARENT" >&2
fi

say "Rendering system config from config.yaml"
python3 render_configs.py --install

# hostapd on Debian reads DAEMON_CONF from this file, and its ExecStart is
# "hostapd -B -P /run/hostapd.pid $DAEMON_OPTS ${DAEMON_CONF}".
if ! grep -q '^DAEMON_CONF="/etc/hostapd/photobooth.conf"' /etc/default/hostapd 2>/dev/null; then
    sed -i 's|^#\?DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/photobooth.conf"|' /etc/default/hostapd
    grep -q '^DAEMON_CONF=' /etc/default/hostapd || \
        echo 'DAEMON_CONF="/etc/hostapd/photobooth.conf"' >> /etc/default/hostapd
    echo "  pointed /etc/default/hostapd at photobooth.conf"
fi

# ...but DAEMON_CONF alone is not enough. The packaged unit also carries
#   ConditionFileNotEmpty=/etc/hostapd/hostapd.conf
# which is evaluated BEFORE ExecStart. With our config under a different name
# the condition fails and systemd silently SKIPS the unit -- it reports
# "skipped, unmet condition check" rather than an error, so `systemctl start`
# appears to do nothing at all. Retarget the condition instead of renaming our
# config, so an unrelated /etc/hostapd/hostapd.conf is never picked up.
install -d -m 0755 /etc/systemd/system/hostapd.service.d
cat > /etc/systemd/system/hostapd.service.d/photobooth.conf <<'UNIT'
# Installed by photobooth setup.sh
[Unit]
# Empty value clears the packaged condition; the second line sets ours.
ConditionFileNotEmpty=
ConditionFileNotEmpty=/etc/hostapd/photobooth.conf
UNIT
echo "  retargeted hostapd.service ConditionFileNotEmpty"
systemctl daemon-reload

say "Generating first passphrase + QR"
# hostapd is not running yet, so the reload step will report that it could not
# reach the control interface. That is expected here and harmless.
python3 rotate_password.py --once --print > /tmp/pb-first-pass.txt 2>/tmp/pb-first-pass.err || {
    echo "  rotation failed:"; cat /tmp/pb-first-pass.err; exit 1; }
echo "  passphrase: $(cat /tmp/pb-first-pass.txt)"
chown pi:pi "$STATE_DIR/wifi-qr.png" 2>/dev/null || true
rm -f /tmp/pb-first-pass.txt /tmp/pb-first-pass.err

say "Enabling nginx site"
ln -sfn /etc/nginx/sites-available/photobooth /etc/nginx/sites-enabled/photobooth
# Debian's default site is also a default_server on :80 and would collide.
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable nginx >/dev/null
systemctl reload nginx || systemctl restart nginx

say "Installing systemd units"
install -m 0644 "$APP_DIR"/systemd/photobooth-*.service /etc/systemd/system/
systemctl daemon-reload
# Portal and ingest are safe to run now — neither touches the radio.
systemctl enable --now photobooth-portal.service photobooth-ingest.service
# The AP stack is deliberately NOT enabled for boot: main.py owns the guest
# Wi-Fi lifecycle, bringing it up on start and taking it down on exit. Enabling
# these would make the SSID appear at boot with no booth running, and would
# make main.py's --no-wifi a lie.
#
# They stay installed (and BindsTo photobooth-ap) so that whenever the AP does
# start, it starts as a whole: photobooth-ap owns the interface address, and
# hostapd/dnsmasq cannot run without it. That ordering is what stops an SSID
# broadcasting with no DHCP server behind it.
systemctl disable photobooth-ap.service photobooth-rotate.service \
    hostapd.service dnsmasq.service >/dev/null 2>&1 || true
echo "  portal + ingest started and enabled; AP stack installed, started by main.py"

say "Done"
cat <<EOF

Setup is complete and NOTHING has touched wlan0 — your Wi-Fi and SSH are intact.

Verify now, over your existing network:
  curl -s -H 'Host: 10.42.0.1' http://127.0.0.1/ | head -20
  systemctl status photobooth-portal photobooth-ingest --no-pager

When you are ready to go live, run the booth — it brings the AP up and takes
it down again on exit:
  sudo python3 $APP_DIR/../main.py

  If wlan0 currently carries your SSH session, do this from the CONSOLE
  (keyboard + monitor): turning it into an AP disconnects that session.

AP without the booth:      sudo python3 $APP_DIR/accesspoint.py start
Stop it / restore Wi-Fi:   sudo python3 $APP_DIR/accesspoint.py stop --release
Status:                    python3 $APP_DIR/accesspoint.py status
EOF
