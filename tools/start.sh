#!/usr/bin/env bash
# Start the photobooth: gallery, thumbnails, then the booth (which brings up
# the guest Wi-Fi itself). Safe to run when it is already running.
set -euo pipefail
[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"

systemctl start photobooth-portal.service photobooth-ingest.service
systemctl start photobooth-booth.service

sleep 5  # give the booth time to connect to the camera and bring up Wi-Fi
systemctl --no-pager list-units --all 'photobooth*' --type=service \
    | grep -E 'photobooth|UNIT'
echo
journalctl -u photobooth-booth.service --since "-10s" --no-pager -o cat | tail -5
