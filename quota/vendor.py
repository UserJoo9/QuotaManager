"""MAC vendor lookup from the bundled IEEE OUI registry (offline).

A DHCP client often sends no hostname, so a device card falls back to
"Unnamed device". The first bytes of a MAC identify its manufacturer, so we
can at least tell *what* an unknown device is. The database is the compact
IEEE MA-L + MA-M + MA-S export at ``quota/oui.txt`` (see
``scripts/update_oui.py`` for provenance); it is loaded lazily on first use
and never touches the network. Lookup is longest-prefix: MA-S (36-bit, 9 hex)
first, then MA-M (28-bit, 7 hex), then MA-L (24-bit, 6 hex) — so a device
allocated from a medium/small block resolves even when its 24-bit parent OUI
is generic or unassigned.
"""

from __future__ import annotations

import pathlib
import re

_DATA = pathlib.Path(__file__).with_name("oui.txt")

# Trailing legal/org boilerplate stripped for display ("TP-LINK TECHNOLOGIES
# CO.,LTD." -> "TP-LINK"). Chained so a run of chunks comes off cleanly; the
# ``$`` anchor keeps it trailing-only, so "Apple, Inc. Computer Co." is
# untouched in the middle.
_LEGAL = (
    r"inc\.?|corp\.?|ltd\.?|llc\.?|gmbh\.?|co\.?,?\s*ltd\.?|"
    r"company|corporation|limited|electronics|technolog(?:y|ies)\.?|"
    r"holdings?|s\.?a\.?|s\.r\.l\.?|s\.p\.?a\.?|kk|incorporated"
)
_SUFFIX = re.compile(r"(?:,?\s+(?:" + _LEGAL + r"))+[.,\s]*$", re.IGNORECASE)

_vendors: dict[str, str] | None = None


def _load() -> dict[str, str]:
    global _vendors
    if _vendors is not None:
        return _vendors
    vendors: dict[str, str] = {}
    try:
        with _DATA.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                prefix, _, vendor = line.rstrip("\n").partition("\t")
                if len(prefix) in (6, 7, 9) and vendor:
                    vendors[prefix] = _display(vendor)
    except OSError:
        pass
    _vendors = vendors
    return vendors


def _display(vendor: str) -> str:
    """Strip the registry's legal boilerplate for the UI."""
    if not vendor:
        return ""
    cleaned = _SUFFIX.sub("", vendor).strip(" ,.")
    return cleaned or vendor


def vendor_for(mac: str) -> str:
    """Return a display-friendly vendor name for ``mac`` ('' when unknown).

    Longest-prefix match across the three IEEE allocation sizes — MA-S
    (36-bit, 9 hex), MA-M (28-bit, 7 hex), then MA-L (24-bit, 6 hex). The
    24-bit fallback keeps classic OUIs working; randomized /
    locally-administered MACs and non-IEEE prefixes simply return ''.
    """
    if not mac:
        return ""
    raw = "".join(ch for ch in mac.lower() if ch in "0123456789abcdef")
    if len(raw) < 6:
        return ""
    vendors = _load()
    for cut in (9, 7, 6):
        if len(raw) >= cut:
            name = vendors.get(raw[:cut])
            if name:
                return name
    return ""
