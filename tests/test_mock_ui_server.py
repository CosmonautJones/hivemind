"""The UI mock (tools/mock_ui_server.py) must answer like the p2-hub and never crash on bad input."""
import importlib.util
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PORT = 19087


@pytest.fixture(scope="module")
def mock():
    os.environ["MOCK_UI_PORT"] = str(PORT)
    spec = importlib.util.spec_from_file_location("mock_ui_server", ROOT / "tools" / "mock_ui_server.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), m.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield m
    srv.shutdown()
    srv.server_close()
    os.environ.pop("MOCK_UI_PORT", None)


def call(method, path, body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_port_is_in_test_range(mock):
    assert 19000 <= mock.PORT <= 19099


def test_shapes_match_hub(mock):
    assert set(call("GET", "/api/brief/all")[1]) == {"brief"}
    peers = call("GET", "/api/state")[1]["cluster"]["peers"]
    assert all(isinstance(p["urls"], list) for p in peers)
    h = call("GET", "/api/health")[1]
    assert "expires_in_s" in h["update_lease"] and {a["kind"] for a in h["alerts"]} >= {"node_offline", "disk_full", "version_behind"}
    assert call("POST", "/api/sessions/claude/free-x/watch", {})[0] == 404
    assert call("POST", "/api/sessions/claude/live-term-1/watch", {})[1]["ttl_s"] == 30
    sch = call("GET", "/api/scheduler")[1]["placement_preview"]
    assert isinstance(sch["claude"], list) and "engine" in sch["any"]
    t = call("GET", "/api/sessions/claude/live-term-1/transcript")[1]
    assert {"events", "cursor", "done", "available"} == set(t) and t["events"][0]["seq"] == 1


def test_state_hides_brief_jobs_but_listing_can_include_them(mock):
    assert not [j for j in call("GET", "/api/state")[1]["jobs"] if j.get("source") == "brief"]
    assert [j for j in call("GET", "/api/jobs?include_brief=true&limit=80")[1] if j.get("source") == "brief"]


def test_create_job_any_resolves_and_409_only_for_terminal_sessions(mock):
    st, r = call("POST", "/api/jobs", {"node_id": "auto", "engine": "any", "prompt": "x"})
    assert st == 200 and {"id", "node_id", "engine", "run_as", "status", "wait_reason"} == set(r) and r["engine"] != "any"
    j = call("GET", f"/api/jobs/{r['id']}")[1]
    assert j["req_engine"] == "any"
    body = {"prompt": "go", "resume": {"tool": "claude", "session_id": "live-term-1"}}
    assert call("POST", "/api/jobs", body)[0] == 409
    assert call("POST", "/api/jobs", body | {"force": True})[0] == 200
    assert call("POST", "/api/jobs", {"prompt": "go", "resume": {"tool": "claude", "session_id": "live-remote-2"}})[0] == 200


def test_transcript_reset_makes_cursor_go_down(mock):
    before = call("GET", "/api/sessions/claude/live-term-1/transcript")[1]["cursor"]
    call("POST", "/mock/reset-transcript", {})
    after = call("GET", f"/api/sessions/claude/live-term-1/transcript?since={before}")[1]
    assert after["cursor"] < before and after["events"] == []
    mock.STATE["tx_reset_at"] = None


def test_patch_and_brief_survive_rebuild(mock):
    assert call("PATCH", "/api/projects/p2", {"name": "Renamed", "pinned": 1})[0] == 200
    call("POST", "/api/projects/p2/brief", {})
    d = call("GET", "/api/projects/p2")[1]
    assert d["name"] == "Renamed" and d["pinned"] is True and "Fresh brief" in d["brief"]["text"]
    assert next(p for p in call("GET", "/api/projects")[1] if p["id"] == "p2")["name"] == "Renamed"


def test_concurrent_rebuilds_never_404_transcripts(mock):
    errs = []

    def hammer():
        for _ in range(30):
            if call("GET", "/api/projects")[0] != 200:
                errs.append("projects")

    def read():
        for _ in range(60):
            if call("GET", "/api/sessions/claude/live-term-1/transcript?since=999")[0] != 200:
                errs.append("transcript")

    ts = [threading.Thread(target=hammer) for _ in range(3)] + [threading.Thread(target=read) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errs == []


def test_bad_input_gets_4xx_not_a_dropped_connection(mock):
    assert call("POST", "/api/jobs", raw=b"{not json")[0] == 400
    assert call("PATCH", "/api/projects/p1", raw=b"[1,2")[0] == 400
    assert call("PATCH", "/x", {})[0] == 404
    assert call("PATCH", "/api/nodes/forge-01", {})[0] == 404
    assert call("PATCH", "/api/peers/nope", {"priority": 1})[0] == 404
    assert call("PATCH", "/api/peers/forge-02", {"priority": "high"})[0] == 400
    assert call("DELETE", "/api/whatever")[0] == 404
    assert call("DELETE", "/api/scheduler/cooldowns/nothing")[1] == {"ok": True, "cleared": False}
