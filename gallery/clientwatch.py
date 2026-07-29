"""Live report of what each guest device is doing, for the booth console.

Exists because a guest's progress is invisible from the Pi: a phone can
associate and get a lease and still never reach the gallery, and nothing in
the normal logs distinguishes "they are browsing photos" from "they joined and
walked away".

This watches three sources and prints one line per event:

  * hostapd_cli list_sta   -- who is associated
  * dnsmasq.leases         -- who has an address, and their hostname
  * the gallery's own log  -- who opened the page, and who is downloading

There is no captive portal, so nothing opens on a guest's phone by itself.
The two steps to watch for are therefore independent: joining the Wi-Fi (the
first QR) and opening the gallery (the second). A device that associates but
never appears in the gallery log has not scanned the second code -- which
points at the booth screen, not at anything the Pi is serving.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from datetime import datetime

import pbcommon

# Which device made the request, inferred from the user agent. Ordered: the
# system-service agents are checked before the generic ones, because an OS
# connectivity check still fires on this network -- it simply fails now, and
# labelling it separately keeps it from reading as a guest opening the page.
AGENTS = [
    ("iOS system",  re.compile(r"CaptiveNetworkSupport|CFNetwork|Darwin", re.I)),
    ("iPhone",      re.compile(r"iPhone|iPad", re.I)),
    ("Android",     re.compile(r"Dalvik|Android", re.I)),
    ("Windows",     re.compile(r"NCSI|Microsoft", re.I)),
]

LOG_LINE = re.compile(
    r'"(?P<method>GET|POST|HEAD) (?P<path>\S+) [^"]*" (?P<status>\d{3}) '
    r'(?P<size>\d+) "(?P<referer>[^"]*)" "(?P<agent>[^"]*)"')


def _classify(agent: str) -> str:
    for name, pattern in AGENTS:
        if pattern.search(agent):
            return name
    return "browser"


def _stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


class ClientWatch:
    """Background reporter. Start it, and it prints until stopped."""

    def __init__(self, config: dict, verbose: bool = False) -> None:
        self.config = config
        self.verbose = verbose
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._known_stations: set[str] = set()
        self._known_leases: dict[str, str] = {}
        self._opened: set[str] = set()

    # -- output --------------------------------------------------------------

    def _say(self, icon: str, message: str) -> None:
        # Leading newline: the booth writes countdown ticks with no trailing
        # newline ("3... 2... "), so without this the two interleave on one
        # line and both become unreadable.
        print(f"\n  {_stamp()} {icon} {message}", flush=True)

    # -- association + leases ------------------------------------------------

    def _poll_clients(self) -> None:
        while not self._stop.is_set():
            try:
                stations = pbcommon.associated_stations(self.config)
                for mac in stations - self._known_stations:
                    self._say("[+]", f"{mac} associated")
                for mac in self._known_stations - stations:
                    self._say("[-]", f"{mac} left")
                    self._opened.discard(mac)
                self._known_stations = stations

                for ip, mac, name in self._leases():
                    if self._known_leases.get(mac) != ip:
                        self._known_leases[mac] = ip
                        label = f" ({name})" if name and name != "*" else ""
                        self._say("[ip]", f"{mac} -> {ip}{label}")
            except Exception as err:
                if self.verbose:
                    self._say("[!]", f"client poll: {err}")
            self._stop.wait(2.0)

    def _leases(self):
        try:
            for line in pbcommon.DNSMASQ_LEASES.read_text().splitlines():
                parts = line.split()
                if len(parts) >= 4:
                    yield parts[2], parts[1].lower(), parts[3]
        except OSError:
            return

    # -- portal requests -----------------------------------------------------

    def _follow_portal(self) -> None:
        """Tail the portal unit's journal and report captive-portal traffic."""
        try:
            proc = subprocess.Popen(
                ["journalctl", "-u", "photobooth-portal", "-f", "-n", "0",
                 "--no-pager", "-o", "cat"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except OSError as err:
            self._say("[!]", f"cannot follow portal log: {err}")
            return

        try:
            for line in proc.stdout:
                if self._stop.is_set():
                    break
                match = LOG_LINE.search(line)
                if not match:
                    continue
                who = _classify(match["agent"])
                path, status = match["path"], match["status"]

                if path == "/":
                    # The event worth seeing now: a guest scanned the gallery
                    # QR and the page loaded.
                    self._opened.add(who)
                    self._say("[web]", f"{who} opened the gallery -> {status} "
                                       f"({match['size']}B)")
                elif path.startswith(("/photos/", "/all-photos.zip")):
                    self._say("[web]", f"{who} downloading {path} -> {status}")
                elif self.verbose:
                    self._say("[web]", f"{who} {path} -> {status}")
        finally:
            proc.terminate()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        for target in (self._poll_clients, self._follow_portal):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()

    def summary(self) -> None:
        """Verdict printed on exit -- the bit that answers 'did it work?'."""
        print("\n  -- guest Wi-Fi summary " + "-" * 40)
        if not self._known_leases:
            print("  No device ever got an address. Check the passphrase/QR.")
            return
        for mac, ip in self._known_leases.items():
            print(f"    {mac}  {ip}")
        if self._opened:
            print("  The gallery was opened by: " + ", ".join(sorted(self._opened)))
        else:
            print("  Nobody opened the gallery.")
            print("  There is no captive portal, so nothing opens by itself --")
            print("  a guest has to scan the second QR on the booth screen.")
            print("  Check that it is on screen and encodes the right address:")
            print("    python3 wifiqr.py --url http://<ip>/ --show")


def preflight(config: dict) -> list[str]:
    """Cheap checks for the things that silently break a guest's visit.

    The captive-portal checks that used to live here are gone with the portal
    itself: option 114 and the `local=/#/` AAAA workaround existed only to make
    a sign-in sheet appear, and their absence is now correct rather than a
    fault to report.
    """
    problems = []

    conf = "/etc/dnsmasq.d/photobooth.conf"
    try:
        with open(conf) as handle:
            # Directives only. The template documents the removed captive
            # options by name in its comments, and matching those reported the
            # portal as still present on a config that had none.
            text = "\n".join(line for line in handle.read().splitlines()
                             if not line.lstrip().startswith("#"))
    except OSError:
        return [f"{conf} missing -- run setup.sh"]

    # The installed file predating the portal removal is the likely surprise:
    # it still hijacks DNS and still advertises a portal, so phones open a
    # sign-in sheet that nothing serves any more.
    if "address=/#/" in text:
        problems.append(
            "dnsmasq still resolves every name to the Pi (address=/#/), left "
            "over from the captive portal. Re-render the configs: "
            "sudo python3 render_configs.py --install")
    if "114" in text:
        problems.append(
            "dnsmasq still advertises a captive-portal URL (DHCP option 114), "
            "so phones will look for a sign-in page that no longer exists. "
            "Re-render the configs: sudo python3 render_configs.py --install")
    if not any(f"address=/{name}/" in text
               for name in ("photobooth", "photobooth.local")):
        problems.append(
            "the photobooth hostname is not resolvable -- guests can still use "
            "the IP, but the QR is the only way in. Re-render the configs")

    ok, out = pbcommon.hostapd_cli(config, "ping")
    if not ok or "PONG" not in out:
        problems.append("hostapd control interface unreachable -- "
                        "session limits and rotation will not work")
    return problems
