"""Passkeys (WebAuthn) for the admin sign-in: Face ID / Touch ID / Windows Hello /
Android screen lock.

* Device-bound by request: ``authenticatorAttachment = platform`` (the key is
  created by the phone/laptop's own secure hardware, not a USB key or a phone
  across the room) and ``userVerification = required`` (biometric or device PIN
  every time). The private key never leaves the device; the gateway stores only
  the public key + signature counter in its DB (``settings.passkeys``), outside
  the project folder, so it survives updates.
* Browsers only allow WebAuthn on a secure context whose host is a NAME
  (``localhost`` or an HTTPS hostname such as the Tailscale address). A bare IP
  (``http://192.168.2.1:8080``) can never be a relying-party id, so
  :func:`relying_party` refuses it with an explanation instead of a cryptic
  browser error.
* A passkey sign-in is already two factors (the device + the biometric/PIN), so it
  does not ask for the TOTP code.
"""
from __future__ import annotations

import ipaddress
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from webauthn import (generate_authentication_options, generate_registration_options,
                      options_to_json, verify_authentication_response,
                      verify_registration_response)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (AttestationConveyancePreference, AuthenticatorAttachment,
                                      AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement, UserVerificationRequirement)

SETTING_KEY = "passkeys"
RP_NAME = "QuotaManager"
USER_ID = b"quotamanager-admin"
CHALLENGE_TTL = 120.0
MAX_PASSKEYS = 20


class PasskeyError(ValueError):
    """A request that cannot succeed; the message is safe to show to the admin."""


def relying_party(host_header: str, scheme: str) -> tuple[str, str]:
    """(rp_id, expected_origin) for this request, or PasskeyError when the
    address can never host a passkey (an IP literal, or plain HTTP on a name
    other than localhost)."""
    host = (host_header or "").strip().lower()
    if not host:
        raise PasskeyError("missing host")
    # strip the port: "[::1]:8080", "box:8080", "box"
    if host.startswith("["):
        name = host[1:host.index("]")] if "]" in host else host
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    try:
        ipaddress.ip_address(name)
        raise PasskeyError("ip-address")
    except ValueError as e:
        if isinstance(e, PasskeyError):
            raise
    if scheme != "https" and name != "localhost":
        raise PasskeyError("not-https")
    return name, f"{scheme}://{host}"


@dataclass
class _Challenge:
    value: bytes
    kind: str
    rp_id: str
    origin: str
    expires: float = field(default_factory=lambda: time.monotonic() + CHALLENGE_TTL)


class PasskeyManager:
    """Credential store (via the settings table) + short-lived challenges in memory."""

    def __init__(self, get_setting: Callable[..., Awaitable[Any]], set_setting: Callable[..., Awaitable[Any]]) -> None:
        self._get = get_setting
        self._set = set_setting
        self._challenges: dict[str, _Challenge] = {}

    # ---------------------------------------------------------------- store
    async def list(self) -> list[dict[str, Any]]:
        raw = await self._get(SETTING_KEY, "")
        try:
            data = json.loads(raw) if raw else []
        except ValueError:
            data = []
        return data if isinstance(data, list) else []

    async def _save(self, items: list[dict[str, Any]]) -> None:
        await self._set(SETTING_KEY, json.dumps(items))

    async def public_list(self) -> list[dict[str, Any]]:
        return [{k: p.get(k) for k in ("id", "name", "rp_id", "created", "last_used", "backed_up")} for p in await self.list()]

    async def remove(self, cred_id: str) -> bool:
        items = await self.list()
        keep = [p for p in items if p.get("id") != cred_id]
        if len(keep) == len(items):
            return False
        await self._save(keep)
        return True

    async def rename(self, cred_id: str, name: str) -> bool:
        items = await self.list()
        for p in items:
            if p.get("id") == cred_id:
                p["name"] = (name or "").strip()[:60] or p.get("name")
                await self._save(items)
                return True
        return False

    # ---------------------------------------------------------------- challenges
    def _put(self, ch: _Challenge) -> str:
        now = time.monotonic()
        for k in [k for k, v in self._challenges.items() if v.expires < now]:
            self._challenges.pop(k, None)
        if len(self._challenges) > 200:  # never grows without bound (unauthenticated endpoint)
            self._challenges.pop(next(iter(self._challenges)))
        token = secrets.token_urlsafe(18)
        self._challenges[token] = ch
        return token

    def _take(self, token: str, kind: str) -> _Challenge:
        ch = self._challenges.pop(token or "", None)
        if ch is None or ch.kind != kind or ch.expires < time.monotonic():
            raise PasskeyError("expired")
        return ch

    # ---------------------------------------------------------------- registration
    async def registration_options(self, rp_id: str, origin: str) -> dict[str, Any]:
        items = await self.list()
        if len(items) >= MAX_PASSKEYS:
            raise PasskeyError("too-many")
        opts = generate_registration_options(
            rp_id=rp_id, rp_name=RP_NAME, user_id=USER_ID, user_name="admin",
            user_display_name="QuotaManager admin", timeout=60000,
            attestation=AttestationConveyancePreference.NONE,
            authenticator_selection=AuthenticatorSelectionCriteria(
                authenticator_attachment=AuthenticatorAttachment.PLATFORM,
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED),
            exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(p["id"]))
                                 for p in items if p.get("rp_id") == rp_id])
        token = self._put(_Challenge(opts.challenge, "reg", rp_id, origin))
        return {"token": token, "options": json.loads(options_to_json(opts))}

    async def register(self, token: str, credential: dict[str, Any], name: str) -> dict[str, Any]:
        ch = self._take(token, "reg")
        try:
            v = verify_registration_response(credential=credential, expected_challenge=ch.value,
                                             expected_rp_id=ch.rp_id, expected_origin=ch.origin,
                                             require_user_verification=True)
        except Exception as e:  # noqa: BLE001 — the library raises several types
            raise PasskeyError("verify-failed") from e
        items = await self.list()
        cid = bytes_to_base64url(v.credential_id)
        if any(p.get("id") == cid for p in items):
            raise PasskeyError("duplicate")
        entry = {"id": cid, "public_key": bytes_to_base64url(v.credential_public_key), "sign_count": v.sign_count,
                 "rp_id": ch.rp_id, "name": (name or "").strip()[:60] or "Passkey", "created": int(time.time()),
                 "last_used": None, "backed_up": bool(getattr(v, "credential_backed_up", False))}
        items.append(entry)
        await self._save(items)
        return {k: entry[k] for k in ("id", "name", "rp_id", "created", "last_used", "backed_up")}

    # ---------------------------------------------------------------- sign-in
    async def authentication_options(self, rp_id: str, origin: str) -> dict[str, Any]:
        # Discoverable credentials: no list of ids is handed to an anonymous caller.
        opts = generate_authentication_options(rp_id=rp_id, timeout=60000,
                                               user_verification=UserVerificationRequirement.REQUIRED)
        token = self._put(_Challenge(opts.challenge, "auth", rp_id, origin))
        return {"token": token, "options": json.loads(options_to_json(opts))}

    async def authenticate(self, token: str, credential: dict[str, Any]) -> Optional[str]:
        """Verify an assertion. Returns the credential id on success, None when it fails."""
        try:
            ch = self._take(token, "auth")
        except PasskeyError:
            return None
        items = await self.list()
        cid = str(credential.get("rawId") or credential.get("id") or "")
        p = next((x for x in items if x.get("id") == cid and x.get("rp_id") == ch.rp_id), None)
        if p is None:
            return None
        try:
            v = verify_authentication_response(credential=credential, expected_challenge=ch.value,
                                               expected_rp_id=ch.rp_id, expected_origin=ch.origin,
                                               credential_public_key=base64url_to_bytes(p["public_key"]),
                                               credential_current_sign_count=int(p.get("sign_count") or 0),
                                               require_user_verification=True)
        except Exception:  # noqa: BLE001
            return None
        p["sign_count"] = v.new_sign_count
        p["last_used"] = int(time.time())
        await self._save(items)
        return cid
