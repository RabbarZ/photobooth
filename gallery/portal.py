"""The photo gallery, served over the booth's own Wi-Fi.

A web app on a network with no internet. Guests scan one QR to join the Wi-Fi
and a second to open this page; Android phones are sent here by themselves
through a captive portal (see /captive-api below and docs/guest-wifi.md).
iPhones are deliberately not.

nginx serves photos and thumbnails directly, and answers the
OS connectivity probes itself; this app only renders the listing and the
captive-portal status. It keeps no state.
"""

from __future__ import annotations

import sys
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request

# pbcommon is shared with wifi/ and deploy/ and lives at the repo root. Done
# here rather than through PYTHONPATH so gunicorn needs no extra environment.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pbcommon  # noqa: E402

CONFIG = pbcommon.load_config()
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}

app = Flask(__name__)


# --------------------------------------------------------------------------
# per-request plumbing
# --------------------------------------------------------------------------

def client_ip() -> str:
    """The guest's address. nginx sets X-Real-IP; fall back to the peer."""
    return request.headers.get("X-Real-IP") or request.remote_addr or ""


def client_mac() -> str | None:
    return pbcommon.mac_for_ip(client_ip())


# --------------------------------------------------------------------------
# gallery
# --------------------------------------------------------------------------

def list_photos() -> list[dict]:
    """Newest photos first, capped by the app-layer per-user limit."""
    photo_dir = Path(CONFIG["gallery"]["photo_dir"])
    limit = int(CONFIG["gallery"]["max_photos_per_user"])
    try:
        entries = [e for e in photo_dir.iterdir()
                   if e.is_file() and e.suffix.lower() in IMAGE_SUFFIXES]
    except OSError:
        return []
    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"name": p.name} for p in entries[:limit]]


@app.route("/")
def gallery():
    return render_template("gallery.html", photos=list_photos())


@app.get("/captive-api")
def captive_api():
    """RFC 8908 captive-portal status. Reached by Android devices only.

    dnsmasq advertises this URL in DHCP option 114, but only to leases tagged
    `android` by their vendor class -- see the dnsmasq template. iOS never
    learns the URL, so it never asks, and iPhones stay on the "connectivity is
    fine" path with no sign-in sheet.

    On Android 11+ this is the primary detection route and the HTTP probe is
    only a fallback, so answering it is what makes the portal appear promptly
    rather than after a delay or not at all.

    The media type is mandatory: a generic application/json is ignored and the
    device falls back to probing, which looks exactly like this not working.
    """
    body = jsonify({
        "captive": True,
        # Where Android opens its login browser.
        "user-portal-url": f"http://{CONFIG['access_point']['address']}/",
        "can-extend-session": True,
    })
    body.headers["Content-Type"] = "application/captive+json"
    return body


@app.get("/api/status")
def status():
    """Small diagnostic view -- handy when tuning, not used by the page."""
    return jsonify({
        "ip": client_ip(),
        "mac": client_mac(),
        "photos": len(list_photos()),
        "associated_stations": len(pbcommon.associated_stations(CONFIG)),
    })


# --------------------------------------------------------------------------
# responses
# --------------------------------------------------------------------------
#
# No OS-probe routes here (/generate_204, /hotspot-detect.html, ...): nginx
# answers those itself, per platform, without waking gunicorn.


@app.errorhandler(404)
def not_found(_err):
    """A wrong URL goes to the gallery -- the only page a guest wants."""
    return redirect("/", code=302)


@app.after_request
def no_store(response):
    """Keep the listing uncached.

    Photos appear while guests are looking at the page, and a cached listing
    hides the shot just taken.
    """
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


if __name__ == "__main__":
    # Development only; production runs under gunicorn (see systemd unit).
    host, _, port = CONFIG["portal"]["bind"].partition(":")
    app.run(host=host, port=int(port), debug=False)
