#!/usr/bin/env bash
# Photobooth — setup for a fresh Raspberry Pi OS (desktop) on a Pi 5.
#
#   sudo deploy/setup.sh
#
# Run it as the desktop user (the one created in Raspberry Pi Imager), with
# sudo. Safe to run again, but only needed once: after pulling changes or
# editing config.yaml, run deploy/deploy.sh instead.
#
# Installs packages, configures the host for tethered capture, renders config
# from config.yaml, installs systemd units and builds the first QR. Does NOT
# touch wlan0: it keeps its current Wi-Fi connection and your SSH session
# survives. Bringing the AP up is main.py's job. If the USB Wi-Fi dongle
# (wlan1) is plugged in, asks for the passwords of its networks.
#
# The booth runs for one desktop user. By default that is the user who ran
# sudo; override with PHOTOBOOTH_USER=name.
set -euo pipefail

# Layout: this script is in deploy/, the daemons it installs live in wifi/ and
# gallery/, and main.py, config.yaml and pbcommon.py sit at the repo root.
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$DEPLOY_DIR")"
WIFI_DIR="$REPO_DIR/wifi"
STATE_DIR=/var/lib/photobooth

# The Pi's own Wi-Fi on the USB dongle (wlan0 is the guest AP): internet, SSH
# and deploys. Most preferred network first.
# Passwords are asked for when setup runs, never stored in the repo.
UPLINK_IF=wlan1
UPLINK_SSIDS=("Kilian" "Kilian 15 Pro")

say()  { printf '\n\033[1m== %s\033[0m\n' "$1"; }
warn() { printf '  \033[33mWARNING:\033[0m %s\n' "$1" >&2; }
die()  { printf '\033[31mERROR:\033[0m %s\n' "$1" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo: sudo $0"

# ---------------------------------------------------------------------------
say "Checking the system"

if [[ -r /etc/os-release ]]; then
    . /etc/os-release
    echo "  OS: ${PRETTY_NAME:-unknown}"
    case "${VERSION_CODENAME:-}" in
        bookworm|trixie) ;;
        *) warn "tested on Raspberry Pi OS bookworm/trixie, this is '${VERSION_CODENAME:-?}'" ;;
    esac
fi
if [[ -r /proc/device-tree/model ]]; then
    model=$(tr -d '\0' < /proc/device-tree/model)
    echo "  board: $model"
    [[ "$model" == *"Raspberry Pi 5"* ]] || warn "built for a Raspberry Pi 5"
fi

BOOTH_USER="${PHOTOBOOTH_USER:-${SUDO_USER:-}}"
if [[ -z "$BOOTH_USER" || "$BOOTH_USER" == root ]]; then
    BOOTH_USER=$(stat -c %U "$REPO_DIR")
fi
[[ "$BOOTH_USER" != root ]] || die "cannot tell which desktop user the booth is for.
       Run this with sudo from that user, or set PHOTOBOOTH_USER=name."
id "$BOOTH_USER" >/dev/null 2>&1 || die "no such user: $BOOTH_USER"
BOOTH_GROUP=$(id -gn "$BOOTH_USER")
BOOTH_UID=$(id -u "$BOOTH_USER")
echo "  booth user: $BOOTH_USER (uid $BOOTH_UID)"

if [[ ! -x /usr/sbin/lightdm && ! -x /usr/bin/labwc ]]; then
    warn "no desktop found. The booth screen is a Wayland client and needs
           Raspberry Pi OS with desktop, not Lite."
fi

# ---------------------------------------------------------------------------
say "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
    `# guest Wi-Fi and gallery` \
    hostapd dnsmasq nginx iw \
    python3-flask python3-yaml python3-qrcode python3-pil gunicorn \
    `# camera, button and screen` \
    gphoto2 python3-gphoto2 python3-pygame python3-gpiozero python3-lgpio \
    `# diagnostics used by tools/ and the docs` \
    gpiod tcpdump curl

# hostapd ships masked and disabled on Debian; we drive it via our own config.
systemctl unmask hostapd 2>/dev/null || true
# dnsmasq starts as soon as it is installed and would hold the DHCP/DNS ports.
# accesspoint.py starts it when the AP comes up.
systemctl disable --now dnsmasq 2>/dev/null || true

# Everything the booth imports, checked as the user that will run it. A
# missing module here would otherwise surface only at the first boot.
sudo -u "$BOOTH_USER" python3 -c \
    "import gphoto2, pygame, gpiozero, lgpio, PIL, yaml, qrcode, flask" \
    || die "a Python module is still missing -- see the error above"
echo "  Python modules OK"

cd "$REPO_DIR"
python3 -c "import pbcommon; c=pbcommon.load_config(); print('  SSID:', c['access_point']['ssid']); print('  AP address:', c['access_point']['address']); print('  channel:', c['access_point']['channel'])"

# ---------------------------------------------------------------------------
say "Giving $BOOTH_USER access to the camera, GPIO and screen"
# plugdev: the camera's USB node (libgphoto2's udev rules). gpio: the button.
# video/render: the display. Raspberry Pi OS's first user is usually in all of
# these already; this makes sure.
for group in plugdev gpio video render input; do
    if getent group "$group" >/dev/null && ! id -nG "$BOOTH_USER" | grep -qw "$group"; then
        usermod -aG "$group" "$BOOTH_USER"
        echo "  added to $group (takes effect at next login)"
    fi
done

# ---------------------------------------------------------------------------
say "Configuring the host for tethered capture"

# The desktop's gvfs auto-mounts any PTP/MTP camera, after which libgphoto2
# cannot claim it ("-53 Could not claim the USB device"). Masked for every
# user, so it holds whichever account logs in.
systemctl --global mask gvfs-gphoto2-volume-monitor.service \
    gvfs-mtp-volume-monitor.service >/dev/null 2>&1 || true
pkill -f 'gvfs-(gphoto2|mtp)-volume-monitor' 2>/dev/null || true
echo "  gvfs camera auto-mount masked"

# USB autosuspend defaults to 2 s, and a suspended camera can drop its PTP
# session while the booth idles. Keyed on ID_GPHOTO2, which libgphoto2's own
# udev rules set, so it covers any supported camera.
cat > /etc/udev/rules.d/70-photobooth-camera.rules <<'UDEV'
# Installed by photobooth setup.sh: never autosuspend a tethered camera.
ACTION!="remove", SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ENV{ID_GPHOTO2}=="1", TEST=="power/control", ATTR{power/control}="on"
UDEV
udevadm control --reload-rules
udevadm trigger --subsystem-match=usb --action=change 2>/dev/null || true
echo "  USB autosuspend off for cameras"

# usbfs caps in-flight USB transfer buffers at 16 MB by default; the standard
# advice for tethering high-resolution bodies is to raise it. usbcore is built
# into the kernel, so modprobe.d does not apply -- tmpfiles writes the sysfs
# value at every boot instead.
cat > /etc/tmpfiles.d/photobooth-usbfs.conf <<'USBFS'
# Installed by photobooth setup.sh
w /sys/module/usbcore/parameters/usbfs_memory_mb - - - - 1000
USBFS
systemd-tmpfiles --create /etc/tmpfiles.d/photobooth-usbfs.conf 2>/dev/null || true
echo "  usbfs_memory_mb = $(cat /sys/module/usbcore/parameters/usbfs_memory_mb 2>/dev/null || echo '?')"

# ---------------------------------------------------------------------------
say "RTC battery charging"
BOOT_CONFIG=/boot/firmware/config.txt
if ! grep -q '^dtparam=rtc_bbat_vchg=' "$BOOT_CONFIG"; then
    printf '\n[all]\ndtparam=rtc_bbat_vchg=4200000\n' >> "$BOOT_CONFIG"
fi
echo "  charging on at 4.2 V for an LIR2032 (after reboot)"

# ---------------------------------------------------------------------------
say "Wi-Fi on $UPLINK_IF (USB dongle: internet, SSH, deploys)"
# wlan0 becomes the guest AP, so the dongle on wlan1 is the Pi's own Wi-Fi:
# internet, SSH, git pull and deploys. NetworkManager keeps managing it, joins the
# highest-priority network in range and falls back to the next one. Passwords
# are prompted for here and stored only in NetworkManager's root-only
# system-connections, never in the repo.
uplink_tty_ok() { { : </dev/tty; } 2>/dev/null; }

if [[ ! -e /sys/class/net/$UPLINK_IF ]]; then
    warn "no $UPLINK_IF: the USB Wi-Fi dongle is missing. Without it the Pi
           has no Wi-Fi of its own once wlan0 is the guest AP.
           Plug it in and run setup.sh again to set up its Wi-Fi."
elif ! command -v nmcli >/dev/null; then
    warn "nmcli not found: cannot set up Wi-Fi on $UPLINK_IF"
elif ! uplink_tty_ok; then
    warn "no terminal to ask for Wi-Fi passwords: $UPLINK_IF not set up.
           Run setup.sh again from an interactive shell."
else
    for i in "${!UPLINK_SSIDS[@]}"; do
        ssid="${UPLINK_SSIDS[$i]}"
        prio=$(( (${#UPLINK_SSIDS[@]} - i) * 10 ))   # first SSID wins
        name="uplink $ssid"
        if nmcli -t -f NAME connection show | grep -Fxq "$name"; then
            hint="Enter keeps the saved one"
        else
            hint="Enter skips"
        fi
        while :; do
            printf '  Password for "%s" (%s): ' "$ssid" "$hint" >/dev/tty
            IFS= read -rs pw </dev/tty || pw=""
            printf '\n' >/dev/tty
            [[ -z "$pw" || ( ${#pw} -ge 8 && ${#pw} -le 64 ) ]] && break
            echo "    WPA passwords are 8 to 63 characters (or 64 hex), try again" >/dev/tty
        done
        if [[ -z "$pw" ]]; then
            echo "  \"$ssid\": unchanged ($hint)"
            continue
        fi
        nmcli connection delete "$name" >/dev/null 2>&1 || true
        if nmcli connection add type wifi ifname "$UPLINK_IF" con-name "$name" \
               ssid "$ssid" wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$pw" \
               connection.autoconnect yes \
               connection.autoconnect-priority "$prio" >/dev/null; then
            echo "  \"$ssid\": saved (priority $prio)"
        else
            warn "could not save \"$ssid\" -- see the nmcli error above"
        fi
    done
    unset pw
    echo "  $UPLINK_IF: $(nmcli -g GENERAL.STATE,GENERAL.CONNECTION device show "$UPLINK_IF" 2>/dev/null | paste -sd ' ' -)"
fi

# ---------------------------------------------------------------------------
say "Desktop: autologin and no screen blanking"
# The booth screen needs a logged-in desktop session at boot, and a screen
# that never goes dark between guests.
if command -v raspi-config >/dev/null; then
    SUDO_USER="$BOOTH_USER" raspi-config nonint do_boot_behaviour B4 \
        && echo "  boots to the desktop, logged in as $BOOTH_USER" \
        || warn "could not set desktop autologin -- set it in raspi-config"
    raspi-config nonint do_blanking 1 \
        && echo "  screen blanking off" \
        || warn "could not turn off screen blanking -- do it in raspi-config"
else
    warn "raspi-config not found: set desktop autologin and turn off screen
           blanking yourself"
fi

# ---------------------------------------------------------------------------
say "Creating directories"
PHOTO_DIR=$(python3 -c "import pbcommon; print(pbcommon.load_config()['gallery']['photo_dir'])")
install -d -m 0755 -o "$BOOTH_USER" -g "$BOOTH_GROUP" \
    "$PHOTO_DIR" "$STATE_DIR" "$STATE_DIR/thumbs"
echo "  $PHOTO_DIR (photos)"
echo "  $STATE_DIR (thumbs, QR, current password)"
install -d -m 0755 /etc/hostapd

say "Granting nginx traversal to the photo directory"
# nginx runs as www-data. Raspberry Pi OS ships home directories as 0700, so
# www-data cannot even traverse into one and every /photos/ and /static/
# request 403s -- with the confusing symptom that /thumbs/ works
# fine, because thumbnails live under /var/lib/photobooth.
#
# o+x is traverse-only: www-data can open a file whose path it is given, but
# cannot list the directory. sshd's StrictModes is unaffected -- it rejects
# group/other WRITE, and there is none here. If you would rather not open the
# home directory at all, the precise alternative is an ACL:
#   setfacl -m u:www-data:x /home/<user>
for target in "$PHOTO_DIR" "$REPO_DIR/gallery/static"; do
    node="$target"
    while [[ "$node" != "/" ]]; do
        if ! sudo -u www-data test -x "$node"; then
            chmod o+x "$node"
            echo "  chmod o+x $node"
        fi
        node=$(dirname "$node")
    done
    if sudo -u www-data test -r "$target"; then
        echo "  www-data can reach $target"
    else
        warn "www-data still cannot read $target"
    fi
done

# ---------------------------------------------------------------------------
say "Rendering system config from config.yaml"
# Renders the systemd units too: they carry this checkout's absolute path and
# the booth user.
python3 "$DEPLOY_DIR/render_configs.py" --install --user "$BOOTH_USER"

# hostapd on Debian reads DAEMON_CONF from this file, and its ExecStart is
# "hostapd -B -P /run/hostapd.pid $DAEMON_OPTS ${DAEMON_CONF}".
if ! grep -q '^DAEMON_CONF="/etc/hostapd/photobooth.conf"' /etc/default/hostapd 2>/dev/null; then
    touch /etc/default/hostapd
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

# ---------------------------------------------------------------------------
say "Generating first passphrase + QR"
# hostapd is not running yet, so the reload step will report that it could not
# reach the control interface. That is expected here and harmless.
python3 "$WIFI_DIR/rotate_password.py" --once --print > /tmp/pb-first-pass.txt 2>/tmp/pb-first-pass.err || {
    echo "  rotation failed:"; cat /tmp/pb-first-pass.err; exit 1; }
echo "  passphrase: $(cat /tmp/pb-first-pass.txt)"
chown "$BOOTH_USER:$BOOTH_GROUP" "$STATE_DIR/wifi-qr.png" 2>/dev/null || true
rm -f /tmp/pb-first-pass.txt /tmp/pb-first-pass.err

say "Enabling nginx site"
ln -sfn /etc/nginx/sites-available/photobooth /etc/nginx/sites-enabled/photobooth
# Debian's default site is also a default_server on :80 and would collide.
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable nginx >/dev/null
systemctl reload nginx || systemctl restart nginx

say "Installing systemd units"
# The unit files themselves were written by render_configs.py above.
systemctl daemon-reload
# Portal and ingest are safe to run now — neither touches the radio.
systemctl enable --now photobooth-portal.service photobooth-ingest.service >/dev/null
# The AP stack (photobooth-ap, photobooth-rotate, hostapd, dnsmasq) is
# deliberately NOT enabled for boot: main.py owns the guest Wi-Fi lifecycle,
# bringing it up on start and taking it down on exit. Enabling these would make
# the SSID appear at boot with no booth running, and would make main.py's
# --no-wifi a lie.
#
# Start them through accesspoint.py (main.py does), never one by one: it sets
# the interface address before hostapd and dnsmasq, which is what stops an SSID
# broadcasting with no DHCP server behind it.
#
# The booth itself is enabled for boot but deliberately NOT started here: it is
# what brings the AP up, and setup.sh promises to leave wlan0 (and your SSH
# session) alone. It comes up on the next reboot, or on demand with
# `systemctl start photobooth`.
systemctl enable photobooth.service >/dev/null
echo "  portal + ingest running; booth enabled for boot; AP stack started by main.py"

say "Done"
cat <<EOF

Setup is complete and NOTHING has touched wlan0 yet — your Wi-Fi and SSH are
intact for now.

Verify now, over your existing network:
  curl -s -H 'Host: 10.42.0.1' http://127.0.0.1/ | head -20
  systemctl status photobooth-portal photobooth-ingest --no-pager
  $REPO_DIR/tools/check-camera.sh

After the next REBOOT, wlan0 belongs to the booth: NetworkManager no longer
manages it, and the booth starts at boot and turns it into the guest access
point. Manage the Pi over $UPLINK_IF (the USB dongle), Ethernet, or keyboard +
monitor from then on.

To try it now without rebooting (from the console, not over Wi-Fi SSH):
  sudo python3 $REPO_DIR/main.py
  sudo python3 $REPO_DIR/main.py --mock-camera   # no camera: needs a .jpg in $PHOTO_DIR

After pulling changes or editing config.yaml, deploy with:
  sudo $DEPLOY_DIR/deploy.sh

Give wlan0 back to normal Wi-Fi:
  sudo systemctl disable --now photobooth
  sudo python3 $WIFI_DIR/accesspoint.py stop --release
EOF
