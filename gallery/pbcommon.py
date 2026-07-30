"""Shared config loading, client identification and hostapd control.

There is no session database here any more. It existed to drive the hard and
soft session limits, which were removed -- see the note in config.yaml.
"""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
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
