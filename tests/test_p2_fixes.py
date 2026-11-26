"""2.2.1 hub fixes: stale-holder fencing, scheduling races, project linking, 'any' engine re-resolution, redaction,
UI shape fixes, clock skew, retention, monotonic stamps."""
import threading

import pytest
from fastapi.testclient import TestClient

import forge_projects as fp
from cluster_net import Fleet
from p2util import AGENT, UI, get_job, job, make_hub, report


@pytest.fixture
def hub(tmp_path):
    return make_hub(tmp_path, {"brief_window": "always", "brief_digest_time": "off"})


def upd(c, jid, **body):
    return c.post(f"/api/agent/jobs/{jid}/update", json=body, headers=AGENT)


# ---------------------------------------------------------------- H1: stale holder after a requeue
def test_sleeping_agent_cannot_write_to_a_job_requeued_to_another_node(hub):
    c, app, clock = hub
    report(c, "w", cap=1)
    report(c, "y", cap=0)
    jid = job(c, "auto", "shell")["id"]
    items = report(c, "w", cap=1).json()["jobs"]
    assert [x["id"] for x in items] == [jid] and items[0]["attempt"] == 0
    assert upd(c, jid, status="running", append="w started\n", node_id="w", attempt=0).status_code == 200
    # w's laptop sleeps > 90 s: the job is requeued (barely started) and y picks it up
    clock.advance(95)
    report(c, "y", cap=0)
    app.state.check_presence()
    assert get_job(c, jid)["status"] == "queued" and get_job(c, jid)["attempts"] == 1
    items = report(c, "y", cap=1).json()["jobs"]
    assert [x["id"] for x in items] == [jid] and items[0]["attempt"] == 1
    # w wakes up, its process finished the job: the hub refuses and tells w to kill its copy
    r = upd(c, jid, status="done", exit_code=0, append="w result\n", node_id="w", attempt=0)
    assert r.status_code == 409 and r.json()["detail"] == "job reassigned"
    j = get_job(c, jid)
    assert j["status"] == "starting" and j["node_id"] == "y" and "w result" not in j["output"]
    rw = report(c, "w", cap=1).json()
    assert jid in rw["cancel"] and rw["jobs"] == []
    # the old attempt number alone is enough to fence (agent that sends attempt but not node_id)
    assert upd(c, jid, append="late\n", attempt=0).status_code == 409
    # y, the real holder, completes it
    assert upd(c, jid, status="running", node_id="y", attempt=1).status_code == 200
    assert upd(c, jid, status="done", exit_code=0, append="y result\n", node_id="y", attempt=1).status_code == 200
    j = get_job(c, jid)
    assert j["status"] == "done" and "y result" in j["output"] and "w result" not in j["output"]
    assert upd(c, jid, status="done", node_id="y", attempt=1).status_code == 409    # terminal: no more writes


def test_job_requeued_to_the_same_node_is_not_handed_back_while_the_old_copy_may_run(hub):
    c, app, clock = hub
    report(c, "w", cap=1)
    jid = job(c, "w", "shell")["id"]                       # explicit node: requeue keeps it on w
    assert [x["id"] for x in report(c, "w", cap=1).json()["jobs"]] == [jid]
    clock.advance(100)
    app.state.check_presence()
    assert get_job(c, jid)["status"] == "queued" and get_job(c, jid)["node_id"] == "w"
    # legacy agent (no node_id/attempt) posts from the old process: the job is queued -> 409
    assert upd(c, jid, status="done", exit_code=0, append="old\n").status_code == 409
    r = report(c, "w", cap=1).json()
    assert r["jobs"] == [] and jid in r["cancel"]           # kill the old copy first; not re-dispatched meanwhile
    clock.advance(61)
    r = report(c, "w", cap=1).json()
    assert [x["id"] for x in r["jobs"]] == [jid] and jid not in r["cancel"]
    assert get_job(c, jid)["output"].count("old") == 0


def test_stale_update_for_never_dispatched_or_unknown_job(hub):
    c, _, _ = hub
    report(c, "w", cap=0)
    jid = job(c, "w", "shell")["id"]
    assert upd(c, jid, append="x").status_code == 409       # still queued, never dispatched
    assert jid not in report(c, "w", cap=0).json()["cancel"]   # nobody to fence
    assert upd(c, "nope", append="x").status_code == 404


# ---------------------------------------------------------------- H3: concurrent reports
def test_concurrent_reports_never_hand_out_a_job_twice_or_relabel_it(hub):
    c, app, _ = hub
    nodes = [f"n{i:02d}" for i in range(12)]
    for n in nodes:
        report(c, n, cap=3)
    ids = [job(c, "auto", "shell")["id"] for _ in range(30)]
    got: dict[str, list] = {}
    lock = threading.Lock()
    barrier = threading.Barrier(len(nodes))

    def agent(n):
        tc = TestClient(app)
        barrier.wait()
        for _ in range(3):
            r = tc.post("/api/agent/report", headers=AGENT, json={
                "node_id": n, "metrics": {"cpu": 5}, "capacity": 3, "info": {"tools": ["claude"], "agent_version": "2.2.0"}})
            with lock:
                for it in r.json()["jobs"]:
                    got.setdefault(it["id"], []).append(n)

    ts = [threading.Thread(target=agent, args=(n,)) for n in nodes]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert all(len(v) == 1 for v in got.values()), {k: v for k, v in got.items() if len(v) > 1}
    for jid, (n,) in got.items():
        j = get_job(c, jid)
        assert j["node_id"] == n and j["dispatched_to"] == n and j["status"] == "starting"
    assert set(got) <= set(ids)


# ---------------------------------------------------------------- M4: resolved 'any' job re-resolves on cooldown
def test_resolved_any_job_moves_to_next_engine_when_its_engine_cools_down(hub):
    c, app, clock = hub
    report(c, "w", tools=("claude",), cap=1)
    report(c, "x", tools=("codex",), cap=1)
    busy = job(c, "w", "claude")["id"]
    report(c, "w", tools=("claude",), cap=1)                # w now runs `busy`; its capacity stays 1 in the hub's view
    j = job(c, "auto", "any")
    assert j["engine"] == "claude" and j["node_id"] == "w"
    app.state.p2.set_cooldown("claude", clock() + 3600, "limit", clock())
    app.state.p2.place_jobs(clock())
    r = get_job(c, j["id"])
    assert r["engine"] == "codex" and r["node_id"] == "x" and r["req_engine"] == "any" and r["wait_reason"] is None
    assert [x["id"] for x in report(c, "x", tools=("codex",), cap=1).json()["jobs"]] == [j["id"]]
    assert busy


def test_explicit_node_any_job_switches_engine_on_that_node(hub):
    c, app, clock = hub
    report(c, "w", tools=("claude", "codex"), cap=0)
    j = job(c, "w", "any")
    assert j["engine"] == "claude"
    app.state.p2.set_cooldown("claude", clock() + 3600, "limit", clock())
    app.state.p2.place_jobs(clock())
    assert get_job(c, j["id"])["engine"] == "codex" and get_job(c, j["id"])["node_id"] == "w"


# ---------------------------------------------------------------- M3: project_id from the UI
def test_job_with_project_id_lands_in_that_project_without_a_stray_local_project(hub):
    c, _, clock = hub
    clock.t = 1_700_000_000.0
    report(c, "m1")
    c.post("/api/agent/sessions", headers=AGENT, json={"node_id": "m1", "sessions": [{
        "tool": "claude", "session_id": "s1", "cwd": "C:\\dev\\forge", "git_remote": "https://github.com/me/forge",
        "last_activity": clock.t - 5, "state": "idle"}]})
    pid = c.get("/api/projects", headers=UI).json()[0]["id"]
    # a different folder on another machine, started from the project page ("New task here")
    report(c, "m2")
    jid = job(c, "m2", "claude", cwd="E:\\elsewhere\\forge", project_id=pid)["id"]
    ps = c.get("/api/projects", headers=UI).json()
    assert len(ps) == 1
    d = c.get(f"/api/projects/{pid}", headers=UI).json()
    assert jid in [s["session_id"] for s in d["sessions"] if s["tool"] == "forge"]
    # no project_id, no repos row: the sessions table still knows the folder's remote
    jid2 = job(c, "m1", "claude", cwd="c:/dev/forge/")["id"]
    assert len(c.get("/api/projects", headers=UI).json()) == 1
    d = c.get(f"/api/projects/{pid}", headers=UI).json()
    assert jid2 in [s["session_id"] for s in d["sessions"] if s["tool"] == "forge"]


# ---------------------------------------------------------------- M6 / (h): redaction
@pytest.mark.parametrize("secret", [
    "sk-ant-api03-AbCdEfGhIjKlMnOp", "sk-proj-abcdefghijklmnop", "ghp_ABCDEFGHIJKLMNOPQRST", "gho_ABCDEFGHIJKLMNOPQRST",
    "ghu_ABCDEFGHIJKLMNOPQRST", "ghs_ABCDEFGHIJKLMNOPQRST", "ghr_ABCDEFGHIJKLMNOPQRST", "github_pat_11ABCDEFG0123456789_xyz",
    "glpat-ABCDEFGHIJ0123456789", "xoxb-1234567890-abcdef", "xoxp-1234567890-abcdef", "AKIAABCDEFGHIJKLMNOP",
    "AIzaSyA1234567890abcdefghij", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc"])
def test_redact_token_formats(secret):
    out = fp.redact(f"token: {secret} end")
    assert secret[:12] not in out and "[redacted]" in out and out.endswith(" end")


@pytest.mark.parametrize("text,leak", [
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----", "MIIEow"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk (truncated", "b3BlbnNzaC1rZXk"),
    ("Server=db;Database=x;User Id=sa;Password=Hunter2!;", "Hunter2"),
    ("DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=abc123+/xyz==;EndpointSuffix=core.windows.net", "abc123"),
    ("curl -d password=s3cret https://x", "s3cret"), ('PASSWORD="two words"', "two words")])
def test_redact_pem_blocks_and_key_value_secrets(text, leak):
    out = fp.redact(text)
    assert leak not in out and "[redacted]" in out


def test_redact_leaves_ordinary_words_alone():
    s = "desk-reservations12345 and task-something1234 passed; the password field is required"
    assert fp.redact(s) == s


def test_forge_transcript_redacts_tokens_across_chunk_boundaries(hub):
    c, _, _ = hub
    report(c, "m1")
    jid = job(c, "m1", "shell")["id"]
    report(c, "m1")
    tok = "ghp_" + "A" * 30
    out = "x" * (fp.EVENT_TEXT_MAX - 10) + tok + " tail"         # the token straddles the 4000-char chunk edge
    upd(c, jid, append=out, status="done", exit_code=0)
    evs = c.get(f"/api/sessions/forge/{jid}/transcript", headers=UI).json()["events"]
    joined = "".join(e["text"] for e in evs[1:])
    assert "ghp_" not in joined and "AAAAAAAAAA" not in joined and "[redacted] tail" in joined


# ---------------------------------------------------------------- M1 / L3 / L4 / L5: UI shapes
def test_brief_all_latest_has_nested_and_flat_fields(hub):
    c, app, clock = hub
    empty = c.get("/api/brief/all", headers=UI).json()
    assert empty["brief"] is None and empty["text"] is None
    report(c, "w", cap=5)
    rid = c.post("/api/brief/all", headers=UI).json()["id"]
    report(c, "w", cap=5)
    upd(c, rid, append="roll-up text", status="done", exit_code=0)
    b = c.get("/api/brief/all", headers=UI).json()
    assert b["brief"]["text"] == "roll-up text" and b["text"] == "roll-up text" and b["ts"] == b["brief"]["ts"]
    assert b["model"] == "haiku" and b["job_id"] == rid


def test_project_summary_has_ahead_and_behind(hub):
    c, _, clock = hub
    clock.t = 1_700_000_000.0
    report(c, "m1")
    c.post("/api/agent/commits", headers=AGENT, json={"node_id": "m1", "repos": [
        {"cwd": "C:\\dev\\a", "git_remote": "https://github.com/me/a", "branch": "main", "ahead": 3, "behind": 1, "commits": []},
        {"cwd": "D:\\a", "git_remote": "https://github.com/me/a", "branch": "dev", "ahead": 5, "behind": 0, "commits": []}]})
    p = c.get("/api/projects", headers=UI).json()[0]
    assert p["ahead"] == 5 and p["behind"] == 1


def test_cluster_block_peers_carry_a_non_loopback_url(tmp_path):
    f = Fleet(tmp_path)
    f.converge(6)
    for n in "ab":
        f.hubs[n].state.store.x("UPDATE peers SET urls=? WHERE id=?", (
            '["http://127.0.0.1:8787", "http://%s:8787"]' % n, n))
    peers = {p["id"]: p for p in f.client("a").get("/api/state", headers=Fleet.ui).json()["cluster"]["peers"]}
    assert peers["a"]["url"] == "http://a:8787" and peers["b"]["url"] == "http://b:8787" and peers["c"]["url"] == "http://c:8787"
    from forge_cluster import Cluster
    assert Cluster._best_url(["http://localhost:8787"]) == "http://localhost:8787" and Cluster._best_url([]) is None


def test_state_can_include_brief_jobs(hub):
    c, app, _ = hub
    report(c, "w")
    app.state.p2.insert_job(node_id="w", engine="ollama", prompt="b", source="brief", project_id="*")
    job(c, "w", "shell")
    assert all(j["source"] != "brief" for j in c.get("/api/state", headers=UI).json()["jobs"])
    assert any(j["source"] == "brief" for j in c.get("/api/state?include_brief=1", headers=UI).json()["jobs"])


# ---------------------------------------------------------------- (f) clock skew alert
def test_agent_clock_skew_alert(hub):
    c, _, clock = hub
    report(c, "ok", agent_time=clock() + 2)
    report(c, "fast", agent_time=clock() + 40)
    report(c, "slow", agent_time=clock() - 9)
    report(c, "legacy")
    alerts = {a["node_id"]: a for a in c.get("/api/health", headers=UI).json()["alerts"] if a["kind"] == "clock_skew"}
    assert set(alerts) == {"fast", "slow"} and "ahead" in alerts["fast"]["msg"] and "behind" in alerts["slow"]["msg"]


# ---------------------------------------------------------------- (g) retention / session churn
def test_retention_prunes_old_jobs_keeping_latest_200_and_active_ones(hub):
    c, app, clock = hub
    p2, store = app.state.p2, app.state.store
    t0 = clock()
    old = []
    for i in range(230):
        clock.t = t0 + i
        old.append(p2.insert_job(node_id="w", engine="shell", prompt="p", status="done", finished=clock()))
    active = p2.insert_job(node_id="w", engine="shell", prompt="still running")
    store.touch("jobs", "id", active, "status='running'")
    clock.t = t0 + 31 * 86400
    res = app.state.retention()
    left = {r["id"] for r in store.q("SELECT id FROM jobs")}
    # 231 jobs, all > 30 days old: the newest 200 stay (incl. the running one), the 31 oldest finished ones go
    assert res["jobs"] == 31 and len(left) == 200 and active in left
    assert old[30] not in left and old[31] in left and old[-1] in left
    assert store.q("SELECT COUNT(*) n FROM tombstones WHERE tbl='jobs'")[0]["n"] == 31
    clock.advance(8 * 86400)
    app.state.trim()
    assert store.q("SELECT COUNT(*) n FROM tombstones")[0]["n"] == 0                 # tombstones > 7 days pruned


def test_briefs_keep_last_20_per_project(hub):
    c, app, clock = hub
    report(c, "w", cap=50)
    for i in range(23):
        clock.advance(1)
        jid = app.state.p2.insert_job(node_id="w", engine="ollama", prompt="b", source="brief", project_id="P1")
        report(c, "w", cap=50)
        upd(c, jid, append=f"brief {i}", status="done", exit_code=0)
    rows = app.state.store.q("SELECT text FROM briefs WHERE project_id='P1' ORDER BY ts DESC")
    assert len(rows) == 20 and rows[0]["text"] == "brief 22" and rows[-1]["text"] == "brief 3"


def test_session_rows_rewritten_only_on_real_change_or_activity_over_60s(hub):
    c, _, clock = hub
    clock.t = 1_700_000_000.0
    s = {"tool": "claude", "session_id": "s1", "cwd": "C:\\dev\\x", "last_activity": clock.t - 100, "state": "idle", "messages": 3}

    def post(**kw):
        return c.post("/api/agent/sessions", headers=AGENT, json={"node_id": "m", "sessions": [s | kw]}).json()["changed"]
    assert post() == 1
    assert post(last_activity=clock.t - 50) == 0           # moved 50 s: not worth a replicated write
    assert post(last_activity=clock.t - 30) == 1           # moved 70 s from the stored value
    assert post(last_activity=clock.t - 29, messages=4) == 1   # another field changed


# ---------------------------------------------------------------- monotonic `updated`
def test_touch_never_moves_updated_backwards(hub):
    _, app, clock = hub
    store = app.state.store
    jid = app.state.p2.insert_job(node_id="w", engine="shell", prompt="p")
    store.x("UPDATE jobs SET updated=? WHERE id=?", (clock() + 30, jid))     # last written by a hub 30 s ahead
    store.touch("jobs", "id", jid, "prompt='q'")
    u = store.q("SELECT updated FROM jobs WHERE id=?", (jid,))[0]["updated"]
    assert u > clock() + 30
