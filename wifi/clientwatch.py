"""Live report of what each guest device is doing (main.py --watch-clients).

Exists because a guest's progress is invisible from the Pi: a phone can
associate and get a lease and still never reach the gallery, and no single log
distinguishes "they are saving photos" from "they joined and walked away".
The booth service runs with this on, so the report lands in
`journalctl -u photobooth`.

This watches four sources and prints one line per event:

  * hostapd_cli list_sta   -- who is associated
  * dnsmasq.leases         -- who has an address, and their hostname
  * the gallery's log      -- who opened the page
  * nginx's download log   -- who saved which photo

Android phones get a captive portal and should open the gallery by themselves
shortly after joining. iPhones do not: they join with the first QR and open
the gallery with the second. An iPhone that associates but never appears in
the gallery log has not scanned the second code, which points at the booth
screen. An Android phone that never appears points at the captive-portal
setup -- check /var/log/nginx/photobooth-probes.log.
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# pbcommon is shared with gallery/ and deploy/ and lives at the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pbcommon  # noqa: E402

# Which device made the request, inferred from the user agent. Ordered: the
# system-service agents are checked before the generic ones, so a request
# from the OS itself does not read as a guest opening the page.
AGENTS = [
    ("iOS system",  re.compile(r"CaptiveNetworkSupport|CFNetwork|Darwin", re.I)),
    ("iPhone",      re.compile(r"iPhone|iPad", re.I)),
    ("Android",     re.compile(r"Dalvik|Android", re.I)),
    ("Windows",     re.compile(r"NCSI|Microsoft", re.I)),
]

# Matches both gunicorn's default access log and nginx's "combined" format.
# Both start with the client address; for gunicorn that is nginx (127.0.0.1),
# so only the nginx download log carries the guest's real address.
LOG_LINE = re.compile(
    r'^(?P<ip>\S+) .*?"(?P<method>GET|POST|HEAD) (?P<path>\S+) [^"]*" '
    r'(?P<status>\d{3}) (?P<size>\d+) "(?P<referer>[^"]*)" "(?P<agent>[^"]*)"')

DOWNLOAD_LOG = "/var/log/nginx/photobooth-downloads.log"


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
        self._downloads: dict[str, int] = {}  # guest address -> photos saved

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

    # -- web requests --------------------------------------------------------

    def _follow(self, command: list[str], handle) -> None:
        """Run a log-following command and hand each parsed line to `handle`."""
        try:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True,
                                    encoding="utf-8", errors="replace")
        except OSError as err:
            self._say("[!]", f"cannot follow {command[-1]}: {err}")
            return
        try:
            for line in proc.stdout:
                if self._stop.is_set():
                    break
                match = LOG_LINE.search(line)
                if match:
                    handle(match)
        finally:
            proc.terminate()

    def _follow_portal(self) -> None:
        """The gallery app's access log: who opened the page."""
        def handle(match) -> None:
            who = _classify(match["agent"])
            path, status = match["path"], match["status"]
            if path == "/":
                # A guest scanned the gallery QR (or Android's portal opened
                # it) and the page loaded.
                self._opened.add(who)
                self._say("[web]", f"{who} opened the gallery -> {status}")
            elif self.verbose:
                self._say("[web]", f"{who} {path} -> {status}")

        self._follow(["journalctl", "-u", "photobooth-portal", "-f", "-n", "0",
                      "--no-pager", "-o", "cat"], handle)

    def _follow_downloads(self) -> None:
        """nginx's download log: who saved which photo."""
        def handle(match) -> None:
            if match["status"] not in ("200", "206"):
                return
            ip = match["ip"]
            self._downloads[ip] = self._downloads.get(ip, 0) + 1
            name = match["path"].rsplit("/", 1)[-1]
            self._say("[dl]", f"{_classify(match['agent'])} {ip} saved {name}")

        # -F: keep following across logrotate, and wait if the file does not
        # exist yet (nginx creates it on the first download).
        self._follow(["tail", "-F", "-n", "0", DOWNLOAD_LOG], handle)

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        for target in (self._poll_clients, self._follow_portal,
                       self._follow_downloads):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()

    def summary(self) -> None:
        """Verdict printed on exit -- the bit that answers 'did it work?'."""
        lines = ["", "  -- guest Wi-Fi summary " + "-" * 40]
        if not self._known_leases:
            lines.append("  No device ever got an address. Check the passphrase/QR.")
        else:
            for mac, ip in self._known_leases.items():
                saved = self._downloads.get(ip, 0)
                lines.append(f"    {mac}  {ip}  {saved} photo(s) saved")
            if self._opened:
                lines.append("  The gallery was opened by: "
                             + ", ".join(sorted(self._opened)))
            else:
                lines += [
                    "  Nobody opened the gallery.",
                    "  iPhones need the second QR on the booth screen. Check that",
                    "  it is on screen and encodes the right address:",
                    "    python3 wifi/wifiqr.py --url http://<ip>/ --show",
                    "  Android should open it by itself; if not, check",
                    "    /var/log/nginx/photobooth-probes.log",
                ]
        # One flushed write: under systemd stdout is a pipe, and this runs on
        # the way out, when a buffered tail could be lost.
        print("\n".join(lines), flush=True)


def preflight(config: dict) -> list[str]:
    """Cheap checks for the things that silently break a guest's visit.

    The captive portal must reach Android and only Android. Two installed
    dnsmasq settings would put a sign-in sheet on iPhones: resolving every name
    to the Pi, and sending option 114 without the android tag. Option 114 with
    the tag is what the template sends, and is correct.
    """
    problems = []
    rerender = "sudo python3 deploy/render_configs.py --install"

    conf = "/etc/dnsmasq.d/photobooth.conf"
    try:
        with open(conf) as handle:
            # Directives only: the template's comments mention these options
            # by name, and matching those would report a fault that is not there.
            directives = [line.strip() for line in handle.read().splitlines()
                          if line.strip() and not line.lstrip().startswith("#")]
    except OSError:
        return [f"{conf} missing -- run deploy/setup.sh"]
    text = "\n".join(directives)

    if "address=/#/" in text:
        problems.append(
            "dnsmasq resolves every name to the Pi (address=/#/), which puts "
            f"a sign-in sheet on iPhones. Re-render the configs: {rerender}")

    def sends_114_to_everyone(line: str) -> bool:
        key, _, value = line.partition("=")
        if key not in ("dhcp-option", "dhcp-option-force"):
            return False
        fields = [field.strip() for field in value.split(",")]
        return "114" in fields and not any(
            field.startswith("tag:") for field in fields)

    if any(sends_114_to_everyone(line) for line in directives):
        problems.append(
            "dnsmasq advertises a captive-portal URL (DHCP option 114) to "
            "every client, not just Android, so iPhones will open a sign-in "
            f"sheet. Re-render the configs: {rerender}")
    if not any(f"address=/{name}/" in text
               for name in ("photobooth", "photobooth.local")):
        problems.append(
            "the photobooth hostname is not resolvable -- guests can still use "
            "the IP, but the QR is the only way in. Re-render the configs")

    ok, out = pbcommon.hostapd_cli(config, "ping")
    if not ok or "PONG" not in out:
        problems.append("hostapd control interface unreachable -- "
                        "passphrase rotation will not work")
    return problems
