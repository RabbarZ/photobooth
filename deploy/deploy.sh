#!/usr/bin/env bash
# Photobooth — put this checkout live after a change.
#
#   git pull && sudo deploy/deploy.sh
#
# setup.sh is the one-time install (packages, USB, groups, autologin). This is
# the everyday step: re-render the system files and units from config.yaml,
# then restart the three services so they run the current code and config.
#
#   photobooth          main.py: camera, screen, guest Wi-Fi
#   photobooth-portal   the gallery page
#   photobooth-ingest   gallery thumbnails
#
# Restarting `photobooth` takes the access point down and up again, so guests
# drop off for a few seconds and any SSH session over wlan0 dies. Run it over
# Ethernet, another Wi-Fi interface, or from the console.
#
# The booth user is the one who ran sudo; override with PHOTOBOOTH_USER=name.
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
die() { printf '\033[31mERROR:\033[0m %s\n' "$1" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo: sudo $0"

user_args=()
[[ -n "${PHOTOBOOTH_USER:-}" ]] && user_args=(--user "$PHOTOBOOTH_USER")

say "Rendering system config and units from config.yaml"
python3 "$DEPLOY_DIR/render_configs.py" --install "${user_args[@]}"
systemctl daemon-reload
# The nginx site is among the rendered files.
nginx -t -q && systemctl reload nginx

say "Restarting services"
systemctl enable photobooth.service photobooth-portal.service \
    photobooth-ingest.service >/dev/null
systemctl restart photobooth-portal.service photobooth-ingest.service
# The booth stops with SIGINT and may wait up to 60 s for a capture in flight.
systemctl restart photobooth.service

systemctl --no-pager --lines=0 status \
    photobooth photobooth-portal photobooth-ingest || true
echo
echo "Booth log: journalctl -fu photobooth"
