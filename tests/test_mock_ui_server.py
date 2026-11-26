"""The UI mock (tools/mock_ui_server.py) must answer like the 2.3 hub contract and never crash on bad input."""
import importlib.util
import json
import os
import re
import time
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PORT = 19211                      # workstream U test range 19210-19249 (CONTRACT-phase3.md §9)


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
    assert 19210 <= mock.PORT <= 19249


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
    assert call("PUT", "/api/stepup/pin", {"pin": "12"})[0] == 400
    assert call("PUT", "/api/projects/p1/sync", {"nodes": "x"})[0] == 400
    assert call("POST", "/api/enroll", {"kind": "toaster"})[0] == 400
    assert call("PATCH", "/api/nodes/forge-01", {})[0] == 404
    assert call("PATCH", "/api/peers/nope", {"priority": 1})[0] == 404
    assert call("PATCH", "/api/peers/forge-02", {"priority": "high"})[0] == 400
    assert call("DELETE", "/api/whatever")[0] == 404
    assert call("DELETE", "/api/scheduler/cooldowns/nothing")[1] == {"ok": True, "cleared": False}


# ---------------------------------------------------------------- 2.3 endpoints (CONTRACT-phase3.md §5)
def test_p3_shapes(mock):
    t = call("GET", "/api/today")[1]
    assert {"now", "status", "needs_you", "happening", "recent", "brief", "first_run"} <= set(t)
    assert set(t["recent"]) == {"today", "yesterday", "earlier"} and len(t["needs_you"]["items"]) <= 3
    assert all(a["action"] is None or {"label", "kind", "target"} <= set(a["action"]) for a in t["needs_you"]["items"])
    r = call("GET", "/api/ask?q=forge&limit=4")[1]["results"]
    assert len(r) == 4 and r[-1]["type"] == "run" and r[-1]["target"]["node_id"] == "auto"
    assert {"id", "key", "kind", "level", "opened", "self_healing", "action"} <= set(call("GET", "/api/alerts?open=1")[1]["alerts"][0])
    assert {"pin_set", "valid_until"} == set(call("GET", "/api/stepup")[1])
    assert set(call("GET", "/api/notify/settings")[1]) == {"ntfy", "webpush", "deadman", "rules"}
    assert all({"name", "title", "steps", "lane", "destructive", "args"} <= set(a) for a in call("GET", "/api/maintenance/actions")[1]["actions"])
    out = call("GET", "/api/jobs/019ac3f2b1a4f3c2d1/output?since_seq=0")[1]
    assert {"chunks", "next_seq", "status", "truncated"} == set(out) and out["chunks"][0]["seq"] == 1
    assert call("GET", "/api/jobs/019ac3f2b1a4f3c2d1")[1]["output_seq"] >= 1


def test_pwa_files(mock):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/sw.js", timeout=5) as r:
        assert r.headers["Service-Worker-Allowed"] == "/" and b"notificationclick" in r.read()
    assert call("GET", "/manifest.webmanifest")[1]["start_url"] == "/#/today"
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/icons/icon-192.png", timeout=5) as r:
        assert r.read(4) == b"\x89PNG"


def test_step_up_then_maintenance_run_progresses(mock):
    mock.PIN["until"] = 0
    body = {"action": "restart_machine", "targets": "all", "confirm": True}
    st, r = call("POST", "/api/maintenance/runs", body)
    assert st == 403 and r["step_up"] == "required"
    assert call("POST", "/api/stepup", {"pin": "000000"})[0] == 403
    assert call("POST", "/api/stepup", {"pin": "123456"})[0] == 200
    pv = call("POST", "/api/maintenance/preview", {"action": "restart_machine", "targets": "all"})[1]
    assert pv["order"][-1] == "forge-01" and pv["skipped"] == [{"node_id": "forge-04", "reason": "offline"}] and pv["destructive"]
    st, r = call("POST", "/api/maintenance/runs", body)
    assert st == 200 and r["order"] == pv["order"]
    run = call("GET", "/api/maintenance/runs/" + r["id"])[1]
    assert run["status"] == "running" and run["current"]["node_id"] == pv["order"][0]
    assert call("POST", f"/api/maintenance/runs/{r['id']}/cancel", {})[0] == 200
    assert call("GET", "/api/maintenance/runs/" + r["id"])[1]["status"] == "cancelled"


def test_maintenance_preview_rejects_bad_args_like_f(mock):
    # Arrange: install_app has a required package id (U-01: the UI must re-preview as the user types it)
    pv = lambda args: call("POST", "/api/maintenance/preview", {"action": "install_app", "args": args, "targets": "all"})
    # Act / Assert
    st, r = pv({})
    assert st == 400 and "required" in r["detail"]
    assert pv({"id": "x" * 200})[0] == 400
    st, r = pv({"id": "Git.Git"})
    assert st == 200 and r["order"]
    assert call("POST", "/api/maintenance/runs", {"action": "install_app", "targets": "all", "confirm": True})[0] == 400


def test_today_all_returns_every_needs_item(mock):
    t, full = call("GET", "/api/today")[1], call("GET", "/api/today?all=1")[1]
    assert full["needs_you"]["total"] == t["needs_you"]["total"] == len(full["needs_you"]["items"])
    assert len(t["needs_you"]["items"]) == min(3, t["needs_you"]["total"])


def test_no_pin_answers_setup_and_pin_can_be_set(mock):
    saved = dict(mock.PIN)
    call("POST", "/mock/nopin", {})
    st, r = call("POST", "/api/jobs", {"engine": "admin", "node_id": "forge-01", "prompt": "whoami", "confirm": True})
    assert st == 403 and r["step_up"] == "setup"
    assert call("PUT", "/api/stepup/pin", {"pin": "654321"})[0] == 200
    assert call("POST", "/api/stepup", {"pin": "654321"})[0] == 200
    assert call("POST", "/api/jobs", {"engine": "admin", "node_id": "forge-01", "prompt": "whoami", "confirm": True})[0] == 200
    mock.PIN.update(saved)


def test_enroll_wizard_states_and_claim(mock):
    st, e = call("POST", "/api/enroll", {"kind": "device"})
    assert st == 200 and e["url"].endswith("?claim=" + e["code"]) and re.fullmatch(r"[0-9A-Z]{4}-[0-9A-Z]{4}", e["code"])
    assert call("GET", "/api/enroll/" + e["id"])[1]["state"] == "pending"
    assert call("POST", "/api/claim", {"code": e["code"].lower().replace("-", "")})[0] == 200
    assert call("POST", "/api/claim", {"code": "NOPE-NOPE"})[0] == 401
    m = call("POST", "/api/enroll", {"kind": "machine"})[1]
    assert "install.ps1?code=" + m["code"] in m["command"]
    mock.ENROLL[m["id"]]["created"] -= 11
    v = call("GET", "/api/enroll/" + m["id"])[1]
    assert v["state"] == "joined" and v["node_id"] == "forge-05"
    assert any(n["id"] == "forge-05" for n in call("GET", "/api/state")[1]["nodes"])
    assert call("DELETE", "/api/enroll/" + m["id"])[0] == 200 and call("GET", "/api/enroll/" + m["id"])[1]["state"] == "expired"


def test_machines_wake_retire_devices_tokens(mock):
    assert call("POST", "/api/machines/forge-04/wake", {})[1]["via"] == ["forge-02"]
    assert call("POST", "/api/machines/forge-01/wake", {})[0] == 409
    assert call("POST", "/api/machines/nope/wake", {})[0] == 404
    assert call("GET", "/api/machines/forge-03/retire")[1]["state"] is None
    mock.PIN["until"] = time.time() + 60
    assert call("POST", "/api/machines/forge-03/retire", {"confirm": True})[0] == 202
    assert call("GET", "/api/machines/forge-03/retire")[1]["state"] == "draining"
    assert call("DELETE", "/api/machines/forge-03/retire")[0] == 200
    d = call("GET", "/api/devices")[1]["devices"]
    assert sum(1 for x in d if x["current"]) == 1
    assert call("PATCH", "/api/devices/" + d[0]["id"], {"label": "Desk"})[1]["label"] == "Desk"
    assert call("POST", "/api/tokens/rotate", {"confirm": True})[0] == 409
    assert call("POST", "/api/tokens/rotate", {"confirm": True, "force": True})[1]["rotated"] == ["agent", "cluster"]


def test_v22_mode_hides_23_endpoints(mock):
    call("POST", "/mock/v22", {})
    try:
        assert call("GET", "/api/today")[0] == 404 and call("POST", "/api/enroll", {"kind": "machine"})[0] == 405
        assert call("GET", "/api/actions")[0] == 200 and call("GET", "/api/state")[0] == 200
    finally:
        call("POST", "/mock/v23", {})


def test_first_run_mode(mock):
    call("POST", "/mock/empty", {})
    try:
        t = call("GET", "/api/today")[1]
        assert t["first_run"] == {"machines": 0, "projects": 0} and call("GET", "/api/state")[1]["nodes"] == []
    finally:
        call("POST", "/mock/full", {})


def test_ack_takes_an_encoded_alert_id(mock):
    ids = lambda: [a["id"] for a in call("GET", "/api/alerts?open=1")[1]["alerts"]]
    aid = next(i for i in ids() if ":" in i)
    assert call("POST", "/api/alerts/" + urllib.request.quote(aid, safe="") + "/ack", {})[0] == 200
    assert aid not in ids()
    mock.ACKED.discard(aid)
