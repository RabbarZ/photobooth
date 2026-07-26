"""Shared config loading, client identification and session state.

State lives in SQLite on /run (tmpfs) rather than a file on the SD card: it is
rewritten every few seconds and none of it needs to survive a reboot, so
keeping it off the card avoids pointless flash wear.
"""

from __future__ import annotations

import ipaddress
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

import yaml

CONFIG_PATH = Path(os.environ.get(
    "PHOTOBOOTH_CONFIG", Path(__file__).resolve().parent / "config.yaml"))

DNSMASQ_LEASES = Path("/var/lib/misc/dnsmasq.leases")
MAC_RE = re.compile(r"^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$")


# Runtime overrides, written by main.py from its command line and merged on top
# of config.yaml. On tmpfs so they vanish at reboot: a timer you passed for one
# evening should not quietly become the permanent default.
OVERRIDE_PATH = Path("/run/photobooth/overrides.yaml")


def _merge(base: dict, overlay: dict) -> dict:
    """Recursive dict merge; overlay wins at the leaves."""
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: Path | None = None, use_overrides: bool = True) -> dict:
    with open(path or CONFIG_PATH) as handle:
        config = yaml.safe_load(handle)
    if use_overrides and OVERRIDE_PATH.exists():
        try:
            overlay = yaml.safe_load(OVERRIDE_PATH.read_text()) or {}
            config = _merge(config, overlay)
        except (OSError, yaml.YAMLError):
            pass  # a corrupt override must never stop the daemons starting
    return config


def write_overrides(overlay: dict) -> Path:
    """Persist runtime overrides for the daemons to pick up on restart."""
    OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OVERRIDE_PATH.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(overlay, default_flow_style=False))
    os.replace(tmp, OVERRIDE_PATH)
    os.chmod(OVERRIDE_PATH, 0o644)
    return OVERRIDE_PATH


def clear_overrides() -> None:
    OVERRIDE_PATH.unlink(missing_ok=True)


def state_path(config: dict) -> Path:
    return Path(config["portal"]["state_dir"]) / "sessions.db"


# --------------------------------------------------------------------------
# session state
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    mac           TEXT PRIMARY KEY,
    first_seen    REAL NOT NULL,
    last_activity REAL NOT NULL,
    grace_until   REAL NOT NULL DEFAULT 0
);
"""


def connect(config: dict) -> sqlite3.Connection:
    """Open the state database, creating it if needed.

    WAL plus a busy timeout so the portal (several gunicorn workers) and the
    session manager can touch it concurrently without tripping over each other.
    """
    path = state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    _share_state_files(path)
    return conn


def _share_state_files(path: Path) -> None:
    """Keep the state DB writable by both the portal and the session manager.

    They run as different users -- the portal as `pi`, the session manager as
    root because it drives hostapd_cli. Whichever creates the database first
    owns it, and SQLite needs write access to the -wal and -shm sidecars too,
    not just the .db. Left alone, a root-created database makes every portal
    request fail with "attempt to write a readonly database", which surfaces
    to guests as an Internal Server Error on the gallery page.

    Only root can hand ownership over, so this is a no-op for the portal.
    """
    if os.geteuid() != 0:
        return
    try:
        owner = path.parent.stat()
        for suffix in ("", "-wal", "-shm"):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists():
                os.chown(sidecar, owner.st_uid, owner.st_gid)
                os.chmod(sidecar, 0o664)
    except OSError:
        pass  # best effort: never stop the daemon over file ownership


def record_activity(conn: sqlite3.Connection, mac: str, now: float | None = None) -> None:
    """Note a portal request from `mac`, creating the session if it is new."""
    now = now if now is not None else time.time()
    with conn:
        conn.execute(
            "INSERT INTO sessions (mac, first_seen, last_activity) VALUES (?, ?, ?) "
            "ON CONFLICT(mac) DO UPDATE SET last_activity=excluded.last_activity",
            (mac, now, now))


def ensure_session(conn: sqlite3.Connection, mac: str, now: float | None = None) -> None:
    """Register an associated station without counting it as portal activity.

    Used by the session manager: association starts the hard-limit clock, but
    a device that never opens the gallery should still hit the idle limit.
    """
    now = now if now is not None else time.time()
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO sessions (mac, first_seen, last_activity) "
            "VALUES (?, ?, ?)", (mac, now, now))


def grant_grace(conn: sqlite3.Connection, mac: str, seconds: float,
                now: float | None = None) -> float:
    """Suppress the idle kick for `mac` for `seconds`. Returns the deadline.

    Never shortens an existing grace window.
    """
    now = now if now is not None else time.time()
    until = now + seconds
    with conn:
        conn.execute(
            "INSERT INTO sessions (mac, first_seen, last_activity, grace_until) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(mac) DO UPDATE SET "
            "  grace_until = MAX(sessions.grace_until, excluded.grace_until),"
            "  last_activity = excluded.last_activity",
            (mac, now, now, until))
    return until


def all_sessions(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT mac, first_seen, last_activity, grace_until FROM sessions").fetchall()
    return {r[0]: {"first_seen": r[1], "last_activity": r[2], "grace_until": r[3]}
            for r in rows}


def forget(conn: sqlite3.Connection, macs) -> None:
    macs = [(m,) for m in macs]
    if macs:
        with conn:
            conn.executemany("DELETE FROM sessions WHERE mac = ?", macs)


# --------------------------------------------------------------------------
# IP -> MAC
# --------------------------------------------------------------------------

def _leases_map() -> dict[str, str]:
    """IP -> MAC from the dnsmasq lease file."""
    mapping: dict[str, str] = {}
    try:
        for line in DNSMASQ_LEASES.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 3 and MAC_RE.match(parts[1].lower()):
                mapping[parts[2]] = parts[1].lower()
    except (OSError, ValueError):
        pass
    return mapping


def _neigh_map() -> dict[str, str]:
    """IP -> MAC from the kernel neighbour table, as a fallback.

    A client with a static address never appears in the lease file, but it does
    show up here once it has exchanged any traffic with us.
    """
    mapping: dict[str, str] = {}
    try:
        out = subprocess.run(["ip", "neigh", "show"], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return mapping
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "lladdr" and MAC_RE.match(parts[4].lower()):
            mapping[parts[0]] = parts[4].lower()
    return mapping


def mac_for_ip(ip: str) -> str | None:
    """Best-effort MAC for a client IP. Leases first, neighbour table second."""
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return None
    return _leases_map().get(ip) or _neigh_map().get(ip)


# --------------------------------------------------------------------------
# hostapd control interface
# --------------------------------------------------------------------------

def hostapd_cli(config: dict, *args: str, timeout: float = 10.0) -> tuple[bool, str]:
    """Run hostapd_cli against the configured interface.

    Returns (ok, output). Never raises -- a missing or dead hostapd must not
    take the caller down with it.
    """
    cmd = ["hostapd_cli", "-i", config["access_point"]["interface"], *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return False, "hostapd_cli not installed"
    except subprocess.SubprocessError as err:
        return False, str(err)
    out = (proc.stdout or "") + (proc.stderr or "")
    ok = proc.returncode == 0 and "FAIL" not in out.upper()
    return ok, out.strip()


def associated_stations(config: dict) -> set[str]:
    """MACs currently associated with the AP."""
    ok, out = hostapd_cli(config, "list_sta")
    if not ok:
        return set()
    return {line.strip().lower() for line in out.splitlines()
            if MAC_RE.match(line.strip().lower())}


def deauthenticate(config: dict, mac: str) -> bool:
    ok, _ = hostapd_cli(config, "deauthenticate", mac)
    return ok
