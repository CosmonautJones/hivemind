"""Workstream U hub side (hub/forge_ui.py): PWA files, GET /api/today, GET /api/ask, and the pure helpers.
Deterministic: fake clock, TestClient, no sockets."""
import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import forge_ui as fu
from app import create_app

ROOT = Path(__file__).resolve().parents[1]
TOKENS = {"ui_token": "ui-tok", "agent_token": "agent-tok", "cluster_token": "cluster-tok"}
UI = {"Authorization": "Bearer ui-tok"}
AGENT = {"X-Agent-Token": "agent-tok"}
T0 = datetime(2026, 11, 25, 15, 0).timestamp()      # local 15:00, so "yesterday" is unambiguous


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
    return TestClient(app), app, clock


def report(c, nid, host=None, **metrics):
    r = c.post("/api/agent/report", headers=AGENT, json={"node_id": nid, "capacity": 2, "metrics": {"cpu": 5, "job_token": "filtered", **metrics},
                                                       "info": {"hostname": host or nid, "tools": ["claude"], "agent_version": "2.3.0"}})
    assert r.status_code == 200


def sessions(c, nid, rows):
    base = {"tool": "claude", "cwd": "C:\\dev\\forge", "git_remote": "https://github.com/me/forge.git", "git_branch": "main",
            "messages": 3, "pid": None}
    r = c.post("/api/agent/sessions", headers=AGENT, json={"node_id": nid, "sessions": [base | x for x in rows]})
    assert r.status_code == 200


# ---------------------------------------------------------------- PWA
def test_pwa_files_are_public_and_standby_local(hub):
    # Arrange
    c, app, _ = hub
    ctx = app.state.ctx
    # Act
    m = c.get("/manifest.webmanifest")
    sw = c.get("/sw.js")
    icons = [c.get(i["src"]) for i in m.json()["icons"]]
    # Assert
    assert m.status_code == 200 and m.headers["content-type"].startswith("application/manifest+json")
    assert m.json()["start_url"] == "/#/today" and m.json()["display"] == "standalone"
    assert {i.get("purpose") for i in m.json()["icons"]} == {None, "maskable"}
    assert sw.status_code == 200 and sw.headers["service-worker-allowed"] == "/" and sw.headers["cache-control"] == "no-cache"
    assert "notificationclick" in sw.text and "X-HM-Offline" in sw.text
    assert all(r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n" for r in icons)
    assert {"/manifest.webmanifest", "/sw.js"} <= ctx.local_paths and "/icons/" in ctx.local_prefixes


@pytest.mark.parametrize("name", ["..%2Fapp.py", "nope.png", "ICON.PNG", "icon-192.svg"])
def test_icon_route_rejects_anything_but_known_pngs(hub, name):
    c, _, _ = hub
    assert c.get("/icons/" + name).status_code == 404


def test_icon_sizes_match_the_manifest():
    for src, size in (("icon-192.png", 192), ("icon-512.png", 512), ("maskable-512.png", 512)):
        b = (ROOT / "hub" / "static" / "icons" / src).read_bytes()
        assert int.from_bytes(b[16:20], "big") == size and int.from_bytes(b[20:24], "big") == size


# ---------------------------------------------------------------- /api/today
def test_today_needs_auth(hub):
    c, _, _ = hub
    assert c.get("/api/today").status_code == 401 and c.get("/api/ask?q=x").status_code == 401


def test_today_first_run_on_an_empty_fleet(hub):
    # Arrange
    c, _, _ = hub
    # Act
    t = c.get("/api/today", headers=UI).json()
    # Assert
    assert t["first_run"] == {"machines": 0, "projects": 0}
    assert t["status"] == {"level": "warn", "sentence": "No machines yet. Add one to get started."}
    assert t["needs_you"] == {"items": [], "total": 0} and t["happening"] == [] and t["brief"] is None
    assert t["recent"] == {"today": [], "yesterday": [], "earlier": []}


def test_today_all_good_happening_and_recent_buckets(hub):
    # Arrange
    c, _, clock = hub
    report(c, "m1", "studio")
    report(c, "hub1")
    sessions(c, "m1", [
        {"session_id": "live1", "title": "live work", "started": T0 - 100, "last_activity": T0 - 5, "state": "live"},
        {"session_id": "old-today", "title": "morning fix", "started": T0 - 7200, "last_activity": T0 - 3600, "state": "idle"},
        {"session_id": "old-yday", "title": "yesterday thing", "started": T0 - 90000, "last_activity": T0 - 86400, "state": "done"},
        {"session_id": "old-long", "title": "ancient", "started": T0 - 900000, "last_activity": T0 - 86400 * 5, "state": "done"}])
    jid = c.post("/api/jobs", headers=UI, json={"node_id": "m1", "engine": "claude", "prompt": "run me"}).json()["id"]
    # Act
    t = c.get("/api/today", headers=UI).json()
    # Assert
    assert t["status"] == {"level": "ok", "sentence": "All good – 2 of 2 machines on"}
    kinds = {(h["kind"], h["id"]) for h in t["happening"]}
    assert ("session", "live1") in kinds and ("job", jid) in kinds
    live = next(h for h in t["happening"] if h["id"] == "live1")
    assert live["project_id"] and live["tool"] == "claude" and live["state"] == "live"
    assert [s["session_id"] for s in t["recent"]["today"]] == ["old-today"]
    assert [s["session_id"] for s in t["recent"]["yesterday"]] == ["old-yday"]
    assert [s["session_id"] for s in t["recent"]["earlier"]] == ["old-long"]
    assert t["recent"]["today"][0]["project_name"] and t["first_run"] == {"machines": 2, "projects": 1}


def test_today_search_filters_recent_by_title_project_or_machine(hub):
    c, _, _ = hub
    report(c, "m1", "studio")
    sessions(c, "m1", [{"session_id": "a", "title": "fix the parser", "last_activity": T0 - 600, "started": T0 - 700, "state": "idle"},
                       {"session_id": "b", "title": "write docs", "last_activity": T0 - 500, "started": T0 - 700, "state": "idle"}])
    ids = lambda q: [s["session_id"] for b in c.get("/api/today?q=" + q, headers=UI).json()["recent"].values() for s in b]
    assert ids("PARSER") == ["a"]
    assert sorted(ids("studio")) == ["a", "b"]          # hostname
    assert sorted(ids("forge")) == ["a", "b"]           # project name
    assert ids("nothing-matches") == []


def test_today_needs_you_from_health_alerts_with_actions(hub):
    # Arrange: m1 goes offline, then a failed UI task
    c, _, clock = hub
    report(c, "m1")
    report(c, "hub1")
    jid = c.post("/api/jobs", headers=UI, json={"node_id": "hub1", "engine": "shell", "prompt": "exit 1"}).json()["id"]
    c.post("/api/agent/report", headers=AGENT, json={"node_id": "hub1", "metrics": {}, "capacity": 1})
    c.post(f"/api/agent/jobs/{jid}/update", headers=AGENT, json={"status": "failed", "exit_code": 1, "node_id": "hub1"})
    clock.t += 120
    report(c, "hub1")
    hub[1].state.tick()                                  # 2.3: N opens the condition alerts on the leader tick
    # Act
    t = c.get("/api/today", headers=UI).json()
    # Assert
    items = t["needs_you"]["items"]
    assert t["status"]["level"] == "error" and t["needs_you"]["total"] == len(items) == 2
    off = next(a for a in items if a["kind"] == "node_offline")
    assert off["action"] == {"label": "Wake it", "kind": "wake_machine", "target": {"node_id": "m1"}}
    failed = next(a for a in items if a["kind"] == "task_failed")
    assert failed["action"]["kind"] == "retry_job" and failed["action"]["target"]["job_id"] == jid
    assert items[0]["level"] == "error"                                    # errors first
    for k in ("id", "key", "kind", "node_id", "level", "msg", "opened", "resolved", "self_healing", "data", "action"):
        assert k in off


def test_today_uses_the_alerts_table_once_n_is_active(hub):
    # Arrange: pretend N registered (raise_alert is set) and has rows
    c, app, clock = hub
    ctx = app.state.ctx

    def raise_alert(*a, **k):
        return None

    ctx.raise_alert = raise_alert
    st = app.state.store
    rows = [("a1", "machine_down:m1", "machine_down", "m1", "error", "m1 is down", T0 - 60, None, 0, "{}"),
            ("a2", "rollout:-", "rollout_in_progress", None, "warn", "rolling", T0 - 60, None, 1, "{}"),             # self-healing, young
            ("a3", "rollout:old", "rollout_in_progress", None, "warn", "stuck", T0 - 3600, None, 1, "{}"),         # self-healing, too old
            ("a4", "x:-", "repo_conflict", "m2", "warn", "diverged", T0 - 60, T0 - 1, 0, json.dumps({"project_id": "p9"})),  # resolved
            ("a5", "rc:m2", "repo_conflict", "m2", "warn", "diverged", T0 - 30, None, 0,
             json.dumps({"project_id": "p9", "action": {"label": "Open", "kind": "open_project", "target": {"project_id": "p9"}}}))]
    for r in rows:
        st.x("INSERT INTO alerts(id,key,kind,node_id,level,msg,opened,resolved,self_healing,data,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
             (*r, T0))
    # Act
    t = c.get("/api/today", headers=UI).json()
    # Assert
    assert [a["id"] for a in t["needs_you"]["items"]] == ["a1", "a5", "a3"]
    assert t["needs_you"]["items"][1]["action"]["kind"] == "open_project"
    assert t["needs_you"]["items"][0]["action"]["kind"] == "wake_machine"


# ---------------------------------------------------------------- /api/ask
def test_ask_matches_projects_machines_sessions_actions_and_ends_with_run(hub):
    # Arrange
    c, _, _ = hub
    report(c, "m1", "studio-pc")
    sessions(c, "m1", [{"session_id": "s1", "title": "Refactor the scheduler", "last_activity": T0 - 60, "started": T0 - 600, "state": "idle"}])
    # Act
    r = lambda q, n=8: c.get(f"/api/ask?q={q}&limit={n}", headers=UI).json()["results"]
    # Assert
    assert r("studio")[0]["type"] == "machine" and r("studio")[0]["target"] == {"node_id": "m1"}
    assert r("scheduler")[0]["type"] == "session" and r("scheduler")[0]["target"]["session_id"] == "s1"
    assert r("forge")[0]["type"] == "project"
    assert r("add machine")[0]["target"] == {"route": "#/add-machine"}
    assert any(x["type"] == "maintenance" and x["target"]["action"] == "update_ai_tools" for x in r("update claude"))
    last = r("compile the docs")[-1]
    assert last["type"] == "run" and last["target"] == {"prompt": "compile the docs", "engine": "any", "node_id": "auto"}
    assert last["title"] == "Run 'compile the docs' on any free machine"
    assert len(r("e", 3)) == 3 and r("e", 3)[-1]["type"] == "run"
    assert c.get("/api/ask?q=", headers=UI).json() == {"results": []}
    assert c.get("/api/ask?q=x&limit=0", headers=UI).status_code == 422


def test_ask_prefers_fs_maintenance_catalog(hub, monkeypatch):
    import forge_upkeep
    c, _, _ = hub
    monkeypatch.setattr(forge_upkeep, "MAINTENANCE", {"update_t3": {"title": "Update T3", "description": "t3 update"}}, raising=False)
    res = c.get("/api/ask?q=update t3", headers=UI).json()["results"]
    assert res[0]["id"] == "update_t3" and res[0]["type"] == "maintenance"


# ---------------------------------------------------------------- pure helpers
def test_fuzzy_score_matches_the_shared_js_cases():
    cases = json.loads((ROOT / "tests" / "js" / "fuzzy_cases.json").read_text(encoding="utf-8"))["cases"]
    for q, t, want in cases:
        assert fu.fuzzy_score(q, t) == want, (q, t)


def test_status_sentence_and_filter_needs():
    assert fu.status_sentence(3, 2, [])["sentence"] == "All good – 2 of 3 machines on"
    assert fu.status_sentence(3, 3, [{"level": "warn"}, {"level": "warn"}]) == {"level": "warn", "sentence": "2 things need you"}
    al = [{"level": "info"}, {"level": "warn", "opened": 5}, {"level": "error", "opened": 1}, {"level": "warn", "resolved": 3},
          {"level": "warn", "self_healing": True, "opened": 100}]
    assert [a["opened"] for a in fu.filter_needs(al, now=200, grace_s=900)] == [1, 5]
    assert len(fu.filter_needs(al, now=2000, grace_s=900)) == 3


def test_day_bucket_local_days():
    now = datetime(2026, 11, 25, 0, 30).timestamp()
    assert fu.day_bucket(datetime(2026, 11, 25, 0, 1).timestamp(), now) == "today"
    assert fu.day_bucket(datetime(2026, 11, 24, 0, 1).timestamp(), now) == "yesterday"
    assert fu.day_bucket(datetime(2026, 11, 22).timestamp(), now) == "earlier"
    assert fu.day_bucket(None, now) == "earlier"


def test_alert_action_defaults():
    assert fu.alert_action({"kind": "node_offline", "node_id": "n"})["kind"] == "wake_machine"
    assert fu.alert_action({"kind": "task_failed", "data": {"job_id": "j"}})["target"] == {"job_id": "j"}
    assert fu.alert_action({"kind": "fleet_bad_version"})["kind"] == "fix"
    assert fu.alert_action({"kind": "disk_full", "node_id": "n"})["kind"] == "open_machine"
    assert fu.alert_action({"kind": "x"}) is None
    own = {"label": "Go", "kind": "open_job", "target": {"job_id": "j"}}
    assert fu.alert_action({"kind": "x", "action": own}) is own


# ---------------------------------------------------------------- review fixes (U-03 U-04 U-09 U-10 U-15)
def _job(st, jid, prompt, finished, status="failed", source="ui", run_as="user", engine="shell", node="m1", created=None):
    st.x("INSERT INTO jobs(id,node_id,engine,prompt,status,created,finished,source,run_as) VALUES(?,?,?,?,?,?,?,?,?)",
         (jid, node, engine, prompt, status, created if created is not None else finished - 5, finished, source, run_as))


def test_failed_tasks_window_lanes_sources_and_retries(hub):
    # Arrange
    _, app, _ = hub
    st, ui = app.state.store, app.state.ui_data
    _job(st, "j-ok", "build it", T0 - 60)                                   # counts
    _job(st, "j-old", "build old", T0 - fu.FAILED_WINDOW_S - 60)            # outside the 12 h window
    _job(st, "j-brief", "summarise", T0 - 60, source="brief")
    _job(st, "j-sync", "git pull", T0 - 60, source="sync")
    _job(st, "j-sys", "winget", T0 - 60, run_as="system", engine="action")
    _job(st, "j-retried", "flaky test", T0 - 600)                           # run again later (any machine) -> gone
    _job(st, "j-retry", "flaky test", T0 - 300, status="done", node="m2")
    # Act
    ids = [a["data"]["job_id"] for a in ui.failed_tasks(T0)]
    # Assert
    assert ids == ["j-ok"]


def test_today_falls_back_cleanly_without_a_health_route(hub):
    # Arrange: a hub where /api/health is missing (route lookup gives None) and nothing failed
    c, app, _ = hub
    report(c, "m1")
    app.state.ui_data._health = None
    app.state.ui_data._health_fn = lambda: None
    # Act
    t = c.get("/api/today", headers=UI).json()
    # Assert
    assert t["needs_you"] == {"items": [], "total": 0} and t["status"]["level"] == "ok"


def test_version_behind_during_a_rollout_is_self_healing(hub):
    # Arrange: the 2.2 health list reports version_behind while a lease is held
    c, app, _ = hub
    hub[1].state.ui_data._n_active = lambda: False      # the pre-N (2.2 health list) path; N is merged in 2.3
    report(c, "m1")
    health = {"alerts": [{"kind": "version_behind", "node_id": "m1", "level": "warn", "msg": "behind", "opened": T0 - 30}],
              "update_lease": {"holder": "m1"}, "rollout": []}
    app.state.ui_data._health = lambda: health
    # Act
    al, grace = app.state.ui_data.alerts(T0)
    t = c.get("/api/today", headers=UI).json()
    # Assert
    assert al[0]["self_healing"] is True and grace == fu.SELFHEAL_GRACE_S
    assert t["needs_you"]["total"] == 0                                      # young self-healing alerts stay out of "Needs you"
    health["update_lease"] = {}
    assert app.state.ui_data.alerts(T0)[0][0]["self_healing"] is False


def test_today_all_returns_every_needs_item_with_the_same_total(hub):
    # Arrange: 5 failed tasks
    c, app, _ = hub
    hub[1].state.ui_data._n_active = lambda: False      # the pre-N (2.2 health list) path; N is merged in 2.3
    report(c, "m1")
    for i in range(5):
        _job(app.state.store, f"f{i}", f"task {i}", T0 - 60 - i)
    # Act
    short, full = c.get("/api/today", headers=UI).json(), c.get("/api/today?all=1", headers=UI).json()
    # Assert
    assert short["needs_you"]["total"] == full["needs_you"]["total"] == 5
    assert len(short["needs_you"]["items"]) == 3 and len(full["needs_you"]["items"]) == 5
    assert c.get("/api/today?all=2", headers=UI).status_code == 422


def test_alerts_prefer_ns_own_list_function(hub, monkeypatch):
    # Arrange: N active and exporting forge_alerts.list_open(ctx, now) (CONTRACT §N1)
    import forge_alerts
    c, app, _ = hub
    app.state.ctx.raise_alert = lambda *a, **k: None
    own = {"label": "Resume", "kind": "resume_session", "target": {"tool": "claude", "session_id": "s1"}}
    monkeypatch.setattr(forge_alerts, "list_open", lambda ctx, now: [
        {"id": "n1", "key": "task_failed:m1:j", "kind": "task_failed", "node_id": "m1", "level": "warn", "msg": "x", "opened": now - 5,
         "self_healing": False, "data": {}, "action": own}], raising=False)
    # Act
    items = c.get("/api/today", headers=UI).json()["needs_you"]["items"]
    # Assert
    assert [a["id"] for a in items] == ["n1"] and items[0]["action"] == own


def test_alerts_table_action_column_wins_over_data(hub):
    # Arrange: N stores the action in its own column instead of data.action
    c, app, _ = hub
    app.state.ctx.raise_alert = lambda *a, **k: None
    st = app.state.store
    cols = {r["name"] for r in st.q("PRAGMA table_info(alerts)")}
    if "action" not in cols:
        st.x("ALTER TABLE alerts ADD COLUMN action TEXT")
    act = {"label": "Open task", "kind": "open_job", "target": {"job_id": "j1"}}
    st.x("INSERT INTO alerts(id,key,kind,node_id,level,msg,opened,resolved,self_healing,data,updated,action) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
         ("b1", "k", "task_failed", "m1", "warn", "failed", T0 - 5, None, 0, "{}", T0, json.dumps(act)))
    # Act
    items = c.get("/api/today", headers=UI).json()["needs_you"]["items"]
    # Assert
    assert items[0]["id"] == "b1" and items[0]["action"] == act


def test_index_is_cached_for_a_moment(hub):
    # Arrange
    _, app, clock = hub
    ui, calls = app.state.ui_data, []
    real = app.state.ctx.p2._index
    app.state.ctx.p2._index = lambda now: calls.append(now) or real(now)
    # Act
    ui.index(clock.t); ui.index(clock.t + 1)
    ui.index(clock.t + fu.INDEX_TTL_S + 0.1)
    # Assert
    assert len(calls) == 2


@pytest.mark.parametrize("cat", [
    {"update_t3": {"title": "Update T3"}, "noop": None},
    [{"name": "update_t3", "title": "Update T3"}, {"title": "no name"}, "junk"],
])
def test_maintenance_catalog_accepts_dict_or_list(hub, monkeypatch, cat):
    import forge_upkeep
    _, app, _ = hub
    monkeypatch.setattr(forge_upkeep, "MAINTENANCE", cat, raising=False)
    got = app.state.ui_data.maintenance_catalog()
    assert got[0] == {"name": "update_t3", "title": "Update T3", "description": ""}
    assert all(set(m) == {"name", "title", "description"} for m in got)


def test_ask_limit_one_is_just_the_run_entry(hub):
    c, _, _ = hub
    r = c.get("/api/ask?q=forge&limit=1", headers=UI).json()["results"]
    assert len(r) == 1 and r[0]["type"] == "run"
