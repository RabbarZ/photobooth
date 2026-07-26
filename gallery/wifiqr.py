"""Wi-Fi join QR codes.

Extracted from rotate_password.py so the payload format lives in exactly one
place: the rotator writes these, and the booth display renders the PNG.

    python3 wifiqr.py --show          # print the payload for the current pass
    python3 wifiqr.py --out /tmp/q.png --ssid Foo --passphrase secret123
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Characters that terminate or delimit fields inside a WIFI: payload. Any of
# these appearing in an SSID or passphrase must be backslash-escaped or the
# scanning phone parses the code into the wrong fields.
_SPECIAL = ("\\", ";", ",", ":", '"')


def escape(value: str) -> str:
    """Escape the WIFI: payload delimiters in an SSID or passphrase.

    Backslash first -- escaping it after the others would double-escape the
    backslashes this function just inserted.
    """
    for char in _SPECIAL:
        value = value.replace(char, "\\" + char)
    return value


def wifi_payload(ssid: str, passphrase: str, hidden: bool = False) -> str:
    """The string a phone's camera turns into a 'join this network' prompt."""
    payload = f"WIFI:T:WPA;S:{escape(ssid)};P:{escape(passphrase)};"
    if hidden:
        payload += "H:true;"
    return payload + ";"


def write_qr(path: Path, ssid: str, passphrase: str,
             box_size: int = 10, border: int = 2) -> Path:
    """Render the join QR to `path`, atomically.

    Atomic because the booth display polls this file: a half-written PNG would
    either fail to load or, worse, render as a corrupt code someone tries to
    scan. os.replace swaps it in one step.

    Low error correction is deliberate: it keeps the module count down, so the
    code stays chunky and scannable at the size shown on the booth screen.
    Wi-Fi payloads are short, and the screen is not a dirty warehouse label.
    """
    import qrcode  # imported late so --help works without the dependency

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L,
                       box_size=box_size, border=border)
    qr.add_data(wifi_payload(ssid, passphrase))
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        image.save(tmp)
        os.chmod(tmp, 0o644)          # the display runs as pi, not root
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def module_count(path: Path) -> int | None:
    """Modules across the generated PNG, including the quiet-zone border.

    The booth display scales this image, and scaling to a non-multiple of the
    module grid gives fractional modules, which phones find harder to read.
    Multiplying this number by an integer yields a crisp display size.
    """
    try:
        from PIL import Image
        with Image.open(path) as image:
            width = image.size[0]
    except Exception:
        return None
    # box_size is uniform, so the grid divides the width exactly.
    for box in range(1, 65):
        if width % box == 0 and 21 <= width // box <= 177:
            return width // box
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--ssid")
    parser.add_argument("--passphrase")
    parser.add_argument("--out", type=Path, help="write the PNG here")
    parser.add_argument("--show", action="store_true",
                        help="print the payload instead of writing a PNG")
    args = parser.parse_args()

    ssid, passphrase = args.ssid, args.passphrase
    if ssid is None or passphrase is None:
        # Fall back to the live configuration and current rotated passphrase.
        import pbcommon
        config = pbcommon.load_config()
        ssid = ssid or config["access_point"]["ssid"]
        if passphrase is None:
            try:
                passphrase = Path(
                    config["password"]["password_path"]).read_text().strip()
            except OSError as err:
                print(f"no current passphrase ({err}); pass --passphrase",
                      file=sys.stderr)
                return 1

    if args.show or not args.out:
        print(wifi_payload(ssid, passphrase))
        return 0

    path = write_qr(args.out, ssid, passphrase)
    modules = module_count(path)
    print(f"{path} ({modules} modules)" if modules else str(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
