"""Phase 2 scheduler: scoring, cooldown parsing, engine choice, requeue rules, 'any' engine, version gating."""
from datetime import timezone, timedelta

import pytest

import forge_sched as fs
from p2util import AGENT, UI, get_job, job, make_hub, report

UTC = timezone.utc
NOW = 1_700_000_000.0          # 2023-11-14 22:13:20 UTC


@pytest.fixture
def hub(tmp_path):
    return make_hub(tmp_path)


# ---------------------------------------------------------------- pure: scoring
def test_score_formula_matches_spec():
    s = fs.score_node(cpu=40, mem_pct=50, active_jobs=2, is_leader=True, has_gpu=True, engine="ollama", work_root_free_gb=10)
    assert s == 100 - 20 - 15 - 30 - 10 + 40 - 50
    assert fs.score_node(0, 0, 0, False, True, "claude", 100) == 100          # gpu bonus is ollama only
    assert fs.score_node(None, None, 0, False, False, "claude", None) == 50   # unknown cpu counts as busy


def test_rank_ties_prefer_free_capacity_then_priority_then_id():
    ranked = sorted([fs.rank_key(80, 1, 1, "a"), fs.rank_key(80, 3, 2, "b"), fs.rank_key(80, 3, 1, "c"), fs.rank_key(90, 0, 9, "z")])
    assert [k[3] for k in ranked] == ["z", "c", "b", "a"]


def test_pick_engine_follows_preference_skips_cooling_and_unavailable():
    pref = ("claude", "codex", "ollama")
    assert fs.pick_engine(pref, {"claude": True, "codex": True}, {}) == "claude"
    assert fs.pick_engine(pref, {"claude": True, "codex": True}, {"claude": NOW + 5}) == "codex"
    assert fs.pick_engine(pref, {"claude": True, "ollama": True}, {"claude": 1}) == "ollama"
    assert fs.pick_engine(pref, {}, {}) is None
    assert fs.pick_engine(pref, {"claude": True}, {"claude": 1}) is None


# ---------------------------------------------------------------- pure: cooldown parsing
@pytest.mark.parametrize("text,hit", [
    ("Claude usage limit reached. Your limit will reset at 5pm", True),
    ("You've hit your 5-hour limit", True), ("429 rate limit exceeded", True), ("You exceeded your current quota", True),
    ("please try again in 20 minutes", True), ("Limit reached|1700003600", True),
    ("all tests passed", False), ("", False)])
def test_detect_limit(text, hit):
    assert fs.detect_limit(text) is hit


def test_parse_reset_relative_absolute_and_epoch():
    assert fs.parse_reset("Try again in 2 hours 15 minutes", NOW) == NOW + 8100
    assert fs.parse_reset("try again in 45 mins.", NOW) == NOW + 2700
    assert fs.parse_reset("Claude AI usage limit reached|1700003600", NOW) == 1700003600
    # wall clock in the given tz: 22:13 UTC now; 11pm today, 5am tomorrow
    assert fs.parse_reset("try again at 11 PM", NOW, UTC) == NOW + (46 * 60 + 40)
    assert fs.parse_reset("limit resets 5am", NOW, UTC) == NOW + (6 * 3600 + 46 * 60 + 40)
    assert fs.parse_reset("try again at 23:30", NOW, UTC) == NOW + (76 * 60 + 40)
    assert fs.parse_reset("rate limit", NOW) is None
    tz = timezone(timedelta(hours=2))
    assert fs.parse_reset("try again at 1:00 AM", NOW, tz) == NOW + (46 * 60 + 40)    # 00:13 local -> 01:00


def test_cooldown_until_default_clamp_and_non_limit():
    assert fs.cooldown_until("hello", NOW) is None
    assert fs.cooldown_until("rate limit hit", NOW) == NOW + 1800                       # no reset time: +30 min
    assert fs.cooldown_until("try again in 400 hours", NOW) == NOW + 12 * 3600          # clamped
    assert fs.cooldown_until("limit reached|1699990000", NOW) == NOW + 60               # past epoch: floor


def test_should_requeue_rules():
    assert fs.should_requeue(10, 5, 0)
    assert not fs.should_requeue(2000, 5, 0)         # >= 1 KB output
    assert not fs.should_requeue(10, 61, 0)          # ran too long
    assert not fs.should_requeue(10, 5, 1)           # only once
    assert not fs.should_requeue(10, 5, 0, cancel_requested=True)


def test_agent_version_gate():
    assert fs.agent_is_new("2.2.0") and fs.agent_is_new("2.10.1") and fs.agent_is_new("3.0.0")
    assert not fs.agent_is_new("2.1.4") and not fs.agent_is_new(None) and not fs.agent_is_new("junk")


# ---------------------------------------------------------------- integration: placement
def test_auto_prefers_less_loaded_node_and_penalises_low_disk(hub):
    c, _, _ = hub
    c.post("/api/agent/report", headers=AGENT, json={"node_id": "full", "metrics": {"cpu": 5, "work_root_free_gb": 5},
                                                     "capacity": 2, "info": {"tools": ["claude"]}})
    report(c, "ok", cpu=30)
    assert job(c)["node_id"] == "ok"                          # -50 for < 20 GB free beats a 25 point cpu advantage


def test_ollama_auto_prefers_gpu_node(hub):
    c, _, _ = hub
    report(c, "cpuonly", cpu=5, tools=("ollama",))
    report(c, "gpubox", cpu=40, tools=("ollama",), gpu=[{"name": "x", "mem_total_mb": 24000, "util": 1}])
    assert job(c, engine="ollama", prompt="hi")["node_id"] == "gpubox"


def test_ollama_never_placed_on_pre_2_2_agent(hub):
    c, _, _ = hub
    report(c, "old", tools=("ollama",), version="2.1.4")
    j = job(c, engine="ollama")
    assert j["node_id"] == "auto" and "capable" in get_job(c, j["id"])["wait_reason"]
    # fixed node: stays queued, never dispatched to the old agent
    j2 = job(c, "old", "ollama")
    r = report(c, "old", tools=("ollama",), version="2.1.4").json()
    assert r["jobs"] == [] and "older than 2.2.0" in get_job(c, j2["id"])["wait_reason"]


def test_ollama_job_dispatch_carries_model_and_default(hub):
    c, _, _ = hub
    report(c, "new", tools=("ollama",))
    a = job(c, "new", "ollama", model="llama3:8b")["id"]
    b = job(c, "new", "ollama")["id"]
    jobs = {x["id"]: x for x in report(c, "new", tools=("ollama",)).json()["jobs"]}
    assert jobs[a]["model"] == "llama3:8b" and jobs[b]["model"] == "qwen2.5:14b" and jobs[a]["engine"] == "ollama"
    assert job(c, "new", "ollama", model="bad model; rm")["_status"] == 400


def test_old_agent_without_version_still_gets_plain_jobs(hub):
    c, _, _ = hub
    report(c, "legacy", version=None)
    j = job(c, "legacy", "claude")["id"]
    assert [x["id"] for x in report(c, "legacy", version=None).json()["jobs"]] == [j]


def test_report_agent_version_updates_stored_info(hub):
    c, app, _ = hub
    report(c, "n", version="2.1.4")
    c.post("/api/agent/report", headers=AGENT, json={"node_id": "n", "metrics": {}, "capacity": 1, "agent_version": "2.2.0"})
    info = c.get("/api/nodes/n", headers=UI).json()["info"]
    assert info["agent_version"] == "2.2.0"


# ---------------------------------------------------------------- integration: cooldowns / any
def hit_limit(c, nid, engine="claude", text="Claude usage limit reached. Try again in 2 hours.",
              tools=("claude", "codex", "ollama")):
    report(c, nid, tools=tools, cap=10)
    jid = job(c, nid, engine)["id"]
    report(c, nid, tools=tools, cap=10)
    c.post(f"/api/agent/jobs/{jid}/update", json={"status": "failed", "exit_code": 1, "append": text}, headers=AGENT)
    return jid


def test_limit_on_completion_sets_cooldown_and_holds_explicit_engine_jobs(hub):
    c, app, clock = hub
    hit_limit(c, "w")
    sch = c.get("/api/scheduler", headers=UI).json()
    assert sch["cooldowns"]["claude"]["until"] == clock() + 7200
    queued = job(c, "w", "claude")
    assert get_job(c, queued["id"])["wait_reason"].startswith("claude cooling down until ")
    assert report(c, "w").json()["jobs"] == []                       # held back
    assert any("usage limit" in e["msg"] and e["level"] == "warn" for e in c.get("/api/state", headers=UI).json()["events"])
    # other engines are unaffected, and the hold lifts when the cooldown expires
    other = job(c, "w", "codex")["id"]
    assert [x["id"] for x in report(c, "w").json()["jobs"]] == [other]
    clock.advance(7300)
    assert [x["id"] for x in report(c, "w").json()["jobs"]] == [queued["id"]]
    assert get_job(c, queued["id"])["wait_reason"] is None


def test_manual_clear_of_cooldown(hub):
    c, _, _ = hub
    hit_limit(c, "w")
    assert c.delete("/api/scheduler/cooldowns/claude", headers=UI).json()["cleared"] is True
    assert c.get("/api/scheduler", headers=UI).json()["cooldowns"] == {}
    assert c.delete("/api/scheduler/cooldowns/claude", headers=UI).json()["cleared"] is False


def test_only_a_nonzero_exit_with_a_cli_limit_message_in_the_tail_sets_a_cooldown(hub):
    c, app, _ = hub
    report(c, "w", cap=10)
    j1 = job(c, "w", "codex")["id"]
    report(c, "w", cap=10)
    # streamed output is never checked, even a real-looking limit line
    c.post(f"/api/agent/jobs/{j1}/update", json={"append": "thinking...\nrate limit reached, try again in 10 minutes\n"}, headers=AGENT)
    assert c.get("/api/scheduler", headers=UI).json()["cooldowns"] == {}
    c.post(f"/api/agent/jobs/{j1}/update", json={"status": "failed", "exit_code": 1}, headers=AGENT)
    cd = c.get("/api/scheduler", headers=UI).json()["cooldowns"]
    assert "codex" in cd                                                     # ...but it is in the tail of a failed job
    c.delete("/api/scheduler/cooldowns/codex", headers=UI)
    # a successful job that talks about rate limits / quotas: no cooldown
    j0 = job(c, "w", "claude")["id"]
    report(c, "w", cap=10)
    c.post(f"/api/agent/jobs/{j0}/update", headers=AGENT, json={
        "append": "Added rate limit middleware\nquota tests pass\nusage limit reached handling added\n", "status": "done", "exit_code": 0})
    # a failed job whose limit line scrolled out of the last 20 lines: no cooldown
    j4 = job(c, "w", "claude")["id"]
    report(c, "w", cap=10)
    c.post(f"/api/agent/jobs/{j4}/update", headers=AGENT, json={
        "append": "Claude usage limit reached.\n" + "\n".join(f"line {i}" for i in range(25)), "status": "failed", "exit_code": 2})
    # a failed job that merely mentions rate limits (not a CLI limit message): no cooldown
    j5 = job(c, "w", "claude")["id"]
    report(c, "w", cap=10)
    c.post(f"/api/agent/jobs/{j5}/update", headers=AGENT, json={
        "append": "FAIL test_rate_limit_middleware\nAssertionError: quota\n", "status": "failed", "exit_code": 1})
    # brief jobs never set cooldowns
    b = app.state.p2.insert_job(node_id="w", engine="claude", prompt="brief", source="brief", project_id="*")
    assert b in [x["id"] for x in report(c, "w", cap=10).json()["jobs"]]
    c.post(f"/api/agent/jobs/{b}/update", headers=AGENT, json={
        "append": "Claude usage limit reached. Try again in 2 hours.", "status": "failed", "exit_code": 1})
    assert c.get("/api/scheduler", headers=UI).json()["cooldowns"] == {}
    j2 = job(c, "w", "claude")["id"]
    report(c, "w")
    c.post(f"/api/agent/jobs/{j2}/update", json={"status": "failed", "exit_code": 1, "append": "AssertionError: 3 != 4"}, headers=AGENT)
    j3 = job(c, "w", "shell")["id"]
    report(c, "w")
    c.post(f"/api/agent/jobs/{j3}/update", json={"status": "failed", "exit_code": 1, "append": "quota exceeded"}, headers=AGENT)
    assert c.get("/api/scheduler", headers=UI).json()["cooldowns"] == {}    # shell jobs never set cooldowns


def test_any_engine_prefers_claude_then_falls_back_to_codex_then_ollama(hub):
    c, _, _ = hub
    report(c, "w", tools=("claude", "codex", "ollama"), cap=10)
    j = job(c, "auto", "any")
    assert j["engine"] == "claude" and j["node_id"] == "w"
    assert get_job(c, j["id"])["requested"] == "auto"
    hit_limit(c, "w")
    j2 = job(c, "auto", "any")
    assert j2["engine"] == "codex"
    hit_limit(c, "w", "codex", "You hit your usage limit. try again at 11pm")
    j3 = job(c, "auto", "any", kind="text")                  # only text jobs may fall through to ollama
    assert j3["engine"] == "ollama"
    j4 = job(c, "auto", "any")                               # a code job never does: it waits
    assert j4["engine"] == "any" and "cooling down" in get_job(c, j4["id"])["wait_reason"]
    assert get_job(c, j4["id"])["kind"] == "code" and get_job(c, j3["id"])["kind"] == "text"
    assert job(c, "auto", "any", kind="poem")["_status"] == 400
    # engine/node resolved before dispatch; engine 'any' is never sent to an agent
    sent = report(c, "w", tools=("claude", "codex", "ollama")).json()["jobs"]
    assert {x["engine"] for x in sent} <= {"codex", "ollama", "claude"} and all(x["engine"] != "any" for x in sent)


def test_any_waits_when_nothing_available(hub):
    c, _, _ = hub
    report(c, "w", tools=("claude",))
    hit_limit(c, "w", tools=("claude",))
    j = job(c, "auto", "any")
    assert j["engine"] == "any" and "cooling down" in get_job(c, j["id"])["wait_reason"]
    c.delete("/api/scheduler/cooldowns/claude", headers=UI)
    report(c, "w", tools=("claude",), cap=10)
    assert get_job(c, j["id"])["engine"] == "claude"


def test_scheduler_view_shape(hub):
    c, _, _ = hub
    report(c, "w", tools=("claude", "ollama"), cpu=20)
    job(c, "w", "claude")
    s = c.get("/api/scheduler", headers=UI).json()
    assert s["queue_depth"] >= 0 and s["cooldowns"] == {}
    assert s["placement_preview"]["claude"][0]["node_id"] == "w" and s["placement_preview"]["any"]["engine"] == "claude"


# ---------------------------------------------------------------- integration: requeue on node loss
def _lose(c, app, clock, run_for, output=""):
    report(c, "w", cap=1)
    jid = job(c, "auto", "shell")["id"]            # requested: auto
    assert [x["id"] for x in report(c, "w", cap=1).json()["jobs"]] == [jid]
    c.post(f"/api/agent/jobs/{jid}/update", json={"status": "running", "append": output}, headers=AGENT)
    clock.advance(run_for)
    report(c, "w", cap=0)
    clock.advance(95)
    app.state.check_presence()
    return jid


def test_quick_lost_job_is_requeued_once_to_auto(hub):
    c, app, clock = hub
    jid = _lose(c, app, clock, 10)
    j = get_job(c, jid)
    assert j["status"] == "queued" and j["node_id"] == "auto" and j["attempts"] == 1 and j["started"] is None
    assert [x["id"] for x in report(c, "other", cap=1, tools=()).json()["jobs"]] == [jid]      # re-placed on a live node
    clock.advance(95)
    report(c, "x", cap=0)
    app.state.check_presence()
    assert get_job(c, jid)["status"] == "failed"                                              # a second loss is final
    assert get_job(c, jid)["attempts"] == 1


def test_lost_job_not_requeued_when_long_running_or_chatty(hub):
    c, app, clock = hub
    assert get_job(c, _lose(c, app, clock, 70))["status"] == "failed"


def test_lost_job_with_output_is_failed(hub):
    c, app, clock = hub
    assert get_job(c, _lose(c, app, clock, 5, "x" * 2000))["status"] == "failed"


def test_requeue_keeps_explicit_node(hub):
    c, app, clock = hub
    report(c, "w", cap=1)
    jid = job(c, "w", "shell")["id"]
    report(c, "w", cap=1)
    clock.advance(100)
    app.state.check_presence()
    j = get_job(c, jid)
    assert j["status"] == "queued" and j["node_id"] == "w"
