#!/usr/bin/env python3
"""Render the hostapd / dnsmasq / nginx / NetworkManager files from config.yaml.

Keeps config.yaml the single source of truth: the system files are generated
artefacts and carry a header saying so. Run via setup.sh, or directly after
editing config.yaml:

    sudo python3 render_configs.py --install
"""

from __future__ import annotations

import argparse
import ipaddress
import shutil
import sys
from pathlib import Path

import pbcommon

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "etc"

# template -> installed location
TARGETS = {
    "hostapd.conf.tmpl": Path("/etc/hostapd/photobooth.conf"),
    "dnsmasq-portal.conf.tmpl": Path("/etc/dnsmasq.d/photobooth.conf"),
    "nginx-portal.conf.tmpl": Path("/etc/nginx/sites-available/photobooth"),
    "nm-unmanaged.conf.tmpl": Path(
        "/etc/NetworkManager/conf.d/99-photobooth-unmanaged.conf"),
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
        "@ZIP_PATH@": str(gal["zip_path"]),
        "@APP_DIR@": str(HERE),
        "@PORTAL_BIND@": str(config["portal"]["bind"]),
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
    args = parser.parse_args()

    config = pbcommon.load_config()
    subs = substitutions(config)

    for name, system_path in TARGETS.items():
        template = TEMPLATES / name
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
            if target.exists() and "render_configs.py" not in target.read_text():
                shutil.copy2(target, target.with_suffix(target.suffix + ".pre-photobooth"))
        else:
            print(f"--- would write {system_path} ---")
            continue

        target.write_text(body)
        print(f"wrote {target}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
