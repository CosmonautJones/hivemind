"""Brief scheduling (2.2.1): night window, GPU gate, daily morning digest, on-demand always allowed."""
from datetime import datetime

import pytest

import forge_phase2 as p2m
from p2util import AGENT, UI, make_hub, report


def local_ts(day: int, hour: int, minute: int = 0) -> float:
    return datetime(2023, 11, day, hour, minute).timestamp()      # naive = hub-local wall clock


def gpu_node(c, nid, util=0):
    return report(c, nid, tools=("claude", "ollama"), gpu=[{"name": "g", "mem_total_mb": 24000, "util": util}])


def activity(c, clock, node="g", sid="s1", msgs=1):
    c.post("/api/agent/sessions", headers=AGENT, json={"node_id": node, "sessions": [{
        "tool": "claude", "session_id": sid, "cwd": "C:\\dev\\forge", "git_remote": "https://github.com/me/forge",
        "last_activity": clock.t - 5, "messages": msgs, "state": "idle"}]})


def brief_jobs(app, project=None):
    sql = "SELECT * FROM jobs WHERE source='brief'" + (" AND project_id=?" if project else "")
    return app.state.store.q(sql, (project,) if project else ())


@pytest.mark.parametrize("raw,want", [
    ("00:00-07:00", (0, 420)), ("22:00-06:30", (1320, 390)), ("always", (0, 1440)), ("09:00-09:00", (0, 1440)),
    ("7-9", None), ("25:00-01:00", None), ("", None)])
def test_parse_window(raw, want):
    assert p2m.parse_window(raw) == want


def test_in_window_wraps_midnight():
    w = p2m.parse_window("22:00-06:00")
    assert p2m.in_window(w, local_ts(15, 23)) and p2m.in_window(w, local_ts(15, 5, 59))
    assert not p2m.in_window(w, local_ts(15, 6)) and not p2m.in_window(w, local_ts(15, 12))


def test_loop_runs_only_in_window_and_while_gpu_is_quiet(tmp_path):
    c, app, clock = make_hub(tmp_path)
    p2 = app.state.p2
    clock.t = local_ts(15, 3)
    gpu_node(c, "g", util=80)                                   # busy GPU at night: wait
    activity(c, clock)
    assert p2.brief_tick(clock()) == 0
    sch = c.get("/api/scheduler", headers=UI).json()["briefs"]
    assert sch["brief_window"] == "00:00-07:00" and sch["in_window"] is True and sch["brief_digest_time"] == "07:30"
    assert sch["brief_gpu_max"] == 30 and sch["brief_node"] == "g" and sch["brief_node_gpu_util"] == 80
    clock.advance(60)
    gpu_node(c, "g", util=10)                                   # quiet: the loop runs right away (interval not consumed)
    assert p2.brief_tick(clock()) == 1
    # daytime: new activity is not briefed automatically...
    clock.t = local_ts(15, 12)
    gpu_node(c, "g", util=0)
    activity(c, clock, msgs=5)
    assert p2.brief_tick(clock()) == 0
    assert c.get("/api/scheduler", headers=UI).json()["briefs"]["in_window"] is False
    # ...but 'Brief me' always works
    pid = c.get("/api/projects", headers=UI).json()[0]["id"]
    assert c.post(f"/api/projects/{pid}/brief", headers=UI).json()["existing"] is False


def test_daily_digest_at_digest_time_once(tmp_path):
    c, app, clock = make_hub(tmp_path)
    p2 = app.state.p2
    clock.t = local_ts(15, 12)
    gpu_node(c, "g")
    activity(c, clock)
    p2.brief_tick(clock())
    assert brief_jobs(app, "*") == []                           # 12:00 is not the digest slot (07:30 + 3 h)
    clock.t = local_ts(16, 7, 29)
    gpu_node(c, "g")
    p2.brief_tick(clock())
    assert brief_jobs(app, "*") == []
    clock.t = local_ts(16, 7, 31)
    gpu_node(c, "g", util=90)                                   # the digest does not wait for a quiet GPU
    p2.brief_tick(clock())
    d = brief_jobs(app, "*")
    assert len(d) == 1 and d[0]["engine"] == "ollama" and d[0]["node_id"] == "g" and d[0]["kind"] == "text"
    clock.advance(600)
    p2.brief_tick(clock())
    assert len(brief_jobs(app, "*")) == 1                       # once per day
    clock.t = local_ts(17, 8)
    p2.brief_tick(clock())
    assert len(brief_jobs(app, "*")) == 2


def test_manual_rollup_after_digest_time_counts_as_the_digest(tmp_path):
    c, app, clock = make_hub(tmp_path, {"brief_digest_time": "06:00", "brief_window": "01:00-02:00"})
    clock.t = local_ts(16, 6, 10)
    gpu_node(c, "g")
    report(c, "w", cap=5)
    c.post("/api/brief/all", headers=UI)
    app.state.p2.brief_tick(clock())
    assert len(brief_jobs(app, "*")) == 1
    s = c.get("/api/scheduler", headers=UI).json()["briefs"]
    assert s["brief_window"] == "01:00-02:00" and s["brief_digest_time"] == "06:00"


def test_bad_config_falls_back_to_defaults_and_digest_can_be_disabled(tmp_path):
    c, app, clock = make_hub(tmp_path, {"brief_window": "nonsense", "brief_digest_time": "off"})
    s = c.get("/api/scheduler", headers=UI).json()["briefs"]
    assert s["brief_window"] == "00:00-07:00" and s["brief_digest_time"] is None
    clock.t = local_ts(16, 7, 45)
    gpu_node(c, "g")
    activity(c, clock)
    app.state.p2.brief_tick(clock())
    assert brief_jobs(app) == []
