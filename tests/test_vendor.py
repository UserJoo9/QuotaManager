"""Unit tests for the bundled IEEE OUI vendor lookup (offline)."""

from __future__ import annotations

import pytest

from quota.vendor import _display, vendor_for


# -- lookup against the real bundled oui.txt -------------------------------

def test_known_apple_oui():
    # 3C:07:54 is an Apple OUI; the registry lists it as "Apple, Inc."
    assert vendor_for("3C:07:54:12:34:56") == "Apple"


def test_known_tp_link_oui():
    # 00:0A:EB -> TP-LINK TECHNOLOGIES CO.,LTD.
    assert vendor_for("00-0A-EB-AB-CD-EF") == "TP-LINK"


def test_known_cisco_oui():
    assert vendor_for("e8-0a-b9-00-11-22") == "Cisco Systems"


def test_known_ma_m_block():
    # C85CE2 (24-bit parent OUI) is an MA-M (28-bit) allocation:
    # C85CE27 -> SYNERGY SYSTEMS AND SOLUTIONS. The 24-bit OUI alone is not
    # enough to resolve it, so longest-prefix lookup must find the 7-hex entry.
    assert vendor_for("c8:5c:e2:7a:12:34") == "SYNERGY SYSTEMS AND SOLUTIONS"


def test_known_ma_s_block():
    # 8C1F64A (MA-M) / 8C1F64AF (MA-S) family: 8C1F64AFA -> DATA ELECTRONIC
    # DEVICES, INC (a 36-bit allocation). A MAC inside that small block
    # resolves by its 9-hex prefix, not the 24-bit OUI.
    assert vendor_for("8c:1f:64:af:a0:00") == "DATA ELECTRONIC DEVICES"


def test_ma_s_precedes_shorter_prefixes():
    # If the same bytes appear at different prefix lengths, the LONGEST
    # (most specific) registration wins. Mecco LLC holds the 36-bit block
    # 8C1F64D0F under the 24-bit OUI 8C1F64 (registered to a different
    # org) — the MA-S entry must win for a MAC inside its block.
    assert vendor_for("8c:1f:64:d0:f1:11") == "Mecco"


def test_unknown_oui_returns_empty():
    # F0:00:00 is not in the registry (and locally-administered bits aside)
    assert vendor_for("f0:00:00:00:00:00") == ""


def test_randomized_mac_returns_empty():
    # 02:xx is locally administered — no registered vendor for the OUI
    assert vendor_for("02:5b:3c:ab:cd:ef") == ""


def test_malformed_mac_returns_empty():
    assert vendor_for("") == ""
    assert vendor_for("zz:zz:zz") == ""
    assert vendor_for("12:34") == ""


def test_case_insensitive_and_separator_agnostic():
    assert vendor_for("A89352abcdef") == vendor_for("a8:93:52:ab:cd:ef")


# -- display cleanup --------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Apple, Inc.", "Apple"),
    ("Cisco Systems, Inc", "Cisco Systems"),
    ("Katun Corporation", "Katun"),
    ("TP-LINK TECHNOLOGIES CO.,LTD.", "TP-LINK"),
    ("Nokia Shanghai Bell Co., Ltd.", "Nokia Shanghai Bell"),
    ("Zebra Technologies Inc.", "Zebra"),
    ("XEROX CORPORATION", "XEROX"),
])
def test_display_strips_legal_boilerplate(raw, expected):
    assert _display(raw) == expected


def test_display_keeps_plain_names():
    assert _display("Raspberry Pi Foundation") == "Raspberry Pi Foundation"
    assert _display("") == ""
