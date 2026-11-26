"""Phase 2 sessions / projects / transcripts / briefs."""
import pytest

import forge_projects as fp
from p2util import AGENT, UI, get_job, job, make_hub, report


@pytest.fixture
def hub(tmp_path):
    # the brief loop's night window / morning digest have their own tests (test_brief_schedule.py)
    return make_hub(tmp_path, {"brief_window": "always", "brief_digest_time": "off"})


# ---------------------------------------------------------------- pure
@pytest.mark.parametrize("raw,want", [
    ("https://github.com/Owner/Repo.git", "github.com/owner/repo"),
    ("git@github.com:Owner/Repo", "github.com/owner/repo"),
    ("git@github.com:Owner/Repo.git", "github.com/owner/repo"),
    ("https://user:ghp_secret@github.com/Owner/Repo/", "github.com/owner/repo"),
    ("ssh://git@GitHub.com:22/Owner/Repo.git", "github.com/owner/repo"),
    ("http://host.local:3000/a/b.git", "host.local/a/b"),
    ("https://dev.azure.com/org/proj/_git/repo", "dev.azure.com/org/proj/_git/repo"),
    ("github.com/owner/repo", None),            # not a URL form git would print
    ("C:\\work\\repo", None), ("", None), (None, None), ("https://github.com/", None)])
def test_normalize_remote_table(raw, want):
    assert fp.normalize_remote(raw) == want


@pytest.mark.parametrize("cwd,misc", [
    ("C:\\Users\\you", True), ("c:/users/you/", True), ("C:\\WINDOWS\\system32", True),
    ("C:\\Users\\you\\AppData\\Local\\Temp\\x", True), ("C:\\ProgramData\\Forge\\scratch\\job1", True),
    ("C:\\ProgramData\\forge\\scratchy", False), ("", True), (None, True),
    ("C:\\Users\\you\\dev\\repo", False), ("D:\\work\\temperature", False)])
def test_misc_cwd(cwd, misc):
    assert fp.is_misc_cwd(cwd) is misc


def test_project_key_and_name():
    assert fp.project_key("n1", "C:\\x", "git@github.com:A/B.git") == "github.com/a/b"
    assert fp.project_key("n1", "C:\\Users\\me", None) == "misc:n1"
    assert fp.project_key("n1", "C:\\Dev\\Thing\\", None) == "local:n1:c:\\dev\\thing"
    assert fp.project_name("github.com/a/b") == "b" and fp.project_name("misc:n1") == "Scratch / misc"
    assert fp.project_name("local:n1:c:\\dev\\thing", "C:\\Dev\\Thing") == "Thing"


def test_redaction_and_session_state():
    t = "use sk-abcdefghijkl and ghp_ABCDEFGHIJKLMNOP plus AKIAABCDEFGHIJKLMNOP eyJhbGciOiJIUzI1NiJ9 xoxb-1234567890-abc"
    r = fp.redact(t)
    assert "sk-" not in r and "ghp_" not in r and "AKIA" not in r and "eyJ" not in r and "xoxb" not in r and r.count("[redacted]") == 5
    assert fp.redact("sk-short") == "sk-short"
    assert len(fp.clean_title("x" * 500)) == 140
    now = 1000000.0
    assert fp.session_state(now - 30, now) == "live" and fp.session_state(now - 3600, now) == "idle"
    assert fp.session_state(now - 3 * 86400, now) == "done" and fp.session_state(now - 3 * 86400, now, True) == "live"
    assert fp.effective_state("live", now - 3600, now, node_online=False) == "idle"
    assert fp.effective_state("live", now - 3600, now, node_online=True) == "live"


def test_brief_prompt_is_metadata_only_and_deterministic():
    now = 1_700_000_000.0
    ctx = {"name": "forge", "now": now,
           "sessions": [{"tool": "claude", "title": "fix the bug with sk-abcdefghijklmn", "state": "live",
                         "last_activity": now - 60, "messages": 12, "transcript": "SECRET TRANSCRIPT BODY"}],
           "commits": [{"sha": "abcdef1234567", "author": "Ann", "ts": now - 7200, "subject": "feat: x", "diff": "SECRET DIFF"}],
           "repos": [{"node_id": "forge-01", "branch": "main", "dirty": True, "ahead": 2, "behind": 0}],
           "jobs": [{"title": "run tests", "status": "done", "ts": now - 90000}]}
    p = fp.build_brief_prompt(ctx)
    assert p == fp.build_brief_prompt(ctx)
    for needle in ("Project: forge", "[claude]", "live", "12 messages", "abcdef1 feat: x", "dirty, ahead 2",
                   "run tests -> done", "3-6 short bullets", "120 words", "25h ago"):
        assert needle in p
    assert "SECRET" not in p and "sk-abc" not in p
    assert "no recent activity" in fp.build_brief_prompt({"name": "empty", "now": now})
    many = fp.build_brief_prompt({"name": "m", "now": now, "sessions": [{"tool": "t", "title": str(i)} for i in range(50)]})
    assert many.count("[t]") == 12


# ---------------------------------------------------------------- ingest / projects API
def sess(sid="s1", tool="claude", cwd="C:\\dev\\forge", remote="https://github.com/Me/Forge.git", **kw):
    return {"tool": tool, "session_id": sid, "cwd": cwd, "git_remote": remote, "git_branch": "main", "title": "do thing",
            "started": 1_699_990_000, "last_activity": 1_700_000_000 - 10, "messages": 4, "state": "live", "pid": None} | kw


def post_sessions(c, node, sessions, removed=()):
    return c.post("/api/agent/sessions", json={"node_id": node, "sessions": sessions, "removed": list(removed)}, headers=AGENT)


def projects(c):
    return c.get("/api/projects", headers=UI).json()


def test_sessions_create_project_normalized_and_shared_across_machines(hub):
    c, _, clock = hub
    clock.t = 1_700_000_000.0
    report(c, "m1"); report(c, "m2")
    post_sessions(c, "m1", [sess("a")])
    post_sessions(c, "m2", [sess("b", cwd="D:\\other\\forge", remote="git@github.com:me/forge")])
    ps = projects(c)
    assert len(ps) == 1 and ps[0]["key"] == "github.com/me/forge" and ps[0]["name"] == "forge"
    assert ps[0]["machines"] == ["m1", "m2"] and ps[0]["live_sessions"] == 2 and ps[0]["sessions_24h"] == 2
    assert ps[0]["branches"] == ["main"]
    d = c.get(f"/api/projects/{ps[0]['id']}", headers=UI).json()
    assert {s["session_id"] for s in d["sessions"]} == {"a", "b"} and d["briefs"] == []


def test_misc_and_local_projects_and_old_sessions_ignored(hub):
    c, _, clock = hub
    clock.t = 1_700_000_000.0
    report(c, "m1")
    post_sessions(c, "m1", [sess("a", cwd="C:\\Users\\me", remote=None), sess("b", cwd="C:\\dev\\notgit", remote=None),
                            sess("old", last_activity=1_700_000_000 - 15 * 86400)])
    keys = {p["key"]: p["name"] for p in projects(c)}
    assert keys == {"misc:m1": "Scratch / misc", "local:m1:c:\\dev\\notgit": "notgit"}


def test_upsert_removed_and_title_redaction(hub):
    c, app, clock = hub
    clock.t = 1_700_000_000.0
    report(c, "m1")
    r = post_sessions(c, "m1", [sess("a", title="key sk-abcdefghijklmnop here")]).json()
    assert r["changed"] == 1
    assert post_sessions(c, "m1", [sess("a", title="key sk-abcdefghijklmnop here")]).json()["changed"] == 0   # unchanged: no write
    pid = projects(c)[0]["id"]
    s = c.get(f"/api/projects/{pid}", headers=UI).json()["sessions"][0]
    assert s["title"] == "key [redacted] here"
    post_sessions(c, "m1", [sess("a", messages=9)])
    assert c.get(f"/api/projects/{pid}", headers=UI).json()["sessions"][0]["messages"] == 9
    post_sessions(c, "m1", [], removed=["a"])
    assert c.get(f"/api/projects/{pid}", headers=UI).json()["sessions"] == []


def test_live_state_decays_when_node_goes_offline(hub):
    c, _, clock = hub
    clock.t = 1_700_000_000.0
    report(c, "m1")
    post_sessions(c, "m1", [sess("a", last_activity=clock.t - 5)])
    assert projects(c)[0]["live_sessions"] == 1
    clock.advance(3600)
    assert projects(c)[0]["live_sessions"] == 0


def test_commits_dedup_by_sha_and_repos(hub):
    c, _, clock = hub
    clock.t = 1_700_000_000.0
    report(c, "m1"); report(c, "m2")
    cm = [{"sha": "a" * 40, "author": "Ann", "ts": 1_699_999_000, "subject": "one"},
          {"sha": "b" * 40, "author": "Ann", "ts": 1_699_998_000, "subject": "two sk-abcdefghijklmn"}]
    for node, dirty in (("m1", True), ("m2", False)):
        c.post("/api/agent/commits", headers=AGENT, json={"node_id": node, "repos": [
            {"cwd": f"C:\\{node}\\forge", "git_remote": "git@github.com:Me/Forge.git", "branch": "main", "dirty": dirty,
             "ahead": 1, "behind": 2, "commits": cm}]})
    ps = projects(c)
    assert len(ps) == 1 and ps[0]["dirty_any"] is True and ps[0]["machines"] == ["m1", "m2"]
    d = c.get(f"/api/projects/{ps[0]['id']}", headers=UI).json()
    assert [x["sha"][0] for x in d["commits"]] == ["a", "b"] and d["commits"][1]["subject"] == "two [redacted]"
    assert sorted((r["node_id"], r["dirty"], r["ahead"], r["behind"]) for r in d["repos"]) == [("m1", True, 1, 2), ("m2", False, 1, 2)]


def test_patch_pin_sort_and_validation(hub):
    c, _, clock = hub
    clock.t = 1_700_000_000.0
    report(c, "m1")
    post_sessions(c, "m1", [sess("a", cwd="C:\\a", remote="https://github.com/o/a"),
                            sess("b", cwd="C:\\b", remote="https://github.com/o/b", last_activity=clock.t - 1000)])
    assert [p["name"] for p in projects(c)] == ["a", "b"]
    b = projects(c)[1]["id"]
    r = c.patch(f"/api/projects/{b}", json={"pinned": True, "group_name": "Work", "name": "Bee"}, headers=UI).json()
    assert r["pinned"] and r["group_name"] == "Work" and r["name"] == "Bee"
    assert [p["name"] for p in projects(c)] == ["Bee", "a"]
    assert c.patch(f"/api/projects/{b}", json={"name": " "}, headers=UI).status_code == 400
    assert c.patch("/api/projects/nope", json={}, headers=UI).status_code == 404
    assert c.get("/api/projects/nope", headers=UI).status_code == 404


def test_merge_keeps_alias_for_future_sessions(hub):
    c, _, clock = hub
    clock.t = 1_700_000_000.0
    report(c, "m1")
    post_sessions(c, "m1", [sess("a", cwd="C:\\a", remote="https://github.com/o/a"), sess("b", cwd="C:\\b", remote="https://github.com/o/b-fork")])
    a, b = [p["id"] for p in sorted(projects(c), key=lambda p: p["name"])]
    m = c.post("/api/projects/merge", json={"ids": [a, b]}, headers=UI).json()
    assert m["id"] == a and len(m["sessions"]) == 2
    assert [p["id"] for p in projects(c)] == [a]
    post_sessions(c, "m1", [sess("c", cwd="C:\\b2", remote="https://github.com/o/b-fork")])     # maps through the alias
    assert len(projects(c)) == 1 and len(c.get(f"/api/projects/{a}", headers=UI).json()["sessions"]) == 3
    assert c.post("/api/projects/merge", json={"ids": [a]}, headers=UI).status_code == 400
    assert c.post("/api/projects/merge", json={"ids": [a, "zzz"]}, headers=UI).status_code == 404


def test_forge_jobs_appear_as_sessions_of_their_project(hub):
    c, _, clock = hub
    clock.t = 1_700_000_000.0
    report(c, "m1")
    c.post("/api/agent/commits", headers=AGENT, json={"node_id": "m1", "repos": [
        {"cwd": "C:\\dev\\forge", "git_remote": "https://github.com/me/forge.git", "branch": "main", "commits": []}]})
    jid = job(c, "m1", "claude", cwd="C:\\dev\\forge", prompt="refactor please")["id"]
    d = c.get(f"/api/projects/{projects(c)[0]['id']}", headers=UI).json()
    f = [s for s in d["sessions"] if s["tool"] == "forge"]
    assert len(projects(c)) == 1 and f and f[0]["session_id"] == jid and f[0]["title"] == "refactor please" and f[0]["state"] == "live"


# ---------------------------------------------------------------- continue / 409
def test_resume_conflict_when_live_in_terminal_unless_forced(hub):
    c, _, clock = hub
    clock.t = 1_700_000_000.0
    report(c, "m1")
    post_sessions(c, "m1", [sess("live1", pid=4242), sess("idle1", state="idle", last_activity=clock.t - 4000),
                            sess("livenopid", last_activity=clock.t - 5)])
    body = {"node_id": "m1", "prompt": "carry on", "resume": {"tool": "claude", "session_id": "live1"}}
    r = c.post("/api/jobs", json=body, headers=UI)
    assert r.status_code == 409 and "live in a terminal" in r.json()["detail"]
    ok = c.post("/api/jobs", json=body | {"force": True}, headers=UI)
    assert ok.status_code == 200
    j = get_job(c, ok.json()["id"])
    assert j["engine"] == "claude" and j["cwd"] == "C:\\dev\\forge" and j["resume"] == {"tool": "claude", "session_id": "live1"}
    # idle, or live without a known pid: allowed; wrong node / unknown session rejected
    assert c.post("/api/jobs", json=body | {"resume": {"tool": "claude", "session_id": "idle1"}}, headers=UI).status_code == 200
    assert c.post("/api/jobs", json=body | {"resume": {"tool": "claude", "session_id": "livenopid"}}, headers=UI).status_code == 200
    assert c.post("/api/jobs", json=body | {"node_id": "m2"}, headers=UI).status_code == 400
    assert c.post("/api/jobs", json=body | {"resume": {"tool": "claude", "session_id": "nope"}}, headers=UI).status_code == 404
    assert c.post("/api/jobs", json=body | {"resume": {"tool": "t3", "session_id": "x"}}, headers=UI).status_code == 400
    assert c.post("/api/jobs", json=body | {"engine": "codex"}, headers=UI).status_code in (400, 409)


def test_resume_dispatched_with_resume_field_and_only_to_new_agents(hub):
    c, _, clock = hub
    clock.t = 1_700_000_000.0
    report(c, "m1", version="2.1.4")
    post_sessions(c, "m1", [sess("i1", state="idle", last_activity=clock.t - 4000, tool="codex")])
    jid = c.post("/api/jobs", json={"node_id": "m1", "prompt": "go", "resume": {"tool": "codex", "session_id": "i1"}}, headers=UI).json()["id"]
    assert report(c, "m1", version="2.1.4").json()["jobs"] == []
    sent = report(c, "m1", version="2.2.0").json()["jobs"]
    assert [x["id"] for x in sent] == [jid] and sent[0]["resume"] == {"tool": "codex", "session_id": "i1"} and sent[0]["engine"] == "codex"


# ---------------------------------------------------------------- transcripts
def ev(i, text="hello", **kw):
    return {"ts": 1_700_000_000 + i, "role": "assistant", "kind": "text", "text": text, "collapsed": False} | kw


def test_watch_report_hint_post_events_and_serve_since(hub):
    c, _, clock = hub
    clock.t = 1_700_000_000.0
    report(c, "m1")
    post_sessions(c, "m1", [sess("a")])
    assert report(c, "m1").json()["watch"] == []
    assert c.post("/api/sessions/claude/a/watch", headers=UI).json()["node_id"] == "m1"
    assert c.post("/api/sessions/claude/zzz/watch", headers=UI).status_code == 404
    assert report(c, "m1").json()["watch"] == [{"tool": "claude", "session_id": "a", "since": None}]
    assert report(c, "m2").json()["watch"] == []                                       # other nodes are not asked
    r = c.post("/api/agent/transcript", headers=AGENT, json={"node_id": "m1", "tool": "claude", "session_id": "a", "cursor": "off:123",
                                                           "events": [ev(1, "one"), ev(2, "two sk-abcdefghijklmn", tool_name="Bash")], "done": False})
    assert r.json()["accepted"] == 2
    assert report(c, "m1").json()["watch"][0]["since"] == "off:123"                    # agent continues from its own cursor
    t = c.get("/api/sessions/claude/a/transcript", headers=UI).json()
    assert [e["text"] for e in t["events"]] == ["one", "two [redacted]"] and t["cursor"] == 2 and t["events"][1]["tool_name"] == "Bash"
    assert [e["seq"] for e in c.get("/api/sessions/claude/a/transcript?since=1", headers=UI).json()["events"]] == [2]


def test_watch_expires_after_30s_and_refreshes(hub):
    c, _, clock = hub
    clock.t = 1_700_000_000.0
    report(c, "m1")
    post_sessions(c, "m1", [sess("a")])
    c.post("/api/sessions/claude/a/watch", headers=UI)
    clock.advance(25)
    c.post("/api/sessions/claude/a/watch", headers=UI)       # UI refresh
    clock.advance(25)
    assert len(report(c, "m1").json()["watch"]) == 1
    clock.advance(10)
    assert report(c, "m1").json()["watch"] == []


def test_transcript_capped_at_1000_events_and_4000_chars(hub):
    c, _, clock = hub
    clock.t = 1_700_000_000.0
    report(c, "m1")
    post_sessions(c, "m1", [sess("a")])
    for batch in range(6):
        c.post("/api/agent/transcript", headers=AGENT, json={"node_id": "m1", "tool": "claude", "session_id": "a", "cursor": batch,
                                                             "events": [ev(i, "x" * 5000 if i == 0 else "y") for i in range(200)], "done": batch == 5})
    t = c.get("/api/sessions/claude/a/transcript", headers=UI).json()
    assert len(t["events"]) == 1000 and t["cursor"] == 1200 and t["events"][0]["seq"] == 201 and t["done"] is True
    first = c.post("/api/agent/transcript", headers=AGENT, json={"node_id": "m1", "tool": "claude", "session_id": "a", "events": [ev(0, "z" * 9000)]})
    assert first.json()["accepted"] == 1
    assert len(c.get("/api/sessions/claude/a/transcript?since=1200", headers=UI).json()["events"][0]["text"]) == 4000


def test_transcript_garbage_events_tolerated_and_unknown_session_404(hub):
    c, _, clock = hub
    clock.t = 1_700_000_000.0
    report(c, "m1")
    post_sessions(c, "m1", [sess("a")])
    r = c.post("/api/agent/transcript", headers=AGENT, json={"tool": "claude", "session_id": "a", "events": ["junk", 5, {"role": "alien", "text": "t"}]})
    assert r.json()["accepted"] == 1
    assert c.get("/api/sessions/claude/a/transcript", headers=UI).json()["events"][0]["role"] == "system"
    assert c.get("/api/sessions/claude/none/transcript", headers=UI).status_code == 404
    assert c.post("/api/agent/transcript", headers=AGENT, json={"tool": "bogus", "session_id": "a"}).status_code == 400


def test_forge_job_transcript_is_served_from_job_output(hub):
    c, _, _ = hub
    report(c, "m1")
    jid = job(c, "m1", "claude", prompt="do it")["id"]
    report(c, "m1")
    c.post(f"/api/agent/jobs/{jid}/update", json={"append": "output text", "status": "done"}, headers=AGENT)
    t = c.get(f"/api/sessions/forge/{jid}/transcript", headers=UI).json()
    assert [e["role"] for e in t["events"]] == ["user", "assistant"] and t["done"] is True and t["events"][1]["text"] == "output text"


# ---------------------------------------------------------------- briefs
def ollama_node(c, nid, gpu_mb=0, version="2.2.0", **extra):
    return report(c, nid, tools=("claude", "ollama"), version=version,
                  gpu=[{"name": "g", "mem_total_mb": gpu_mb, "util": 0}] if gpu_mb else None, **extra)


def test_brief_loop_queues_ollama_job_on_biggest_gpu_node_and_stores_result(hub):
    c, app, clock = hub
    clock.t = 1_700_000_000.0
    ollama_node(c, "small", 8000); ollama_node(c, "big", 24000); report(c, "plain")
    post_sessions(c, "plain", [sess("a")])
    assert app.state.p2.pick_brief_node(clock()) == "big"
    assert app.state.p2.brief_tick(clock()) == 1
    briefs = [j for j in c.get("/api/jobs?include_brief=1", headers=UI).json() if j["source"] == "brief"]
    assert len(briefs) == 1 and briefs[0]["engine"] == "ollama" and briefs[0]["node_id"] == "big"
    assert c.get("/api/jobs", headers=UI).json() == [] and all(j["source"] != "brief" for j in c.get("/api/state", headers=UI).json()["jobs"])
    sent = ollama_node(c, "big", 24000).json()["jobs"]
    assert sent[0]["model"] == "qwen2.5:14b" and sent[0]["source"] == "brief" and "Project: forge" in sent[0]["prompt"]
    c.post(f"/api/agent/jobs/{sent[0]['id']}/update", headers=AGENT,
           json={"append": "pulling manifest\nsuccess\n- all good\n- next: ship\n", "status": "done", "exit_code": 0})
    p = projects(c)[0]
    assert p["brief"]["text"] == "- all good\n- next: ship" and p["brief"]["model"] == "qwen2.5:14b"
    assert [b["text"] for b in c.get(f"/api/projects/{p['id']}", headers=UI).json()["briefs"]] == ["- all good\n- next: ship"]
    # nothing new -> nothing queued; new activity after the interval -> queued again
    clock.advance(1900)
    assert app.state.p2.brief_tick(clock()) == 0
    post_sessions(c, "plain", [sess("a", messages=50, last_activity=clock.t - 1)])
    clock.advance(1900)
    assert app.state.p2.brief_tick(clock()) == 1


def test_brief_loop_respects_interval_pending_and_cap_and_failed_attempts(hub):
    c, app, clock = hub
    clock.t = 1_700_000_000.0
    ollama_node(c, "g", 12000)
    for i in range(9):
        post_sessions(c, "g", [sess(f"s{i}", cwd=f"C:\\p{i}", remote=f"https://github.com/o/p{i}", last_activity=clock.t - i)])
    p2 = app.state.p2
    assert p2.brief_tick(clock()) == 6                       # max 6 per loop
    assert p2.brief_tick(clock()) == 0                       # not before 30 min
    clock.advance(1900)
    assert p2.brief_tick(clock()) == 3                       # the 6 pending are skipped, remaining 3 enqueued
    clock.advance(1900)
    assert p2.brief_tick(clock()) == 0                       # everything pending


def test_failed_brief_is_not_retried_until_new_activity(hub):
    c, app, clock = hub
    clock.t = 1_700_000_000.0
    ollama_node(c, "g", 12000)
    post_sessions(c, "g", [sess("a")])
    assert app.state.p2.brief_tick(clock()) == 1
    jid = ollama_node(c, "g", 12000).json()["jobs"][0]["id"]
    c.post(f"/api/agent/jobs/{jid}/update", headers=AGENT, json={"status": "failed", "exit_code": 1})
    clock.advance(1900)
    assert app.state.p2.brief_tick(clock()) == 0
    assert projects(c)[0]["brief"] is None


def test_brief_loop_skips_without_capable_node_and_never_uses_old_agents(hub):
    c, app, clock = hub
    clock.t = 1_700_000_000.0
    report(c, "plain", tools=("claude",))
    ollama_node(c, "old", 24000, version="2.1.4")
    post_sessions(c, "plain", [sess("a")])
    assert app.state.p2.pick_brief_node(clock()) is None
    assert app.state.p2.brief_tick(clock()) == 0
    assert c.get("/api/jobs?include_brief=1", headers=UI).json() == []


def test_brief_node_config_and_briefs_capability(tmp_path):
    c, app, clock = make_hub(tmp_path, {"brief_node": "chosen", "brief_model": "llama3:8b"})
    clock.t = 1_700_000_000.0
    ollama_node(c, "chosen", 4000); ollama_node(c, "bigger", 48000)
    assert app.state.p2.pick_brief_node(clock()) == "chosen" and app.state.p2.brief_model() == "llama3:8b"
    # an agent that advertises capabilities without 'briefs' is not eligible
    c.post("/api/agent/report", headers=AGENT, json={"node_id": "nocap", "metrics": {}, "capacity": 1,
                                                     "info": {"tools": ["ollama"], "agent_version": "2.2.0", "capabilities": ["sessions"]}})
    assert "nocap" not in {app.state.p2.pick_brief_node(clock())}


def test_brief_me_uses_claude_haiku_on_auto_and_rollup(hub):
    c, app, clock = hub
    clock.t = 1_700_000_000.0
    report(c, "w", cap=5)
    post_sessions(c, "w", [sess("a")])
    pid = projects(c)[0]["id"]
    r = c.post(f"/api/projects/{pid}/brief", headers=UI).json()
    j = get_job(c, r["id"])
    assert j["engine"] == "claude" and j["model"] == "haiku" and j["source"] == "brief" and j["project_id"] == pid and j["node_id"] == "w"
    assert c.post(f"/api/projects/{pid}/brief", headers=UI).json() == {"id": r["id"], "existing": True}
    sent = report(c, "w", cap=5).json()["jobs"]
    assert sent[0]["model"] == "haiku"
    c.post(f"/api/agent/jobs/{r['id']}/update", headers=AGENT, json={"append": "Brief text", "status": "done"})
    assert projects(c)[0]["brief"]["text"] == "Brief text"
    ra = c.post("/api/brief/all", headers=UI).json()
    assert ra["projects"] == 1 and get_job(c, ra["id"])["project_id"] == "*"
    jid = report(c, "w", cap=5).json()["jobs"][0]["id"]
    c.post(f"/api/agent/jobs/{jid}/update", headers=AGENT, json={"append": "roll-up", "status": "done"})
    assert c.get("/api/brief/all", headers=UI).json()["brief"]["text"] == "roll-up"
    assert c.post("/api/projects/nope/brief", headers=UI).status_code == 404


def test_brief_loop_never_fires_inside_leader_grace(hub):
    c, app, clock = hub
    assert app.state.cluster.is_leader        # lone hub leads immediately; tick() waits LEADER_GRACE before p2.tick
    clock.t = 1_700_000_000.0
    ollama_node(c, "g", 12000)
    post_sessions(c, "g", [sess("a")])
    app.state.tick()
    assert c.get("/api/jobs?include_brief=1", headers=UI).json() == []
    clock.advance(40)
    ollama_node(c, "g", 12000)
    app.state.tick()
    assert len(c.get("/api/jobs?include_brief=1", headers=UI).json()) == 1
