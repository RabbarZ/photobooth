#!/usr/bin/env bash
# Stop the photobooth: booth (camera, screen, guest Wi-Fi), gallery, thumbnails.
#
# Stops them until the next reboot or tools/start.sh. Don't run it while a
# photo is being taken: interrupting a capture can lock up the camera until
# it is switched off and on.
set -euo pipefail
[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"

# The booth first: on its way down it also takes the guest Wi-Fi down
# (photobooth-ap, photobooth-rotate) and lets go of the camera.
systemctl stop photobooth-booth.service
systemctl stop photobooth-ingest.service photobooth-portal.service
# Anything still running, e.g. the Wi-Fi units if the booth died uncleanly.
systemctl stop 'photobooth*'

systemctl --no-pager list-units --all 'photobooth*' --type=service \
    | grep -E 'photobooth|UNIT'
echo "Stopped. Start again with tools/start.sh"
