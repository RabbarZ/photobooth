#!/usr/bin/env bash
# Diagnose the camera connection before running the booth.
set -uo pipefail

step() { printf '\n== %s ==\n' "$1"; }

step "USB devices"
lsusb

step "Processes that could steal the camera"
if ps -eo args | grep -E 'gvfs-(gphoto2|mtp)-volume-monitor|gvfsd-gphoto2' | grep -qv grep; then
    ps -eo pid,args | grep -E 'gvfs-(gphoto2|mtp)-volume-monitor|gvfsd-gphoto2' | grep -v grep
    echo "WARNING: a gvfs monitor is running and will block libgphoto2."
    echo "Fix: systemctl --user mask gvfs-gphoto2-volume-monitor.service"
else
    echo "OK - nothing holding the camera."
fi

step "Cameras libgphoto2 can see"
gphoto2 --auto-detect

step "Camera summary"
gphoto2 --summary 2>&1 | head -20

step "Capture target / supported operations"
gphoto2 --get-config capturetarget 2>&1 | head -10
