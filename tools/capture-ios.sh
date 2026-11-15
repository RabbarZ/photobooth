#!/usr/bin/env bash
# Capture what a phone does when it joins, to diagnose connectivity detection.
#   sudo tools/capture-ios.sh [seconds]     (default 90)
# Forget the network on the phone first, then rejoin while this runs.
#
# What a healthy run looks like differs by platform, on purpose (see
# docs/guest-wifi.md):
#   iPhone   no option 114, probes captive.apple.com, gets 200 -> no prompt
#   Android  option 114 present, probes */generate_204, gets 302 -> portal opens
SECS=${1:-90}
OUT=/tmp/ios-capture.txt
echo "Capturing on wlan0 for ${SECS}s -> $OUT"
echo ">>> Now: forget the Wi-Fi on the phone and rejoin. Do not open a browser."
timeout "$SECS" tcpdump -i wlan0 -n -s0 -A 'port 53 or port 67 or port 68 or port 80' > "$OUT" 2>/dev/null

# Print the matches indented, or a fallback line when there are none. (A plain
# `grep | sed || echo` never reaches the echo: the pipeline's status is sed's.)
show() {
    local found
    found=$(cat)
    if [[ -n "$found" ]]; then
        printf '%s\n' "$found" | sed 's/^/  /'
    else
        echo "  $1"
    fi
}

echo
echo "=== DHCP option 114 (captive-portal URL) in a reply? ==="
echo "  Sent to Android only. Seeing it for an iPhone is the fault: iOS would"
echo "  open a sign-in sheet."
grep -aoE 'http://[0-9.]+/captive-api' "$OUT" | sort -u | head -3 \
    | show "not seen (correct for an iPhone, wrong for Android)"
echo "=== names the phone looked up ==="
grep -aoE '\b[a-z0-9-]+\.(apple|gstatic|google|android|googleapis|msftconnecttest|msftncsi|mozilla|ubuntu)\.(com|org)' "$OUT" \
    | sort -u | show "none"
echo "=== HTTP requests ==="
grep -aoE 'GET [^ ]+ HTTP' "$OUT" | sort | uniq -c | show "none"
echo "=== HTTP replies ==="
echo "  Want 200 for Apple/Windows/Firefox probes, 302 for Android's generate_204."
grep -aoE 'HTTP/1\.[01] [0-9]{3}' "$OUT" | sort | uniq -c | show "none"
echo "=== User-Agents seen ==="
grep -aoE 'User-Agent: [^\r]+' "$OUT" | sort -u | cut -c1-90 | show "none"
echo
echo "Full capture: $OUT"
