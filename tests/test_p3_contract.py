"""Phase 3 plug-in wiring (CONTRACT-phase3.md §2): ctx exists, hooks are called, module routes win, hook errors are contained."""
from fastapi import Depends
from fastapi.testclient import TestClient

from app import create_app
from forge_store import REPLICATED


def make(tmp_path):
    app = create_app(tmp_path, start_monitor=False, node_id="h1")
    cfg = app.state.cfg
    return app, TestClient(app), {"X-Agent-Token": cfg["agent_token"]}, {"Authorization": "Bearer " + cfg["ui_token"]}


def test_ctx_is_built_and_p3_tables_replicate(tmp_path):
    # Arrange / Act
    app, _, _, _ = make(tmp_path)
    ctx = app.state.ctx
    # Assert
    assert ctx.node_id == "h1" and ctx.store is app.state.store and ctx.require_ui is not None
    assert {"ui_sessions", "enroll_codes", "secrets", "job_output", "alerts", "push_subs", "prefs",
            "maintenance_runs", "retirements"} <= set(REPLICATED)
    assert "presence" not in REPLICATED and "healthz_probe" not in REPLICATED


def test_report_hook_mutates_response_and_failing_hook_is_contained(tmp_path):
    # Arrange
    app, c, agent, _ = make(tmp_path)
    ctx = app.state.ctx
    ctx.on_report.append(lambda nid, body, resp, now: 1 / 0)
    ctx.on_report.append(lambda nid, body, resp, now: resp.update(wol=[{"mac": "AA-BB-CC-DD-EE-FF"}]))
    # Act
    r = c.post("/api/agent/report", headers=agent, json={"node_id": "n1", "metrics": {}, "capacity": 1})
    # Assert
    assert r.status_code == 200 and r.json()["wol"] == [{"mac": "AA-BB-CC-DD-EE-FF"}]


def test_job_finished_hook_and_alert_source(tmp_path):
    # Arrange
    app, c, agent, ui = make(tmp_path)
    ctx = app.state.ctx
    seen = []
    ctx.on_job_finished.append(lambda job, status, out, now: seen.append((job["id"], status)))
    ctx.alert_sources.append(lambda now: [{"level": "error", "kind": "job_token_full", "node_id": "n1", "msg": "x"}])
    c.post("/api/agent/report", headers=agent, json={"node_id": "n1", "metrics": {}, "capacity": 0})
    jid = c.post("/api/jobs", headers=ui, json={"node_id": "n1", "engine": "shell", "prompt": "echo hi"}).json()["id"]
    jobs = c.post("/api/agent/report", headers=agent, json={"node_id": "n1", "metrics": {}, "capacity": 1}).json()["jobs"]
    # Act
    c.post(f"/api/agent/jobs/{jid}/update", headers=agent, json={"status": "done", "exit_code": 0, "node_id": "n1"})
    health = c.get("/api/health", headers=ui).json()
    # Assert
    assert [j["id"] for j in jobs] == [jid] and seen == [(jid, "done")]
    assert any(a["kind"] == "job_token_full" for a in health["alerts"])


def test_module_route_replaces_core_route_and_slots_override(tmp_path, monkeypatch):
    # Arrange: a module that replaces /healthz via the slot and adds a route
    import forge_ui

    def register(app, ctx):
        ctx.healthz = lambda: (503, {"ok": False, "node_id": ctx.node_id})

        @app.get("/api/today", dependencies=[Depends(ctx.require_ui)])
        def today() -> dict:
            return {"ok": True}

    monkeypatch.setattr(forge_ui, "register", register)
    app, c, _, ui = make(tmp_path)
    # Act / Assert
    assert c.get("/healthz").status_code == 503
    assert c.get("/api/today").status_code == 401 and c.get("/api/today", headers=ui).json() == {"ok": True}
