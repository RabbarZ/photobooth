"""Captive portal that IS the gallery.

Deliberately never marks a client as authenticated. Every OS connectivity
probe gets a redirect instead of the success response it wants, so the device
stays in "login required" and the OS re-pops the portal when the phone wakes.
That is the intended re-entry path after a guest takes another photo, not a
bug to be fixed.

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


# iOS opens captive portals in the Captive Network Assistant, a restricted
# mini-browser that identifies itself with this token. It gives up and lets the
# user dismiss the sheet if the page is slow, and a full gallery is dozens of
# thumbnails over a saturated 2.4 GHz link. Serving it a light page makes the
# sheet appear reliably; the guest taps through to the real gallery in Safari,
# where downloads work properly anyway (the CNA cannot save files).
CNA_TOKEN = "CaptiveNetworkSupport"


def is_captive_browser() -> bool:
    """True when the request comes from a captive-portal mini-browser.

    Detecting this is fiddlier than it looks. Only the initial probe carries a
    CaptiveNetworkSupport User-Agent; once iOS opens the sheet, its page
    requests use an ordinary Safari-like UA with no marker at all. Matching
    solely on that token therefore sends the CNA the full gallery -- dozens of
    thumbnails over a busy 2.4 GHz link -- and iOS closes the sheet before it
    finishes loading, which looks exactly like detection failing.

    The reliable signal is the absence of a Referer combined with an Apple
    mobile UA: the sheet always opens a page cold, whereas a guest arriving in
    Safari has either typed the address or followed our link.
    """
    if request.headers.get("X-Captive-Probe"):
        return True
    agent = request.headers.get("User-Agent", "")
    if CNA_TOKEN in agent:
        return True
    # Apple mini-browser: iOS UA, no Referer, and not our own gallery link.
    apple_mobile = ("iPhone" in agent or "iPad" in agent) and "Safari/" not in agent
    return apple_mobile and not request.headers.get("Referer")


@app.route("/")
def gallery():
    touch_session()
    if is_captive_browser():
        # Deliberately no thumbnails: one small HTML response, nothing else to
        # fetch, so the sheet renders instantly instead of timing out.
        return render_template("welcome.html", photos=len(list_photos()))
    return render_template(
        "gallery.html",
        photos=list_photos(),
        zip_file=zip_info(),
        heartbeat_seconds=int(CONFIG["session"]["heartbeat_interval_seconds"]),
        grace_minutes=int(CONFIG["session"]["bulk_download_grace_minutes"]),
    )


@app.get("/captive-api")
def captive_api():
    """RFC 8908 Captive Portal API, advertised by DHCP option 114.

    Option 114 (RFC 8910) does NOT point at a login page -- it points at this
    JSON endpoint, and iOS 14+ / Android 11+ query it *instead of* running the
    old HTTP probe. Serving HTML here is why the sheet never opened: the OS
    fetched the URL, failed to parse a captive-status document, and gave up
    without ever deciding it was behind a portal.

    `captive: true` is what actually tells the device "you must log in", and
    it is what makes the sign-in sheet appear. We never flip it to false, so
    the device stays in the login-required state and re-opens the portal when
    it wakes -- the same re-entry behaviour the HTTP probes give us.
    """
    touch_session()
    body = jsonify({
        "captive": True,
        # Where to send the user. iOS opens this in the sheet.
        "user-portal-url": f"http://{CONFIG['access_point']['address']}/",
        # No venue-info-url: it adds a second fetch for no benefit here.
        "can-extend-session": True,
    })
    # The media type is mandatory; a generic application/json is ignored.
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
# captive-portal behaviour
# --------------------------------------------------------------------------
#
# nginx bounces probes aimed at other hostnames, but a probe can also arrive
# addressed straight at us (some clients retry against the portal host). These
# routes make sure such a request still gets a redirect rather than the success
# response that would clear the "login required" flag.

PROBE_PATHS = [
    "/generate_204",                    # Android / Chrome
    "/gen_204",
    "/hotspot-detect.html",             # iOS / macOS
    "/library/test/success.html",
    "/connecttest.txt",                 # Windows
    "/ncsi.txt",
    "/success.txt",                     # Firefox
    "/canonical.html",                  # Ubuntu / GNOME
]

for _path in PROBE_PATHS:
    app.add_url_rule(_path, f"probe{_path}", lambda: redirect("/", code=302),
                     methods=["GET", "HEAD"])


@app.errorhandler(404)
def catch_all(_err):
    """Anything unrecognised goes to the gallery, never a 404 dead end."""
    return redirect("/", code=302)


@app.after_request
def no_store(response):
    """Never let a probe response or the gallery be cached.

    A cached 204 or a cached "Success" page would let the OS decide it has
    internet, and the portal would stop re-opening.
    """
    # set(), not add: nginx also adds Cache-Control, and duplicate headers on
    # a captive-portal probe response are needless ambiguity.
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


if __name__ == "__main__":
    # Development only; production runs under gunicorn (see systemd unit).
    host, _, port = CONFIG["portal"]["bind"].partition(":")
    app.run(host=host, port=int(port), debug=False)
