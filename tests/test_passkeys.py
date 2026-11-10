"""Passkeys (WebAuthn): a software platform authenticator runs the real ceremonies
against the API — register, sign in, counter, replay, wrong origin, IP-address hosts."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from api.app import create_app
from quota import db as _db
from quota.engine import SnapshotHolder
from quota.passkeys import PasskeyError, relying_party
from quota.service import QuotaService

RP, ORIGIN = "box.example", "https://box.example"
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
unb64 = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731


class SoftAuthenticator:
    """Face ID / Windows Hello stand-in: ES256 key, user present + verified."""

    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.count = 0

    def _cose(self) -> bytes:
        n = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

    def create(self, options: dict, origin: str = ORIGIN, uv: bool = True) -> dict:
        cd = json.dumps({"type": "webauthn.create", "challenge": options["challenge"], "origin": origin}).encode()
        flags = 0x01 | (0x04 if uv else 0) | 0x40
        auth = hashlib.sha256(options["rp"]["id"].encode()).digest() + bytes([flags]) + (0).to_bytes(4, "big") \
            + bytes(16) + len(self.cred_id).to_bytes(2, "big") + self.cred_id + self._cose()
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth})
        return {"id": b64(self.cred_id), "rawId": b64(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64(cd), "attestationObject": b64(att)}, "clientExtensionResults": {}}

    def get(self, options: dict, origin: str = ORIGIN) -> dict:
        self.count += 1
        cd = json.dumps({"type": "webauthn.get", "challenge": options["challenge"], "origin": origin}).encode()
        auth = hashlib.sha256(options["rpId"].encode()).digest() + bytes([0x05]) + self.count.to_bytes(4, "big")
        sig = self.key.sign(auth + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64(self.cred_id), "rawId": b64(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64(cd), "authenticatorData": b64(auth), "signature": b64(sig), "userHandle": None},
                "clientExtensionResults": {}}


@pytest.fixture
def client(tmp_path):
    loop = asyncio.new_event_loop()
    database = _db.Database(tmp_path / "pk.db")
    loop.run_until_complete(database.connect())
    app = create_app(database, QuotaService(database, timezone="Africa/Cairo"), SnapshotHolder())
    with TestClient(app, base_url=ORIGIN) as c:
        yield c, database, loop
    loop.run_until_complete(database.close())
    loop.close()


def _login(c):
    assert c.post("/api/login", json={"password": "admin"}).json()["ok"] is True


def _register(c, auth: SoftAuthenticator, name="Test phone") -> dict:
    o = c.post("/api/passkeys/register/options").json()
    sel = o["options"]["authenticatorSelection"]
    # device-bound: built-in authenticator, discoverable, biometric/PIN required
    assert sel["authenticatorAttachment"] == "platform" and sel["userVerification"] == "required" and sel["residentKey"] == "required"
    r = c.post("/api/passkeys/register/verify", json={"token": o["token"], "credential": auth.create(o["options"]), "name": name})
    assert r.status_code == 200, r.text
    return r.json()["passkey"]


def test_relying_party_needs_a_name_and_a_secure_origin():
    assert relying_party("box.example", "https") == ("box.example", "https://box.example")
    assert relying_party("Box.Example:8443", "https") == ("box.example", "https://box.example:8443")
    assert relying_party("localhost:8080", "http")[0] == "localhost"
    for host, scheme, why in (("192.168.2.1:8080", "http", "ip-address"), ("192.168.2.1", "https", "ip-address"),
                              ("[fd00::1]:8080", "https", "ip-address"), ("gateway.lan", "http", "not-https")):
        with pytest.raises(PasskeyError, match=why):
            relying_party(host, scheme)


def test_register_requires_admin_session(client):
    c, _, _ = client
    assert c.post("/api/passkeys/register/options").status_code == 401
    assert c.get("/api/passkeys").status_code == 401


def test_full_ceremony_sign_in_skips_totp_and_counts(client):
    c, database, loop = client
    _login(c)
    # even with 2FA on: a passkey is already two factors (device + biometric/PIN)
    from quota import totp as _totp
    loop.run_until_complete(database.set_setting("totp_secret", _totp.generate_secret()))
    loop.run_until_complete(database.set_setting("totp_enabled", "1"))
    auth = SoftAuthenticator()
    pk = _register(c, auth)
    assert pk["rp_id"] == RP and pk["name"] == "Test phone"
    listed = c.get("/api/passkeys").json()
    assert listed["usable_here"] is True and [p["id"] for p in listed["passkeys"]] == [pk["id"]]
    assert "public_key" not in listed["passkeys"][0]  # the API never hands out key material
    c.post("/api/logout")
    assert c.get("/api/me").json()["authenticated"] is False
    assert c.post("/api/login", json={"password": "admin"}).json() == {"ok": False, "totp": True}  # password alone: no
    o = c.post("/api/passkeys/login/options").json()
    assert o["options"]["allowCredentials"] == [] and o["options"]["userVerification"] == "required"
    r = c.post("/api/passkeys/login/verify", json={"token": o["token"], "credential": auth.get(o["options"])})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert c.get("/api/me").json()["authenticated"] is True
    stored = json.loads(loop.run_until_complete(database.get_setting("passkeys")))
    assert stored[0]["sign_count"] == 1 and stored[0]["last_used"]


def test_replayed_challenge_and_wrong_origin_are_refused(client):
    c, _, _ = client
    _login(c)
    auth = SoftAuthenticator()
    _register(c, auth)
    c.post("/api/logout")
    o = c.post("/api/passkeys/login/options").json()
    cred = auth.get(o["options"])
    assert c.post("/api/passkeys/login/verify", json={"token": o["token"], "credential": cred}).status_code == 200
    c.post("/api/logout")
    # the same challenge cannot be used twice
    assert c.post("/api/passkeys/login/verify", json={"token": o["token"], "credential": cred}).status_code == 401
    # a phishing page on another origin cannot reuse a fresh challenge
    o2 = c.post("/api/passkeys/login/options").json()
    bad = auth.get(o2["options"], origin="https://evil.example")
    assert c.post("/api/passkeys/login/verify", json={"token": o2["token"], "credential": bad}).status_code == 401
    assert c.get("/api/me").json()["authenticated"] is False


def test_unknown_or_removed_passkey_cannot_sign_in(client):
    c, _, _ = client
    _login(c)
    auth = SoftAuthenticator()
    pk = _register(c, auth)
    assert c.delete(f"/api/passkeys/{pk['id']}").status_code == 200
    assert c.get("/api/passkeys").json()["passkeys"] == []
    c.post("/api/logout")
    o = c.post("/api/passkeys/login/options").json()
    r = c.post("/api/passkeys/login/verify", json={"token": o["token"], "credential": auth.get(o["options"])})
    assert r.status_code == 401


def test_registration_without_user_verification_is_refused(client):
    c, _, _ = client
    _login(c)
    o = c.post("/api/passkeys/register/options").json()
    r = c.post("/api/passkeys/register/verify", json={"token": o["token"], "credential": SoftAuthenticator().create(o["options"], uv=False)})
    assert r.status_code == 400


def test_ip_address_dashboard_explains_instead_of_failing(tmp_path):
    loop = asyncio.new_event_loop()
    database = _db.Database(tmp_path / "ip.db")
    loop.run_until_complete(database.connect())
    app = create_app(database, QuotaService(database, timezone="Africa/Cairo"), SnapshotHolder())
    with TestClient(app, base_url="http://192.168.2.1:8080") as c:
        _login(c)
        body = c.get("/api/passkeys").json()
        assert body["usable_here"] is False and body["reason"] == "ip-address"
        r = c.post("/api/passkeys/register/options")
        assert r.status_code == 400 and "ip-address" in r.json()["detail"]
    loop.run_until_complete(database.close())
    loop.close()


def test_gateway_boots_without_the_passkey_library(tmp_path, monkeypatch):
    """A folder-copy update that skipped `pip install` must still start; passkeys just say why not."""
    import api.app as appmod
    monkeypatch.setattr(appmod, "_passkeys", None)
    loop = asyncio.new_event_loop()
    database = _db.Database(tmp_path / "nolib.db")
    loop.run_until_complete(database.connect())
    app = appmod.create_app(database, QuotaService(database, timezone="Africa/Cairo"), SnapshotHolder())
    with TestClient(app, base_url=ORIGIN) as c:
        _login(c)
        assert c.get("/api/passkeys").json()["reason"] == "server-missing"
        assert c.post("/api/passkeys/login/options").status_code == 503
        assert c.get("/api/me").json()["authenticated"] is True
    loop.run_until_complete(database.close())
    loop.close()
