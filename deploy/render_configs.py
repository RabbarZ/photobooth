#!/usr/bin/env python3
"""Render the hostapd / dnsmasq / nginx / NetworkManager files and systemd units.

Keeps config.yaml the single source of truth: the system files are generated
artefacts and carry a header saying so. The systemd units are rendered too,
because they need the absolute path of this checkout and must not hard-code
one. Run via setup.sh, or directly after editing config.yaml:

    sudo python3 deploy/render_configs.py --install
"""

from __future__ import annotations

import argparse
import grp
import ipaddress
import os
import pwd
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_DIR = HERE.parent
TEMPLATES = HERE / "etc"
UNITS = HERE / "systemd"
UNIT_TARGET_DIR = Path("/etc/systemd/system")

# pbcommon is shared with wifi/ and gallery/ and lives at the repo root.
sys.path.insert(0, str(REPO_DIR))

import pbcommon  # noqa: E402

# template -> installed location
TARGETS = {
    "hostapd.conf.tmpl": Path("/etc/hostapd/photobooth.conf"),
    "dnsmasq-portal.conf.tmpl": Path("/etc/dnsmasq.d/photobooth.conf"),
    "nginx-portal.conf.tmpl": Path("/etc/nginx/sites-available/photobooth"),
    "nm-unmanaged.conf.tmpl": Path(
        "/etc/NetworkManager/conf.d/99-photobooth-unmanaged.conf"),
}


def default_user() -> str:
    """The desktop user the booth runs for.

    Raspberry Pi OS has no fixed default user any more -- the Imager asks for
    one -- so it is never hard-coded. The person running `sudo` is the best
    guess, then whoever owns the checkout.
    """
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root":
        return sudo_user
    return pwd.getpwuid(REPO_DIR.stat().st_uid).pw_name


def user_substitutions(name: str) -> dict[str, str]:
    try:
        entry = pwd.getpwnam(name)
    except KeyError:
        raise SystemExit(f"no such user: {name}")
    if entry.pw_uid == 0:
        raise SystemExit("the booth user must not be root -- pass --user NAME")
    return {
        "@USER@": entry.pw_name,
        "@GROUP@": grp.getgrgid(entry.pw_gid).gr_name,
        "@UID@": str(entry.pw_uid),
        "@GID@": str(entry.pw_gid),
    }


def substitutions(config: dict) -> dict[str, str]:
    ap = config["access_point"]
    gal = config["gallery"]
    lim = config["limits"]

    network = ipaddress.ip_network(f"{ap['address']}/{ap['prefix_length']}",
                                   strict=False)
    channel = int(ap["channel"])
    if channel not in (1, 6, 11):
        print(f"warning: channel {channel} overlaps its neighbours; "
              f"1, 6 or 11 are the only non-overlapping 2.4 GHz choices",
              file=sys.stderr)

    return {
        "@INTERFACE@": str(ap["interface"]),
        "@SSID@": str(ap["ssid"]),
        "@CHANNEL@": str(channel),
        "@COUNTRY_CODE@": str(ap["country_code"]),
        "@MAX_NUM_STA@": str(int(ap["max_num_sta"])),
        "@ADDRESS@": str(ap["address"]),
        "@PREFIX@": str(ap["prefix_length"]),
        "@NETMASK@": str(network.netmask),
        "@DHCP_START@": str(ap["dhcp_range_start"]),
        "@DHCP_END@": str(ap["dhcp_range_end"]),
        "@DHCP_LEASE@": str(ap["dhcp_lease_time"]),
        "@PSK_FILE@": str(config["password"]["psk_file"]),
        "@PHOTO_DIR@": str(Path(gal["photo_dir"]).resolve()),
        "@THUMB_DIR@": str(gal["thumb_dir"]),
        # wifi/: the access point and rotator units run from it.
        "@WIFI_DIR@": str(REPO_DIR / "wifi"),
        # gallery/: nginx serves static/ from it, portal and ingest run in it.
        "@GALLERY_DIR@": str(REPO_DIR / "gallery"),
        # The checkout itself: main.py and the README live here.
        "@REPO_DIR@": str(REPO_DIR),
        "@PORTAL_BIND@": str(config["portal"]["bind"]),
        "@WORKERS@": str(max(1, int(config["portal"]["workers"]))),
        "@PER_DEVICE@": str(int(lim["per_device_downloads"])),
        "@GLOBAL@": str(int(lim["global_downloads"])),
        "@RATE@": str(int(lim["portal_rate_per_second"])),
        "@BURST@": str(int(lim["portal_rate_burst"])),
    }


def render(template: Path, subs: dict[str, str]) -> str:
    text = template.read_text()
    for key, value in subs.items():
        text = text.replace(key, value)
    leftover = [line for line in text.splitlines()
                if "@" in line and line.lstrip().startswith("@")]
    if leftover:
        raise SystemExit(f"{template.name}: unsubstituted placeholder: {leftover[0]}")
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--install", action="store_true",
                        help="write to the system paths (needs root)")
    parser.add_argument("--out-dir", type=Path,
                        help="render into this directory instead (for review)")
    parser.add_argument("--user", default=None,
                        help="desktop user the booth and gallery run as "
                             "(default: the sudo user, else the repo's owner)")
    args = parser.parse_args()

    config = pbcommon.load_config()
    subs = substitutions(config)
    subs.update(user_substitutions(args.user or default_user()))

    # (template, installed path, back up a foreign file first?)
    jobs = [(TEMPLATES / name, system_path, True)
            for name, system_path in TARGETS.items()]
    # Units are ours alone and systemd would try to make sense of a stray
    # backup beside them, so they are overwritten without one.
    jobs += [(unit, UNIT_TARGET_DIR / unit.name, False)
             for unit in sorted(UNITS.glob("photobooth-*.service"))]

    for template, system_path, backup in jobs:
        name = template.name
        if not template.exists():
            raise SystemExit(f"missing template: {template}")
        body = render(template, subs)

        if args.out_dir:
            # Name by template, not by basename: hostapd and dnsmasq both
            # install as "photobooth.conf", so using the basename here would
            # have one silently overwrite the other in review mode.
            target = args.out_dir / name.removesuffix(".tmpl")
            target.parent.mkdir(parents=True, exist_ok=True)
        elif args.install:
            target = system_path
            target.parent.mkdir(parents=True, exist_ok=True)
            # Keep one backup of anything we did not write ourselves.
            if (backup and target.exists()
                    and "render_configs.py" not in target.read_text()):
                shutil.copy2(target, target.with_suffix(target.suffix + ".pre-photobooth"))
        else:
            print(f"--- would write {system_path} ---")
            continue

        # newline="\n": these are Linux files, whatever platform renders them.
        target.write_text(body, newline="\n")
        print(f"wrote {target}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
