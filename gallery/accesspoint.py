"""Bring the photobooth access point up and down.

Replaces the old activate.sh / deactivate.sh / ap-interface.sh trio. One
implementation, used by both callers:

  * photobooth-ap.service runs `accesspoint.py up` / `down` at boot, so a
    reboot restores the AP instead of leaving hostapd broadcasting an SSID
    with no DHCP behind it.
  * main.py calls start()/stop() around the booth loop.

Only the configured AP interface is ever touched. NetworkManager is never
reloaded or restarted -- on this box the management link and any SSH session
ride on eth0, and bouncing NM would take them down with it.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

import pbcommon

log = logging.getLogger("accesspoint")

# Started in this order, stopped in reverse. hostapd must own the radio before
# dnsmasq binds to the interface address, and the guest-facing daemons come
# last because they are useless until the network exists.
RADIO_UNITS = ("hostapd", "dnsmasq")
GUEST_UNITS = ("photobooth-rotate", "photobooth-sessions")
# Never stopped by us: these serve the gallery over whatever network the Pi is
# on and are harmless (and useful) with the AP down.
ALWAYS_UNITS = ("photobooth-portal", "photobooth-ingest", "nginx")


def _run(*args: str, check: bool = False, timeout: float = 30.0):
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout, check=check)
    except FileNotFoundError as err:
        log.debug("%s not available: %s", args[0], err)
    except subprocess.SubprocessError as err:
        log.debug("%s failed: %s", args[0], err)
    return None


def _systemctl(action: str, *units: str) -> None:
    for unit in units:
        result = _run("systemctl", action, unit)
        ok = result is not None and result.returncode == 0
        log.log(logging.INFO if ok else logging.WARNING,
                "systemctl %s %s%s", action, unit, "" if ok else " FAILED")


def unit_active(unit: str) -> bool:
    result = _run("systemctl", "is-active", "--quiet", unit)
    return result is not None and result.returncode == 0


class AccessPoint:
    """The photobooth AP: interface address plus hostapd and dnsmasq."""

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or pbcommon.load_config()
        ap = self.config["access_point"]
        self.interface = ap["interface"]
        self.address = ap["address"]
        self.prefix = int(ap["prefix_length"])
        self.ssid = ap["ssid"]

    # -- interface -----------------------------------------------------------

    def _release_from_networkmanager(self) -> None:
        """Stop NM managing the AP interface, and only that interface.

        Without this NM keeps scanning and trying to reassociate as a station
        on the same radio, and hostapd loses the interface seconds after it
        starts. `nmcli device set` acts immediately; the conf.d file installed
        by setup.sh is what makes it survive a reboot.
        """
        _run("nmcli", "device", "set", self.interface, "managed", "no")
        _run("rfkill", "unblock", "wlan")
        _run("nmcli", "radio", "wifi", "on")
        time.sleep(1)
        # Enabling the radio can make NM re-evaluate the device, so say it again.
        _run("nmcli", "device", "set", self.interface, "managed", "no")
        # wpa_supplicant holds the radio in station mode and would fight
        # hostapd. Only the instance bound to this interface is killed.
        _run("pkill", "-f", f"wpa_supplicant.*{self.interface}")

    def has_address(self) -> bool:
        result = _run("ip", "-4", "addr", "show", "dev", self.interface)
        return bool(result and f"{self.address}/{self.prefix}" in result.stdout)

    def up(self) -> None:
        """Give the interface its static address. Idempotent."""
        self._release_from_networkmanager()
        _run("ip", "link", "set", self.interface, "up")
        if self.has_address():
            log.info("%s already %s/%d", self.interface, self.address, self.prefix)
            return
        _run("ip", "addr", "flush", "dev", self.interface)
        _run("ip", "addr", "add", f"{self.address}/{self.prefix}",
             "dev", self.interface)
        # No default route is added: the photobooth network has no upstream,
        # and leaving the existing default route alone is what keeps the Pi's
        # own connectivity (and any SSH session) working.
        log.info("%s is %s/%d", self.interface, self.address, self.prefix)

    def down(self) -> None:
        _run("ip", "addr", "flush", "dev", self.interface)
        _run("ip", "link", "set", self.interface, "down")
        log.info("%s released", self.interface)

    def give_back_to_networkmanager(self) -> None:
        Path("/etc/NetworkManager/conf.d/"
             "99-photobooth-unmanaged.conf").unlink(missing_ok=True)
        _run("nmcli", "device", "set", self.interface, "managed", "yes")
        _run("ip", "link", "set", self.interface, "up")
        log.info("%s handed back to NetworkManager", self.interface)

    # -- whole stack ---------------------------------------------------------

    def start(self, guests: bool = True) -> bool:
        """Bring up the AP. True if hostapd and dnsmasq are both running."""
        self.up()
        _systemctl("start", *RADIO_UNITS)
        time.sleep(2)

        failed = [u for u in RADIO_UNITS if not unit_active(u)]
        if failed:
            log.error("did not start: %s", ", ".join(failed))
            for unit in failed:
                result = _run("journalctl", "-u", unit, "-n", "12", "--no-pager")
                if result:
                    log.error("%s:\n%s", unit, result.stdout.strip())
            return False

        if guests:
            _systemctl("restart", *GUEST_UNITS)
        self.write_portal_qr()
        log.info('AP "%s" up on %s', self.ssid, self.address)
        return True

    def write_portal_qr(self) -> None:
        """Draw the QR that opens the gallery directly.

        Written here rather than by the rotation timer because the URL is
        fixed: it only depends on the interface address, which is settled by
        the time the AP is up. Never fatal -- a missing portal QR costs one
        convenience, an aborted AP costs the whole booth.
        """
        path = self.config["password"].get("portal_qr_path")
        if not path:
            return
        try:
            import wifiqr
            wifiqr.write_url_qr(Path(path), f"http://{self.address}/")
        except Exception as err:
            log.warning("could not write the portal QR to %s: %s", path, err)

    def stop(self, release: bool = False) -> None:
        """Stop the AP. `release` also hands the interface back to NM."""
        _systemctl("stop", *reversed(GUEST_UNITS))
        _systemctl("stop", *reversed(RADIO_UNITS))
        self.down()
        if release:
            self.give_back_to_networkmanager()
        log.info("AP stopped")

    def status(self) -> dict:
        return {
            "ssid": self.ssid,
            "interface": self.interface,
            "address": self.address if self.has_address() else None,
            **{unit: unit_active(unit)
               for unit in RADIO_UNITS + GUEST_UNITS + ALWAYS_UNITS},
        }

    def __enter__(self) -> "AccessPoint":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("action",
                        choices=["up", "down", "start", "stop", "status"],
                        help="up/down: interface address only (systemd uses "
                             "these). start/stop: the whole AP stack.")
    parser.add_argument("--release", action="store_true",
                        help="with stop: hand the interface back to NetworkManager")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = AccessPoint()

    if args.action == "up":
        ap.up()
    elif args.action == "down":
        ap.down()
    elif args.action == "start":
        return 0 if ap.start() else 1
    elif args.action == "stop":
        ap.stop(release=args.release)
    else:
        for key, value in ap.status().items():
            print(f"  {key:22s} {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
