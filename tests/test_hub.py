from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import create_app
from cluster_net import FakeClock


@pytest.fixture
def ctx(tmp_path):
    app = create_app(tmp_path, start_monitor=False, clock=FakeClock(), releases_dir=tmp_path / "rel")
    c = TestClient(app)
    cfg = app.state.cfg
    agent = {"X-Agent-Token": cfg["agent_token"]}
    ui = {"Authorization": f"Bearer {cfg['ui_token']}"}
    return c, app, agent, ui


def report(c, agent, **extra):
    body = {"node_id": "Box1", "metrics": {"cpu": 12, "mem_pct": 40, "disks": [{"name": "C:", "total_gb": 100, "free_gb": 25}]},
            "procs": [], "agents": [], "capacity": 2} | extra
    return c.post("/api/agent/report", json=body, headers=agent)


def test_endpoints_require_auth(ctx):
    c, _, _, _ = ctx
    assert c.get("/api/state").status_code == 401
    assert c.post("/api/agent/report", json={"node_id": "x"}, headers={"X-Agent-Token": "nope"}).status_code == 401
    assert c.post("/api/login", json={"token": "nope"}).status_code == 401


def test_report_registers_node_and_state_shows_it(ctx):
    c, _, agent, ui = ctx
    r = report(c, agent, info={"hostname": "BOX1"})
    assert r.status_code == 200 and r.json()["jobs"] == []
    s = c.get("/api/state", headers=ui).json()
    node = s["nodes"][0]
    assert node["id"] == "box1" and node["online"] and node["disk_pct"] == 75.0
    assert any("joined" in e["msg"] for e in s["events"])


def test_job_lifecycle_dispatch_output_and_finish(ctx):
    c, _, agent, ui = ctx
    report(c, agent)
    jid = c.post("/api/jobs", json={"node_id": "box1", "engine": "claude", "prompt": "hi"}, headers=ui).json()["id"]
    # dispatched exactly once
    jobs = report(c, agent).json()["jobs"]
    assert [j["id"] for j in jobs] == [jid] and jobs[0]["full_access"] is False
    assert report(c, agent).json()["jobs"] == []
    c.post(f"/api/agent/jobs/{jid}/update", json={"status": "running", "pid": 42, "append": "a"}, headers=agent)
    c.post(f"/api/agent/jobs/{jid}/update", json={"status": "done", "exit_code": 0, "append": "b"}, headers=agent)
    j = c.get(f"/api/jobs/{jid}", headers=ui).json()
    assert j["status"] == "done" and j["output"] == "ab" and j["pid"] == 42 and j["finished"]


def test_capacity_zero_holds_jobs(ctx):
    c, _, agent, ui = ctx
    report(c, agent)
    c.post("/api/jobs", json={"node_id": "box1", "engine": "shell", "prompt": "dir"}, headers=ui)
    assert report(c, agent, capacity=0).json()["jobs"] == []
    assert len(report(c, agent).json()["jobs"]) == 1


def test_cancel_queued_and_running(ctx):
    c, _, agent, ui = ctx
    report(c, agent)
    q = c.post("/api/jobs", json={"node_id": "box1", "engine": "shell", "prompt": "x"}, headers=ui).json()["id"]
    c.post(f"/api/jobs/{q}/cancel", headers=ui)
    assert c.get(f"/api/jobs/{q}", headers=ui).json()["status"] == "cancelled"
    r = c.post("/api/jobs", json={"node_id": "box1", "engine": "shell", "prompt": "y"}, headers=ui).json()["id"]
    report(c, agent)
    c.post(f"/api/jobs/{r}/cancel", headers=ui)
    assert report(c, agent).json()["cancel"] == [r]


def test_rejects_bad_engine_and_unknown_node(ctx):
    c, _, agent, ui = ctx
    report(c, agent)
    assert c.post("/api/jobs", json={"node_id": "box1", "engine": "rm", "prompt": "x"}, headers=ui).status_code == 400
    assert c.post("/api/jobs", json={"node_id": "ghost", "engine": "shell", "prompt": "x"}, headers=ui).status_code == 404


def test_agent_boot_fails_orphaned_jobs(ctx):
    c, _, agent, ui = ctx
    report(c, agent)
    jid = c.post("/api/jobs", json={"node_id": "box1", "engine": "shell", "prompt": "x"}, headers=ui).json()["id"]
    report(c, agent)
    report(c, agent, boot=True, capacity=0)
    assert c.get(f"/api/jobs/{jid}", headers=ui).json()["status"] == "failed"


def test_presence_marks_offline(ctx):
    c, app, agent, ui = ctx
    report(c, agent)
    app.state.check_presence(app.state.store.now())
    app.state.check_presence(app.state.store.now() + 120)
    msgs = [e["msg"] for e in c.get("/api/state", headers=ui).json()["events"]]
    assert "went offline" in msgs


def test_report_advertises_legacy_agent_version_so_v1_agents_never_jump_to_v2(ctx):
    c, _, agent, _ = ctx
    legacy = (Path(__file__).resolve().parents[1] / "agent" / "legacy" / "forge-agent-v1.ps1").read_text(encoding="utf-8")
    import re
    expected = re.search(r"\$AgentVersion\s*=\s*'([^']+)'", legacy).group(1)
    assert expected == "1.1.1"
    assert report(c, agent).json()["agent_version"] == expected
    assert c.get("/agent/forge-agent.ps1").text == legacy


def test_installer_embeds_hub_and_token(ctx):
    c, app, _, _ = ctx
    tok = app.state.cfg["agent_token"]
    assert c.get("/agent/install.ps1?t=bad").status_code == 401
    body = c.get(f"/agent/install.ps1?t={tok}").text
    assert tok in body and "http://testserver" in body and "__HUB__" not in body
