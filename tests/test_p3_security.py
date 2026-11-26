"""Workstream S (security, CONTRACT-phase3.md §5 S): secret split, enrollment codes, device sessions, step-up PIN,
token rotation, signed bundles, job-token alerts. Single in-process hubs with a fake clock (no sockets)."""
import base64
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import forge_security as fs
from app import create_app
from cluster_net import TOKENS, FakeClock, Fleet
from forge_bundle import BundleError, build_zip

ROOT = Path(__file__).resolve().parents[1]
UI = {"Authorization": f"Bearer {TOKENS['ui_token']}"}
AGENT = {"X-Agent-Token": TOKENS["agent_token"]}
CL = {"X-Cluster-Token": TOKENS["cluster_token"]}
KEY_XML = (ROOT / "tests" / "fixtures" / "test-signing-key.xml").read_text(encoding="utf-8")   # TEST-ONLY key
BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0 Safari/537.36"}


def _xml(tag):
    return int.from_bytes(base64.b64decode(re.search(f"<{tag}>(.*?)</{tag}>", KEY_XML).group(1)), "big")


N, E, D = _xml("Modulus"), _xml("Exponent"), _xml("D")
PUB = fs.canonical_pubkey(KEY_XML)


def sign(message: bytes, n=N, d=D) -> bytes:
    k = (n.bit_length() + 7) // 8
    t = fs.SHA256_DIGESTINFO + hashlib.sha256(message).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")


def make_hub(tmp_path, private=False, **cfg_extra):
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    if private:
        (data / "private").mkdir()
    (data / "config.json").write_text(json.dumps(TOKENS | {"node_id": "hub1"} | cfg_extra), encoding="utf-8")
    clock = FakeClock()
    app = create_app(data, start_monitor=False, clock=clock, releases_dir=tmp_path / "releases")
    return TestClient(app), app, clock


@pytest.fixture
def hub(tmp_path):
    return make_hub(tmp_path)


def report(c, node="box", version="2.3.0", headers=AGENT, **metrics):
    return c.post("/api/agent/report", headers=headers, json={
        "node_id": node, "metrics": metrics, "capacity": 1, "agent_version": version,
        "info": {"tools": ["claude"], "agent_version": version}})


def set_pin(c, pin="123456"):
    assert c.put("/api/stepup/pin", json={"pin": pin}, headers=UI).status_code == 200


def login(c, label=None):
    r = c.post("/api/login", json={"token": TOKENS["ui_token"]} | ({"label": label} if label else {}))
    assert r.status_code == 200, r.text
    return r


# ====================================================================== S2 secret split
def test_load_config_migrates_secrets_into_private_dir(tmp_path):
    # Arrange
    data = tmp_path / "data"
    (data / "private").mkdir(parents=True)
    (data / "config.json").write_text(json.dumps(TOKENS | {"node_id": "n1", "port": 8787}), encoding="utf-8")
    (data / "forge.db").write_bytes(b"")
    # Act
    cfg = fs.load_config(data)
    dbp = fs.db_path(data)
    # Assert
    on_disk = json.loads((data / "config.json").read_text(encoding="utf-8"))
    sec = json.loads((data / "private" / "secrets.json").read_text(encoding="utf-8"))
    assert on_disk == {"node_id": "n1", "port": 8787, "agent_token": "agent-tok"}
    assert sec == {"ui_token_hash": fs.token_hash("ui-tok"), "cluster_token": "cluster-tok"}
    assert json.loads((data / "private" / "config.json.pre23.bak").read_text())["ui_token"] == "ui-tok"
    assert cfg["cluster_token"] == "cluster-tok" and cfg["ui_token_hash"] == fs.token_hash("ui-tok") and "ui_token" not in cfg
    assert dbp == data / "private" / "forge.db" and dbp.exists() and not (data / "forge.db").exists()
    # second run is a no-op (backup not rewritten, nothing regenerated)
    assert fs.load_config(data) == cfg


def test_hub_with_private_dir_accepts_legacy_ui_token_by_hash_and_never_leaks_it(tmp_path):
    # Arrange
    c, app, _ = make_hub(tmp_path, private=True)
    code = c.post("/api/enroll", json={"kind": "machine"}, headers=UI).json()["code"]
    # Act
    boot = c.get(f"/cluster/bootstrap?code={code}").json()
    # Assert
    assert c.get("/api/state", headers=UI).status_code == 200
    assert c.get("/api/state", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert "ui_token" not in boot and boot["ui_token_hash"] == fs.token_hash("ui-tok")
    assert boot["cluster_token"] == "cluster-tok" and app.state.store.db.execute("PRAGMA database_list").fetchone()[2] \
        .endswith(str(Path("private") / "forge.db"))


def test_bootstrap_with_agent_token_never_returns_cluster_secrets(tmp_path):
    # Arrange: the agent token is in user-readable config.json, so a filtered job can read it (S-R1)
    c, _, _ = make_hub(tmp_path, private=True)
    legacy_c, _, _ = make_hub(tmp_path / "legacy", private=True, legacy_bootstrap_secrets=True)
    # Act
    boot = c.get(f"/cluster/bootstrap?t={TOKENS['agent_token']}")
    legacy = legacy_c.get(f"/cluster/bootstrap?t={TOKENS['agent_token']}").json()
    # Assert
    b = boot.json()
    assert boot.status_code == 200 and b["agent_token"] == "agent-tok" and {"peers", "bundle_version"} <= set(b)
    assert not {"cluster_token", "ui_token_hash", "signing_pubkey", "ui_token"} & set(b)
    assert "cluster-tok" not in boot.text and "sha256:" not in boot.text
    assert legacy["cluster_token"] == "cluster-tok" and "ui_token" not in legacy     # explicit 2.2-installer opt-in


def test_no_private_dir_keeps_22_layout(tmp_path):
    # Arrange / Act
    cfg = fs.load_config(tmp_path / "d")
    # Assert
    assert {"ui_token", "agent_token", "cluster_token"} <= set(cfg)
    assert fs.db_path(tmp_path / "d") == tmp_path / "d" / "forge.db"


# ====================================================================== S4 sessions
def test_login_sets_session_cookie_not_token_and_session_is_hashed(hub):
    # Arrange
    c, app, _ = hub
    # Act
    r = login(c, "my phone")
    # Assert
    sid = r.cookies.get("forge_sid")
    assert sid and sid != TOKENS["ui_token"] and "forge_token" not in r.cookies
    rows = app.state.store.q("SELECT id,label FROM ui_sessions")
    assert [(x["id"], x["label"]) for x in rows] == [(hashlib.sha256(sid.encode()).hexdigest(), "my phone")]
    assert r.json()["session"] == {"id": rows[0]["id"][:12], "label": "my phone"}
    assert c.get("/api/state").status_code == 200                    # cookie jar carries forge_sid
    assert c.post("/api/login", json={"token": "wrong"}).status_code == 401


def test_devices_list_rename_revoke_and_logout(hub):
    # Arrange
    c, app, _ = hub
    other = TestClient(app)
    login(other, "laptop")
    login(c, "phone")
    # Act
    devs = c.get("/api/devices").json()["devices"]
    lap = next(d for d in devs if d["label"] == "laptop")
    renamed = c.patch(f"/api/devices/{lap['id']}", json={"label": "work laptop"}).json()
    revoked = c.delete(f"/api/devices/{lap['id']}")
    # Assert
    assert sorted(d["label"] for d in devs) == ["laptop", "phone"]
    assert [d["current"] for d in devs if d["label"] == "phone"] == [True] and len(lap["id"]) == 12
    assert renamed["label"] == "work laptop" and revoked.json() == {"ok": True}
    assert other.get("/api/state").status_code == 401                # revoked device is logged out
    assert c.delete("/api/devices/ffffffffffff").status_code == 404
    assert c.post("/api/logout").json() == {"ok": True}
    assert c.get("/api/state").status_code == 401


def test_session_sliding_expiry(hub):
    # Arrange
    c, app, clock = hub
    login(c)
    st = app.state.store
    exp0 = st.q("SELECT expires FROM ui_sessions")[0]["expires"]
    # Act: within 10 min nothing is written; after that last_seen/expires slide
    clock.advance(300)
    c.get("/api/state")
    exp1 = st.q("SELECT expires FROM ui_sessions")[0]["expires"]
    clock.advance(400)
    c.get("/api/state")
    exp2 = st.q("SELECT expires FROM ui_sessions")[0]["expires"]
    # Assert
    assert exp1 == exp0 and exp2 == exp0 + 700
    clock.advance(91 * 86400)
    assert c.get("/api/state").status_code == 401


def test_legacy_cookie_is_upgraded_to_a_session(hub):
    # Arrange
    c, app, _ = hub
    c.cookies.set("forge_token", TOKENS["ui_token"])
    # Act
    r = c.get("/api/state", headers=BROWSER)
    # Assert
    assert r.status_code == 200 and r.cookies.get("forge_sid")
    assert len(app.state.store.q("SELECT id FROM ui_sessions")) == 1


def test_legacy_cookie_does_not_mint_a_session_per_request_or_reset_the_pin_limit(hub):
    # Arrange: a client that sends the old cookie but never keeps Set-Cookie (curl, a script) (S-R3)
    c, app, clock = hub
    set_pin(c)
    legacy = {"Cookie": f"forge_token={TOKENS['ui_token']}"}

    def fresh():
        return TestClient(app)                     # empty cookie jar every time
    # Act
    states = [fresh().get("/api/state", headers=legacy | BROWSER).status_code for _ in range(10)]
    script = fresh().get("/api/state", headers=legacy | {"User-Agent": "curl/8.4"}).status_code
    sessions = len(app.state.store.q("SELECT id FROM ui_sessions"))
    pins = [fresh().post("/api/tokens/rotate", json={"confirm": True},
                         headers=legacy | BROWSER | {"X-Admin-Pin": f"{i:06d}"}).status_code for i in range(7)]
    # Assert
    assert states == [200] * 10 and script == 200
    assert sessions == 1                                          # reused, not 10 (and none for curl)
    assert pins == [403] * 5 + [429] * 2                          # the limiter is not reset by new sessions
    clock.advance(601)
    assert fresh().post("/api/tokens/rotate", json={"confirm": True, "force": True},
                        headers=legacy | BROWSER | {"X-Admin-Pin": "123456"}).status_code == 200


def test_legacy_upgrades_are_capped_per_hour(hub):
    # Arrange
    c, app, clock = hub
    legacy = {"Cookie": f"forge_token={TOKENS['ui_token']}"}
    # Act: 30 different browsers within the hour
    codes = [TestClient(app).get("/api/state", headers=legacy | {"User-Agent": f"Mozilla/5.0 n{i}"}).status_code
             for i in range(30)]
    # Assert: all authenticate, only LEGACY_UPGRADES_PER_HOUR sessions were created
    assert codes == [200] * 30
    assert len(app.state.store.q("SELECT id FROM ui_sessions")) == fs.LEGACY_UPGRADES_PER_HOUR


def test_pin_limit_is_not_reset_by_minting_new_sessions(hub):
    # Arrange: a ui-token holder logs in again and again to get a fresh session per guess batch
    c, app, _ = hub
    set_pin(c)
    # Act
    codes = []
    for i in range(6):
        dev = TestClient(app)
        login(dev)
        codes.append(dev.post("/api/stepup", json={"pin": f"{900000 + i}"}).status_code)
    # Assert: every new session starts with an empty per-session bucket, but the per-IP bucket (10) still stops it
    more = []
    for i in range(6):
        dev = TestClient(app)
        login(dev)
        more.append(dev.post("/api/stepup", json={"pin": f"{910000 + i}"}).status_code)
    assert codes == [403] * 6
    assert more[:4] == [403] * 4 and more[4:] == [429, 429]


# ====================================================================== S3 enrollment
def test_enroll_machine_code_install_and_bootstrap_redeem_once(hub, tmp_path):
    # Arrange
    c, app, clock = hub
    rel = Path(app.state.ctx.release_dir)
    c.post("/cluster/join", json={"id": "hub1", "urls": ["http://127.0.0.1:8787", "http://hub1.tail9.ts.net:8787"]},
           headers=CL)
    # Act
    e = c.post("/api/enroll", json={"kind": "machine", "label": "atlas"}, headers=UI).json()
    code = e["code"]
    inst = c.get(f"/agent/install.ps1?code={code.lower()}")
    st1 = c.get(f"/api/enroll/{e['id']}", headers=UI).json()["state"]
    boot = c.get(f"/cluster/bootstrap?code={code.replace('-', '')}&node=Atlas")
    again = c.get(f"/cluster/bootstrap?code={code}")
    st2 = c.get(f"/api/enroll/{e['id']}", headers=UI).json()
    report(c, "atlas")
    st3 = c.get(f"/api/enroll/{e['id']}", headers=UI).json()
    # Assert
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}", code)
    assert e["hub"] == "http://hub1.tail9.ts.net:8787" and TOKENS["agent_token"] not in e["command"]
    assert e["command"] == f"irm 'http://hub1.tail9.ts.net:8787/agent/install.ps1?code={code}' | iex"
    assert inst.status_code == 200 and st1 == "pending"
    tpl = (rel / "agent" / "install.ps1").read_text(encoding="utf-8")
    assert "__CODE__" not in inst.text and ("__CODE__" not in tpl or code in inst.text)
    b = boot.json()
    assert boot.status_code == 200 and b["agent_token"] == "agent-tok" and b["cluster_token"] == "cluster-tok"
    assert "ui_token" not in b and b["node_hint"] == "atlas" and {"peers", "bundle_version", "bundle_sha256",
                                                                  "signing_pubkey", "ui_token_hash"} <= set(b)
    assert again.status_code == 401
    assert st2["state"] == "redeemed" and st2["node_id"] == "atlas" and st3["state"] == "joined"
    assert c.get(f"/agent/install.ps1?code={code}").status_code == 401   # used codes no longer serve the installer


def test_enroll_code_expiry_cancel_legacy_and_hub_url_rules(hub):
    # Arrange
    c, app, clock = hub
    # Act
    e = c.post("/api/enroll", json={"kind": "machine"}, headers=UI).json()
    clock.advance(601)
    expired = c.get(f"/cluster/bootstrap?code={e['code']}")
    state = c.get(f"/api/enroll/{e['id']}", headers=UI).json()["state"]
    leg = c.get("/api/enroll", headers=UI).json()
    gone = c.delete(f"/api/enroll/{leg['id']}", headers=UI)
    cancelled = c.get(f"/cluster/bootstrap?code={leg['code']}")
    # Assert
    assert expired.status_code == 401 and state == "expired"
    assert leg["kind"] == "machine" and "?code=" in leg["command"] and "?t=" not in leg["command"]
    assert "localhost" not in leg["hub"] and "127.0.0.1" not in leg["hub"] and "testserver" in leg["hub"]
    assert gone.json() == {"ok": True} and cancelled.status_code == 401
    assert c.post("/api/enroll", json={"kind": "toaster"}, headers=UI).status_code == 400
    assert c.post("/api/enroll", json={"kind": "machine", "label": "x" * 65}, headers=UI).status_code == 400


def test_enroll_public_url_wins(tmp_path):
    c, _, _ = make_hub(tmp_path, public_url="https://hive.example.ts.net/")
    assert c.post("/api/enroll", json={"kind": "device"}, headers=UI).json()["url"].startswith(
        "https://hive.example.ts.net/?claim=")


def test_bootstrap_rate_limit(hub):
    # Arrange
    c, _, clock = hub
    # Act
    codes = [c.get("/cluster/bootstrap?code=AAAA-AAAA").status_code for _ in range(11)]
    clock.advance(61)
    later = c.get(f"/cluster/bootstrap?t={TOKENS['agent_token']}").status_code
    # Assert
    assert codes[:10] == [401] * 10 and codes[10] == 429 and later == 200


def test_rate_limits_ignore_a_spoofed_forwarded_for(hub):
    # Arrange: X-Forge-Via + a fresh X-Forwarded-For on every request (S-R2)
    c, _, _ = hub

    def spoof(i):
        return {"X-Forge-Via": "x", "X-Forwarded-For": f"10.0.{i // 256}.{i % 256}"}
    # Act
    logins = [c.post("/api/login", json={"token": "wrong"}, headers=spoof(i)).status_code for i in range(11)]
    boots = [c.get("/cluster/bootstrap?code=AAAA-AAAA", headers=spoof(100 + i)).status_code for i in range(11)]
    fake_proxy = c.post("/api/login", json={"token": "wrong"},
                        headers=spoof(500) | {"X-Forge-Proxy": "not-the-cluster-token"}).status_code
    # Assert
    assert logins == [401] * 10 + [429] and boots == [401] * 10 + [429] and fake_proxy == 429


def test_client_ip_trusts_only_the_last_hop_of_a_proven_peer(hub):
    # Arrange
    from starlette.requests import Request
    _, app, _ = hub
    sec = app.state.security

    def req(headers):
        return Request({"type": "http", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
                        "client": ("100.64.0.9", 5000)})
    # Act / Assert
    assert sec.client_ip(req({})) == "100.64.0.9"
    assert sec.client_ip(req({"X-Forge-Via": "b", "X-Forwarded-For": "1.1.1.1"})) == "100.64.0.9"
    assert sec.client_ip(req({"X-Forge-Via": "b", "X-Forwarded-For": "6.6.6.6, 1.1.1.1",
                              "X-Forge-Proxy": TOKENS["cluster_token"]})) == "1.1.1.1"
    assert sec.client_ip(req({"X-Forwarded-For": "1.1.1.1", "X-Forge-Proxy": TOKENS["cluster_token"]})) == "100.64.0.9"


def test_claim_device_code_creates_session_once(hub):
    # Arrange
    c, app, _ = hub
    e = c.post("/api/enroll", json={"kind": "device", "label": "iPhone"}, headers=UI).json()
    phone = TestClient(app)
    # Act
    r = phone.post("/api/claim", json={"code": e["code"]})
    again = TestClient(app).post("/api/claim", json={"code": e["code"]})
    machine = c.post("/api/enroll", json={"kind": "machine"}, headers=UI).json()
    wrong_kind = TestClient(app).post("/api/claim", json={"code": machine["code"]})
    # Assert
    assert r.status_code == 200 and r.json()["session"]["label"] == "iPhone" and r.cookies.get("forge_sid")
    assert phone.get("/api/state").status_code == 200
    assert again.status_code == 401 and wrong_kind.status_code == 401
    assert c.get(f"/api/enroll/{e['id']}", headers=UI).json()["state"] == "joined"


def test_firstrun_claim_file_is_imported_once(tmp_path):
    # Arrange
    data = tmp_path / "data"
    (data / "private").mkdir(parents=True)
    (data / "private" / "firstrun-claim.json").write_text(json.dumps(
        {"code_sha256": fs.code_id("ABCD-EFGH"), "expires": 1_700_000_600, "label": "first device"}), encoding="utf-8")
    (data / "config.json").write_text(json.dumps({"node_id": "hub1"}), encoding="utf-8")
    app = create_app(data, start_monitor=False, clock=FakeClock(), releases_dir=tmp_path / "rel")
    c = TestClient(app)
    # Act
    r = c.post("/api/claim", json={"code": "abcd-efgh"})
    # Assert
    assert not (data / "private" / "firstrun-claim.json").exists()
    assert r.status_code == 200 and r.json()["session"]["label"] == "first device"
    assert c.get("/api/state").status_code == 200
    assert c.post("/api/claim", json={"code": "ABCDEFGH"}).status_code == 401


def test_code_normalization():
    assert fs.normalize_code(" abcd-efgh ") == "ABCDEFGH"
    assert fs.normalize_code("IL0O-uuuu") == "1100VVVV"
    assert fs.code_id("abcd-efgh") == hashlib.sha256(b"ABCDEFGH").hexdigest()


def test_enroll_and_login_on_standby_are_handled_by_leader(tmp_path):
    # Arrange: hubs a (leader) and b (standby); everything goes through b
    fleet = Fleet(tmp_path, names=("a", "b"), fast=True)
    try:
        fleet.converge()
        b = fleet.client("b")
        # Act
        e = b.post("/api/enroll", json={"kind": "machine"}, headers=UI).json()
        boot = b.get(f"/cluster/bootstrap?code={e['code']}&node=newbox")
        again = b.get(f"/cluster/bootstrap?code={e['code']}")
        lg = b.post("/api/login", json={"token": TOKENS["ui_token"], "label": "via b"})
        # Assert
        assert boot.status_code == 200 and again.status_code == 401
        a_store = fleet.hubs["a"].state.store
        assert a_store.q("SELECT used_by FROM enroll_codes")[0]["used_by"] == "newbox"
        assert lg.status_code == 200 and lg.cookies.get("forge_sid")
        assert [r["label"] for r in a_store.q("SELECT label FROM ui_sessions")] == ["via b"]
        assert a_store.q("SELECT ip FROM ui_sessions")[0]["ip"] == "testclient"   # b's view of the client (last hop)
    finally:
        fleet.close()


# ====================================================================== S7 step-up
def test_step_up_setup_then_required_then_valid_for_10_minutes(hub):
    # Arrange
    c, app, clock = hub
    login(c)
    report(c)
    admin = {"node_id": "box", "engine": "admin", "prompt": "Get-Date", "confirm": True}
    # Act / Assert
    r = c.post("/api/jobs", json=admin)
    assert r.status_code == 403 and r.json() == {"detail": "admin PIN not set", "step_up": "setup"}
    assert c.get("/api/stepup").json() == {"pin_set": False, "valid_until": None}
    assert c.put("/api/stepup/pin", json={"pin": "12345"}).status_code == 400
    assert c.put("/api/stepup/pin", json={"pin": "123456"}).status_code == 200      # setting it steps up too
    assert c.post("/api/jobs", json=admin).status_code == 200
    clock.advance(601)
    r = c.post("/api/jobs", json=admin)
    assert r.status_code == 403 and r.json() == {"detail": "admin PIN required", "step_up": "required"}
    assert c.post("/api/stepup", json={"pin": "000000"}).status_code == 403
    ok = c.post("/api/stepup", json={"pin": "123456"}).json()
    assert ok["ok"] and ok["valid_until"] == clock() + 600
    assert c.get("/api/stepup").json()["valid_until"] == ok["valid_until"]
    assert c.post("/api/jobs", json=admin).status_code == 200
    assert c.post("/api/jobs", json={"node_id": "box", "engine": "action", "action": "winget_upgrade"}).status_code == 200


def test_step_up_wrong_pin_rate_limit_and_header_path(hub):
    # Arrange
    c, _, clock = hub
    login(c)
    set_pin(c)
    # Act
    hdr_ok = c.post("/api/tokens/rotate", json={"confirm": True, "which": ["agent"]},
                    headers=UI | {"X-Admin-Pin": "123456"})
    hdr_bad = c.post("/api/tokens/rotate", json={"confirm": True}, headers=UI | {"X-Admin-Pin": "111111"})
    codes = [c.post("/api/stepup", json={"pin": "999999"}).status_code for _ in range(5)]
    # Assert: the header failure counts too (same session): 1 + 4 wrong -> the 6th attempt is refused
    assert hdr_ok.status_code == 200 and hdr_bad.status_code == 403
    assert codes == [403] * 4 + [429]
    clock.advance(601)
    assert c.post("/api/stepup", json={"pin": "123456"}).status_code == 200


def test_pin_change_needs_old_pin_or_step_up(hub):
    # Arrange
    c, app, clock = hub
    set_pin(c)
    # Act / Assert
    r = c.put("/api/stepup/pin", json={"pin": "222222"}, headers=UI)
    assert r.status_code == 403 and r.json()["step_up"] == "required"
    assert c.put("/api/stepup/pin", json={"pin": "222222", "old_pin": "000000"}, headers=UI).status_code == 403
    assert c.put("/api/stepup/pin", json={"pin": "222222", "old_pin": "123456"}, headers=UI).status_code == 200
    stored = app.state.store.q("SELECT value FROM secrets WHERE name='admin_pin_hash'")[0]["value"]
    assert stored.startswith("pbkdf2_sha256$200000$") and fs.verify_pin("222222", stored)
    assert json.loads((Path(app.state.ctx.data_dir) / "config.json").read_text())["admin_pin_hash"] == stored


def test_pin_hash_format_matches_contract():
    h = fs.hash_pin("654321", salt=bytes(16))
    algo, iters, salt, dk = h.split("$")
    assert (algo, iters, salt) == ("pbkdf2_sha256", "200000", base64.b64encode(bytes(16)).decode())
    assert base64.b64decode(dk) == hashlib.pbkdf2_hmac("sha256", b"654321", bytes(16), 200000)
    assert fs.verify_pin("654321", h) and not fs.verify_pin("654320", h) and not fs.verify_pin("1", "garbage")


# ====================================================================== S5 rotation
def test_rotate_requires_confirm_pin_and_new_agents(hub):
    # Arrange
    c, app, clock = hub
    set_pin(c)
    pin = UI | {"X-Admin-Pin": "123456"}
    report(c, "old", version="2.2.0")
    # Act / Assert
    assert c.post("/api/tokens/rotate", json={}, headers=pin).json()["detail"] == "confirmation required"
    assert c.post("/api/tokens/rotate", json={"confirm": True}, headers=UI).status_code == 403
    r = c.post("/api/tokens/rotate", json={"confirm": True}, headers=pin)
    assert r.status_code == 409 and r.json()["detail"] == "agents older than 2.3 would be locked out: old"
    r = c.post("/api/tokens/rotate", json={"confirm": True, "force": True}, headers=pin)
    assert r.status_code == 200 and r.json() == {"ok": True, "rotated": ["agent", "cluster"], "grace_until": clock() + 600}


def test_rotation_grace_report_hint_files_and_lockout(hub):
    # Arrange
    c, app, clock = hub
    set_pin(c)
    report(c, "box")
    ctx = app.state.ctx
    # Act
    c.post("/api/tokens/rotate", json={"confirm": True}, headers=UI | {"X-Admin-Pin": "123456"})
    new_agent, new_cluster = ctx.cfg["agent_token"], ctx.cfg["cluster_token"]
    old_resp = report(c, "box").json()
    new_resp = report(c, "box", headers={"X-Agent-Token": new_agent})
    sys_resp = c.post("/api/agent/report", headers=AGENT, json={"node_id": "box", "lane": "system", "capacity": 1}).json()
    tokens = c.get("/api/tokens", headers=UI).json()
    on_disk = json.loads((Path(ctx.data_dir) / "config.json").read_text())
    old_cl = c.get("/cluster/status", headers=CL).status_code
    clock.advance(601)
    # Assert
    assert new_agent != "agent-tok" and new_cluster != "cluster-tok"
    assert old_resp["rotate"] == {"agent_token": new_agent, "grace_until": tokens["agent"]["grace_until"]}
    assert new_resp.status_code == 200 and sys_resp["rotate"]["agent_token"] == new_agent
    assert tokens["agent"]["rotated"] == tokens["cluster"]["rotated"] and old_cl == 200
    assert on_disk["agent_token"] == new_agent and on_disk["cluster_token"] == new_cluster
    assert report(c, "box").status_code == 401                         # grace over: old token locked out
    assert c.get("/cluster/status", headers=CL).status_code == 401
    ok = report(c, "box", headers={"X-Agent-Token": new_agent})
    assert ok.status_code == 200 and "rotate" not in ok.json()
    assert c.get("/cluster/status", headers={"X-Cluster-Token": new_cluster}).status_code == 200


def test_standby_adopts_replicated_secret_rows(tmp_path):
    # Arrange: a hub with a private dir sees a newer `secrets` row (as replication would deliver it)
    c, app, clock = make_hub(tmp_path, private=True)
    st, ctx = app.state.store, app.state.ctx
    st.x("INSERT INTO secrets(name,value,prev_value,rotated,updated) VALUES(?,?,?,?,?)",
         ("cluster_token", "cl-new", "cluster-tok", clock(), clock()))
    st.x("INSERT INTO secrets(name,value,prev_value,rotated,updated) VALUES(?,?,?,?,?)",
         ("agent_token", "ag-new", "agent-tok", clock(), clock()))
    st.x("INSERT INTO secrets(name,value,prev_value,rotated,updated) VALUES(?,?,?,?,?)",
         ("signing_pubkey", PUB, None, None, clock()))
    # Act
    app.state.tick()
    # Assert
    data = Path(ctx.data_dir)
    sec = json.loads((data / "private" / "secrets.json").read_text())
    cfgf = json.loads((data / "config.json").read_text())
    assert ctx.cfg["cluster_token"] == "cl-new" and ctx.cfg["agent_token"] == "ag-new"
    assert sec["cluster_token"] == "cl-new" and sec["cluster_token_prev"] == "cluster-tok" and sec["signing_pubkey"] == PUB
    assert cfgf["agent_token"] == "ag-new" and "cluster_token" not in cfgf
    assert c.get("/cluster/status", headers=CL).json()["signing"] == {"pinned": True, "key_id": fs.key_id(PUB)}


def test_leader_seeds_pin_from_local_secrets(tmp_path):
    # Arrange
    c, app, clock = make_hub(tmp_path, admin_pin_hash=fs.hash_pin("123456", iterations=1000))
    # Act
    clock.advance(31)
    app.state.tick()
    # Assert
    assert app.state.store.q("SELECT value FROM secrets WHERE name='admin_pin_hash'")[0]["value"].startswith("pbkdf2")
    assert app.state.store.q("SELECT value FROM secrets WHERE name='ui_token_hash'")[0]["value"] == fs.token_hash("ui-tok")


# ====================================================================== S6 signed bundles
def make_release(root: Path, version: str) -> Path:
    for d in ("hub", "agent", "watchdog"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "hub" / "app.py").write_text("app = None\n", encoding="utf-8")
    (root / "agent" / "x.ps1").write_text("# agent", encoding="utf-8")
    (root / "watchdog" / "w.ps1").write_text("# wd", encoding="utf-8")
    (root / "requirements.txt").write_text("httpx\n", encoding="utf-8")
    (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    return root


def sign_dir(root: Path, version: str, n=N, d=D) -> None:
    m = fs.bundle_manifest(root)
    (root / "bundle.sig").write_text(json.dumps({
        "v": 1, "version": version, "manifest_sha256": hashlib.sha256(m).hexdigest(),
        "sig": base64.b64encode(sign(m, n, d)).decode(), "key_id": fs.key_id(PUB)}), encoding="utf-8")


def test_manifest_format_and_exclusions(tmp_path):
    # Arrange
    r = make_release(tmp_path / "r", "9.0.0")
    (r / "bundle.zip").write_bytes(b"z")
    (r / "venv" / "Lib").mkdir(parents=True)
    (r / "venv" / "Lib" / "x.py").write_text("x")
    (r / "hub" / "__pycache__").mkdir()
    (r / "hub" / "__pycache__" / "app.cpython-310.pyc").write_bytes(b"c")
    # Act
    lines = fs.bundle_manifest(r).decode().splitlines()
    # Assert
    assert [ln.split(" ", 1)[1] for ln in lines] == ["VERSION", "agent/x.ps1", "hub/app.py", "requirements.txt",
                                                     "watchdog/w.ps1"]
    assert lines[0].split(" ")[0] == hashlib.sha256((r / "VERSION").read_bytes()).hexdigest()


@pytest.mark.parametrize("extra", ["hub/__pycache__/app.cpython-310.pyc", "hub/app.pyc", "hub/x.PYO",
                                   "agent/__PYCACHE__/z.txt", "venv/Lib/site.py"])
def test_verify_release_refuses_unsigned_code_the_manifest_skips(tmp_path, extra):
    # Arrange: a genuinely signed release plus one file the manifest never covers (S-R4)
    r = make_release(tmp_path / "r", "9.0.0")
    sign_dir(r, "9.0.0")
    assert fs.verify_release(r, KEY_XML)["version"] == "9.0.0"
    f = r / extra
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"\x00evil")
    # Act / Assert: the signature alone would still verify, the bundle is refused anyway
    assert fs.bundle_manifest(r) == fs.bundle_manifest(r)
    with pytest.raises(BundleError, match="bundle contains unsigned code"):
        fs.verify_release(r, KEY_XML)


def test_publish_refuses_signed_bundle_with_pyc_and_corrupt_key_is_a_bundle_error(hub, tmp_path):
    # Arrange
    c, app, _ = hub
    set_pin(c)
    pin = UI | {"X-Admin-Pin": "123456"}
    assert c.put("/api/security/signing-key", json={"pubkey_xml": KEY_XML}, headers=pin).status_code == 200
    root = make_release(tmp_path / "g", "9.0.0")
    sign_dir(root, "9.0.0")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        with zipfile.ZipFile(io.BytesIO(build_zip(root))) as src:
            for n in src.namelist():
                z.writestr(n, src.read(n))
        z.writestr("hub/__pycache__/app.cpython-310.pyc", b"\x00evil")
    # Act
    r1 = c.post("/cluster/publish", content=buf.getvalue(), headers=CL)
    app.state.store.x("UPDATE secrets SET value=? WHERE name='signing_pubkey'", ("<RSAKeyValue>garbage",))
    r2 = c.post("/cluster/publish", content=build_zip(root), headers=CL)
    alerts = c.get("/api/health", headers=UI).json()["alerts"]
    # Assert
    assert (r1.status_code, r1.json()["detail"]) == (400, "bundle contains unsigned code: hub/__pycache__")
    assert (r2.status_code, r2.json()["detail"]) == (500, "signing key invalid")
    assert any(a["kind"] == "bundle_unsigned" for a in alerts)
    with pytest.raises(BundleError, match="signing key invalid"):
        fs.verify_release(root, "not a key")


def test_rsa_verify_and_verify_release(tmp_path):
    # Arrange
    r = make_release(tmp_path / "r", "9.0.0")
    sign_dir(r, "9.0.0")
    # Act / Assert
    assert fs.verify_release(r, KEY_XML)["version"] == "9.0.0"
    assert fs.rsa_verify(N, E, b"hello", sign(b"hello")) and not fs.rsa_verify(N, E, b"hellO", sign(b"hello"))
    (r / "agent" / "x.ps1").write_text("# tampered", encoding="utf-8")
    with pytest.raises(BundleError, match="bundle signature invalid"):
        fs.verify_release(r, KEY_XML)
    (r / "bundle.sig").unlink()
    with pytest.raises(BundleError, match="bundle not signed"):
        fs.verify_release(r, KEY_XML)


def test_publish_with_pinned_key_refuses_unsigned_and_accepts_signed(hub, tmp_path):
    # Arrange
    c, app, _ = hub
    set_pin(c)
    pin = UI | {"X-Admin-Pin": "123456"}
    assert c.put("/api/security/signing-key", json={"pubkey_xml": KEY_XML}, headers=pin).json() == \
        {"ok": True, "key_id": fs.key_id(PUB)}
    unsigned = build_zip(make_release(tmp_path / "u", "9.0.0"))
    bad_root = make_release(tmp_path / "b", "9.0.0")
    sign_dir(bad_root, "9.0.0")
    (bad_root / "hub" / "app.py").write_text("app = 1\n", encoding="utf-8")
    good_root = make_release(tmp_path / "g", "9.0.0")
    sign_dir(good_root, "9.0.0")
    # Act
    r1 = c.post("/cluster/publish", content=unsigned, headers=CL)
    r2 = c.post("/cluster/publish", content=build_zip(bad_root), headers=CL)
    health = c.get("/api/health", headers=UI).json()
    r3 = c.post("/cluster/publish", content=build_zip(good_root), headers=CL)
    # Assert
    assert (r1.status_code, r1.json()["detail"]) == (400, "bundle not signed")
    assert (r2.status_code, r2.json()["detail"]) == (400, "bundle signature invalid")
    assert any(a["kind"] == "bundle_unsigned" for a in health["alerts"])
    assert r3.status_code == 200 and r3.json()["version"] == "9.0.0"
    stored = Path(app.state.releases.releases_dir) / "9.0.0"
    assert (stored / "bundle.sig").is_file()
    # the deterministic re-zip of a signed release still carries bundle.sig and verifies once extracted
    out = tmp_path / "x"
    zipfile.ZipFile(io.BytesIO(build_zip(stored))).extractall(out)
    assert fs.verify_release(out, KEY_XML)["version"] == "9.0.0"


def test_signing_key_put_validation(hub):
    # Arrange
    c, _, _ = hub
    set_pin(c)
    pin = UI | {"X-Admin-Pin": "123456"}
    small = f"<RSAKeyValue><Modulus>{fs._intb64((1 << 2047) + 1)}</Modulus><Exponent>AQAB</Exponent></RSAKeyValue>"
    other = f"<RSAKeyValue><Modulus>{fs._intb64((1 << 3071) + 12345)}</Modulus><Exponent>AQAB</Exponent></RSAKeyValue>"
    # Act / Assert
    assert c.get("/api/security/signing-key", headers=UI).json() == {"pinned": False, "key_id": None}
    assert c.put("/api/security/signing-key", json={"pubkey_xml": "nope"}, headers=pin).status_code == 400
    assert c.put("/api/security/signing-key", json={"pubkey_xml": small}, headers=pin).status_code == 400
    assert c.put("/api/security/signing-key", json={"pubkey_xml": KEY_XML}, headers=UI).status_code == 403
    assert c.put("/api/security/signing-key", json={"pubkey_xml": KEY_XML}, headers=pin).status_code == 200
    assert c.put("/api/security/signing-key", json={"pubkey_xml": KEY_XML}, headers=pin).status_code == 200  # same key
    assert c.put("/api/security/signing-key", json={"pubkey_xml": other}, headers=pin).status_code == 409
    assert c.put("/api/security/signing-key", json={"pubkey_xml": other, "replace": True}, headers=pin).status_code == 200
    assert c.get("/api/security/signing-key", headers=UI).json()["key_id"] == fs.key_id(other)


def test_key_id_is_canonical():
    spaced = PUB.replace("<Modulus>", "<Modulus>\n ").replace("</RSAKeyValue>", "</RSAKeyValue>\n")
    assert fs.key_id(spaced) == fs.key_id(PUB) == hashlib.sha256(PUB.encode()).hexdigest()[:16]
    assert fs.key_id(KEY_XML) == fs.key_id(PUB)       # private-key XML yields the public key's id


# ====================================================================== S1 job-token alerts
def test_job_token_alerts(hub):
    # Arrange
    c, _, _ = hub
    report(c, "good", job_token="filtered")
    report(c, "bad", job_token="unavailable", job_token_error="SaferComputeTokenFromLevel failed")
    report(c, "old", version="2.2.0")
    report(c, "wd", job_token="filtered", watchdog={"signature_refused": "9.9.9"})
    # Act
    alerts = c.get("/api/health", headers=UI).json()["alerts"]
    kinds = {(a["kind"], a["node_id"]) for a in alerts if a["kind"].startswith(("job_token", "bundle"))}
    # Assert
    assert kinds == {("job_token_full", "bad"), ("job_token_unknown", "old"), ("bundle_unsigned", "wd")}
    full = next(a for a in alerts if a["kind"] == "job_token_full")
    assert full["level"] == "error" and "SaferComputeTokenFromLevel" in full["msg"]
    nodes = {n["id"]: n for n in c.get("/api/state", headers=UI).json()["nodes"]}
    assert nodes["good"]["metrics"]["job_token"] == "filtered"


def test_pin_reset_file_clears_the_pin_everywhere(tmp_path):
    # Arrange: leader with a private dir and a PIN; a standby that adopted it (S-R9)
    c, app, clock = make_hub(tmp_path, private=True)
    login(c)
    set_pin(c)
    sec = app.state.security
    data = Path(app.state.ctx.data_dir)
    assert "admin_pin_hash" in json.loads((data / "private" / "secrets.json").read_text())
    sb, sb_app, _ = make_hub(tmp_path / "sb", private=True)
    sb_sec = sb_app.state.security
    row = dict(app.state.store.q("SELECT * FROM secrets WHERE name='admin_pin_hash'")[0])
    sb_app.state.store.x("INSERT INTO secrets(name,value,prev_value,rotated,updated) VALUES(?,?,?,?,?)",
                         (row["name"], row["value"], None, None, row["updated"]))
    sb_sec.adopt()
    assert sb_sec.value("admin_pin_hash")
    # Act
    (data / "private" / fs.PIN_RESET_FILE).write_text("{}", encoding="utf-8")
    app.state.tick()
    cleared = dict(app.state.store.q("SELECT * FROM secrets WHERE name='admin_pin_hash'")[0])
    sb_app.state.store.x("UPDATE secrets SET value=NULL, updated=? WHERE name='admin_pin_hash'", (cleared["updated"],))
    sb_sec.adopt()
    # Assert
    assert cleared["value"] is None and not (data / "private" / fs.PIN_RESET_FILE).exists()
    assert "admin_pin_hash" not in json.loads((data / "private" / "secrets.json").read_text())
    assert c.get("/api/stepup").json() == {"pin_set": False, "valid_until": None}
    assert fs.load_config(data).get("admin_pin_hash") is None                       # stays cleared after a restart
    assert sb_sec.value("admin_pin_hash") is None and "admin_pin_hash" not in sb_app.state.ctx.cfg
    assert "admin_pin_hash" not in json.loads((Path(sb_app.state.ctx.data_dir) / "private" / "secrets.json").read_text())
    events = [e["msg"] for e in app.state.store.q("SELECT msg FROM events")]
    assert any("admin PIN cleared by the local reset file" in m for m in events)
    set_pin(c, "654321")                                                            # a new PIN can be set again
    assert fs.verify_pin("654321", sec.value("admin_pin_hash"))


def test_rotate_export_for_r6(hub):
    c, app, _ = hub
    out = fs.rotate(app.state.ctx, ["agent", "bogus"])
    assert out["rotated"] == ["agent"] and app.state.ctx.cfg["agent_token"] != "agent-tok"
