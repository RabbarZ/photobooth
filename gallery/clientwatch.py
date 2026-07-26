"""Live report of what each guest device is doing, for the booth console.

Exists because captive-portal problems are invisible from the Pi: a phone can
associate, get a lease, and still never show its login sheet, and nothing in
the normal logs distinguishes "the sheet opened" from "the guest gave up and
typed the address by hand".

This watches three sources and prints one line per event:

  * hostapd_cli list_sta   -- who is associated
  * dnsmasq.leases         -- who has an address, and their hostname
  * the portal's own log   -- which probes each device made, and what it got

The probe requests are the interesting part. An iOS device that is detecting
the portal correctly fetches /hotspot-detect.html *by itself*, with a
CaptiveNetworkSupport user agent, within a second or two of joining. If that
never appears, the phone is not probing -- which points at DHCP option 114 or
the device's own settings, not at anything nginx is serving.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from datetime import datetime

import pbcommon

# Who made the request, inferred from the user agent.
AGENTS = [
    ("iOS probe",   re.compile(r"CaptiveNetworkSupport", re.I)),
    ("iOS sheet",   re.compile(r"iPhone|iPad", re.I)),
    ("iOS system",  re.compile(r"CFNetwork|Darwin", re.I)),
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
        self._probed: set[str] = set()

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
                    self._probed.discard(mac)
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

                if path == "/captive-api":
                    # RFC 8908: this is the signal that actually opens the
                    # sheet on iOS 14+/Android 11+.
                    self._probed.add(who)
                    self._say("[iOS]", f"captive-api -> {status}  "
                                       f"<< RFC 8908 query, sheet should open")
                elif who == "iOS probe" and "hotspot-detect" in path:
                    self._probed.add(who)
                    self._say("[iOS]", f"probe {path} -> {status} "
                                       f"({match['size']}B)  << legacy probe")
                elif who == "iOS probe":
                    # The CNA fetching a page is NOT detection -- it means the
                    # sheet is already open, or the user navigated there.
                    self._say("[iOS]", f"CNA fetched {path} -> {status} "
                                       f"(not a detection probe)")
                elif who == "iOS sheet":
                    self._say("[iOS]", f"login sheet opened {path} -> {status}")
                elif who in ("Android", "Windows"):
                    self._say(f"[{who[:3]}]", f"probe {path} -> {status}")
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
        if self._probed:
            print("  Captive detection was observed for: "
                  + ", ".join(sorted(self._probed)))
        else:
            print("  No captive-api query or legacy probe was seen.")
            print("  If an iPhone joined and its sheet never appeared, that")
            print("  points at DHCP option 114 or iCloud Private Relay --")
            print("  not at nginx, which only answers probes that arrive.")


def preflight(config: dict) -> list[str]:
    """Cheap checks for the things that silently break captive detection."""
    problems = []

    conf = "/etc/dnsmasq.d/photobooth.conf"
    try:
        with open(conf) as handle:
            text = handle.read()
    except OSError:
        return [f"{conf} missing -- run setup.sh"]

    # Two independent ways option 114 silently fails, both invisible in the
    # dnsmasq log:
    #
    #   1. Unquoted -- dnsmasq encodes the wrong option type and iOS discards it.
    #   2. Plain dhcp-option -- only sent if the client listed 114 in its
    #      Parameter Request List (option 55), and iOS does not always ask.
    #      -force sends it unconditionally.
    if 'dhcp-option-force=114,"' not in text:
        hint = ('it is not sent with -force, so it only reaches clients that '
                'explicitly request it'
                if "114" in text else "it is missing entirely")
        problems.append(
            f'DHCP option 114 will not reach iOS: {hint}. It is the primary '
            'captive signal on iOS 14+. Expected: '
            'dhcp-option-force=114,"http://<ip>/captive-api"')
    if "local=/#/" not in text:
        problems.append(
            "dnsmasq will answer AAAA with REFUSED, which reads to iOS as a "
            "broken resolver rather than a captive network. Add local=/#/")

    ok, out = pbcommon.hostapd_cli(config, "ping")
    if not ok or "PONG" not in out:
        problems.append("hostapd control interface unreachable -- "
                        "session limits and rotation will not work")
    return problems
