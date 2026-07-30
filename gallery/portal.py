"""The photo gallery, served over the booth's own Wi-Fi.

An ordinary web app on a network with no internet. There is no captive portal:
guests scan one QR to join the Wi-Fi and a second to open this page. Nothing
here hijacks DNS, answers OS connectivity probes, or claims the device must
log in -- a phone on this network simply has no internet, and says so.

nginx serves photos, thumbnails and the prebuilt zip directly; this app only
renders the listing.

It keeps no state at all. It used to track per-device session activity to feed
the hard and soft limits, which have been removed -- a guest's time is bounded
by the passphrase ring instead, see config.yaml.
"""

from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request

import pbcommon

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


def zip_info() -> dict | None:
    """Size of the prebuilt zip, or None if ingest has not made one yet."""
    path = Path(CONFIG["gallery"]["zip_path"])
    try:
        size = path.stat().st_size
    except OSError:
        return None
    return {"bytes": size, "megabytes": round(size / 1_000_000, 1)} if size else None


@app.route("/")
def gallery():
    # No zip_file: the page offers per-photo saving only. The zip itself still
    # exists -- see /api/download-all.
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


@app.post("/api/download-all")
def download_all():
    """Hand back the static zip URL.

    No longer reachable from the gallery: the "Download all" button was taken
    out, so guests save photos individually. Kept because the zip is still
    built by ingest.py and still served by nginx at /all-photos.zip, and
    anyone fetching that URL directly -- the operator collecting the evening's
    photos, most likely -- may want to discover it programmatically.

    This used to register a server-side grace window so a long transfer was not
    cut short by the idle kick. There is no idle kick any more, so there is
    nothing to suppress. Note that a rotation still deauthenticates everyone
    briefly, which a download has always had to survive.
    """
    return jsonify({"ok": True, "url": "/all-photos.zip"})


@app.get("/api/status")
def status():
    """Small diagnostic view -- handy when tuning, not used by the page."""
    return jsonify({
        "ip": client_ip(),
        "mac": client_mac(),
        "photos": len(list_photos()),
        "zip": zip_info(),
        "associated_stations": len(pbcommon.associated_stations(CONFIG)),
    })


# --------------------------------------------------------------------------
# responses
# --------------------------------------------------------------------------
#
# There are deliberately no OS-probe routes here. The old build answered
# /generate_204, /hotspot-detect.html, /connecttest.txt and friends with a 302
# so no device could ever conclude it had internet, which is what kept the
# sign-in sheet re-appearing. With no captive portal those probes are none of
# our business: they no longer resolve to us, and a phone is free to decide
# this network has no internet, because it does not.


@app.errorhandler(404)
def not_found(_err):
    """A wrong URL goes to the gallery -- the only page a guest wants."""
    return redirect("/", code=302)


@app.after_request
def no_store(response):
    """Keep the listing uncached.

    Not a captive-portal measure any more: photos appear while guests are
    looking at the page, and a cached listing hides the shot just taken.
    """
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


if __name__ == "__main__":
    # Development only; production runs under gunicorn (see systemd unit).
    host, _, port = CONFIG["portal"]["bind"].partition(":")
    app.run(host=host, port=int(port), debug=False)
