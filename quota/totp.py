"""Opt-in TOTP second factor for the admin account (RFC 6238, stdlib only).

A standard time-based one-time password implementation (HMAC-SHA1, 30 s
step, 6 digits, ±1-step tolerance) that works with any authenticator app
(Google Authenticator, Authy, 1Password, etc.) via the ``otpauth://`` URI.
Zero external dependencies — ``hmac`` + ``base64`` are all the algorithm
needs. Enrollment stores a per-install base32 secret in the DB ``settings``
table; verification is constant-time via ``hmac.compare_digest``.

The 2FA is opt-in (``totp_enabled`` setting). Once enabled the login flow
requires the 6-digit code in the same request as the password — a valid TOTP
code ALONE is never sufficient (the password must always verify first).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time

#: TOTP step (seconds) per RFC 6238.
_STEP = 30
#: Number of digits in the emitted code.
_DIGITS = 6
#: Clock-drift tolerance: accept the code for ``now`` +- ``_TOLERANCE`` steps.
_TOLERANCE = 1


def generate_secret() -> str:
    """A fresh 20-byte random base32 secret (32 chars, no padding)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def otpauth_uri(secret: str, issuer: str = "QuotaManager",
                label: str = "admin") -> str:
    """The ``otpauth://`` provisioning URI for scanning with an authenticator."""
    return (f"otpauth://totp/{label}?secret={secret}"
            f"&issuer={issuer}&algorithm=SHA1&digits={_DIGITS}&period={_STEP}")


def _hotp(secret_bytes: bytes, counter: int) -> str:
    """HMAC-based one-time password (RFC 4226) at ``counter``."""
    msg = struct.pack(">Q", counter)
    digest = hmac.new(secret_bytes, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = ((digest[offset] & 0x7F) << 24 |
              (digest[offset + 1] & 0xFF) << 16 |
              (digest[offset + 2] & 0xFF) << 8 |
              (digest[offset + 3] & 0xFF))
    return str(binary % (10 ** _DIGITS)).zfill(_DIGITS)


def _totp(secret: str, at: float) -> str:
    """The TOTP code valid at unix time ``at`` for ``secret``."""
    secret_bytes = base64.b32decode(secret.upper().encode() + b"=" * (
        (-len(secret)) % 8))
    return _hotp(secret_bytes, int(at) // _STEP)


def verify_code(secret: str, code: str) -> bool:
    """Constant-time check of a user-entered 6-digit code (spaces stripped).

    Accepts the code for ``now`` +- one step (clock drift + the window the
    user needs to type). Returns False for anything malformed — a garbage
    input is a failed attempt, never an error.
    """
    code = code.strip().replace(" ", "")
    if not code or not code.isdigit() or len(code) != _DIGITS:
        return False
    now = time.time()
    expected = [_totp(secret, now + i * _STEP)
                for i in range(-_TOLERANCE, _TOLERANCE + 1)]
    return any(hmac.compare_digest(code, candidate)
               for candidate in expected)


def is_valid_secret(secret: str) -> bool:
    """Sanity guard for the stored value (must be base32-looking)."""
    try:
        _totp(secret, time.time())
        return True
    except (ValueError, TypeError):
        return False