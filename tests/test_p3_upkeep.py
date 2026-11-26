"""Phase 3 workstream F (first run + upkeep), hub side: CONTRACT-phase3.md section 5 "Workstream F".

F2 user-lane actions + maintenance runs + weekly schedule, F3 repo sync, F4 Windows Update windows, F5 Wake-on-LAN,
F6a misc project keys, F6b brief-model pre-pull. Single in-process hub, fake clock, Arrange / Act / Assert.
"""
import json
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

import forge_projects as fp
import forge_upkeep as fu
from forge_actions import find_action, load_catalog, validate_args
from p2util import AGENT, UI, admin, make_hub


# ---------------------------------------------------------------- helpers
def local_ts(day: datetime, hh: int, mm: int) -> float:
    return datetime(day.year, day.month, day.day, hh, mm).timestamp()     # naive = local time (like the hub)


def next_weekday(wd: int) -> datetime:
    d = datetime(2026, 11, 2)
    while d.weekday() != wd:
        d += timedelta(days=1)
    return d


def ureport(c, node, version="2.3.0", cap=2, tools=("claude", "codex", "git"), metrics=None, **extra):
    body = {"node_id": node, "capacity": cap, "metrics": {"cpu": 5} | (metrics or {}),
            "info": {"tools": list(tools), "agent_version": version}, "agent_version": version} | extra
    r = c.post("/api/agent/report", json=body, headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


def sreport(c, node, cap=1):
    r = c.post("/api/agent/report", json={"node_id": node, "lane": "system", "capacity": cap, "agent_version": "2.3.0"},
               headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


def finish(c, jid, node, status="done", out=""):
    r = c.post(f"/api/agent/jobs/{jid}/update", json={"status": status, "append": out, "node_id": node,
                                                       "exit_code": 0 if status == "done" else 1}, headers=AGENT)
    assert r.status_code == 200, r.text


def add_peer(app, pid, prio):
    app.state.store.x("INSERT OR REPLACE INTO peers(id,priority,urls,updated) VALUES(?,?,?,?)", (pid, prio, "[]", 1.0))


class Alerts:
    def __init__(self, ctx):
        self.raised, self.resolved, self.notified = [], [], []
        ctx.raise_alert = lambda kind, **kw: self.raised.append((kind, kw))
        ctx.resolve_alert = lambda key, **kw: self.resolved.append(key)
        ctx.notify = lambda kind, title, body, **kw: self.notified.append((kind, title))

    def kinds(self):
        return [k for k, _ in self.raised]


@pytest.fixture
def hub(tmp_path):
    c, app, clock = make_hub(tmp_path)
    return c, app, clock, app.state.upkeep


def jobs_of(app, **where):
    sql = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(f"{k}=?" for k in where) if where else "") + " ORDER BY id"
    return [dict(r) for r in app.state.store.q(sql, tuple(where.values()))]


# ---------------------------------------------------------------- catalog / validation (pure)
CAT = load_catalog()


def test_catalog_lanes_and_new_actions():
    # Arrange / Act
    lanes = {a["name"]: a["lane"] for a in CAT["actions"]}
    # Assert
    assert {n for n, l in lanes.items() if l == "user"} == {"update_claude", "update_codex", "update_t3", "git_sync", "ollama_pull"}
    assert lanes["windows_update"] == lanes["update_ollama"] == lanes["free_disk"] == lanes["forge_uninstall"] == "system"
    assert find_action(CAT, "forge_uninstall")["destructive"] is True


@pytest.mark.parametrize("name,args,ok", [
    ("git_sync", {"cwd": "C:\\work\\repo"}, True),
    ("git_sync", {"cwd": "repo"}, False),
    ("git_sync", {"cwd": "C:\\a\" & calc"}, False),
    ("git_sync", {}, False),
    ("ollama_pull", {"model": "qwen2.5:14b"}, True),
    ("ollama_pull", {"model": "-x"}, False),
    ("windows_update", {}, True),
    ("windows_update", {"reboot": "always"}, False),
    ("forge_uninstall", {"keep_data": True}, True),
    ("forge_uninstall", {"keep_data": "yes"}, False),
    ("update_claude", {"x": 1}, False),
])
def test_new_action_validation(name, args, ok):
    spec = find_action(CAT, name)
    if ok:
        validate_args(spec, args)
    else:
        with pytest.raises(ValueError):
            validate_args(spec, args)


def test_forge_uninstall_default_and_windows_update_default():
    assert validate_args(find_action(CAT, "forge_uninstall"), {}) == {"keep_data": False}
    assert validate_args(find_action(CAT, "windows_update"), {}) == {"reboot": "never"}


# ---------------------------------------------------------------- pure helpers
def test_parse_result_takes_the_last_result_line():
    out = "x\n##RESULT {\"result\":\"dirty\"}\nmore\n##RESULT {\"result\": \"ff\", \"branch\": \"main\"}\n"
    assert fu.parse_result(out) == {"result": "ff", "branch": "main"}
    assert fu.parse_result("no result") is None and fu.parse_result("##RESULT not json") is None


def test_summarize_windows_update_and_generic():
    assert fu.summarize("windows_update", {"installed": 2, "failed": 1, "reboot_required": True}, "done") == \
        "2 installed, 1 failed, reboot required"
    assert fu.summarize("update_claude", {"summary": "claude 1 -> 2"}, "done") == "claude 1 -> 2"
    assert fu.summarize("git_sync", None, "failed") == "failed"


def test_macs_cleaned_and_wol_items_use_directed_and_limited_broadcast():
    macs = fu.clean_macs({"iface": "Ethernet", "mac": "aa:bb:cc:dd:ee:01", "ip": "192.168.1.10", "prefix": 24})
    assert macs == [{"iface": "Ethernet", "mac": "AA-BB-CC-DD-EE-01", "ip": "192.168.1.10", "prefix": 24}]
    assert fu.clean_macs([{"mac": "00-00-00-00-00-00"}, {"mac": "zz"}, "x"]) == []
    assert fu.wol_items(macs) == [{"mac": "AA-BB-CC-DD-EE-01", "broadcast": "192.168.1.255", "port": 9},
                                  {"mac": "AA-BB-CC-DD-EE-01", "broadcast": "255.255.255.255", "port": 9}]


@pytest.mark.parametrize("v,expect", [("03:00-05:00", "03:00-05:00"), (" OFF ", "off"), (None, None), ("", None)])
def test_wu_window_normalized(v, expect):
    assert fu.normalize_wu_window(v) == expect


@pytest.mark.parametrize("v", ["3:00-5:00", "03:00", "25:00-26:00", "03:00-03:00", 5])
def test_wu_window_rejected(v):
    with pytest.raises(HTTPException):
        fu.normalize_wu_window(v)


def test_window_offset_wraps_midnight():
    day = datetime(2026, 11, 10)
    since, length, opened = fu.window_offset((23 * 60, 60), local_ts(day + timedelta(days=1), 0, 30))
    assert (since, length, opened) == (90, 120, "2026-11-10")


def test_model_present_untagged_means_latest():
    assert fu.model_present(["qwen2.5:14b", "llama3:latest"], "llama3")
    assert not fu.model_present(["qwen2.5:7b"], "qwen2.5:14b")


# ---------------------------------------------------------------- F6a project keys
@pytest.mark.parametrize("cwd", [
    r"C:\Users\you\AppData\Roaming\Claude\claude-data\abc123\scratch-workspaces\ws-1",
    r"C:\Users\you\AppData\Roaming\Claude\claude-data\abc123\scratch-workspaces\ws-1\deeper\x",
    r"C:\Users\you\AppData\Local\Temp\tmpabc",
    r"C:\Users\YOU~1\AppData\Local\Temp\x\y",
])
def test_scratch_workspaces_and_temp_are_misc(cwd):
    assert fp.project_key("box", cwd, None) == "misc:box"


def test_normal_folder_stays_local():
    assert fp.project_key("box", r"D:\code\claude-data-viewer", None) == r"local:box:d:\code\claude-data-viewer"


def test_pre23_scratch_projects_are_merged_into_misc(hub):
    # Arrange: a project created by 2.2 for one scratch workspace
    c, app, clock, up = hub
    key = r"local:box:c:\users\u\appdata\roaming\claude\claude-data\abc\scratch-workspaces\ws1"
    app.state.store.x("INSERT INTO projects(id,key,name,pinned,created,updated) VALUES('p1',?, 'ws1',0,1,1)", (key,))
    # Act
    n = up.merge_misc_projects(clock())
    # Assert
    rows = [dict(r) for r in app.state.store.q("SELECT id,key FROM projects")]
    assert n == 1 and [r["key"] for r in rows] == ["misc:box"]
    assert app.state.store.q("SELECT project_id FROM project_alias WHERE id=?", (key,))[0]["project_id"] == rows[0]["id"]


# ---------------------------------------------------------------- F2 user-lane action jobs (A-F1)
def test_user_lane_action_job_dispatched_to_23_agent_with_action_and_args(hub):
    # Arrange
    c, app, clock, _ = hub
    ureport(c, "box", cap=0)
    # Act
    r = c.post("/api/jobs", json={"node_id": "box", "engine": "action", "action": "git_sync",
                                  "args": {"cwd": "C:\\work\\repo"}}, headers=UI)
    items = ureport(c, "box", cap=2)["jobs"]
    # Assert
    assert r.status_code == 200 and r.json()["run_as"] == "user"
    assert items == [items[0]] and items[0]["action"] == "git_sync" and items[0]["run_as"] == "user"
    assert items[0]["args"] == {"cwd": "C:\\work\\repo"} and items[0]["cwd"] is None
    assert not sreport(c, "box")["jobs"]


def test_user_lane_action_waits_for_agent_23(hub):
    # Arrange
    c, app, clock, _ = hub
    ureport(c, "old", version="2.2.0", cap=0)
    # Act
    jid = c.post("/api/jobs", json={"node_id": "old", "engine": "action", "action": "update_claude"}, headers=UI).json()["id"]
    items = ureport(c, "old", version="2.2.0", cap=2)["jobs"]
    # Assert
    assert items == [] and jobs_of(app, id=jid)[0]["wait_reason"] == "needs agent 2.3"


def test_user_lane_action_auto_node_picks_a_23_agent(hub):
    c, app, clock, _ = hub
    ureport(c, "old", version="2.2.0")
    ureport(c, "new", version="2.3.0")
    r = c.post("/api/jobs", json={"node_id": "auto", "engine": "action", "action": "update_codex"}, headers=UI)
    assert r.status_code == 200 and r.json()["node_id"] == "new"


def test_user_lane_action_rejects_run_as_system_and_bad_args(hub):
    c, *_ = hub
    ureport(c, "box")
    assert c.post("/api/jobs", json={"node_id": "box", "engine": "action", "action": "update_t3", "run_as": "system"},
                  headers=UI).status_code == 400
    assert c.post("/api/jobs", json={"node_id": "box", "engine": "action", "action": "git_sync", "args": {"cwd": "x"}},
                  headers=UI).status_code == 400


def test_system_actions_still_need_a_machine(hub):
    c, *_ = hub
    ureport(c, "box")
    r = c.post("/api/jobs", json={"node_id": "auto", "engine": "action", "action": "update_ollama"}, headers=UI)
    assert r.status_code == 400


# ---------------------------------------------------------------- A-F2 hidden sync jobs
def test_sync_jobs_hidden_unless_include_sync(hub):
    # Arrange
    c, app, clock, up = hub
    ureport(c, "box")
    up.queue_action("box", "git_sync", {"cwd": "C:\\r"}, source="sync")
    c.post("/api/jobs", json={"node_id": "box", "engine": "shell", "prompt": "echo"}, headers=UI)
    # Act / Assert
    assert [j["source"] for j in c.get("/api/jobs", headers=UI).json()] == ["ui"]
    assert {j["source"] for j in c.get("/api/jobs?include_sync=1", headers=UI).json()} == {"ui", "sync"}
    assert [j["source"] for j in c.get("/api/state", headers=UI).json()["jobs"]] == ["ui"]
    assert len(c.get("/api/nodes/box", headers=UI).json()["jobs"]) == 1
    assert len(c.get("/api/nodes/box?include_sync=1", headers=UI).json()["jobs"]) == 2


# ---------------------------------------------------------------- F2 maintenance runs
def fleet(c, app, canary=None):
    """hub1 (leader, priority 1), n2 (2), n3 (3); all lanes online on 2.3."""
    add_peer(app, "n2", 2)
    add_peer(app, "n3", 3)
    for n in ("hub1", "n2", "n3"):
        ureport(c, n, cap=0)
        sreport(c, n, cap=0)
    if canary:
        c.put(f"/api/nodes/{canary}/settings", json={"canary": True}, headers=UI)


def test_maintenance_actions_listed_with_lane_and_destructive(hub):
    c, *_ = hub
    acts = {a["name"]: a for a in c.get("/api/maintenance/actions", headers=UI).json()["actions"]}
    assert set(acts) == {"update_app", "update_ai_tools", "restart_hivemind", "free_disk", "restart_machine"}
    assert acts["update_ai_tools"]["lane"] == "user" and acts["update_ai_tools"]["steps"] == ["update_claude", "update_codex"]
    assert acts["restart_machine"]["destructive"] is True and acts["update_app"]["args"]["id"]["required"] is True


def test_preview_order_canary_first_priority_desc_leader_last_and_skips_offline(hub):
    # Arrange
    c, app, clock, _ = hub
    fleet(c, app, canary="n2")
    ureport(c, "w4", version="2.2.0", cap=0)          # a worker on 2.2: needs agent 2.3 for user-lane actions
    # Act
    p = c.post("/api/maintenance/preview", json={"action": "update_ai_tools", "targets": "all"}, headers=UI).json()
    s = c.post("/api/maintenance/preview", json={"action": "free_disk", "targets": ["hub1", "n3", "w4"]}, headers=UI).json()
    # Assert
    assert p["order"] == ["n2", "n3", "hub1"] and p["skipped"] == [{"node_id": "w4", "reason": "needs agent 2.3"}]
    assert p["needs_step_up"] is False and p["destructive"] is False and p["online"] == 4
    assert s["order"] == ["n3", "hub1"] and s["skipped"] == [{"node_id": "w4", "reason": "offline"}]
    assert s["needs_step_up"] is True


def test_run_needs_confirm_known_action_valid_args_and_targets(hub):
    c, app, *_ = hub
    fleet(c, app)
    post = lambda b: c.post("/api/maintenance/runs", json=b, headers=admin(c))
    assert post({"action": "update_ai_tools", "targets": "all"}).status_code == 400
    assert post({"action": "nope", "targets": "all", "confirm": True}).status_code == 400
    assert post({"action": "update_app", "targets": "all", "confirm": True}).status_code == 400      # id required
    assert post({"action": "update_app", "args": {"id": "a b"}, "targets": "all", "confirm": True}).status_code == 400
    assert post({"action": "free_disk", "targets": ["ghost"], "confirm": True}).status_code == 400
    assert post({"action": "forge_uninstall", "targets": ["n2"], "confirm": True}).status_code == 400


def test_system_lane_run_asks_for_step_up(hub):
    # Arrange: S7 step-up refuses (no PIN set)
    c, app, *_ = hub
    fleet(c, app)

    def refuse(request):
        raise HTTPException(403, {"detail": "admin PIN not set", "step_up": "setup"})
    app.state.ctx.require_step_up = refuse
    # Act
    sys_run = c.post("/api/maintenance/runs", json={"action": "free_disk", "targets": "all", "confirm": True}, headers=admin(c))
    user_run = c.post("/api/maintenance/runs", json={"action": "update_ai_tools", "targets": "all", "confirm": True}, headers=admin(c))
    # Assert
    assert sys_run.status_code == 403 and sys_run.json()["detail"]["step_up"] == "setup"
    assert user_run.status_code == 200


def test_run_rolls_one_machine_at_a_time_and_completes(hub):
    # Arrange
    c, app, clock, up = hub
    fleet(c, app)
    rid = c.post("/api/maintenance/runs", json={"action": "update_ai_tools", "targets": "all", "confirm": True},
                 headers=admin(c)).json()["id"]
    seen = []
    # Act: 3 machines x 2 steps
    for _ in range(6):
        up.on_tick(clock(), True)
        run = c.get(f"/api/maintenance/runs/{rid}", headers=UI).json()
        node, jid = run["current"]["node_id"], run["current"]["job_id"]
        assert len(jobs_of(app, run_id=rid, status="queued")) == 1          # never two at once
        item = ureport(c, node, cap=1)["jobs"][0]
        seen.append((node, item["action"]))
        finish(c, jid, node, out='##RESULT {"result":"updated","summary":"ok %s"}\n' % item["action"])
    up.on_tick(clock(), True)
    run = c.get(f"/api/maintenance/runs/{rid}", headers=UI).json()
    # Assert
    assert seen == [("n3", "update_claude"), ("n3", "update_codex"), ("n2", "update_claude"), ("n2", "update_codex"),
                    ("hub1", "update_claude"), ("hub1", "update_codex")]
    assert run["status"] == "done" and run["current"] is None and len(run["results"]) == 6
    assert run["results"][0]["summary"] == "ok update_claude" and run["results"][0]["step"] == "update_claude"
    assert all(j["source"] == "upkeep" for j in jobs_of(app, run_id=rid))


def test_run_stops_on_first_failure_and_raises_upkeep_failed(hub):
    # Arrange
    c, app, clock, up = hub
    alerts = Alerts(app.state.ctx)
    fleet(c, app)
    rid = c.post("/api/maintenance/runs", json={"action": "free_disk", "targets": "all", "confirm": True},
                 headers=admin(c)).json()["id"]
    up.on_tick(clock(), True)
    jid = sreport(c, "n3")["jobs"][0]["id"]
    # Act
    finish(c, jid, "n3", status="failed", out="boom\n")
    up.on_tick(clock(), True)
    run = c.get(f"/api/maintenance/runs/{rid}", headers=UI).json()
    # Assert
    assert run["status"] == "failed" and [r["node_id"] for r in run["results"]] == ["n3"]
    assert jobs_of(app, run_id=rid) == [jobs_of(app, id=jid)[0]]
    assert alerts.kinds() == ["upkeep_failed"] and alerts.raised[0][1]["key"] == "upkeep_failed:n3:free_disk"
    assert alerts.notified[0][0] == "upkeep_failed"


def test_offline_machine_is_skipped_at_its_turn(hub):
    # Arrange
    c, app, clock, up = hub
    fleet(c, app)
    rid = c.post("/api/maintenance/runs", json={"action": "free_disk", "targets": ["n3", "n2"], "confirm": True},
                 headers=admin(c)).json()["id"]
    clock.advance(40)                              # everyone's system lane goes quiet ...
    sreport(c, "n2", cap=0)                         # ... except n2
    # Act
    up.on_tick(clock(), True)
    run = c.get(f"/api/maintenance/runs/{rid}", headers=UI).json()
    # Assert
    assert run["results"][0] == run["results"][0] | {"node_id": "n3", "status": "skipped", "summary": "offline"}
    assert run["current"]["node_id"] == "n2"


def test_overlapping_run_conflicts_and_cancel(hub):
    # Arrange
    c, app, clock, up = hub
    fleet(c, app)
    rid = c.post("/api/maintenance/runs", json={"action": "update_ai_tools", "targets": ["n2"], "confirm": True},
                 headers=admin(c)).json()["id"]
    # Act / Assert
    assert c.post("/api/maintenance/runs", json={"action": "update_ai_tools", "targets": "all", "confirm": True},
                  headers=admin(c)).status_code == 409
    up.on_tick(clock(), True)
    jid = c.get(f"/api/maintenance/runs/{rid}", headers=UI).json()["current"]["job_id"]
    assert c.post(f"/api/maintenance/runs/{rid}/cancel", headers=UI).json() == {"ok": True}
    assert jobs_of(app, id=jid)[0]["status"] == "cancelled"
    assert c.get(f"/api/maintenance/runs/{rid}", headers=UI).json()["status"] == "cancelled"
    assert c.post(f"/api/maintenance/runs/{rid}/cancel", headers=UI).status_code == 409
    assert c.post("/api/maintenance/runs", json={"action": "update_ai_tools", "targets": "all", "confirm": True},
                  headers=admin(c)).status_code == 200
    assert len(c.get("/api/maintenance/runs", headers=UI).json()["runs"]) == 2


def test_standby_does_nothing(hub):
    c, app, clock, up = hub
    fleet(c, app)
    c.post("/api/maintenance/runs", json={"action": "update_ai_tools", "targets": "all", "confirm": True}, headers=admin(c))
    up.on_tick(clock(), False)
    assert jobs_of(app, source="upkeep") == []


def test_weekly_schedule_creates_runs_once(hub):
    # Arrange: Sunday 04:10 local
    c, app, clock, up = hub
    fleet(c, app)
    clock.t = local_ts(next_weekday(6), 4, 10)
    for n in ("hub1", "n2", "n3"):
        ureport(c, n, cap=0)
        sreport(c, n, cap=0)
    # Act
    up.schedule_tick(clock())
    up.schedule_tick(clock())
    runs = c.get("/api/maintenance/runs", headers=UI).json()["runs"]
    # Assert
    assert sorted(r["action"] for r in runs) == sorted(fu.SCHEDULE_DEFAULT["actions"])
    assert {r["source"] for r in runs} == {"schedule"}
    up.step_runs(clock())                                  # FIFO: only one run holds the machines
    assert len([r for r in c.get("/api/maintenance/runs", headers=UI).json()["runs"] if r["status"] == "running"]) == 1


def test_weekly_schedule_not_on_other_days_or_when_disabled(hub):
    c, app, clock, up = hub
    fleet(c, app)
    clock.t = local_ts(next_weekday(2), 4, 10)
    up.schedule_tick(clock())
    c.put("/api/upkeep/settings", json={"schedule": {"enabled": False}}, headers=UI)
    clock.t = local_ts(next_weekday(6), 4, 10)
    up.schedule_tick(clock())
    assert c.get("/api/maintenance/runs", headers=UI).json()["runs"] == []


def test_upkeep_settings_roundtrip_and_validation(hub):
    c, *_ = hub
    put = lambda b: c.put("/api/upkeep/settings", json=b, headers=UI)
    r = put({"schedule": {"day": "sat", "time": "05:30", "actions": ["update_t3"]},
             "windows_update": {"window": "02:00-04:00", "stagger_min": 20}, "repo_sync_every_s": 900})
    assert r.status_code == 200
    got = c.get("/api/upkeep/settings", headers=UI).json()
    assert got["schedule"] | {} == {"enabled": True, "day": "sat", "time": "05:30", "actions": ["update_t3"]}
    assert got["windows_update"] == {"enabled": True, "window": "02:00-04:00", "stagger_min": 20}
    assert got["repo_sync_every_s"] == 900 and "wu_slots" in got
    for bad in ({"schedule": {"day": "sunday"}}, {"schedule": {"time": "4am"}}, {"schedule": {"actions": ["update_app"]}},
                {"schedule": {"actions": ["restart_machine"]}}, {"windows_update": {"stagger_min": -1}},
                {"windows_update": {"window": "x"}}, {"repo_sync_every_s": 10}):
        assert put(bad).status_code == 400, bad


# ---------------------------------------------------------------- F3 repo sync
def project_with_repo(app, node="box", cwd="C:\\work\\repo"):
    st = app.state.store
    st.x("INSERT INTO projects(id,key,name,pinned,created,updated) VALUES('p1','github.com/o/r','r',0,1,1)")
    st.x("INSERT INTO repos(id,node_id,cwd,git_remote,project_key,branch,dirty,ahead,behind,commits,updated) "
         "VALUES(?,?,?,?,?,?,0,0,0,'[]',1)", (f"{node}|{cwd.lower()}", node, cwd, "github.com/o/r", "github.com/o/r", "main"))


def test_project_sync_nodes_api(hub):
    c, app, *_ = hub
    ureport(c, "box")
    project_with_repo(app)
    assert c.put("/api/projects/p1/sync", json={"nodes": ["box", "box"]}, headers=UI).json() == {"ok": True, "sync_nodes": ["box"]}
    assert c.put("/api/projects/p1/sync", json={"nodes": ["ghost"]}, headers=UI).status_code == 400
    assert c.put("/api/projects/nope/sync", json={"nodes": []}, headers=UI).status_code == 404
    assert c.get("/api/projects", headers=UI).json()[0]["sync_nodes"] == ["box"]
    assert c.get("/api/projects/p1", headers=UI).json()["sync_nodes"] == ["box"]


def test_sync_tick_queues_git_sync_once_per_repo(hub):
    # Arrange
    c, app, clock, up = hub
    ureport(c, "box")
    project_with_repo(app)
    c.put("/api/projects/p1/sync", json={"nodes": ["box"]}, headers=UI)
    # Act
    n1 = up.sync_tick(clock())
    n2 = up.sync_tick(clock())
    # Assert
    js = jobs_of(app, action="git_sync")
    assert (n1, n2) == (1, 0) and len(js) == 1
    assert js[0]["source"] == "sync" and js[0]["project_id"] == "p1" and json.loads(js[0]["args"]) == {"cwd": "C:\\work\\repo"}
    assert js[0]["run_as"] == "user"


def test_sync_skips_offline_and_old_agents(hub):
    c, app, clock, up = hub
    ureport(c, "box", version="2.2.0")
    project_with_repo(app)
    c.put("/api/projects/p1/sync", json={"nodes": ["box"]}, headers=UI)
    assert up.sync_tick(clock()) == 0


@pytest.mark.parametrize("result,status,alert", [("diverged", "done", True), ("error", "failed", True),
                                                  ("dirty", "done", False), ("ff", "done", False)])
def test_sync_result_raises_or_resolves_repo_conflict(hub, result, status, alert):
    # Arrange
    c, app, clock, up = hub
    alerts = Alerts(app.state.ctx)
    ureport(c, "box", cap=0)
    project_with_repo(app)
    c.put("/api/projects/p1/sync", json={"nodes": ["box"]}, headers=UI)
    up.sync_tick(clock())
    jid = ureport(c, "box", cap=1)["jobs"][0]["id"]
    # Act
    finish(c, jid, "box", status=status, out='##RESULT {"result": "%s"}\n' % result)
    # Assert
    key = "repo_conflict:box:p1"
    if alert:
        assert alerts.kinds() == ["repo_conflict"] and alerts.raised[0][1]["key"] == key
        assert alerts.raised[0][1]["action"]["kind"] == "open_project"
    else:
        assert alerts.raised == [] and (key in alerts.resolved) == (result == "ff")


# ---------------------------------------------------------------- F4 Windows Update
def test_wu_slots_staggered_by_rollout_order_leader_last(hub):
    c, app, clock, up = hub
    fleet(c, app)
    slots = {p["node_id"]: p["slot"] for p in up.wu_plan(clock())}
    assert slots == {"n3": "03:00", "n2": "03:30", "hub1": "04:00"}


def test_wu_tick_queues_each_node_at_its_slot_once_per_day(hub):
    # Arrange
    c, app, clock, up = hub
    fleet(c, app)
    day = datetime(2026, 11, 10)

    def at(hh, mm):
        clock.t = local_ts(day, hh, mm)
        for n in ("hub1", "n2", "n3"):
            sreport(c, n, cap=0)
        return up.wu_tick(clock())
    # Act / Assert
    assert at(2, 59) == 0
    assert at(3, 1) == 1 and [j["node_id"] for j in jobs_of(app, action="windows_update")] == ["n3"]
    assert at(3, 10) == 0
    assert at(3, 31) == 1 and at(4, 1) == 1
    assert [j["node_id"] for j in jobs_of(app, action="windows_update")] == ["n3", "n2", "hub1"]
    assert json.loads(jobs_of(app, action="windows_update")[0]["args"]) == {"reboot": "never"}
    assert at(5, 1) == 0
    assert json.loads(app.state.store.q("SELECT wu_last FROM settings WHERE node_id='n3'")[0]["wu_last"])["date"] == "2026-11-10"


def test_wu_override_off_and_own_window(hub):
    c, app, clock, up = hub
    fleet(c, app)
    assert c.put("/api/nodes/n3/settings", json={"wu_window": "off"}, headers=UI).status_code == 200
    assert c.put("/api/nodes/n2/settings", json={"wu_window": "01:00-02:00"}, headers=UI).status_code == 200
    assert c.put("/api/nodes/n2/settings", json={"wu_window": "1-2"}, headers=UI).status_code == 400
    slots = {p["node_id"]: p["slot"] for p in up.wu_plan(clock())}
    assert slots["n3"] is None and slots["n2"] == "01:00"
    st = c.get("/api/state", headers=UI).json()
    assert {n["id"]: n["settings"]["wu_window"] for n in st["nodes"]}["n3"] == "off"


def test_node_settings_put_keeps_wu_window_and_canary(hub):
    c, app, *_ = hub
    ureport(c, "box")
    c.put("/api/nodes/box/settings", json={"wu_window": "02:00-03:00"}, headers=UI)
    c.put("/api/nodes/box/settings", json={"canary": True}, headers=UI)
    c.put("/api/nodes/box/settings", json={"max_jobs": 3}, headers=UI)
    r = dict(app.state.store.q("SELECT * FROM settings WHERE node_id='box'")[0])
    assert (r["wu_window"], r["canary"], r["max_jobs"]) == ("02:00-03:00", 1, 3)


def run_wu(c, app, up, clock, node, out, hh=3, mm=1):
    clock.t = local_ts(datetime(2026, 11, 10), hh, mm)
    for n in ("hub1", "n2", "n3"):
        sreport(c, n, cap=0)
    up.wu_tick(clock())
    jid = [j for j in jobs_of(app, action="windows_update") if j["node_id"] == node][-1]["id"]
    assert sreport(c, node)["jobs"][0]["id"] == jid
    finish(c, jid, node, out=out)
    return json.loads(app.state.store.q("SELECT wu_last FROM settings WHERE node_id=?", (node,))[0]["wu_last"])


def test_wu_result_reboots_when_another_hub_is_healthy_and_window_open(hub):
    # Arrange
    c, app, clock, up = hub
    alerts = Alerts(app.state.ctx)
    fleet(c, app)
    # Act: n3 (not the leader) installs 2 updates and needs a reboot; the leader hub is healthy
    last = run_wu(c, app, up, clock, "n3", '##RESULT {"installed": 2, "failed": 0, "reboot_required": true, "kb": ["KB1"]}\n')
    # Assert
    assert last | {} == last and last["installed"] == 2 and last["reboot_required"] is True and last["reboot"] == "queued"
    rb = jobs_of(app, action="reboot")
    assert len(rb) == 1 and rb[0]["node_id"] == "n3" and json.loads(rb[0]["args"]) == {"delay_s": 60}
    assert "reboot_pending" not in alerts.kinds() and "windows_update_failed:n3" in alerts.resolved


def test_wu_result_on_lone_leader_leaves_reboot_pending(hub):
    c, app, clock, up = hub
    alerts = Alerts(app.state.ctx)
    fleet(c, app)
    last = run_wu(c, app, up, clock, "hub1", '##RESULT {"installed": 1, "failed": 1, "reboot_required": true}\n', 4, 1)
    assert last["reboot"] == "pending" and jobs_of(app, action="reboot") == []
    assert sorted(alerts.kinds()) == ["reboot_pending", "windows_update_failed"]


def test_wu_missed_hook_result_picked_up_by_tick(hub):
    c, app, clock, up = hub
    fleet(c, app)
    clock.t = local_ts(datetime(2026, 11, 10), 3, 1)
    for n in ("hub1", "n2", "n3"):
        sreport(c, n, cap=0)
    up.wu_tick(clock())
    jid = jobs_of(app, action="windows_update")[0]["id"]
    app.state.store.touch("jobs", "id", jid, "status='done', output=?", ('##RESULT {"installed": 0, "failed": 0}\n',))
    up.wu_tick(clock())
    last = json.loads(app.state.store.q("SELECT wu_last FROM settings WHERE node_id='n3'")[0]["wu_last"])
    assert last["state"] == "done" and last["installed"] == 0


# ---------------------------------------------------------------- F5 Wake-on-LAN
def macs(ip, mac):
    return {"macs": [{"iface": "Ethernet", "mac": mac, "ip": ip, "prefix": 24}]}


def test_wake_sends_wol_via_a_peer_on_the_same_subnet(hub):
    # Arrange
    c, app, clock, up = hub
    ureport(c, "sleepy", metrics=macs("192.168.1.10", "AA-BB-CC-DD-EE-01"))
    ureport(c, "other", metrics=macs("172.16.0.5", "AA-BB-CC-DD-EE-03"))
    clock.advance(40)
    ureport(c, "helper", metrics=macs("192.168.1.20", "AA-BB-CC-DD-EE-02"))
    ureport(c, "other", metrics=macs("172.16.0.5", "AA-BB-CC-DD-EE-03"))
    # Act
    r = c.post("/api/machines/sleepy/wake", headers=UI)
    other = ureport(c, "other")
    resp = ureport(c, "helper")
    again = ureport(c, "helper")
    # Assert
    assert r.status_code == 200 and r.json() == {"ok": True, "via": ["helper"], "mac": "AA-BB-CC-DD-EE-01"}
    assert "wol" not in other and "wol" not in again
    assert resp["wol"] == [{"mac": "AA-BB-CC-DD-EE-01", "broadcast": "192.168.1.255", "port": 9},
                           {"mac": "AA-BB-CC-DD-EE-01", "broadcast": "255.255.255.255", "port": 9}]


def test_wake_errors(hub):
    c, app, clock, up = hub
    ureport(c, "on", metrics=macs("192.168.1.10", "AA-BB-CC-DD-EE-01"))
    ureport(c, "nomac")
    ureport(c, "lonely", metrics=macs("172.16.0.9", "AA-BB-CC-DD-EE-09"))
    clock.advance(40)
    ureport(c, "on", metrics=macs("192.168.1.10", "AA-BB-CC-DD-EE-01"))
    assert c.post("/api/machines/ghost/wake", headers=UI).status_code == 404
    assert c.post("/api/machines/on/wake", headers=UI).status_code == 409
    assert c.post("/api/machines/nomac/wake", headers=UI).status_code == 422
    assert c.post("/api/machines/lonely/wake", headers=UI).status_code == 503


def test_wol_expires_after_60s(hub):
    c, app, clock, up = hub
    ureport(c, "sleepy", metrics=macs("192.168.1.10", "AA-BB-CC-DD-EE-01"))
    clock.advance(40)
    ureport(c, "helper", metrics=macs("192.168.1.20", "AA-BB-CC-DD-EE-02"))
    c.post("/api/machines/sleepy/wake", headers=UI)
    clock.advance(61)
    up.on_tick(clock(), True)
    assert "wol" not in ureport(c, "helper")


def test_macs_stored_in_prefs_only_when_changed(hub):
    c, app, clock, up = hub
    ureport(c, "box", metrics=macs("192.168.1.10", "AA-BB-CC-DD-EE-01"))
    u1 = app.state.store.q("SELECT updated FROM prefs WHERE key='upkeep.macs.box'")[0]["updated"]
    clock.advance(5)
    ureport(c, "box", metrics=macs("192.168.1.10", "AA-BB-CC-DD-EE-01"))
    assert app.state.store.q("SELECT updated FROM prefs WHERE key='upkeep.macs.box'")[0]["updated"] == u1
    assert up.known_macs("box")[0]["mac"] == "AA-BB-CC-DD-EE-01"


# ---------------------------------------------------------------- F6b pre-pull
def test_brief_node_prepulls_missing_model_once_per_day(hub):
    # Arrange: 01:00 local is inside the default brief window (00:00-07:00)
    c, app, clock, up = hub
    clock.t = local_ts(datetime(2026, 11, 10), 1, 0)
    ureport(c, "gpu", tools=("ollama",), metrics={"ollama_models": ["llama3:latest"],
                                                  "gpu": [{"mem_total_mb": 24000, "util": 0}]})
    # Act
    j1 = up.prepull_tick(clock())
    j2 = up.prepull_tick(clock())
    # Assert
    js = jobs_of(app, action="ollama_pull")
    assert j1 and j2 is None and len(js) == 1 and js[0]["node_id"] == "gpu" and js[0]["run_as"] == "user"
    assert json.loads(js[0]["args"]) == {"model": app.state.p2.brief_model()}


def test_no_prepull_when_model_present_unknown_or_outside_window(hub):
    c, app, clock, up = hub
    clock.t = local_ts(datetime(2026, 11, 10), 1, 0)
    model = app.state.p2.brief_model()
    ureport(c, "gpu", tools=("ollama",), metrics={"ollama_models": [model]})
    assert up.prepull_tick(clock()) is None
    up.models.clear()
    assert up.prepull_tick(clock()) is None                        # model list unknown (agent < 2.3)
    ureport(c, "gpu", tools=("ollama",), metrics={"ollama_models": []})
    clock.t = local_ts(datetime(2026, 11, 10), 12, 0)
    ureport(c, "gpu", tools=("ollama",), metrics={"ollama_models": []})
    assert up.prepull_tick(clock()) is None


# ---------------------------------------------------------------- wiring
def test_state_extras_and_hooks_registered(hub):
    c, app, clock, up = hub
    ctx = app.state.ctx
    assert up.on_tick in ctx.on_tick and up.on_report in ctx.on_report and up.on_job_finished in ctx.on_job_finished
    ureport(c, "box", metrics=macs("192.168.1.10", "AA-BB-CC-DD-EE-01"))
    clock.advance(40)
    node = c.get("/api/state", headers=UI).json()["nodes"][0]
    assert node["upkeep"]["wake"] is True and node["settings"]["wu_window"] is None


def test_full_tick_is_quiet_on_a_fresh_hub(hub):
    c, app, clock, up = hub
    clock.advance(31)
    app.state.tick()
    assert jobs_of(app) == []


# ---------------------------------------------------------------- queue timeouts (a machine that went away never blocks)
def test_run_step_not_picked_up_is_skipped_after_30_min(hub):
    c, app, clock, up = hub
    fleet(c, app)
    rid = c.post("/api/maintenance/runs", json={"action": "free_disk", "targets": ["n3", "n2"], "confirm": True},
                 headers=admin(c)).json()["id"]
    up.on_tick(clock(), True)
    jid = c.get(f"/api/maintenance/runs/{rid}", headers=UI).json()["current"]["job_id"]
    clock.advance(1801)
    sreport(c, "n2", cap=0)
    up.step_runs(clock())
    run = c.get(f"/api/maintenance/runs/{rid}", headers=UI).json()
    assert jobs_of(app, id=jid)[0]["status"] == "cancelled"
    assert run["results"][0]["status"] == "skipped" and run["current"]["node_id"] == "n2"


def test_stale_queued_sync_job_is_replaced(hub):
    c, app, clock, up = hub
    ureport(c, "box", cap=0)
    project_with_repo(app)
    c.put("/api/projects/p1/sync", json={"nodes": ["box"]}, headers=UI)
    up.sync_tick(clock())
    clock.advance(2 * fu.REPO_SYNC_DEFAULT_S + 1)
    ureport(c, "box", cap=0)
    assert up.sync_tick(clock()) == 1
    assert [j["status"] for j in jobs_of(app, action="git_sync")] == ["cancelled", "queued"]


def test_missed_windows_update_is_cancelled_after_6h(hub):
    c, app, clock, up = hub
    fleet(c, app)
    clock.t = local_ts(datetime(2026, 11, 10), 3, 1)
    for n in ("hub1", "n2", "n3"):
        sreport(c, n, cap=0)
    up.wu_tick(clock())
    jid = jobs_of(app, action="windows_update")[0]["id"]
    clock.advance(6 * 3600 + 60)
    up.wu_tick(clock())
    last = json.loads(app.state.store.q("SELECT wu_last FROM settings WHERE node_id='n3'")[0]["wu_last"])
    assert jobs_of(app, id=jid)[0]["status"] == "cancelled" and last["state"] == "missed"


# ---------------------------------------------------------------- review fixes (F-02 .. F-10)
def ureport_boot(c, node, boot, version="2.3.0", cap=0):
    """A user-lane agent's first report after it (or the machine) started: boot flag + info.boot."""
    body = {"node_id": node, "capacity": cap, "metrics": {"cpu": 5}, "boot": True, "agent_version": version,
            "info": {"tools": ["claude"], "agent_version": version, "boot": boot}}
    r = c.post("/api/agent/report", json=body, headers=AGENT)
    assert r.status_code == 200, r.text


def run_of(c, rid):
    return c.get(f"/api/maintenance/runs/{rid}", headers=UI).json()


def sreport_v(c, node, version):
    r = c.post("/api/agent/report", json={"node_id": node, "lane": "system", "capacity": 0, "agent_version": version},
               headers=AGENT)
    assert r.status_code == 200, r.text


def test_rolling_reboot_waits_for_the_machine_to_come_back_before_the_next_one(hub):
    # Arrange: restart n3 then n2
    c, app, clock, up = hub
    fleet(c, app)
    rid = up.create_run("restart_machine", {}, ["n3", "n2"], source="ui")["id"]
    up.on_tick(clock(), True)
    jid = sreport(c, "n3")["jobs"][0]["id"]
    finish(c, jid, "n3")                                   # shutdown /r /t 60 returned at once
    # Act 1: next ticks while n3 is still going down / booting
    up.on_tick(clock(), True)
    clock.advance(90)
    sreport(c, "n2", cap=0)
    up.on_tick(clock(), True)
    # Assert 1: n2 not touched yet, the run waits on n3
    assert [j["node_id"] for j in jobs_of(app, action="reboot")] == ["n3"]
    run = run_of(c, rid)
    assert run["status"] == "running" and run["current"] is None and run["results"][-1]["wait"]["kind"] == "reboot"
    # Act 2: n3 is back (new LastBootUpTime)
    ureport_boot(c, "n3", "2026-11-25T03:05:00")
    sreport(c, "n2", cap=0)
    up.on_tick(clock(), True)
    # Assert 2: only now n2 reboots
    assert [j["node_id"] for j in jobs_of(app, action="reboot")] == ["n3", "n2"]
    assert run_of(c, rid)["current"]["node_id"] == "n2"


def test_rolling_reboot_fails_when_the_machine_does_not_come_back(hub):
    # Arrange
    c, app, clock, up = hub
    alerts = Alerts(app.state.ctx)
    fleet(c, app)
    rid = up.create_run("restart_machine", {}, ["n3", "n2"], source="ui")["id"]
    up.on_tick(clock(), True)
    finish(c, sreport(c, "n3")["jobs"][0]["id"], "n3")
    up.on_tick(clock(), True)
    # Act
    clock.advance(fu.WAIT_TIMEOUT_S["reboot"] + 60)
    sreport(c, "n2", cap=0)
    up.on_tick(clock(), True)
    # Assert
    run = run_of(c, rid)
    assert run["status"] == "failed" and run["results"][-1]["summary"] == "machine did not come back after the restart"
    assert [j["node_id"] for j in jobs_of(app, action="reboot")] == ["n3"]
    assert alerts.kinds() == ["upkeep_failed"]


def test_restart_hivemind_waits_for_hub_then_agent(hub, monkeypatch):
    # Arrange
    c, app, clock, up = hub
    fleet(c, app)
    healthy = {"n3": False}
    real = app.state.ctx.lease._healthy
    monkeypatch.setattr(app.state.ctx.lease, "_healthy", lambda pid: healthy.get(pid, real(pid)))
    rid = up.create_run("restart_hivemind", {}, ["n3"], source="ui")["id"]
    up.on_tick(clock(), True)
    finish(c, sreport(c, "n3")["jobs"][0]["id"], "n3")       # restart_task ForgeHub
    up.on_tick(clock(), True)
    # Act / Assert: the agent restart waits until the hub is healthy again (and at least the grace period)
    clock.advance(fu.HUB_RESTART_GRACE_S + 1)
    ureport(c, "n3", cap=0)
    sreport(c, "n3", cap=0)
    up.on_tick(clock(), True)
    assert len(jobs_of(app, run_id=rid)) == 1
    healthy["n3"] = True
    up.on_tick(clock(), True)
    js = jobs_of(app, run_id=rid)
    assert len(js) == 2 and {json.loads(j["args"])["name"] for j in js} == {"ForgeHub", "ForgeAgent"}
    second = [j for j in js if json.loads(j["args"])["name"] == "ForgeAgent"][0]
    finish(c, sreport(c, "n3")["jobs"][0]["id"], "n3")       # restart_task ForgeAgent
    assert jobs_of(app, id=second["id"])[0]["status"] == "done"
    up.on_tick(clock(), True)
    assert run_of(c, rid)["status"] == "running"
    clock.advance(5)
    ureport_boot(c, "n3", "2026-11-01T00:00:00")             # the restarted agent's first report
    up.on_tick(clock(), True)
    assert run_of(c, rid)["status"] == "done"


def test_mixed_fleet_22_system_agent_is_skipped_for_23_system_actions(hub):
    # Arrange: n2's SYSTEM agent is still 2.2
    c, app, clock, up = hub
    fleet(c, app)
    sreport_v(c, "n2", "2.2.0")
    # Act
    p = c.post("/api/maintenance/preview", json={"action": "free_disk", "targets": "all"}, headers=UI).json()
    r = c.post("/api/maintenance/preview", json={"action": "restart_machine", "targets": "all"}, headers=UI).json()
    clock.t = local_ts(datetime(2026, 11, 10), 4, 59)       # every slot is open
    for n in ("hub1", "n3"):
        sreport(c, n, cap=0)
    sreport_v(c, "n2", "2.2.0")
    up.wu_tick(clock())
    # Assert
    assert p["skipped"] == [{"node_id": "n2", "reason": "needs agent 2.3"}] and p["order"] == ["n3", "hub1"]
    assert "n2" in r["order"]                                # a 2.2 SYSTEM agent knows reboot
    assert sorted(j["node_id"] for j in jobs_of(app, action="windows_update")) == ["hub1", "n3"]


def test_wu_reboot_waits_for_running_jobs_on_the_machine(hub):
    # Arrange: n3 runs a user job while its Windows Update finishes with reboot_required
    c, app, clock, up = hub
    alerts = Alerts(app.state.ctx)
    fleet(c, app)
    jid = c.post("/api/jobs", json={"node_id": "n3", "engine": "shell", "prompt": "echo"}, headers=UI).json()["id"]
    ureport(c, "n3", cap=1)
    # Act
    last = run_wu(c, app, up, clock, "n3", '##RESULT {"installed": 1, "failed": 0, "reboot_required": true}\n')
    # Assert: no reboot while the job runs
    assert last["reboot"] == "waiting" and jobs_of(app, action="reboot") == []
    up.wu_tick(clock())
    assert jobs_of(app, action="reboot") == []
    finish(c, jid, "n3")
    up.wu_tick(clock())
    rb = jobs_of(app, action="reboot")
    assert [j["node_id"] for j in rb] == ["n3"] and up.wu_last("n3")["reboot_job"] == rb[0]["id"]
    assert "reboot_pending" not in alerts.kinds()


def test_wu_skips_a_machine_with_a_maintenance_step_in_progress(hub):
    c, app, clock, up = hub
    fleet(c, app)
    up.create_run("update_ai_tools", {}, ["n3"], source="ui")
    up.step_runs(clock())
    clock.t = local_ts(datetime(2026, 11, 10), 3, 1)
    for n in ("hub1", "n2", "n3"):
        ureport(c, n, cap=0)
        sreport(c, n, cap=0)
    assert up.wu_tick(clock()) == 0 and jobs_of(app, action="windows_update") == []


def test_wu_reboot_pending_when_admin_lane_disabled(hub):
    c, app, clock, up = hub
    alerts = Alerts(app.state.ctx)
    fleet(c, app)
    clock.t = local_ts(datetime(2026, 11, 10), 3, 1)
    for n in ("hub1", "n2", "n3"):
        sreport(c, n, cap=0)
    up.wu_tick(clock())
    wu = jobs_of(app, action="windows_update")[0]
    app.state.ctx.cfg["admin_lane_enabled"] = False
    up.on_wu_finished(wu, "done", '##RESULT {"installed": 1, "failed": 0, "reboot_required": true}\n', clock())
    assert up.wu_last("n3")["reboot"] == "pending" and "reboot_pending" in alerts.kinds()


def test_wu_queued_reboot_job_failed_becomes_pending(hub):
    # Arrange: reboot queued for n3
    c, app, clock, up = hub
    alerts = Alerts(app.state.ctx)
    fleet(c, app)
    last = run_wu(c, app, up, clock, "n3", '##RESULT {"installed": 1, "failed": 0, "reboot_required": true}\n')
    assert last["reboot"] == "queued"
    # Act: the reboot job fails
    finish(c, sreport(c, "n3")["jobs"][0]["id"], "n3", status="failed")
    up.wu_tick(clock())
    # Assert
    assert up.wu_last("n3")["reboot"] == "pending" and alerts.kinds() == ["reboot_pending"]


def test_wu_queued_reboot_overdue_becomes_pending(hub):
    c, app, clock, up = hub
    alerts = Alerts(app.state.ctx)
    fleet(c, app)
    run_wu(c, app, up, clock, "n3", '##RESULT {"installed": 1, "failed": 0, "reboot_required": true}\n')
    finish(c, sreport(c, "n3")["jobs"][0]["id"], "n3")       # shutdown accepted ... but the machine never restarts
    clock.advance(fu.REBOOT_DELAY_S + fu.REBOOT_SETTLE_S + 1)
    up.wu_tick(clock())
    assert up.wu_last("n3")["reboot"] == "pending" and "reboot_pending" in alerts.kinds()


def test_run_waits_while_a_post_update_reboot_is_imminent(hub):
    c, app, clock, up = hub
    fleet(c, app)
    run_wu(c, app, up, clock, "n3", '##RESULT {"installed": 1, "failed": 0, "reboot_required": true}\n')
    rid = up.create_run("update_ai_tools", {}, ["n3"], source="ui")["id"]
    ureport(c, "n3", cap=0)
    up.step_runs(clock())
    assert jobs_of(app, run_id=rid) == [] and run_of(c, rid)["status"] in ("pending", "running")


def test_wu_slots_never_wrap_with_five_machines():
    offs = [fu.wu_slot_offset(r, 30, 120, 5) for r in range(5)]
    assert offs == sorted(set(offs)) and offs[0] == 0 and offs[-1] < 120
    assert [fu.wu_slot_offset(r, 30, 60, 4) for r in range(4)] == [0, 19, 38, 57]
    assert [fu.wu_slot_offset(r, 30, 120, 3) for r in range(3)] == [0, 30, 60]


def test_wu_plan_five_nodes_leader_last(hub):
    c, app, clock, up = hub
    for i, n in enumerate(("n2", "n3", "n4", "n5"), start=2):
        add_peer(app, n, i)
    for n in ("hub1", "n2", "n3", "n4", "n5"):
        ureport(c, n, cap=0)
    plan = up.wu_plan(clock())
    offs = [p["_off"] for p in plan]
    assert plan[-1]["node_id"] == "hub1" and offs == sorted(set(offs)) and offs[-1] < 120


def test_other_hub_not_healthy_while_it_updates_itself(hub):
    c, app, clock, up = hub
    fleet(c, app)
    assert up.other_hub_healthy("n3")                         # hub1 (this hub) is healthy and eligible
    up.queue_action("hub1", "windows_update", {}, source="upkeep")
    assert not up.other_hub_healthy("n3")


def test_broken_run_is_failed_and_does_not_block_other_runs_or_tasks(hub):
    # Arrange: a stored run whose args no longer validate, plus a good run
    c, app, clock, up = hub
    fleet(c, app)
    bad = up.create_run("update_app", {"id": "Git.Git"}, ["n2"], source="ui")["id"]
    app.state.store.touch("maintenance_runs", "id", bad, "args=?", (json.dumps({"id": "bad id!"}),))
    good = up.create_run("update_ai_tools", {}, ["n3"], source="ui")["id"]
    # Act
    up.on_tick(clock(), True)
    # Assert
    assert run_of(c, bad)["status"] == "failed" and "no longer valid" in run_of(c, bad)["results"][-1]["summary"]
    assert run_of(c, good)["current"]["node_id"] == "n3"
    assert "misc" in up.last and "wu" in up.last           # the throttled tasks still ran


def test_on_tick_survives_an_exception_in_step_runs(hub, monkeypatch):
    c, app, clock, up = hub

    def boom(now):
        raise RuntimeError("boom")
    monkeypatch.setattr(up, "step_runs", boom)
    up.on_tick(clock(), True)
    assert "wu" in up.last and "sync" in up.last


@pytest.mark.parametrize("status,out", [("cancelled", ""), ("failed", "[hub] job lost\n"), ("failed", "timeout\n")])
def test_cancelled_or_lost_sync_does_not_raise_repo_conflict(hub, status, out):
    c, app, clock, up = hub
    alerts = Alerts(app.state.ctx)
    up.on_sync_finished({"node_id": "box", "project_id": "p1", "args": json.dumps({"cwd": "C:\\r"}), "id": "j"}, status, out)
    assert alerts.raised == []


def test_settings_put_is_all_or_nothing(hub):
    c, app, clock, up = hub
    before = c.get("/api/upkeep/settings", headers=UI).json()
    r = c.put("/api/upkeep/settings", json={"schedule": {"day": "sat"}, "windows_update": {"window": "x"}}, headers=UI)
    assert r.status_code == 400
    assert c.get("/api/upkeep/settings", headers=UI).json()["schedule"] == before["schedule"]
