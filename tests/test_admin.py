"""SYSTEM admin lane: action validation, confirm gate, lane-filtered dispatch, lane heartbeats, events, /api/actions."""
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import create_app
from cluster_net import TOKENS, FakeClock
from forge_actions import ActionError, find_action, load_catalog, validate_args
from forge_security import hash_pin
from forge_store import Store

# 2.3 S7: admin / destructive jobs need the admin PIN; Bearer clients send it as X-Admin-Pin
PIN_HASH = hash_pin("123456", iterations=1000)
UI = {"Authorization": f"Bearer {TOKENS['ui_token']}", "X-Admin-Pin": "123456"}
AGENT = {"X-Agent-Token": TOKENS["agent_token"]}


def make_hub(tmp_path, **cfg_extra):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "config.json").write_text(json.dumps(TOKENS | {"node_id": "hub1", "admin_pin_hash": PIN_HASH} | cfg_extra), encoding="utf-8")
    clock = FakeClock()
    app = create_app(data, start_monitor=False, clock=clock, releases_dir=tmp_path / "releases")
    return TestClient(app), app, clock


@pytest.fixture
def hub(tmp_path):
    return make_hub(tmp_path)


def user_report(c, node="box", cap=2, **extra):
    body = {"node_id": node, "metrics": {"cpu": 33, "mem_pct": 50}, "capacity": cap, "info": {"tools": ["claude"]},
            "procs": [{"name": "x", "pid": 1}]} | extra
    return c.post("/api/agent/report", json=body, headers=AGENT)


def sys_report(c, node="box", cap=1, **extra):
    body = {"node_id": node, "lane": "system", "capacity": cap, "running": 0, "agent_version": "2.1.0"} | extra
    return c.post("/api/agent/report", json=body, headers=AGENT)


def post_job(c, **body):
    return c.post("/api/jobs", json={"node_id": "box"} | body, headers=UI)


def action_job(c, action, args=None, **extra):
    return post_job(c, engine="action", action=action, args=args or {}, **extra)


# ---------------------------------------------------------------- catalog / validation (pure)
CAT = load_catalog()


@pytest.mark.parametrize("name,args,expected", [
    ("winget_install", {"id": "Git.Git"}, {"id": "Git.Git"}),
    ("winget_install", {"id": "Microsoft.VisualStudioCode", "scope": "machine"}, {"id": "Microsoft.VisualStudioCode", "scope": "machine"}),
    ("winget_upgrade", {}, {}),
    ("npm_global_install", {"package": "@anthropic-ai/claude-code@latest"}, {"package": "@anthropic-ai/claude-code@latest"}),
    ("npm_global_install", {"package": "typescript"}, {"package": "typescript"}),
    ("restart_task", {"name": "T3 Code Server"}, {"name": "T3 Code Server"}),
    ("restart_service", {"name": "Spooler"}, {"name": "Spooler"}),
    ("reboot", {}, {"delay_s": 60}),
    ("reboot", {"delay_s": 0}, {"delay_s": 0}),
    ("ensure_task", {"name": "T3Code", "command": "C:\\Program Files\\nodejs\\node.exe", "args": "server.js --port 3000",
                     "run_as": "user", "trigger": "logon"},
     {"name": "T3Code", "command": "C:\\Program Files\\nodejs\\node.exe", "args": "server.js --port 3000",
      "run_as": "user", "trigger": "logon"}),
])
def test_validate_accepts_good_args_and_applies_defaults(name, args, expected):
    # Arrange
    spec = find_action(CAT, name)
    # Act
    out = validate_args(spec, args)
    # Assert
    assert out == expected


@pytest.mark.parametrize("name,args,why", [
    ("winget_install", {}, "required"),
    ("winget_install", {"id": "Git.Git; rm -rf"}, "invalid"),
    ("winget_install", {"id": "Git.Git", "scope": "global"}, "one of"),
    ("winget_install", {"id": "Git.Git", "extra": 1}, "unknown"),
    ("winget_upgrade", {"id": "a b"}, "invalid"),
    ("npm_global_install", {"package": "Typescript"}, "invalid"),          # npm names are lowercase
    ("npm_global_install", {"package": "x && calc"}, "invalid"),
    ("npm_global_install", {"package": "../evil"}, "invalid"),
    ("restart_task", {"name": "a\\b"}, "invalid"),
    ("restart_service", {"name": "svc\n"}, "control"),
    ("reboot", {"delay_s": 3601}, "between"),
    ("reboot", {"delay_s": -1}, "between"),
    ("reboot", {"delay_s": "60"}, "integer"),
    ("reboot", {"delay_s": True}, "integer"),
    ("reboot", {"delay_s": 1.5}, "integer"),
    ("ensure_task", {"name": "ForgeHub", "command": "x.exe", "run_as": "system", "trigger": "startup"}, "may not"),
    ("ensure_task", {"name": "forgesystemagent", "command": "x.exe", "run_as": "system", "trigger": "startup"}, "may not"),
    ("ensure_task", {"name": "T", "command": "x\".exe", "run_as": "system", "trigger": "startup"}, "invalid"),
    ("ensure_task", {"name": "T", "command": "x.exe", "run_as": "admin", "trigger": "startup"}, "one of"),
    ("ensure_task", {"name": "T", "command": "x.exe", "run_as": "user", "trigger": "boot"}, "one of"),
    ("ensure_task", {"name": "T", "command": "x.exe", "args": "a\r\nb", "run_as": "user", "trigger": "logon"}, "control"),
])
def test_validate_rejects_bad_args(name, args, why):
    with pytest.raises(ActionError, match=why):
        validate_args(find_action(CAT, name), args)


VECTORS = json.loads((Path(__file__).resolve().parent / "ps" / "fixtures" / "action-vectors.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("v", VECTORS["valid"], ids=lambda v: v["action"])
def test_shared_vectors_valid(v):
    assert validate_args(find_action(CAT, v["action"]), v["args"]) == v["expect"]


@pytest.mark.parametrize("v", VECTORS["invalid"], ids=lambda v: f"{v['action']}:{json.dumps(v['args'])[:40]}")
def test_shared_vectors_invalid(v):
    spec = find_action(CAT, v["action"])
    if spec is None:
        return  # unknown action: rejected by find_action
    with pytest.raises(ActionError):
        validate_args(spec, v["args"])


def test_catalog_shape_destructive_flags_and_unique_names():
    names = [a["name"] for a in CAT["actions"]]
    assert len(names) == len(set(names))
    assert set(names) == {"winget_install", "winget_upgrade", "npm_global_install", "restart_task", "restart_service",
                          "reboot", "ensure_task",
                          # 2.3 workstream F (F2-F4, R6 forge_uninstall)
                          "update_claude", "update_codex", "update_t3", "update_ollama", "git_sync", "ollama_pull",
                          "windows_update", "free_disk", "forge_uninstall"}
    assert find_action(CAT, "reboot")["destructive"] is True
    assert find_action(CAT, "forge_uninstall")["destructive"] is True
    assert find_action(CAT, "winget_install")["destructive"] is False
    for a in CAT["actions"]:
        assert a.get("lane") in ("system", "user")
        for rule in a["args"].values():
            assert rule["type"] in ("string", "int", "bool") and isinstance(rule.get("required", False), bool)


# ---------------------------------------------------------------- job creation
def test_action_job_created_as_system_with_validated_args(hub):
    c, app, _ = hub
    user_report(c)
    r = action_job(c, "winget_install", {"id": "Git.Git"})
    assert r.status_code == 200 and r.json()["run_as"] == "system"
    row = app.state.store.q("SELECT * FROM jobs WHERE id=?", (r.json()["id"],))[0]
    assert row["run_as"] == "system" and row["action"] == "winget_install" and json.loads(row["args"]) == {"id": "Git.Git"}
    assert "winget_install" in row["prompt"]


def test_bad_action_or_args_rejected_with_400(hub):
    c, _, _ = hub
    user_report(c)
    assert action_job(c, "format_c", {}).status_code == 400
    assert action_job(c, "winget_install", {"id": "x|y"}).status_code == 400
    assert action_job(c, "winget_install", {}).status_code == 400
    assert post_job(c, engine="action").status_code == 400


def test_user_engines_cannot_run_as_system_and_auto_not_allowed_for_system(hub):
    c, _, _ = hub
    user_report(c)
    r = post_job(c, engine="shell", prompt="whoami", run_as="system")
    assert r.status_code == 400 and "user lane only" in r.json()["detail"]
    assert post_job(c, engine="shell", prompt="x", run_as="root").status_code == 400
    r = c.post("/api/jobs", json={"node_id": "auto", "engine": "action", "action": "winget_upgrade", "args": {}}, headers=UI)
    assert r.status_code == 400 and "not auto" in r.json()["detail"]


def test_run_as_user_on_action_is_forced_to_system(hub):
    c, _, _ = hub
    user_report(c)
    assert action_job(c, "winget_upgrade", {}, run_as="user").json()["run_as"] == "system"


def test_confirm_gate_for_admin_and_destructive_actions(hub):
    c, _, _ = hub
    user_report(c)
    # admin without confirm
    r = post_job(c, engine="admin", prompt="Get-Service")
    assert r.status_code == 400 and r.json()["detail"] == "confirmation required"
    # destructive action without confirm
    r = action_job(c, "reboot", {"delay_s": 30})
    assert r.status_code == 400 and r.json()["detail"] == "confirmation required"
    # non-destructive needs none
    assert action_job(c, "winget_upgrade", {}).status_code == 200
    # with confirm
    assert post_job(c, engine="admin", prompt="Get-Service", confirm=True).status_code == 200
    assert action_job(c, "reboot", {"delay_s": 30}, confirm=True).status_code == 200


def test_admin_job_requires_prompt_and_rejects_action_fields(hub):
    c, _, _ = hub
    user_report(c)
    assert post_job(c, engine="admin", prompt="  ", confirm=True).status_code == 400
    assert post_job(c, engine="admin", prompt="x", action="reboot", confirm=True).status_code == 400


def test_system_jobs_emit_warn_events_with_source(hub):
    c, _, _ = hub
    user_report(c)
    action_job(c, "restart_service", {"name": "Spooler"}, source="phone")
    post_job(c, engine="admin", prompt="Get-Date", confirm=True)
    ev = c.get("/api/state", headers=UI).json()["events"]
    warn = [e["msg"] for e in ev if e["level"] == "warn"]
    assert any(m.startswith("ACTION restart_service") and "source phone" in m for m in warn)
    assert any(m.startswith("ADMIN") and "source ui" in m for m in warn)


def test_admin_lane_disabled_on_hub_fails_job_clearly(tmp_path):
    c, app, _ = make_hub(tmp_path, admin_lane_enabled=False)
    user_report(c)
    r = action_job(c, "winget_upgrade", {})
    assert r.status_code == 200 and r.json()["status"] == "failed"
    job = c.get(f"/api/jobs/{r.json()['id']}", headers=UI).json()
    assert "admin lane is disabled" in job["output"]
    assert sys_report(c).json()["jobs"] == []
    assert c.get("/api/actions", headers=UI).json()["admin_lane_enabled"] is False


# ---------------------------------------------------------------- dispatch by lane
def test_lane_filtered_dispatch(hub):
    c, _, _ = hub
    user_report(c, cap=0)
    sid = action_job(c, "winget_upgrade", {}).json()["id"]
    uid = post_job(c, engine="shell", prompt="dir").json()["id"]
    # user lane only gets the user job
    got = user_report(c, cap=5).json()["jobs"]
    assert [j["id"] for j in got] == [uid]
    # system lane only gets the system job, with action + args
    got = sys_report(c, cap=5).json()["jobs"]
    assert [j["id"] for j in got] == [sid]
    assert got[0]["engine"] == "action" and got[0]["run_as"] == "system" and got[0]["action"] == "winget_upgrade"
    assert got[0]["args"] == {}


def test_legacy_agents_without_lane_never_receive_system_jobs(hub):
    c, _, _ = hub
    user_report(c, cap=0)
    action_job(c, "winget_upgrade", {})
    post_job(c, engine="admin", prompt="whoami", confirm=True)
    # v1 / 2.0.x agent: no lane field, plenty of capacity
    for _ in range(3):
        assert c.post("/api/agent/report", json={"node_id": "box", "capacity": 8}, headers=AGENT).json()["jobs"] == []


def test_system_jobs_with_null_run_as_are_user_jobs(hub):
    """Rows replicated from a pre-2.1 leader have run_as NULL: they belong to the user lane."""
    c, app, clock = hub
    user_report(c, cap=0)
    jid = post_job(c, engine="shell", prompt="dir").json()["id"]
    app.state.store.x("UPDATE jobs SET run_as=NULL WHERE id=?", (jid,))
    assert sys_report(c).json()["jobs"] == []
    assert [j["id"] for j in user_report(c).json()["jobs"]] == [jid]


def test_system_lane_capacity_and_cancel_are_lane_scoped(hub):
    c, _, _ = hub
    user_report(c, cap=0)
    a = action_job(c, "winget_upgrade", {}).json()["id"]
    b = action_job(c, "restart_service", {"name": "Spooler"}).json()["id"]
    assert sys_report(c, cap=0).json()["jobs"] == []
    first = [j["id"] for j in sys_report(c, cap=1).json()["jobs"]]
    assert len(first) == 1 and first[0] in (a, b)
    c.post(f"/api/jobs/{first[0]}/cancel", headers=UI)
    assert user_report(c, cap=0).json()["cancel"] == []
    assert sys_report(c, cap=0).json()["cancel"] == first
    assert [j["id"] for j in sys_report(c, cap=1).json()["jobs"]] == [({a, b} - set(first)).pop()]


def test_lane_boot_only_fails_its_own_jobs(hub):
    c, app, _ = hub
    user_report(c, cap=0)
    s = action_job(c, "winget_upgrade", {}).json()["id"]
    u = post_job(c, engine="shell", prompt="x").json()["id"]
    sys_report(c)
    user_report(c)
    user_report(c, boot=True)      # user agent restarted
    st = {j["id"]: j["status"] for j in c.get("/api/state", headers=UI).json()["jobs"]}
    assert st[u] == "failed" and st[s] == "starting"
    sys_report(c, boot=True)
    assert c.get(f"/api/jobs/{s}", headers=UI).json()["status"] == "failed"


def test_system_job_lost_when_system_lane_goes_silent(hub):
    c, app, clock = hub
    user_report(c, cap=0)
    s = action_job(c, "winget_upgrade", {}).json()["id"]
    sys_report(c)
    # user lane keeps reporting, system lane silent for > 90 s
    for _ in range(4):
        clock.advance(30)
        user_report(c, cap=0)
    app.state.check_presence()
    j = c.get(f"/api/jobs/{s}", headers=UI).json()
    assert j["status"] == "failed" and "node offline" in j["output"]


# ---------------------------------------------------------------- heartbeats
def test_system_lane_heartbeat_does_not_clobber_user_metrics(hub):
    c, app, clock = hub
    user_report(c, cap=2)
    clock.advance(2)
    sys_report(c, cap=1)
    s = c.get("/api/state", headers=UI).json()
    n = s["nodes"][0]
    assert n["metrics"]["cpu"] == 33 and n["capacity"] == 2 and n["info"]["tools"] == ["claude"]
    assert n["system_lane"]["online"] is True and n["system_lane"]["capacity"] == 1 and n["system_lane"]["version"] == "2.1.0"
    d = c.get("/api/nodes/box", headers=UI).json()
    assert d["procs"] == [{"name": "x", "pid": 1}] and d["system_lane"]["online"]
    assert app.state.store.q("SELECT COUNT(*) n FROM metrics")[0]["n"] == 1   # no metrics row from the system lane


def test_system_lane_online_offline_in_state_and_health(hub):
    c, _, clock = hub
    user_report(c)
    h = c.get("/api/health", headers=UI).json()["nodes"][0]
    assert h["system_lane"] == {"online": False, "last_seen": None, "capacity": 0, "version": None}
    sys_report(c)
    assert c.get("/api/health", headers=UI).json()["nodes"][0]["system_lane"]["online"] is True
    clock.advance(31)
    user_report(c)
    assert c.get("/api/state", headers=UI).json()["nodes"][0]["system_lane"]["online"] is False
    assert c.get("/api/state", headers=UI).json()["nodes"][0]["online"] is True


def test_system_lane_report_for_unknown_node_does_not_mark_it_online(hub):
    c, _, _ = hub
    r = sys_report(c, node="fresh")
    assert r.status_code == 200 and r.json()["lane"] == "system" and "settings" in r.json()
    n = c.get("/api/state", headers=UI).json()["nodes"][0]
    assert n["id"] == "fresh" and n["online"] is False and n["system_lane"]["online"] is True


# ---------------------------------------------------------------- /api/actions + migration
def test_api_actions_returns_catalog(hub):
    c, _, _ = hub
    assert c.get("/api/actions").status_code == 401
    r = c.get("/api/actions", headers=UI).json()
    assert {a["name"] for a in r["actions"]} >= {"winget_install", "reboot", "ensure_task"}
    assert r["admin_lane_enabled"] is True
    reboot = next(a for a in r["actions"] if a["name"] == "reboot")
    assert reboot["destructive"] is True and reboot["args"]["delay_s"]["type"] == "int"


def test_job_rows_expose_run_as_and_args(hub):
    c, _, _ = hub
    user_report(c)
    sid = action_job(c, "winget_install", {"id": "Git.Git"}).json()["id"]
    uid = post_job(c, engine="shell", prompt="x").json()["id"]
    jobs = {j["id"]: j for j in c.get("/api/state", headers=UI).json()["jobs"]}
    assert jobs[sid]["run_as"] == "system" and jobs[sid]["args"] == {"id": "Git.Git"}
    assert jobs[uid]["run_as"] == "user" and jobs[uid]["args"] is None


def test_existing_2_0_database_gets_new_columns(tmp_path):
    # Arrange: a 2.0 schema (no run_as / sys_* columns)
    db = tmp_path / "forge.db"
    con = sqlite3.connect(str(db))
    con.executescript("""
      CREATE TABLE nodes (id TEXT PRIMARY KEY, created REAL, last_seen REAL, online INTEGER DEFAULT 0, addr TEXT,
        info TEXT DEFAULT '{}', metrics TEXT DEFAULT '{}', procs TEXT DEFAULT '[]', agents TEXT DEFAULT '[]',
        capacity INTEGER DEFAULT 0, updated REAL DEFAULT 0);
      CREATE TABLE jobs (id TEXT PRIMARY KEY, node_id TEXT, engine TEXT, prompt TEXT, cwd TEXT, full_access INTEGER,
        status TEXT, created REAL, started REAL, finished REAL, exit_code INTEGER, pid INTEGER, output TEXT DEFAULT '',
        cancel_requested INTEGER DEFAULT 0, source TEXT, requested TEXT, updated REAL DEFAULT 0);
      INSERT INTO jobs(id,node_id,engine,prompt,status,updated) VALUES('j1','box','shell','x','queued',1);
    """)
    con.commit(); con.close()
    # Act
    s = Store(db)
    # Assert
    assert {"sys_last_seen", "sys_capacity", "sys_version"} <= set(s.columns("nodes"))
    assert {"run_as", "action", "args"} <= set(s.columns("jobs"))
    assert s.q("SELECT run_as FROM jobs WHERE id='j1'")[0]["run_as"] == "user"
