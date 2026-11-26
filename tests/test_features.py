"""Single-hub features: auto targeting, offline jobs, settings, join/bootstrap, bundles, health, legacy compat."""
import hashlib
import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import create_app
from cluster_net import TOKENS, FakeClock
from forge_store import new_id

ROOT = Path(__file__).resolve().parents[1]
UI = {"Authorization": f"Bearer {TOKENS['ui_token']}"}
AGENT = {"X-Agent-Token": TOKENS["agent_token"]}
CL = {"X-Cluster-Token": TOKENS["cluster_token"]}


def make_release(root: Path, version: str, app_src: str = "app = None\n") -> Path:
    (root / "hub").mkdir(parents=True, exist_ok=True)
    (root / "agent").mkdir(exist_ok=True)
    (root / "hub" / "app.py").write_text(app_src, encoding="utf-8")
    (root / "agent" / "x.ps1").write_text("# agent", encoding="utf-8")
    (root / "requirements.txt").write_text("httpx\n", encoding="utf-8")
    (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    return root


def zip_of(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return buf.getvalue()


@pytest.fixture
def hub(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps(TOKENS | {"node_id": "hub1"}), encoding="utf-8")
    rel = make_release(tmp_path / "release", "1.0.0")
    clock = FakeClock()
    app = create_app(data, start_monitor=False, clock=clock, release_dir=rel, releases_dir=tmp_path / "releases")
    return TestClient(app), app, clock


def report(c, node, cpu=10, cap=2, tools=("claude", "codex"), **extra):
    body = {"node_id": node, "metrics": {"cpu": cpu}, "capacity": cap, "info": {"tools": list(tools)}} | extra
    return c.post("/api/agent/report", json=body, headers=AGENT)


def mkjob(c, node="auto", engine="claude"):
    return c.post("/api/jobs", json={"node_id": node, "engine": engine, "prompt": "p"}, headers=UI).json()


# ---------------------------------------------------------------- ids / storage
def test_ids_are_text_and_time_sortable():
    ids = []
    for t in (1_700_000_000.0, 1_700_000_001.0, 1_700_000_500.0):
        ids.append(new_id(lambda t=t: t))
    assert all(isinstance(i, str) and len(i) == 17 for i in ids) and ids == sorted(ids)


def test_v1_database_is_archived_not_modified(tmp_path):
    db = tmp_path / "forge.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, node_id TEXT)")
    con.execute("INSERT INTO jobs(node_id) VALUES('old')")
    con.commit()
    con.close()
    app = create_app(tmp_path, start_monitor=False, clock=FakeClock(), releases_dir=tmp_path / "r")
    assert list(tmp_path.glob("forge.db.v1-*.bak"))
    assert app.state.store.q("SELECT COUNT(*) n FROM jobs")[0]["n"] == 0


# ---------------------------------------------------------------- auto target
def test_auto_picks_lowest_cpu_node_that_has_the_engine(hub):
    c, _, _ = hub
    report(c, "busy", cpu=80)
    report(c, "idle", cpu=5, tools=("codex",))   # lacks claude
    report(c, "calm", cpu=20)
    j = mkjob(c, engine="claude")
    row = c.get(f"/api/jobs/{j['id']}", headers=UI).json()
    assert row["node_id"] == "calm" and row["requested"] == "auto"
    assert mkjob(c, engine="shell")["node_id"] == "idle"


def test_auto_tie_breaks_on_free_capacity_then_priority(hub):
    c, app, _ = hub
    report(c, "n1", cpu=10, cap=1)
    report(c, "n2", cpu=10, cap=3)
    assert mkjob(c)["node_id"] == "n2"          # more free capacity
    app.state.store.x("DELETE FROM jobs")
    report(c, "n3", cpu=10, cap=3)
    for nid, pr in (("n2", 5), ("n3", 2)):
        app.state.store.x("INSERT OR REPLACE INTO peers(id,priority,urls,updated) VALUES(?,?,?,?)", (nid, pr, "[]", 1.0))
    assert mkjob(c)["node_id"] == "n3"          # equal cpu and capacity: lowest priority number


def test_auto_waits_until_a_capable_node_reports(hub):
    c, _, _ = hub
    j = mkjob(c)
    assert j["node_id"] == "auto"
    report(c, "late", cap=0)                     # no capacity: still waiting
    assert c.get(f"/api/jobs/{j['id']}", headers=UI).json()["node_id"] == "auto"
    r = report(c, "late", cap=1).json()
    assert [x["id"] for x in r["jobs"]] == [j["id"]]
    assert c.get(f"/api/jobs/{j['id']}", headers=UI).json()["node_id"] == "late"


def test_auto_skips_offline_nodes(hub):
    c, _, clock = hub
    report(c, "gone", cpu=1)
    clock.advance(60)
    report(c, "here", cpu=90)
    assert mkjob(c)["node_id"] == "here"


# ---------------------------------------------------------------- offline job loss
def test_running_job_fails_after_node_offline_for_90s(hub):
    c, app, clock = hub
    report(c, "w")
    jid = mkjob(c, "w", "shell")["id"]
    report(c, "w")
    c.post(f"/api/agent/jobs/{jid}/update", json={"status": "running"}, headers=AGENT)
    clock.advance(70)
    report(c, "w")                    # Phase 2: a job lost within 60 s of starting is requeued, so let it run a while
    clock.advance(60)
    app.state.check_presence()
    assert c.get(f"/api/jobs/{jid}", headers=UI).json()["status"] == "running"   # offline but < 90 s
    clock.advance(40)
    app.state.check_presence()
    j = c.get(f"/api/jobs/{jid}", headers=UI).json()
    assert j["status"] == "failed" and "[hub] node offline; job lost" in j["output"] and j["finished"]


def test_queued_jobs_survive_node_offline(hub):
    c, app, clock = hub
    report(c, "w", cap=0)
    jid = mkjob(c, "w", "shell")["id"]
    clock.advance(500)
    app.state.check_presence()
    assert c.get(f"/api/jobs/{jid}", headers=UI).json()["status"] == "queued"


# ---------------------------------------------------------------- settings
def test_settings_round_trip_and_reach_the_agent(hub):
    c, _, _ = hub
    assert report(c, "w").json()["settings"] == {"work_root": None, "max_jobs": None}
    r = c.put("/api/nodes/w/settings", json={"work_root": "D:\\forge", "max_jobs": 3}, headers=UI)
    assert r.json() == {"work_root": "D:\\forge", "max_jobs": 3, "canary": False}
    assert report(c, "w").json()["settings"] == {"work_root": "D:\\forge", "max_jobs": 3}
    assert c.get("/api/state", headers=UI).json()["nodes"][0]["settings"]["max_jobs"] == 3
    c.put("/api/nodes/w/settings", json={"work_root": ""}, headers=UI)          # partial update; "" clears
    assert report(c, "w").json()["settings"] == {"work_root": None, "max_jobs": 3}


def test_settings_validation_and_auth(hub):
    c, _, _ = hub
    report(c, "w")
    assert c.put("/api/nodes/w/settings", json={"max_jobs": 0}, headers=UI).status_code == 400
    assert c.put("/api/nodes/w/settings", json={"max_jobs": "x"}, headers=UI).status_code == 400
    assert c.put("/api/nodes/ghost/settings", json={"max_jobs": 2}, headers=UI).status_code == 404
    assert c.put("/api/nodes/w/settings", json={"max_jobs": 2}).status_code == 401


def test_report_response_has_cluster_fields_and_work_root_metrics_are_kept(hub):
    c, _, _ = hub
    m = {"cpu": 1, "work_root": "D:\\forge", "work_root_free_gb": 123.4}
    r = c.post("/api/agent/report", json={"node_id": "w", "metrics": m, "capacity": 1}, headers=AGENT).json()
    assert r["bundle_version"] == "1.0.0" and r["hub_urls"] == []
    node = c.get("/api/state", headers=UI).json()["nodes"][0]
    assert node["metrics"]["work_root"] == "D:\\forge" and node["metrics"]["work_root_free_gb"] == 123.4


# ---------------------------------------------------------------- bootstrap / join / peers
def test_bootstrap_requires_agent_token_and_returns_secrets_and_peers(hub):
    c, _, _ = hub
    assert c.get("/cluster/bootstrap").status_code == 401
    assert c.get("/cluster/bootstrap?t=nope").status_code == 401
    b = c.get(f"/cluster/bootstrap?t={TOKENS['agent_token']}").json()
    # 2.3 S2/S3: the raw ui token is never handed out again; 2.3 S-R1: the agent token (readable by filtered jobs)
    # no longer buys the cluster token either (an enrollment code does, see test_p3_security)
    assert not {"ui_token", "ui_token_hash", "cluster_token", "signing_pubkey"} & set(b)
    assert b["agent_token"] == TOKENS["agent_token"]
    assert [p["id"] for p in b["peers"]] == ["hub1"]
    assert b["bundle_version"] == "1.0.0" and len(b["bundle_sha256"]) == 64


def test_join_requires_cluster_token_and_assigns_priority(hub):
    c, _, _ = hub
    assert c.post("/cluster/join", json={"id": "x", "urls": []}).status_code == 401
    assert c.post("/cluster/join", json={"id": "x", "urls": []}, headers=AGENT).status_code == 401
    r1 = c.post("/cluster/join", json={"id": "Box2", "urls": ["http://box2:8787", "bad", "http://1.2.3.4:8787/"]}, headers=CL)
    assert r1.json() == {"id": "box2", "priority": 2, "urls": ["http://box2:8787", "http://1.2.3.4:8787"]}
    r2 = c.post("/cluster/join", json={"id": "box2", "urls": ["http://new:8787"]}, headers=CL)
    assert r2.json()["priority"] == 2 and r2.json()["urls"] == ["http://new:8787"]   # keeps priority
    assert c.post("/cluster/join", json={"id": "box3", "urls": []}, headers=CL).json()["priority"] == 3
    assert report(c, "w").json()["hub_urls"] == ["http://new:8787"]


def test_peer_patch_and_delete(hub):
    c, _, _ = hub
    c.post("/cluster/join", json={"id": "box2", "urls": []}, headers=CL)
    assert c.patch("/api/peers/box2", json={"priority": 1}, headers=UI).json() == {"id": "box2", "priority": 1}
    assert c.patch("/api/peers/box2", json={"priority": 0}, headers=UI).status_code == 422
    assert c.patch("/api/peers/ghost", json={"priority": 3}, headers=UI).status_code == 404
    assert c.patch("/api/peers/box2", json={"priority": 3}).status_code == 401
    assert c.delete("/api/peers/box2", headers=UI).json()["step_up"] == "setup"      # 2.3 S7: admin PIN first
    c.put("/api/stepup/pin", json={"pin": "123456"}, headers=UI)
    pin = UI | {"X-Admin-Pin": "123456"}
    assert c.delete("/api/peers/hub1", headers=pin).status_code == 400
    assert c.delete("/api/peers/box2", headers=pin).status_code == 200
    ids = [p["id"] for p in c.get("/api/state", headers=UI).json()["cluster"]["peers"]]
    assert ids == ["hub1"]


# ---------------------------------------------------------------- bundles
def test_status_sha_matches_served_bundle(hub):
    c, _, _ = hub
    st = c.get("/cluster/status", headers=CL).json()
    r = c.get("/cluster/bundle", headers=CL)
    assert r.headers["content-type"] == "application/zip"
    assert hashlib.sha256(r.content).hexdigest() == st["bundle_sha256"] == r.headers["x-bundle-sha256"]
    assert st["version"] == r.headers["x-bundle-version"] == "1.0.0"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "VERSION" in names and "hub/app.py" in names and "requirements.txt" in names
    assert c.get("/cluster/bundle").status_code == 401


def test_bundle_is_deterministic_across_nodes(tmp_path):
    rel = make_release(tmp_path / "rel", "1.0.0")
    shas = set()
    for n in "ab":
        d = tmp_path / n
        d.mkdir()
        (d / "config.json").write_text(json.dumps(TOKENS), encoding="utf-8")
        app = create_app(d, start_monitor=False, clock=FakeClock(), release_dir=rel, releases_dir=d / "r")
        shas.add(app.state.releases.latest()[1])
    assert len(shas) == 1


def test_publish_stores_release_and_advertises_it(hub, tmp_path):
    c, _, _ = hub
    z = zip_of({"VERSION": "1.1.0\n", "hub/app.py": "x = 1\n", "requirements.txt": "httpx\n"})
    r = c.post("/cluster/publish", content=z, headers=CL)
    assert r.status_code == 200 and r.json()["version"] == "1.1.0"
    assert (tmp_path / "releases" / "1.1.0" / "hub" / "app.py").exists()      # extracted for the watchdog
    st = c.get("/cluster/status", headers=CL).json()
    assert st["version"] == "1.1.0" and st["running_version"] == "1.0.0"
    assert st["bundle_sha256"] == hashlib.sha256(z).hexdigest()
    assert c.get("/cluster/bundle", headers=CL).content == z                  # default = latest available
    assert c.get("/cluster/bundle?version=1.0.0", headers=CL).headers["x-bundle-version"] == "1.0.0"
    assert c.get("/cluster/bundle?version=9.9.9", headers=CL).status_code == 404
    assert report(c, "w").json()["bundle_version"] == "1.1.0"


@pytest.mark.parametrize("version", ["1.0.0", "0.9.9", "1.0.0.1x", "banana"])
def test_publish_rejects_equal_lower_or_malformed_version(hub, version):
    c, _, _ = hub
    z = zip_of({"VERSION": version, "hub/app.py": "x = 1\n"})
    assert c.post("/cluster/publish", content=z, headers=CL).status_code in (400, 409)
    assert c.get("/cluster/status", headers=CL).json()["version"] == "1.0.0"


def test_publish_rejects_non_compiling_app_bad_zip_missing_files_and_zip_slip(hub):
    c, _, _ = hub
    bad = zip_of({"VERSION": "2.0.0", "hub/app.py": "def broken(:\n"})
    r = c.post("/cluster/publish", content=bad, headers=CL)
    assert r.status_code == 400 and "compile" in r.json()["detail"]
    assert c.post("/cluster/publish", content=b"not a zip", headers=CL).status_code == 400
    assert c.post("/cluster/publish", content=zip_of({"VERSION": "2.0.0"}), headers=CL).status_code == 400
    slip = zip_of({"VERSION": "2.0.0", "hub/app.py": "x=1", "../evil.txt": "x"})
    assert c.post("/cluster/publish", content=slip, headers=CL).status_code == 400
    assert c.get("/cluster/status", headers=CL).json()["version"] == "1.0.0"


def test_publish_requires_cluster_token_and_second_publish_must_be_newer(hub):
    c, _, _ = hub
    z = zip_of({"VERSION": "1.2.0", "hub/app.py": "x = 1\n"})
    assert c.post("/cluster/publish", content=z).status_code == 401
    assert c.post("/cluster/publish", content=z, headers=AGENT).status_code == 401
    assert c.post("/cluster/publish", content=z, headers=CL).status_code == 200
    assert c.post("/cluster/publish", content=z, headers=CL).status_code == 409


# ---------------------------------------------------------------- health
def test_health_alerts(hub):
    c, _, clock = hub
    assert c.get("/api/health").status_code == 401
    old = clock()
    c.post("/api/agent/report", headers=AGENT, json={
        "node_id": "full", "capacity": 1, "bundle_version": "0.9.0",
        "metrics": {"cpu": 1, "disks": [{"name": "C:", "total_gb": 100, "free_gb": 5}],
                    "watchdog": {"last_run": old - 1000, "actions": ["restarted hub"]}}})
    c.post("/api/agent/report", headers=AGENT, json={
        "node_id": "fine", "capacity": 1, "bundle_version": "1.0.0",
        "metrics": {"cpu": 1, "watchdog": {"last_run": "2026-01-01T00:00:00.1234567+00:00"}}})
    report(c, "legacy")                                    # no watchdog data: no stale alert
    clock.advance(40)
    report(c, "full2")                                     # keep clock-based nodes distinct
    h = c.get("/api/health", headers=UI).json()
    kinds = {(a["kind"], a["node_id"]) for a in h["alerts"]}
    assert ("node_offline", "full") in kinds and ("node_offline", "fine") in kinds
    # refresh the nodes so they are online again and inspect the per-node alerts
    c.post("/api/agent/report", headers=AGENT, json={
        "node_id": "full", "capacity": 1, "bundle_version": "0.9.0",
        "metrics": {"cpu": 1, "disks": [{"name": "C:", "total_gb": 100, "free_gb": 5}],
                    "watchdog": {"last_run": clock() - 1000, "actions": ["restarted hub"]}}})
    h = c.get("/api/health", headers=UI).json()
    kinds = {(a["kind"], a["node_id"]) for a in h["alerts"]}
    assert {("disk_full", "full"), ("version_behind", "full"), ("watchdog_stale", "full")} <= kinds
    assert not any(a["node_id"] == "legacy" for a in h["alerts"] if a["kind"] == "watchdog_stale")
    full = [n for n in h["nodes"] if n["id"] == "full"][0]
    assert full["behind"] and full["watchdog_last_action"] == "restarted hub"
    assert h["running_version"] == "1.0.0" and h["leader_id"] == "hub1"
    assert h["rollout"] == [{"node_id": "hub1", "version": "1.0.0", "status": "current"}]


def test_single_element_lists_from_ps51_are_normalized(hub):
    # Arrange: PowerShell 5.1 sends a one-item array as a bare object
    c, _, _ = hub
    body = {"node_id": "onedisk", "capacity": 1,
            "metrics": {"cpu": 5, "disks": {"name": "C:", "total_gb": 100, "free_gb": 20},
                        "gpu": {"name": "T1200", "util": 7}, "ips": {"iface": "Wi-Fi", "ip": "192.168.1.5"}},
            "agents": {"kind": "claude", "pid": 1}, "procs": {"name": "x", "pid": 2},
            "info": {"hostname": "ONEDISK", "gpus": "T1200", "tools": "claude"}}
    # Act
    assert c.post("/api/agent/report", headers=AGENT, json=body).status_code == 200
    s = c.get("/api/state", headers=UI)
    # Assert
    assert s.status_code == 200
    n = [x for x in s.json()["nodes"] if x["id"] == "onedisk"][0]
    assert n["disk_pct"] == 80.0 and isinstance(n["metrics"]["disks"], list)
    assert n["agents"] == [{"kind": "claude", "pid": 1}] and n["info"]["tools"] == ["claude"]
    assert c.get("/api/nodes/onedisk", headers=UI).status_code == 200


# ---------------------------------------------------------------- legacy v1 agent compatibility
def test_legacy_and_v2_agent_scripts_and_installer(tmp_path):
    rel = make_release(tmp_path / "rel", "1.0.0")
    (rel / "agent" / "legacy").mkdir()
    (rel / "agent" / "legacy" / "forge-agent-v1.ps1").write_text("$AgentVersion = '1.1.1'\n# v1\n", encoding="utf-8")
    (rel / "agent" / "forge-agent.ps1").write_text("$AgentVersion = '2.0.0'\n# v2\n", encoding="utf-8")
    (rel / "agent" / "install.ps1").write_text("hub=__HUB__ tok=__TOKEN__", encoding="utf-8")
    d = tmp_path / "d"
    d.mkdir()
    (d / "config.json").write_text(json.dumps(TOKENS), encoding="utf-8")
    c = TestClient(create_app(d, start_monitor=False, clock=FakeClock(), release_dir=rel, releases_dir=d / "r"))
    assert "# v1" in c.get("/agent/forge-agent.ps1").text
    assert "# v2" in c.get("/agent/v2/forge-agent.ps1").text
    assert report(c, "w").json()["agent_version"] == "1.1.1"
    assert c.get("/agent/install.ps1?t=bad").status_code == 401
    assert c.get(f"/agent/install.ps1?t={TOKENS['agent_token']}").text == f"hub=http://testserver tok={TOKENS['agent_token']}"


def test_v1_agent_flow_with_string_job_ids(hub):
    c, _, _ = hub
    report(c, "w")
    jid = mkjob(c, "w", "shell")["id"]
    jobs = report(c, "w").json()["jobs"]
    assert jobs[0]["id"] == jid and isinstance(jid, str)
    assert c.post(f"/api/agent/jobs/{jid}/update", json={"status": "done", "exit_code": 0, "append": "ok"},
                  headers=AGENT).json() == {"ok": True}
    assert c.post("/api/agent/jobs/123/update", json={"status": "done"}, headers=AGENT).status_code == 404
