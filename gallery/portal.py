"""The photo gallery, served over the booth's own Wi-Fi.

An ordinary web app on a network with no internet. There is no captive portal:
guests scan one QR to join the Wi-Fi and a second to open this page. Nothing
here hijacks DNS, answers OS connectivity probes, or claims the device must
log in -- a phone on this network simply has no internet, and says so.

nginx serves photos, thumbnails and the prebuilt zip directly; this app only
renders the listing and tracks session activity.
"""

from __future__ import annotations

import time
from pathlib import Path

from flask import (Flask, g, jsonify, redirect, render_template, request)

import pbcommon

CONFIG = pbcommon.load_config()
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}

app = Flask(__name__)


# --------------------------------------------------------------------------
# per-request plumbing
# --------------------------------------------------------------------------

def db():
    if "db" not in g:
        g.db = pbcommon.connect(CONFIG)
    return g.db


@app.teardown_appcontext
def _close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def client_ip() -> str:
    """The guest's address. nginx sets X-Real-IP; fall back to the peer."""
    return request.headers.get("X-Real-IP") or request.remote_addr or ""


def client_mac() -> str | None:
    return pbcommon.mac_for_ip(client_ip())


def touch_session() -> str | None:
    """Count this request as activity, resetting the idle timer."""
    mac = client_mac()
    if mac:
        pbcommon.record_activity(db(), mac)
    return mac


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
    touch_session()
    # No zip_file/grace_minutes: the page offers per-photo saving only. The zip
    # itself still exists -- see /api/download-all.
    return render_template(
        "gallery.html",
        photos=list_photos(),
        heartbeat_seconds=int(CONFIG["session"]["heartbeat_interval_seconds"]),
    )


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
    touch_session()
    body = jsonify({
        "captive": True,
        # Where Android opens its login browser.
        "user-portal-url": f"http://{CONFIG['access_point']['address']}/",
        "can-extend-session": True,
    })
    body.headers["Content-Type"] = "application/captive+json"
    return body


@app.post("/api/heartbeat")
def heartbeat():
    """Called by the open gallery page; resets the soft-limit timer."""
    mac = touch_session()
    return jsonify({"ok": True, "identified": mac is not None})


@app.post("/api/download-all")
def download_all():
    """Register a grace window, then hand back the static zip URL.

    A ~500 MB transfer on this radio takes minutes and can easily outlive the
    page that started it -- phone locks, browser is backgrounded, heartbeats
    stop. Relying on the client-side heartbeat alone would kick the guest
    mid-download, so the grace window is recorded server-side instead.

    No longer reachable from the gallery: the "Download all" button was taken
    out, so guests save photos individually. Kept because the zip is still
    built by ingest.py and still served by nginx at /all-photos.zip, and
    anyone fetching that URL directly -- the operator collecting the evening's
    photos, most likely -- needs the same protection from the idle kick.
    """
    mac = touch_session()
    grace = float(CONFIG["session"]["bulk_download_grace_minutes"]) * 60
    until = pbcommon.grant_grace(db(), mac, grace) if mac else 0.0
    return jsonify({
        "ok": True,
        "url": "/all-photos.zip",
        "grace_seconds": int(grace) if mac else 0,
        "grace_until": until,
        "identified": mac is not None,
    })


@app.get("/api/status")
def status():
    """Small diagnostic view -- handy when tuning, not used by the page."""
    mac = client_mac()
    sessions = pbcommon.all_sessions(db())
    now = time.time()
    mine = sessions.get(mac or "", {})
    return jsonify({
        "ip": client_ip(),
        "mac": mac,
        "photos": len(list_photos()),
        "zip": zip_info(),
        "session": {
            "age_seconds": round(now - mine["first_seen"]) if mine else None,
            "idle_seconds": round(now - mine["last_activity"]) if mine else None,
            "grace_remaining": max(0, round(mine.get("grace_until", 0) - now)) if mine else 0,
        },
        "active_sessions": len(sessions),
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
