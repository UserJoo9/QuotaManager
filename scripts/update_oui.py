"""Regenerate quota/oui.txt from the IEEE OUI registry (offline vendor lookup).

Usage:
    python scripts/update_oui.py            # download all three IEEE registries
                                            # (MA-L / MA-M / MA-S) and rewrite
                                            # quota/oui.txt
    python scripts/update_oui.py FILE       # parse an existing oui.txt instead
                                            # of downloading (FILE = raw IEEE)

The output is a compact tab-separated file: ``<hex-prefix>\\t<vendor>``, one
entry per line. The prefix is 6 hex digits (24-bit MA-L), 7 hex digits (28-bit
MA-M) or 9 hex digits (36-bit MA-S); ``quota/vendor.py`` does a longest-prefix
lookup, so a device allocated from an MA-M/MA-S block resolves to the right
vendor even when its 24-bit parent OUI is generic or unassigned. It ships in
the repo so the gateway can resolve MAC manufacturers with no network and no
extra dependencies (see ``quota/vendor.py``). The IEEE registries are public
data and freely redistributable.
"""

from __future__ import annotations

import csv
import io
import pathlib
import sys
import urllib.request

# The three IEEE registries cover every registered allocation size:
#   MA-L (OUI, 24-bit, 6 hex)  -> https://standards-oui.ieee.org/oui/oui.csv
#   MA-M (OUI-28, 7 hex)       -> https://standards-oui.ieee.org/oui28/mam.csv
#   MA-S (OUI-36, 9 hex)       -> https://standards-oui.ieee.org/oui36/oui36.csv
# (note the MA-S file base name is ``oui36``, not ``ma-s``). The CSV
# ``assignment`` column holds the exact prefix, so no range arithmetic is
# needed. All three are aggregated into one oui.txt.
REGISTRIES = (
    "https://standards-oui.ieee.org/oui/oui.csv",
    "https://standards-oui.ieee.org/oui28/mam.csv",
    "https://standards-oui.ieee.org/oui36/oui36.csv",
)
OUT = pathlib.Path(__file__).resolve().parent.parent / "quota" / "oui.txt"
# IEEE rejects the default urllib user agent (HTTP 418); identify ourselves.
_HEADERS = {"User-Agent": "QuotaManager/1.0 (offline OUI refresh)"}


def parse(text: str) -> list[tuple[str, str]]:
    """Extract ``(prefix, vendor)`` pairs from one raw IEEE CSV export.

    Accepts the CSV ``registry,assignment,organizationName,...`` layout for all
    three registries (MA-L 6-hex, MA-M 7-hex, MA-S 9-hex assignments). Uses the
    ``csv`` module because vendor names are quoted and may contain commas
    (e.g. ``"DATA ELECTRONIC DEVICES, INC"``) — a naive split truncates them.
    """
    out: list[tuple[str, str]] = []
    rows = csv.reader(io.StringIO(text))
    for row in rows:
        if not row or row[0] not in ("MA-L", "MA-M", "MA-S"):
            continue
        prefix = row[1].strip().lower()
        vendor = row[2].strip()
        if (len(prefix) in (6, 7, 9)
                and all(c in "0123456789abcdef" for c in prefix)
                and vendor):
            out.append((prefix, vendor))
    return out


def main() -> None:
    if len(sys.argv) > 1:
        text = pathlib.Path(sys.argv[1]).read_text(
            encoding="utf-8", errors="replace")
        entries = parse(text)
    else:
        entries = []
        for url in REGISTRIES:
            print(f"downloading {url}")
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=60) as resp:
                text = resp.read().decode("utf-8", "replace")
            got = parse(text)
            print(f"  {len(got)} entries")
            entries.extend(got)
    lines = sorted(f"{p}\t{v}" for p, v in set(entries))
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    by_len = {6: 0, 7: 0, 9: 0}
    for p, _ in set(entries):
        by_len[len(p)] += 1
    print(f"wrote {OUT}: {len(lines)} entries "
          f"(MA-L {by_len[6]} / MA-M {by_len[7]} / MA-S {by_len[9]})")


if __name__ == "__main__":
    main()
