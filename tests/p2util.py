"""Shared helpers for the Phase 2 tests (single in-process hub with a fake clock)."""
import json

from fastapi.testclient import TestClient

from app import create_app
from cluster_net import TOKENS, FakeClock

UI = {"Authorization": f"Bearer {TOKENS['ui_token']}"}
AGENT = {"X-Agent-Token": TOKENS["agent_token"]}
CL = {"X-Cluster-Token": TOKENS["cluster_token"]}


STEP_PIN = "123456"


def admin(c):
    """UI headers plus the per-request admin PIN (2.3 S7 step-up); sets the PIN on first use."""
    if not c.get("/api/stepup", headers=UI).json().get("pin_set"):
        assert c.put("/api/stepup/pin", json={"pin": STEP_PIN}, headers=UI).status_code == 200
    return UI | {"X-Admin-Pin": STEP_PIN}


def make_hub(tmp_path, cfg_extra=None):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps(TOKENS | {"node_id": "hub1"} | (cfg_extra or {})), encoding="utf-8")
    clock = FakeClock()
    app = create_app(data, start_monitor=False, clock=clock, releases_dir=tmp_path / "releases")
    app.state.store.db.execute("PRAGMA synchronous=OFF")
    return TestClient(app), app, clock


def report(c, node, cpu=10, cap=2, tools=("claude", "codex"), version="2.2.0", gpu=None, **extra):
    metrics = {"cpu": cpu} | ({"gpu": gpu} if gpu else {})
    body = {"node_id": node, "metrics": metrics, "capacity": cap,
            "info": {"tools": list(tools), "agent_version": version} if version else {"tools": list(tools)}} | extra
    return c.post("/api/agent/report", json=body, headers=AGENT)


def job(c, node="auto", engine="claude", **kw):
    r = c.post("/api/jobs", json={"node_id": node, "engine": engine, "prompt": kw.pop("prompt", "p")} | kw, headers=UI)
    return r.json() | {"_status": r.status_code}


def get_job(c, jid):
    return c.get(f"/api/jobs/{jid}", headers=UI).json()
