"""Shared config loading, client identification and hostapd control."""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
from pathlib import Path

import yaml

# The repo root. This module sits there, beside config.yaml, because it is the
# one thing wifi/, gallery/ and deploy/ all share. Their scripts put this
# directory on sys.path before importing it.
REPO_ROOT = Path(__file__).resolve().parent

CONFIG_PATH = Path(os.environ.get(
    "PHOTOBOOTH_CONFIG", REPO_ROOT / "config.yaml"))

DNSMASQ_LEASES = Path("/var/lib/misc/dnsmasq.leases")
MAC_RE = re.compile(r"^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$")


def load_config(path: Path | None = None) -> dict:
    # UTF-8 explicitly: the screen texts have umlauts and arrows, and under
    # systemd or sudo the locale can be plain ASCII.
    with open(path or CONFIG_PATH, encoding="utf-8") as handle:
        config = yaml.safe_load(handle)

    # A relative photo_dir is relative to the repo, not to whichever directory
    # a daemon happened to start in. Resolved here, once, so ingest, the portal
    # and the rendered nginx config all agree on the same absolute path.
    gallery = config.get("gallery") or {}
    photo_dir = gallery.get("photo_dir")
    if photo_dir and not Path(photo_dir).is_absolute():
        gallery["photo_dir"] = str(REPO_ROOT / photo_dir)
    return config



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
