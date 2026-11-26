"""Contract test: the response fields hub/static/index.html reads, checked against the real Phase 2 hub.

Skipped when the hub in this checkout has no Phase 2 code (forge_phase2). To check this UI branch against another
checkout's hub, run this file alone with FORGE_CONTRACT_HUB=<path to that checkout's hub/ folder>.
"""
import json
import os
import sys

import pytest

if os.environ.get("FORGE_CONTRACT_HUB"):
    sys.path.insert(0, os.environ["FORGE_CONTRACT_HUB"])

pytest.importorskip("forge_phase2", reason="hub has no Phase 2 API in this checkout")
from fastapi.testclient import TestClient  # noqa: E402

import forge_projects as fp  # noqa: E402
from app import create_app  # noqa: E402

TOKENS = {"ui_token": "ui-tok", "agent_token": "agent-tok", "cluster_token": "cluster-tok"}
UI = {"Authorization": f"Bearer {TOKENS['ui_token']}"}
AGENT = {"X-Agent-Token": TOKENS["agent_token"]}
T0 = 1_700_000_000.0


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


@pytest.fixture
def hub(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps(TOKENS | {"node_id": "hub1"}), encoding="utf-8")
    clock = Clock()
    app = create_app(data, start_monitor=False, clock=clock, releases_dir=tmp_path / "releases")
    c = TestClient(app)
    tools = ["claude", "codex", "ollama"]
    for n in ("m1", "hub1"):
        assert c.post("/api/agent/report", headers=AGENT, json={
            "node_id": n, "capacity": 10, "metrics": {"cpu": 10, "ips": [{"iface": "Ethernet", "ip": "192.168.1.9"}]},
            "info": {"tools": tools, "agent_version": "2.2.0"}}).status_code == 200
    sess = {"tool": "claude", "cwd": "C:\\dev\\forge", "git_remote": "https://github.com/me/forge.git", "git_branch": "main",
            "title": "do thing", "started": T0 - 600, "last_activity": T0 - 5, "messages": 4, "state": "live"}
    assert c.post("/api/agent/sessions", headers=AGENT, json={"node_id": "m1", "sessions": [
        sess | {"session_id": "term", "pid": 4242}, sess | {"session_id": "free", "pid": None}]}).status_code == 200
    return c, app, clock


def post_events(c, sid, n, start=0):
    evs = [{"ts": T0 + i, "role": "assistant", "kind": "text", "text": f"e{i}", "collapsed": False} for i in range(start, start + n)]
    r = c.post("/api/agent/transcript", headers=AGENT, json={"node_id": "m1", "tool": "claude", "session_id": sid,
                                                           "cursor": f"off:{start + n}", "events": evs, "done": False})
    assert r.status_code == 200


def test_state_peers_carry_urls_and_jobs_hide_briefs(hub):
    c, _, _ = hub
    s = c.get("/api/state", headers=UI).json()
    for k in ("nodes", "jobs", "events", "cluster"):
        assert k in s
    peers = s["cluster"]["peers"]
    assert peers and all(isinstance(p.get("urls"), list) for p in peers)        # t3Url reads peer.urls (not peer.url)
    for k in ("leader_id", "viewing"):
        assert k in s["cluster"]
    n = next(x for x in s["nodes"] if x["id"] == "m1")
    for k in ("online", "addr", "metrics", "info", "system_lane"):
        assert k in n


def test_brief_all_is_wrapped(hub):
    c, _, _ = hub
    r = c.get("/api/brief/all", headers=UI).json()
    assert "brief" in r and r["brief"] is None                                  # UI unwraps raw.brief (hub 2.2.1 also adds flat fields)
    assert all(r.get(k) is None for k in ("text", "ts", "model", "job_id"))


def test_transcript_shape_and_cursor_goes_down_after_hub_restart(hub):
    c, app, _ = hub
    w = c.post("/api/sessions/claude/free/watch", headers=UI).json()
    assert w["ok"] is True and "ttl_s" in w
    post_events(c, "free", 5)
    t = c.get("/api/sessions/claude/free/transcript", headers=UI).json()
    assert {"events", "cursor", "done", "available"} <= set(t)
    assert [e["seq"] for e in t["events"]] == [1, 2, 3, 4, 5] and t["cursor"] == 5 and t["available"] is True
    for k in ("ts", "role", "kind", "text", "collapsed"):
        assert k in t["events"][0]
    assert c.get("/api/sessions/claude/free/transcript?since=5", headers=UI).json()["events"] == []
    # Leader restart / failover: transcripts live in memory, so seq numbering starts again at 1.
    app.state.p2.eph = fp.Ephemeral()
    t2 = c.get("/api/sessions/claude/free/transcript?since=5", headers=UI).json()
    assert t2["cursor"] < 5 and t2["available"] is False                        # UI must treat a lower cursor as "reset"
    post_events(c, "free", 2, start=100)
    t3 = c.get("/api/sessions/claude/free/transcript?since=5", headers=UI).json()
    assert t3["cursor"] == 2 and t3["events"] == []                             # old cursor would hide new events forever
    assert [e["seq"] for e in c.get("/api/sessions/claude/free/transcript?since=0", headers=UI).json()["events"]] == [1, 2]


def test_continue_409_then_force_and_job_response_shape(hub):
    c, _, _ = hub
    body = {"node_id": "m1", "prompt": "carry on", "resume": {"tool": "claude", "session_id": "term"}, "source": "ui"}
    r = c.post("/api/jobs", json=body, headers=UI)
    assert r.status_code == 409 and "live in a terminal" in r.json()["detail"]
    ok = c.post("/api/jobs", json=body | {"force": True}, headers=UI)
    assert ok.status_code == 200
    assert {"id", "node_id", "engine", "run_as", "status", "wait_reason"} <= set(ok.json())
    # a live session without a pid is not "in a terminal": no 409
    assert c.post("/api/jobs", json=body | {"resume": {"tool": "claude", "session_id": "free"}}, headers=UI).status_code == 200


def test_resolved_any_job_keeps_req_engine(hub):
    c, _, _ = hub
    r = c.post("/api/jobs", json={"node_id": "auto", "engine": "any", "prompt": "p"}, headers=UI).json()
    assert r["engine"] != "any"
    j = c.get(f"/api/jobs/{r['id']}", headers=UI).json()
    assert j["req_engine"] == "any" and j["requested"] == "auto" and "wait_reason" in j    # UI shows "Best fit" from req_engine
    rows = c.get("/api/state", headers=UI).json()["jobs"]
    assert any(x["id"] == r["id"] and x["req_engine"] == "any" for x in rows)


def test_include_brief_listing(hub):
    c, _, _ = hub
    r = c.get("/api/jobs?include_brief=true&limit=80", headers=UI)
    assert r.status_code == 200 and isinstance(r.json(), list)


def test_scheduler_cooldowns_until(hub):
    c, _, _ = hub
    jid = c.post("/api/jobs", json={"node_id": "m1", "engine": "claude", "prompt": "p"}, headers=UI).json()["id"]
    c.post("/api/agent/report", headers=AGENT, json={"node_id": "m1", "capacity": 10, "metrics": {"cpu": 10},
                                                    "info": {"tools": ["claude", "codex"], "agent_version": "2.2.0"}})
    c.post(f"/api/agent/jobs/{jid}/update", headers=AGENT,
           json={"status": "failed", "exit_code": 1, "append": "Claude usage limit reached. Try again in 2 hours."})
    s = c.get("/api/scheduler", headers=UI).json()
    assert isinstance(s["cooldowns"]["claude"]["until"], (int, float)) and "queue_depth" in s


def test_health_fields(hub):
    c, _, _ = hub
    h = c.get("/api/health", headers=UI).json()
    for k in ("alerts", "rollout", "update_lease", "bad_versions", "bundle_version", "nodes"):
        assert k in h
    assert isinstance(h["bad_versions"], list)
    assert h["update_lease"] is None or {"holder", "version", "expires_in_s"} <= set(h["update_lease"])
    for r in h["rollout"]:
        assert r["status"] in ("unreachable", "missing", "current", "updating")
    for a in h["alerts"]:
        assert {"level", "kind", "msg"} <= set(a) and a["level"] in ("warn", "error", "info")
    assert all("system_lane" in n for n in h["nodes"])


def test_project_summary_and_detail_fields(hub):
    c, _, _ = hub
    ps = c.get("/api/projects", headers=UI).json()
    assert ps
    for k in ("id", "name", "pinned", "machines", "live_sessions", "last_activity", "dirty_any", "brief"):
        assert k in ps[0]
    d = c.get(f"/api/projects/{ps[0]['id']}", headers=UI).json()
    for k in ("sessions", "commits", "repos", "briefs"):
        assert isinstance(d[k], list)
    s = next(x for x in d["sessions"] if x["session_id"] == "term")
    for k in ("tool", "session_id", "node_id", "state", "pid", "cwd", "last_activity"):
        assert k in s


def test_p3_today_and_ask_fields_the_ui_reads(hub):
    c, _, _ = hub
    t = c.get("/api/today", headers=UI).json()
    assert {"now", "status", "needs_you", "happening", "recent", "brief", "first_run"} <= set(t)
    assert {"level", "sentence"} <= set(t["status"]) and {"items", "total"} <= set(t["needs_you"])
    assert {"machines", "projects"} <= set(t["first_run"])
    h = next(x for x in t["happening"] if x["kind"] == "session")
    for k in ("kind", "id", "tool", "title", "node_id", "project_id", "since", "state"):
        assert k in h
    r = c.get("/api/ask?q=forge", headers=UI).json()["results"]
    assert all({"type", "id", "title", "subtitle", "score", "target"} <= set(x) for x in r) and r[-1]["type"] == "run"
    j = c.post("/api/jobs", headers=UI, json={"node_id": "auto", "engine": "any", "prompt": "p", "notify": True})
    assert j.status_code == 200                                                # extra `notify` is accepted before N lands
