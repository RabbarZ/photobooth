#!/usr/bin/env python3
"""Enforce the two session limits by deauthenticating stations.

  HARD limit -- wall clock since first association.
  SOFT limit -- no portal request (page load or heartbeat) for this long,
                unless a bulk-download grace window is open.

There is deliberately no MAC blocklist. Deauth alone would let a device
reassociate immediately, so the gatekeeper is password rotation: a kicked
guest cannot rejoin until they rescan the QR. That keeps this daemon simple
and means a guest who genuinely deserves another turn just scans again.
"""

from __future__ import annotations

import logging
import signal
import sys
import time

import pbcommon

log = logging.getLogger("sessions")
_running = True


def _stop(_signum, _frame) -> None:
    global _running
    _running = False


def sweep(config: dict, conn) -> None:
    """One pass: reconcile state with reality, then kick who is over limit."""
    hard = float(config["session"]["hard_limit_minutes"]) * 60
    soft = float(config["session"]["soft_limit_minutes"]) * 60
    now = time.time()

    associated = pbcommon.associated_stations(config)

    # Association starts the hard-limit clock even for a device that never
    # opens the gallery -- otherwise idling squatters would be invisible.
    for mac in associated:
        pbcommon.ensure_session(conn, mac, now)

    sessions = pbcommon.all_sessions(conn)

    # Drop state for anyone no longer associated so a returning guest gets a
    # fresh allowance rather than inheriting an expired one.
    stale = set(sessions) - associated
    if stale:
        pbcommon.forget(conn, stale)
        log.info("forgot %d departed station(s)", len(stale))

    for mac in sorted(associated):
        entry = sessions.get(mac)
        if entry is None:
            continue  # created this pass; give it a full cycle
        age = now - entry["first_seen"]
        idle = now - entry["last_activity"]
        in_grace = now < entry["grace_until"]

        if age >= hard:
            reason = f"hard limit ({age / 60:.1f} min)"
        elif idle >= soft and not in_grace:
            reason = f"idle {idle / 60:.1f} min"
        elif idle >= soft and in_grace:
            log.debug("%s idle %.1f min but %.0fs of download grace left",
                      mac, idle / 60, entry["grace_until"] - now)
            continue
        else:
            continue

        if pbcommon.deauthenticate(config, mac):
            log.info("deauthenticated %s -- %s", mac, reason)
            pbcommon.forget(conn, [mac])
        else:
            # Leave the session in place and retry next pass rather than
            # forgetting a station we failed to kick.
            log.warning("failed to deauthenticate %s (%s)", mac, reason)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    config = pbcommon.load_config()
    interval = float(config["session"]["poll_interval_seconds"])
    conn = pbcommon.connect(config)

    log.info("hard=%s min soft=%s min grace=%s min poll=%ss",
             config["session"]["hard_limit_minutes"],
             config["session"]["soft_limit_minutes"],
             config["session"]["bulk_download_grace_minutes"], interval)

    while _running:
        try:
            sweep(config, conn)
        except Exception:
            # A transient failure (hostapd restarting, lease file mid-write)
            # must not kill the daemon and leave limits unenforced.
            log.exception("sweep failed; continuing")
        # Sleep in slices so SIGTERM is noticed promptly.
        deadline = time.time() + interval
        while _running and time.time() < deadline:
            time.sleep(min(0.5, max(0.0, deadline - time.time())))

    conn.close()
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
