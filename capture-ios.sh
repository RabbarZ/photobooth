#!/usr/bin/env bash
# Capture what a phone does when it joins, to diagnose captive-portal detection.
#   sudo ./capture-ios.sh [seconds]     (default 90)
# Forget the network on the phone first, then rejoin while this runs.
SECS=${1:-90}
OUT=/tmp/ios-capture.txt
echo "Capturing on wlan0 for ${SECS}s -> $OUT"
echo ">>> Now: forget the Wi-Fi on the phone and rejoin. Do not open a browser."
timeout "$SECS" tcpdump -i wlan0 -n -s0 -A 'port 53 or port 67 or port 68 or port 80' > "$OUT" 2>/dev/null
echo
echo "=== DHCP option 114 present in the ACK? ==="
grep -aoE 'http://10\.42\.0\.1/' "$OUT" | head -3 | sed 's/^/  /' || echo "  NOT SEEN - iOS never received the portal URL"
echo "=== names the phone looked up ==="
grep -aoE '\b[a-z0-9-]+\.(apple|gstatic|msftconnecttest|mozilla)\.com' "$OUT" | sort -u | sed 's/^/  /' || echo "  none"
echo "=== HTTP requests ==="
grep -aoE 'GET [^ ]+ HTTP' "$OUT" | sort | uniq -c | sed 's/^/  /' || echo "  none"
echo "=== User-Agents seen ==="
grep -aoE 'User-Agent: [^\r]+' "$OUT" | sort -u | cut -c1-90 | sed 's/^/  /' || echo "  none"
echo
echo "Full capture: $OUT"
