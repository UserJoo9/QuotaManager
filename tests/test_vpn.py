import json
import pytest

from quota.vpn_parser import (
    parse_vpn_link,
    generate_sing_box_config,
    validate_sing_box_config,
)
from quota.history_analytics import classify_domain, get_base_domain


def test_vless_reality_parser():
    uri = (
        "vless://a58463c6-8d2c-446d-af7b-094c00f70e86@141.95.67.238:10410"
        "?security=reality&sni=ea.com&fp=chrome"
        "&pbk=FoZ9E318eNFWUamdTGsUClliyx6pSR9OEFdHONzArgQ"
        "&sid=3c8df165d3&flow=xtls-rprx-vision#France-Reality"
    )
    name, proto, outbound = parse_vpn_link(uri)
    assert name == "France-Reality"
    assert proto == "vless"
    assert outbound["server"] == "141.95.67.238"
    assert outbound["server_port"] == 10410
    assert outbound["uuid"] == "a58463c6-8d2c-446d-af7b-094c00f70e86"
    assert outbound["flow"] == "xtls-rprx-vision"
    assert outbound["tls"]["enabled"] is True
    assert outbound["tls"]["server_name"] == "ea.com"
    assert outbound["tls"]["reality"]["public_key"] == "FoZ9E318eNFWUamdTGsUClliyx6pSR9OEFdHONzArgQ"
    assert outbound["tls"]["reality"]["short_id"] == "3c8df165d3"
    assert outbound["tls"]["utls"]["fingerprint"] == "chrome"

    cfg = generate_sing_box_config(outbound)
    assert cfg["inbounds"][0]["interface_name"] == "quota-vpn"
    assert cfg["experimental"]["clash_api"]["external_controller"] == "127.0.0.1:9090"
    valid, err = validate_sing_box_config(cfg)
    assert valid is True
    assert err == ""


def test_vmess_parser():
    # vmess:// with base64 json
    uri = (
        "vmess://eyJhZGQiOiIxLjIuMy40IiwicG9ydCI6IjQ0MyIsImlkIjoiYTU4NDYzYzYtOGQyYy00NDZkLWFmN2ItMDk0YzAwZjcwZTg2"
        "IiwiYWlkIjowLCJzY3kiOiJhdXRvIiwibmV0Ijoid3MiLCJob3N0IjoiZXhhbXBsZS5jb20iLCJwYXRoIjoiL3dzIiwidGxzIjoidGxzIiwicHMiOiJUZXN0Vk1lc3MifQ=="
    )
    name, proto, outbound = parse_vpn_link(uri)
    assert name == "TestVMess"
    assert proto == "vmess"
    assert outbound["server"] == "1.2.3.4"
    assert outbound["server_port"] == 443
    assert outbound["uuid"] == "a58463c6-8d2c-446d-af7b-094c00f70e86"
    assert outbound["transport"]["type"] == "ws"
    assert outbound["transport"]["path"] == "/ws"

    cfg = generate_sing_box_config(outbound)
    valid, err = validate_sing_box_config(cfg)
    assert valid is True


def test_trojan_parser():
    uri = "trojan://mypassword@9.8.7.6:443?sni=trojan.example.com#TrojanNode"
    name, proto, outbound = parse_vpn_link(uri)
    assert name == "TrojanNode"
    assert proto == "trojan"
    assert outbound["password"] == "mypassword"
    assert outbound["server"] == "9.8.7.6"
    assert outbound["tls"]["server_name"] == "trojan.example.com"


def test_shadowsocks_parser():
    # ss://base64(method:password)@server:port#name
    # aes-128-gcm:secretpass -> YWVzLTEyOC1nY206c2VjcmV0cGFzcw==
    uri = "ss://WVlaLTEyOC1nY206c2VjcmV0cGFzcw==@1.2.3.4:8388#MySSNode"
    name, proto, outbound = parse_vpn_link(uri)
    assert name == "MySSNode"
    assert proto == "shadowsocks"
    assert outbound["server"] == "1.2.3.4"
    assert outbound["server_port"] == 8388


def test_invalid_link_raises():
    with pytest.raises(ValueError, match="Unsupported link protocol"):
        parse_vpn_link("http://example.com")


def test_history_classifier():
    assert classify_domain("rr3---sn-vgqsrn7e.googlevideo.com")[0] == "YouTube"
    assert classify_domain("static.xx.fbcdn.net")[0] == "Facebook"
    assert classify_domain("web.whatsapp.com")[0] == "WhatsApp"
    assert classify_domain("v16-webapp-prime.tiktok.com")[0] == "TikTok"
    assert classify_domain("chatgpt.com")[0] == "ChatGPT / AI"
    assert get_base_domain("sub.domain.co.uk") == "domain.co.uk"
    assert get_base_domain("portal.quota.com.eg") == "quota.com.eg"
    assert get_base_domain("api.github.com") == "github.com"


def test_vpn_api_endpoints(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from fastapi.testclient import TestClient
    from api.app import create_app
    from quota import db as _db
    from quota.service import QuotaService
    from quota.engine import SnapshotHolder

    TZ = ZoneInfo("Africa/Cairo")
    database = _db.Database(tmp_path / "vpn_api.db")
    service = QuotaService(database, timezone="Africa/Cairo",
                           clock=lambda: datetime(2026, 8, 15, 12, 0, tzinfo=TZ))
    holder = SnapshotHolder()

    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(database.connect())

    app = create_app(database=database, service=service, holder=holder)
    c = TestClient(app)

    # Login
    r_login = c.post("/api/login", json={"password": "admin"})
    assert r_login.status_code == 200

    # 1. Check VPN status initially
    r = c.get("/api/vpn/status")
    assert r.status_code == 200
    st = r.json()
    assert st["state"] == "disconnected"
    assert st["active_node_id"] is None

    # 2. Add a VPN Node
    vless_uri = (
        "vless://a58463c6-8d2c-446d-af7b-094c00f70e86@141.95.67.238:10410"
        "?security=reality&sni=ea.com&fp=chrome"
        "&pbk=FoZ9E318eNFWUamdTGsUClliyx6pSR9OEFdHONzArgQ"
        "&sid=3c8df165d3&flow=xtls-rprx-vision#MyVlessNode"
    )
    r = c.post("/api/vpn/nodes", json={"raw": vless_uri, "name": "CustomNodeName"})
    assert r.status_code == 201
    node = r.json()
    assert node["name"] == "CustomNodeName"
    assert node["protocol"] == "vless"
    node_id = node["id"]

    # 3. List VPN nodes
    r = c.get("/api/vpn/nodes")
    assert r.status_code == 200
    nodes = r.json()
    assert len(nodes) == 1
    assert nodes[0]["id"] == node_id

    # 4. Check routing rules
    r = c.get("/api/vpn/routing")
    assert r.status_code == 200
    routing = r.json()
    assert "users" in routing
    assert "devices" in routing

    # 5. Set routing rule
    r = c.post("/api/vpn/routing", json={"target_type": "user", "target_id": 1, "route_vpn": False})
    assert r.status_code == 200
    assert r.json()["route_vpn"] is False

    # 6. Delete VPN node
    r = c.delete(f"/api/vpn/nodes/{node_id}")
    assert r.status_code == 200
    assert r.json()["deleted"] is True

    # Check list is empty
    r = c.get("/api/vpn/nodes")
    assert len(r.json()) == 0


def test_history_analytics_endpoint(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from fastapi.testclient import TestClient
    from api.app import create_app
    from quota import db as _db
    from quota.service import QuotaService
    from quota.engine import SnapshotHolder

    TZ = ZoneInfo("Africa/Cairo")
    database = _db.Database(tmp_path / "hist_api.db")
    service = QuotaService(database, timezone="Africa/Cairo",
                           clock=lambda: datetime(2026, 8, 15, 12, 0, tzinfo=TZ))
    holder = SnapshotHolder()

    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(database.connect())

    # Add a device and some mock dns history
    async def seed_data():
        dev = await database.upsert_device("aa:bb:cc:dd:ee:01", name="Tester Phone")
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        rows = [
            (dev.id, now_str, "r2---sn-vgqsrn7e.googlevideo.com", 25),
            (dev.id, now_str, "web.whatsapp.com", 15),
            (dev.id, now_str, "news.bbc.co.uk", 10),
        ]
        await database.batch_add_dns_history(rows)
        return dev

    dev = loop.run_until_complete(seed_data())

    app = create_app(database=database, service=service, holder=holder)
    c = TestClient(app)
    c.post("/api/login", json={"password": "admin"})

    r = c.get(f"/api/history/{dev.id}/analytics?hours=24")
    assert r.status_code == 200
    data = r.json()
    assert data["total_queries"] >= 50
    apps = [a["name"] for a in data["top_apps"]]
    assert "YouTube" in apps
    assert "WhatsApp" in apps
    websites = [w["domain"] for w in data["top_websites"]]
    assert "bbc.co.uk" in websites


def test_user_vless_tls_link():
    uri = (
        "vless://8689fa6b-c467-4d46-b2f1-b6d6e062c938@yax.ddns.net:443"
        "?alpn=h2%2Chttp%2F1.1&encryption=none"
        "&fm=%7B%22tcp%22%3A%5B%7B%22settings%22%3A%7B%22delays%22%3A%5B%5D%2C%22lengths%22%3A%5B%22100-200%22%5D%2C%22maxSplit%22%3A%22300-400%22%2C%22packets%22%3A%22tlshello%22%7D%2C%22type%22%3A%22fragment%22%7D%5D%7D"
        "&fp=chrome&security=tls&sni=www.speedtest.net&type=tcp#main"
    )
    name, proto, outbound = parse_vpn_link(uri)
    assert name == "main"
    assert proto == "vless"
    assert outbound["server"] == "yax.ddns.net"
    assert outbound["server_port"] == 443
    assert outbound["tls"]["enabled"] is True
    assert outbound["tls"]["server_name"] == "www.speedtest.net"

    cfg = generate_sing_box_config(outbound)
    valid, err = validate_sing_box_config(cfg)
    assert valid is True, f"Validation failed: {err}"
    assert err == ""


def test_allow_insecure_sing_box_config():
    uri = "vless://8689fa6b-c467-4d46-b2f1-b6d6e062c938@1.1.1.1:443?security=tls&sni=example.com#test"
    _, _, outbound = parse_vpn_link(uri)
    cfg_secure = generate_sing_box_config(outbound, allow_insecure=False)
    assert cfg_secure["outbounds"][0]["tls"].get("insecure") is not True

    cfg_insecure = generate_sing_box_config(outbound, allow_insecure=True)
    assert cfg_insecure["outbounds"][0]["tls"]["insecure"] is True


def test_vpn_node_patch_and_delete(tmp_path):
    import asyncio
    from fastapi.testclient import TestClient
    from api.app import create_app
    from quota import db as _db
    from quota.service import QuotaService
    from quota.engine import SnapshotHolder

    database = _db.Database(tmp_path / "test_patch.db")
    service = QuotaService(database)
    holder = SnapshotHolder()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(database.connect())

    app = create_app(database=database, service=service, holder=holder)
    client = TestClient(app)
    client.post("/api/login", json={"password": "admin"})

    # 1. Create a node
    uri = "vless://8689fa6b-c467-4d46-b2f1-b6d6e062c938@1.1.1.1:443?security=tls&sni=example.com#NodeOriginal"
    r = client.post("/api/vpn/nodes", json={"raw": uri})
    assert r.status_code == 201
    node_id = r.json()["id"]
    assert r.json()["name"] == "NodeOriginal"

    # 2. Patch name only
    r = client.patch(f"/api/vpn/nodes/{node_id}", json={"name": "Renamed Node"})
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed Node"

    # 3. Patch raw configuration
    uri2 = "vless://11111111-2222-3333-4444-555555555555@2.2.2.2:443?security=tls&sni=other.com#UpdatedLink"
    r = client.patch(f"/api/vpn/nodes/{node_id}", json={"raw": uri2})
    assert r.status_code == 200
    assert "2.2.2.2" in r.json()["config_json"]

    # 3b. Patch structured fields (server, port, uuid, sni, flow, transport)
    r = client.patch(f"/api/vpn/nodes/{node_id}", json={
        "name": "youssef-laptop",
        "server": "yax.ddns.net",
        "server_port": 443,
        "uuid": "2dbd2d69-bd7b-4ad2-872b-61991c421eba",
        "sni": "www.speedtest.net",
        "flow": "xtls-rprx-vision",
        "transport": "TCP",
    })
    assert r.status_code == 200
    cfg = json.loads(r.json()["config_json"])
    assert cfg["server"] == "yax.ddns.net"
    assert cfg["server_port"] == 443
    assert cfg["uuid"] == "2dbd2d69-bd7b-4ad2-872b-61991c421eba"
    assert cfg["flow"] == "xtls-rprx-vision"
    assert cfg["tls"]["server_name"] == "www.speedtest.net"

    # 4. Delete node
    r = client.delete(f"/api/vpn/nodes/{node_id}")
    assert r.status_code == 200
    assert r.json()["deleted"] is True

    # Check it's gone
    r = client.get("/api/vpn/nodes")
    assert not any(n["id"] == node_id for n in r.json())


def test_vpn_settings_endpoint(tmp_path):
    import asyncio
    from fastapi.testclient import TestClient
    from api.app import create_app
    from quota import db as _db
    from quota.service import QuotaService
    from quota.engine import SnapshotHolder

    database = _db.Database(tmp_path / "test_settings.db")
    service = QuotaService(database)
    holder = SnapshotHolder()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(database.connect())

    app = create_app(database=database, service=service, holder=holder)
    client = TestClient(app)
    client.post("/api/login", json={"password": "admin"})

    # Default is false
    r = client.get("/api/vpn/settings")
    assert r.status_code == 200
    assert r.json()["allow_insecure"] is False

    # Turn on
    r = client.post("/api/vpn/settings", json={"allow_insecure": True})
    assert r.status_code == 200
    assert r.json()["allow_insecure"] is True

    # Verify persisted
    r = client.get("/api/vpn/settings")
    assert r.json()["allow_insecure"] is True


def test_vpn_connect_disconnect_persists_vpn_share(tmp_path):
    import asyncio
    from quota import db as _db
    from quota.vpn_manager import VpnManager

    database = _db.Database(tmp_path / "test_mgr.db")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(database.connect())

    mgr = VpnManager(db=database)

    # Initial state: vpn_share_enabled should be 0
    st = loop.run_until_complete(database.get_setting("vpn_share_enabled", "0"))
    assert st == "0"

    # Simulate connect setting persistence
    async def sim_connect():
        await database.set_setting("vpn_share_enabled", "1")
        await database.set_setting("vpn_share_interface", "quota-vpn")
        await database.set_setting("vpn_allow_insecure", "1")
    loop.run_until_complete(sim_connect())

    assert loop.run_until_complete(database.get_setting("vpn_share_enabled", "0")) == "1"
    assert loop.run_until_complete(database.get_setting("vpn_share_interface", "")) == "quota-vpn"

    # Disconnect
    loop.run_until_complete(mgr.disconnect())
    assert loop.run_until_complete(database.get_setting("vpn_share_enabled", "0")) == "0"
    assert loop.run_until_complete(database.get_setting("vpn_share_interface", "")) == ""


