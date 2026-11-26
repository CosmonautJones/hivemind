"""Workstream R (reliability), CONTRACT-phase3.md §5 R1-R7: health gate, DB safety, job_output chunks, presence,
drain timeout, URL re-announce, machine retire, N-1 compatibility (2.2 agent / 2.2 leader against 2.3 code)."""
import json
import sqlite3
from datetime import datetime

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app as app_mod
import forge_reliability as fr
from app import create_app
from cluster_net import TOKENS, FakeClock, Fleet
from p2util import AGENT, CL, UI, admin, get_job, job, make_hub, report


@pytest.fixture
def hub(tmp_path):
    return make_hub(tmp_path)


def settle(app, clock):
    """A lone hub leads from the start; it is 'settled' (fleet duties) after the 30 s leader grace."""
    clock.advance(31)
    app.state.tick()


def run_job(c, node="w", engine="shell"):
    report(c, node, cap=1)
    jid = job(c, node, engine)["id"]
    assert [x["id"] for x in report(c, node, cap=1).json()["jobs"]] == [jid]
    return jid


def upd(c, jid, node="w", **body):
    return c.post(f"/api/agent/jobs/{jid}/update", json={"node_id": node} | body, headers=AGENT)


# ============================================================== R1 health gate
def test_healthz_ok_has_db_probe_and_disk(hub):
    c, app, clock = hub
    r = c.get("/healthz")
    b = r.json()
    assert r.status_code == 200 and b["ok"] and b["db_ok"] and b["checks"] == {"db_write": True, "disk": True}
    assert b["node_id"] == "hub1" and b["role"] == "leader" and "running_version" in b and "version" in b
    assert app.state.store.q("SELECT ts FROM healthz_probe WHERE id=1")[0]["ts"] == clock()


def test_healthz_503_on_low_disk_and_on_failed_db_write(hub):
    c, app, _ = hub
    rel = app.state.reliability
    rel.disk_free_gb = lambda p: 0.4
    r = c.get("/healthz")
    assert r.status_code == 503 and r.json()["ok"] is False and r.json()["checks"]["disk"] is False and r.json()["free_gb"] == 0.4
    rel.disk_free_gb = lambda p: 50.0
    app.state.store.x("DROP TABLE healthz_probe")
    r = c.get("/healthz")
    assert r.status_code == 503 and r.json()["checks"] == {"db_write": False, "disk": True}


def test_healthz_min_free_is_configurable(tmp_path):
    c, app, _ = make_hub(tmp_path, {"healthz_min_free_gb": 100})
    app.state.reliability.disk_free_gb = lambda p: 50.0
    assert c.get("/healthz").status_code == 503


def test_cluster_status_local_agent_db_and_disk(hub):
    c, app, clock = hub
    report(c, "hub1", version="2.3.0", bundle_version="2.3.0")
    st = c.get("/cluster/status", headers=CL).json()
    assert st["local_agent"]["online"] is True and st["local_agent"]["bundle_version"] == "2.3.0"
    assert st["local_agent"]["agent_version"] == "2.3.0"
    assert st["db"] == {"quick_check": "ok", "reseed_pending": False, "last_backup": None}
    assert st["disk"]["ok"] is True
    clock.advance(200)
    assert c.get("/cluster/status", headers=CL).json()["local_agent"]["online"] is False


def test_local_agent_reports_draining_target(hub):
    c, _, _ = hub
    c.post("/api/agent/report", headers=AGENT, json={"node_id": "hub1", "capacity": 0, "bundle_version": "2.2.0",
                                                     "metrics": {"bundle_version": "2.2.0", "draining_to": "2.3.0"}})
    la = c.get("/cluster/status", headers=CL).json()["local_agent"]
    assert la["bundle_version"] == "2.2.0" and la["draining_to"] == "2.3.0"


def test_low_disk_alert_source(hub):
    c, app, _ = hub
    app.state.reliability.disk_free_gb = lambda p: 0.2
    kinds = [a["kind"] for a in c.get("/api/health", headers=UI).json()["alerts"]]
    assert "low_disk_hub" in kinds


def test_bad_version_report_raises_rolled_back_alert_and_notifies(hub):
    c, app, _ = hub
    ctx, seen = app.state.ctx, []
    ctx.raise_alert = lambda kind, **kw: seen.append(("alert", kind, kw["key"], kw["level"]))
    ctx.notify = lambda kind, title, body, **kw: seen.append(("notify", kind, title))
    r = c.post("/cluster/bad-version", headers=CL, json={"version": "2.3.1", "node_id": "nova"})
    assert r.status_code == 200
    assert ("alert", "rolled_back", "rolled_back:nova:2.3.1", "error") in seen
    assert ("notify", "rolled_back", "Update 2.3.1 rolled back") in seen


# ============================================================== R3 DB safety
def _corrupt(path):
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE t(x)")
    con.executemany("INSERT INTO t VALUES(?)", [("x" * 500,)] * 200)
    con.commit()
    con.close()
    raw = bytearray(path.read_bytes())
    for i in range(4096 + 100, len(raw), 997):        # scribble over every page after the first
        raw[i] = 0xFF
    path.write_bytes(bytes(raw))


def test_prepare_db_ok_fresh_and_corrupt(tmp_path):
    db = tmp_path / "forge.db"
    assert fr.prepare_db(db, {})["quick_check"] == "ok"
    sqlite3.connect(str(db)).close()
    st = fr.prepare_db(db, {})
    assert st["quick_check"] == "ok" and not st.get("reseed_pending")
    db.unlink()
    _corrupt(db)
    st = fr.prepare_db(db, {"seed_peers": [{"id": "x"}]}, clock=lambda: 1_700_000_000)
    assert st["quick_check"] == "reseeded" and st["reseed_pending"] and not db.exists()
    assert list(tmp_path.glob("forge.db.corrupt-*")), "corrupt file kept for inspection"
    assert fr.reseed_marker(db).exists(), "reseed pending is remembered outside the database"


def test_prepare_db_standalone_restores_newest_healthy_backup(tmp_path):
    bdir = tmp_path / "backup"
    bdir.mkdir()
    good = sqlite3.connect(str(bdir / "forge-20261120.db"))
    good.execute("CREATE TABLE marker(v)")
    good.execute("INSERT INTO marker VALUES('restored')")
    good.commit()
    good.close()
    _corrupt(bdir / "forge-20261121.db")               # newer but damaged: skipped
    db = tmp_path / "forge.db"
    _corrupt(db)
    st = fr.prepare_db(db, {})
    assert st["quick_check"] == "restored" and st["restored_from"].endswith("forge-20261120.db")
    assert sqlite3.connect(str(db)).execute("SELECT v FROM marker").fetchone()[0] == "restored"


def test_corrupt_db_hub_stays_ineligible_until_it_reseeds_from_the_leader(tmp_path):
    f = Fleet(tmp_path, names=("a", "b"))
    try:
        f.converge()
        assert f.leaders() == ["a"]
        f.client("a").post("/api/agent/report", headers=Fleet.agent, json={"node_id": "n1", "capacity": 1, "metrics": {}})
        f.converge(3)
        # b restarts on a damaged database
        bdir = [d for d in tmp_path.iterdir() if d.name.startswith("b-")][0]
        f.kill("b")
        f.hubs["b"].state.store.db.close()
        dbp = bdir / "forge.db"
        for sfx in ("-wal", "-shm"):
            (bdir / f"forge.db{sfx}").unlink(missing_ok=True)
        _corrupt(dbp)
        cfg = json.loads((bdir / "config.json").read_text())
        app = create_app(bdir, start_monitor=False, clock=f.clock, client_factory=lambda: f.net.factory("b"),
                         releases_dir=bdir / "releases")
        f.hubs["b"] = f.net.apps["b"] = app
        f.net.down.discard("b")
        assert app.state.cluster.reseed_pending and not app.state.cluster.eligible and cfg["node_id"] == "b"
        f.kill("a")                                     # no leader: b must NOT self-elect on an empty copy
        for _ in range(10):
            f.round(3)
        assert not app.state.cluster.eligible and not app.state.cluster.is_leader
        st = f.client("b").get("/cluster/status", headers=Fleet.cluster).json()
        assert st["db"]["quick_check"] == "reseeded" and st["db"]["reseed_pending"] is True
        assert [a["level"] for a in app.state.reliability.alerts(f.clock()) if a["kind"] == "db_reseeded"] == ["error"]
        f.net.down.discard("a")
        f.converge(6)
        assert app.state.cluster.eligible and not app.state.cluster.reseed_pending
        assert app.state.store.q("SELECT 1 FROM nodes WHERE id='n1'"), "data copied from the leader"
        alerts = app.state.reliability.alerts(f.clock())
        assert [a["level"] for a in alerts if a["kind"] == "db_reseeded"] == ["warn"], "reseeded: shown for 1 h as warn"
    finally:
        f.close()


def test_nightly_backup_on_settled_leader_keeps_backup_keep_files(tmp_path):
    c, app, clock = make_hub(tmp_path, {"backup_keep": 2, "backup_time": "00:00"})
    rel = app.state.reliability
    settle(app, clock)
    bdir = rel.backup_dir()
    files = sorted(p.name for p in bdir.glob("forge-*.db"))
    assert files == [f"forge-{datetime.fromtimestamp(clock()).astimezone():%Y%m%d}.db"]
    con = sqlite3.connect(str(bdir / files[0]))
    assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok" and con.execute("SELECT COUNT(*) FROM peers").fetchone()[0] == 1
    con.close()
    app.state.tick()
    assert len(list(bdir.glob("forge-*.db"))) == 1, "one backup per day"
    for _ in range(3):
        clock.advance(86400)
        app.state.tick()
    assert len(list(bdir.glob("forge-*.db"))) == 2
    b = c.get("/api/backups", headers=UI).json()
    assert len(b["backups"]) == 2 and b["last_backup"] and b["backups"][0]["size"] > 0
    assert c.get("/cluster/status", headers=CL).json()["db"]["last_backup"] == b["last_backup"]


def test_backup_waits_for_backup_time(tmp_path):
    c, app, clock = make_hub(tmp_path, {"backup_time": "23:59"})
    d = datetime.fromtimestamp(clock()).astimezone()
    rel = app.state.reliability
    if d.hour * 60 + d.minute < 23 * 60 + 59:
        assert rel.backup_due(clock()) is None
    assert rel.backup_due(clock() + 86400 - (d.hour * 3600 + d.minute * 60) + 23 * 3600 + 59 * 60 + 30)


# ============================================================== R4 job output chunks
def test_job_output_is_append_only_chunks(hub):
    c, app, _ = hub
    st = app.state.store
    jid = run_job(c)
    upd(c, jid, status="running", append="hello ")
    upd(c, jid, append="world")
    rows = st.q("SELECT id, seq, text FROM job_output WHERE job_id=? ORDER BY seq", (jid,))
    assert [(r["id"], r["text"]) for r in rows] == [(f"{jid}:00000001", "hello "), (f"{jid}:00000002", "world")]
    assert st.q("SELECT output FROM jobs WHERE id=?", (jid,))[0]["output"] == "", "jobs.output is no longer rewritten"
    j = get_job(c, jid)
    assert j["output"] == "hello world" and j["output_seq"] == 2
    lst = c.get("/api/jobs", headers=UI).json()
    assert lst[0]["tail"] == "hello world" and "output" not in lst[0]


def test_job_output_endpoint_pages_by_seq(hub):
    c, _, _ = hub
    jid = run_job(c)
    for t in ("a", "b", "c"):
        upd(c, jid, append=t)
    r = c.get(f"/api/jobs/{jid}/output", headers=UI).json()
    assert [x["text"] for x in r["chunks"]] == ["a", "b", "c"] and r["next_seq"] == 3 and r["truncated"] is False
    assert r["status"] == "starting"
    r = c.get(f"/api/jobs/{jid}/output?since_seq=2", headers=UI).json()
    assert [x["text"] for x in r["chunks"]] == ["c"] and r["next_seq"] == 3
    r = c.get(f"/api/jobs/{jid}/output?since_seq=3", headers=UI).json()
    assert r["chunks"] == [] and r["next_seq"] == 3
    assert c.get("/api/jobs/nope/output", headers=UI).status_code == 404
    assert c.get(f"/api/jobs/{jid}/output").status_code == 401


def test_large_appends_split_and_old_chunks_are_trimmed(hub):
    c, app, _ = hub
    st = app.state.store
    jid = run_job(c)
    upd(c, jid, append="x" * 150_000)                  # -> 3 chunks of <= 64 000
    assert [r["n"] for r in st.q("SELECT LENGTH(text) n FROM job_output WHERE job_id=? ORDER BY seq", (jid,))] == [64000, 64000, 22000]
    for i in range(3):
        upd(c, jid, append=str(i) * 100_000)
    row = st.q("SELECT output_seq, output_len, output_min_seq FROM jobs WHERE id=?", (jid,))[0]
    kept = st.q("SELECT SUM(LENGTH(text)) n, MIN(seq) m FROM job_output WHERE job_id=?", (jid,))[0]
    assert row["output_len"] == 450_000 and kept["n"] <= 256_000 and kept["m"] == row["output_min_seq"] > 1
    out = get_job(c, jid)["output"]
    assert out.startswith("[...truncated...]\n") and out.endswith("2" * 1000)
    r = c.get(f"/api/jobs/{jid}/output", headers=UI).json()
    assert r["truncated"] is True and r["chunks"][0]["seq"] == row["output_min_seq"]


def test_final_output_reaches_hooks_and_cooldown_detection(hub):
    c, app, _ = hub
    seen = []
    app.state.ctx.on_job_finished.append(lambda j, status, out, now: seen.append(out))
    report(c, "w", cap=1)
    jid = job(c, "w", "claude")["id"]
    report(c, "w", cap=1)
    upd(c, jid, status="running", append="working\n")
    upd(c, jid, status="failed", exit_code=1, append="Claude AI usage limit reached|1900000000\n")
    assert seen == ["working\nClaude AI usage limit reached|1900000000\n"]
    assert app.state.store.q("SELECT 1 FROM cooldowns WHERE engine='claude'")


def test_hub_lines_of_lost_jobs_go_to_chunks(hub):
    c, app, clock = hub
    jid = run_job(c)
    upd(c, jid, status="running", append="started\n")
    clock.advance(200)
    report(c, "other", cap=0)
    app.state.check_presence()
    j = get_job(c, jid)
    assert j["status"] in ("failed", "queued") and j["output"].startswith("started\n") and "[hub] node offline" in j["output"]


def test_legacy_output_column_is_read_and_seeds_the_first_chunk(hub):
    c, app, _ = hub
    st = app.state.store
    jid = run_job(c)
    st.touch("jobs", "id", jid, "output=?", ("from a 2.2 leader\n",))       # a job whose output a 2.2 leader wrote
    assert get_job(c, jid)["output"] == "from a 2.2 leader\n"
    r = c.get(f"/api/jobs/{jid}/output", headers=UI).json()
    assert r["chunks"][0]["seq"] == 1 and r["chunks"][0]["text"] == "from a 2.2 leader\n"
    upd(c, jid, append="more")
    assert get_job(c, jid)["output"] == "from a 2.2 leader\nmore"
    assert [x["text"] for x in st.job_chunks(jid)] == ["from a 2.2 leader\n", "more"]


def test_forge_transcript_reads_chunks(hub):
    c, _, _ = hub
    jid = run_job(c)
    upd(c, jid, append="chunked output")
    t = c.get(f"/api/sessions/forge/{jid}/transcript", headers=UI).json()
    assert any(e["text"] == "chunked output" for e in t["events"])


def test_chunks_replicate_trims_and_retention_follow_on_standby(tmp_path):
    f = Fleet(tmp_path, names=("a", "b"))
    try:
        f.converge()
        a, b = f.hubs["a"].state.store, f.hubs["b"].state.store
        ca = f.client("a")
        ca.post("/api/agent/report", headers=Fleet.agent, json={"node_id": "w", "capacity": 1, "metrics": {}})
        jid = ca.post("/api/jobs", headers=Fleet.ui, json={"node_id": "w", "engine": "shell", "prompt": "p"}).json()["id"]
        ca.post("/api/agent/report", headers=Fleet.agent, json={"node_id": "w", "capacity": 1, "metrics": {}})
        ca.post(f"/api/agent/jobs/{jid}/update", headers=Fleet.agent, json={"status": "running", "append": "y" * 100_000})
        f.converge(2)
        assert b.job_output_text(jid) == "y" * 100_000
        for i in range(4):
            ca.post(f"/api/agent/jobs/{jid}/update", headers=Fleet.agent, json={"append": str(i) * 100_000})
        f.converge(2)
        assert b.job_output_text(jid) == a.job_output_text(jid)
        assert b.q("SELECT MIN(seq) m FROM job_output WHERE job_id=?", (jid,))[0]["m"] == \
            a.q("SELECT output_min_seq m FROM jobs WHERE id=?", (jid,))[0]["m"] > 1
        ca.post(f"/api/agent/jobs/{jid}/update", headers=Fleet.agent, json={"status": "done", "exit_code": 0})
        a.x("UPDATE jobs SET created=created-40*86400")
        with a.tx():
            a.delete_job_cascade(jid)
        f.converge(2)
        assert not b.q("SELECT 1 FROM job_output WHERE job_id=?", (jid,)) and not b.q("SELECT 1 FROM jobs WHERE id=?", (jid,))
    finally:
        f.close()


def test_retention_deletes_job_chunks(hub):
    c, app, clock = hub
    jid = run_job(c)
    upd(c, jid, status="done", exit_code=0, append="old")
    st = app.state.store
    st.x("UPDATE jobs SET created=? WHERE id=?", (clock() - 40 * 86400, jid))
    app.state.p2.retention(clock())   # JOB_KEEP_MIN keeps the newest 200: lower it for the test
    if st.q("SELECT 1 FROM jobs WHERE id=?", (jid,)):
        import forge_phase2
        old = forge_phase2.JOB_KEEP_MIN
        forge_phase2.JOB_KEEP_MIN = 0
        try:
            app.state.p2.retention(clock())
        finally:
            forge_phase2.JOB_KEEP_MIN = old
    assert not st.q("SELECT 1 FROM jobs WHERE id=?", (jid,)) and not st.q("SELECT 1 FROM job_output WHERE job_id=?", (jid,))


# ============================================================== R4 presence
def test_presence_is_local_and_nodes_row_is_written_every_60s(hub):
    c, app, clock = hub
    st = app.state.store
    beat = lambda cpu: c.post("/api/agent/report", headers=AGENT, json={"node_id": "w", "capacity": 1, "metrics": {"cpu": cpu}})
    beat(10)
    u0 = st.q("SELECT updated FROM nodes WHERE id='w'")[0]["updated"]
    clock.advance(5)
    beat(55)
    assert st.q("SELECT updated FROM nodes WHERE id='w'")[0]["updated"] == u0, "nodes row not rewritten within 60 s"
    assert json.loads(st.q("SELECT metrics FROM presence WHERE node_id='w'")[0]["metrics"])["cpu"] == 55
    n = [x for x in c.get("/api/state", headers=UI).json()["nodes"] if x["id"] == "w"][0]
    assert n["metrics"]["cpu"] == 55 and n["online"] and n["last_seen"] == clock()
    assert c.get("/api/nodes/w", headers=UI).json()["metrics"]["cpu"] == 55
    clock.advance(60)
    beat(70)
    assert st.q("SELECT updated FROM nodes WHERE id='w'")[0]["updated"] > u0
    assert "presence" not in app.state.cluster.sync_page(-1)["tables"]


def test_system_lane_presence_is_throttled_too(hub):
    c, app, clock = hub
    st = app.state.store
    sysr = {"node_id": "w", "lane": "system", "capacity": 1, "agent_version": "2.3.0"}
    c.post("/api/agent/report", headers=AGENT, json=sysr)
    u0 = st.q("SELECT updated FROM nodes WHERE id='w'")[0]["updated"]
    clock.advance(10)
    c.post("/api/agent/report", headers=AGENT, json=sysr)
    assert st.q("SELECT updated FROM nodes WHERE id='w'")[0]["updated"] == u0
    n = [x for x in c.get("/api/state", headers=UI).json()["nodes"] if x["id"] == "w"][0]
    assert n["system_lane"]["online"] and n["system_lane"]["last_seen"] == clock() and n["system_lane"]["version"] == "2.3.0"


def test_scheduler_sees_presence(hub):
    c, app, clock = hub
    report(c, "w", cap=1)
    clock.advance(40)                                 # nodes.last_seen is now 40 s old, presence is fresh
    report(c, "w", cap=1)
    r = job(c, "auto", "shell")
    assert r["node_id"] == "w"


# ============================================================== R5 drain timeout
def test_report_response_carries_drain_timeout(tmp_path):
    c, _, _ = make_hub(tmp_path, {"drain_timeout_s": 900})
    assert report(c, "w").json()["drain_timeout_s"] == 900
    (tmp_path / "x").mkdir()
    c2, _, _ = make_hub(tmp_path / "x")
    assert report(c2, "w").json()["drain_timeout_s"] == 1800


# ============================================================== R6 URL re-announce
def test_standby_reannounces_changed_urls_to_the_leader(tmp_path):
    f = Fleet(tmp_path, names=("a", "b"))
    try:
        f.converge()
        rel = f.hubs["b"].state.reliability
        urls = ["http://b.tail.ts.net:8787", "http://100.64.1.2:8787"]
        rel.url_source = lambda: list(urls)
        f.round()
        peer = {p["id"]: p for p in f.hubs["a"].state.cluster.peers()}["b"]
        assert peer["urls"] == urls and rel.announces == 1
        f.net.apps["b.tail.ts.net"] = f.hubs["b"]          # keep the fake network routing the new name
        f.round()
        assert rel.announces == 1, "unchanged set: no new join before announce_every_s"
        urls[1] = "http://100.64.9.9:8787"
        f.clock.advance(61)
        f.round()
        f.converge(3)
        assert {p["id"]: p for p in f.hubs["a"].state.cluster.peers()}["b"]["urls"][1] == "http://100.64.9.9:8787"
        assert rel.announces == 2
    finally:
        f.close()


def test_leader_announces_itself_and_join_is_quiet_when_unchanged(hub):
    c, app, clock = hub
    rel, st = app.state.reliability, app.state.store
    rel.url_source = lambda: ["http://hub1.ts.net:8787"]
    app.state.tick()
    assert json.loads(st.q("SELECT urls FROM peers WHERE id='hub1'")[0]["urls"]) == ["http://hub1.ts.net:8787"]
    u = st.q("SELECT updated FROM peers WHERE id='hub1'")[0]["updated"]
    app.state.cluster.join("hub1", ["http://hub1.ts.net:8787"])
    assert st.q("SELECT updated FROM peers WHERE id='hub1'")[0]["updated"] == u


def test_build_urls_order_dedupe_and_limit():
    ts = {"dns": "forge-01.tail1.ts.net", "ips": ["100.101.1.2"]}
    assert fr.build_urls(8787, ts, ["192.168.1.5", "100.101.1.2"]) == [
        "http://forge-01.tail1.ts.net:8787", "http://100.101.1.2:8787", "http://192.168.1.5:8787"]
    assert len(fr.build_urls(1, {}, [f"192.168.1.{i}" for i in range(20)])) == 8


def test_announce_is_off_without_a_monitor_or_url_source(hub):
    _, app, clock = hub
    rel = app.state.reliability
    assert rel.announce_enabled is False
    app.state.tick()
    assert rel.last_discover is None


# ============================================================== R6 retire
def _retire(c, nid, **body):
    return c.post(f"/api/machines/{nid}/retire", headers=admin(c), json={"confirm": True, "uninstall": False} | body)


def test_retire_validation(hub):
    c, app, _ = hub
    report(c, "w")
    assert c.post("/api/machines/w/retire", headers=admin(c), json={}).status_code == 400
    assert _retire(c, "ghost").status_code == 404
    assert _retire(c, "hub1").status_code == 409                    # the only hub
    # (2.3 integration: F ships forge_uninstall, so uninstall=True is valid here; the uninstall flow has its own tests)
    assert c.post("/api/machines/w/retire", json={"confirm": True}).status_code == 401
    assert _retire(c, "w").status_code == 202
    assert _retire(c, "w").status_code == 409                       # already retiring


def test_retire_requires_step_up(hub):
    c, app, _ = hub
    report(c, "w")

    def deny(request):
        raise HTTPException(403, {"detail": "admin PIN required", "step_up": "required"})
    app.state.ctx.require_step_up = deny
    r = _retire(c, "w")
    assert r.status_code == 403 and not app.state.store.q("SELECT 1 FROM retirements")


def test_retire_drains_then_removes_the_machine(hub):
    c, app, clock = hub
    st = app.state.store
    settle(app, clock)
    jid = run_job(c)
    upd(c, jid, status="running")
    pinned = job(c, "w", "shell")["id"]                         # queued, pinned to w: cancelled by the drain
    c.post("/api/agent/sessions", headers=AGENT, json={"node_id": "w", "sessions": [
        {"tool": "claude", "session_id": "s-1", "cwd": "C:\\x", "last_activity": clock()}]})
    r = _retire(c, "w", rotate_tokens=False)
    assert r.status_code == 202 and r.json() == {"ok": True, "state": "draining"}
    assert get_job(c, pinned)["status"] == "cancelled"
    other = job(c, "w", "shell")["id"]
    assert report(c, "w", cap=2).json()["jobs"] == [], "a draining node gets no new jobs"
    assert c.get("/api/machines/w/retire", headers=UI).json()["state"] == "draining"
    app.state.tick()
    assert c.get("/api/machines/w/retire", headers=UI).json()["state"] == "draining", "waits for the running job"
    upd(c, jid, status="done", exit_code=0)
    app.state.tick()
    assert c.get("/api/machines/w/retire", headers=UI).json()["state"] == "removing"
    app.state.tick()
    v = c.get("/api/machines/w/retire", headers=UI).json()
    assert v["state"] == "done" and v["finished"]
    assert not st.q("SELECT 1 FROM nodes WHERE id='w'") and not st.q("SELECT 1 FROM settings WHERE node_id='w'")
    assert not st.q("SELECT 1 FROM sessions WHERE node_id='w'") and not st.q("SELECT 1 FROM presence WHERE node_id='w'")
    assert st.q("SELECT 1 FROM tombstones WHERE tbl='nodes' AND id='w'")
    assert get_job(c, other)["status"] == "cancelled"


def test_retire_drain_timeout_cancels_running_jobs(tmp_path):
    c, app, clock = make_hub(tmp_path, {"drain_timeout_s": 600})
    settle(app, clock)
    jid = run_job(c)
    upd(c, jid, status="running")
    _retire(c, "w")
    clock.advance(601)
    report(c, "w", cap=0)                              # still reporting: only the drain timeout ends the wait
    app.state.tick()
    assert app.state.store.q("SELECT cancel_requested FROM jobs WHERE id=?", (jid,))[0]["cancel_requested"] == 1
    app.state.tick()
    assert c.get("/api/machines/w/retire", headers=UI).json()["state"] == "done"
    j = get_job(c, jid)
    assert j["status"] == "failed" and "removed from the fleet" in j["output"]


def _with_uninstall(app):
    app.state.ctx.actions_catalog = {"actions": list(app.state.ctx.actions_catalog["actions"]) + [
        {"name": "forge_uninstall", "description": "x", "destructive": True,
         "args": {"keep_data": {"type": "string", "enum": ["true", "false"], "default": "false"}}}]}


def test_retire_with_uninstall_runs_the_system_job_then_removes(hub):
    c, app, clock = hub
    _with_uninstall(app)
    settle(app, clock)
    report(c, "w")
    sysr = {"node_id": "w", "lane": "system", "capacity": 1, "agent_version": "2.3.0"}
    c.post("/api/agent/report", headers=AGENT, json=sysr)
    assert _retire(c, "w", uninstall=True, rotate_tokens=True).status_code == 202
    app.state.tick()
    v = c.get("/api/machines/w/retire", headers=UI).json()
    assert v["state"] == "uninstalling" and v["job_id"]
    jobs = c.post("/api/agent/report", headers=AGENT, json=sysr).json()["jobs"]
    assert [j["action"] for j in jobs] == ["forge_uninstall"] and jobs[0]["args"] == {"keep_data": False}
    app.state.tick()
    assert c.get("/api/machines/w/retire", headers=UI).json()["state"] == "uninstalling"
    clock.advance(301)                                 # the uninstall killed the agents: offline for 5 min
    app.state.tick()
    app.state.tick()
    assert c.get("/api/machines/w/retire", headers=UI).json()["state"] == "done"
    assert any("rotat" in e["msg"] for e in app.state.store.q("SELECT msg FROM events"))


def test_retire_failed_uninstall_raises_alert_and_can_be_cancelled(hub):
    c, app, clock = hub
    _with_uninstall(app)
    alerts = []
    app.state.ctx.raise_alert = lambda kind, **kw: alerts.append((kind, kw["key"]))
    settle(app, clock)
    report(c, "w")
    sysr = {"node_id": "w", "lane": "system", "capacity": 1}
    c.post("/api/agent/report", headers=AGENT, json=sysr)
    _retire(c, "w", uninstall=True)
    app.state.tick()
    jid = c.get("/api/machines/w/retire", headers=UI).json()["job_id"]
    c.post("/api/agent/report", headers=AGENT, json=sysr)
    c.post(f"/api/agent/jobs/{jid}/update", headers=AGENT, json={"status": "failed", "exit_code": 3})
    report(c, "w")
    app.state.tick()
    v = c.get("/api/machines/w/retire", headers=UI).json()
    assert v["state"] == "failed" and "exit 3" in v["error"] and ("retire_failed", "retire_failed:w") in alerts
    assert c.delete("/api/machines/w/retire", headers=UI).json() == {"ok": True}
    assert app.state.store.q("SELECT drain FROM settings WHERE node_id='w'")[0]["drain"] == 0
    assert c.get("/api/machines/w/retire", headers=UI).status_code == 404


def test_cancel_only_while_draining(hub):
    c, app, clock = hub
    settle(app, clock)
    report(c, "w")
    _retire(c, "w")
    app.state.store.touch("retirements", "node_id", "w", "state='removing'")
    assert c.delete("/api/machines/w/retire", headers=UI).status_code == 409
    assert c.delete("/api/machines/x/retire", headers=UI).status_code == 404


def test_drained_node_is_not_auto_placed(hub):
    c, app, _ = hub
    report(c, "w", cap=2)
    report(c, "v", cap=2, cpu=90)
    app.state.store.x("INSERT INTO settings(node_id,drain,updated) VALUES('w',1,1)")
    assert job(c, "auto", "shell")["node_id"] == "v"


# ============================================================== N-1 compatibility
def test_n1_a_22_agent_against_a_23_hub(hub):
    """A 2.2.x agent: no new fields, PowerShell 5.1 shapes (single-element arrays as objects), updates without node_id."""
    c, app, _ = hub
    body = {"node_id": "old", "capacity": 1, "boot": True, "agent_version": "2.2.1", "bundle_version": "2.2.1",
            "metrics": {"cpu": 5, "disks": {"name": "C:", "free_gb": 10, "total_gb": 100}, "ips": {"iface": "e", "ip": "192.168.1.2"}},
            "procs": {"name": "x", "pid": 1, "cpu": 0, "mem_mb": 1}, "agents": [], "running": [],
            "info": {"tools": "claude", "agent_version": "2.2.1"}}
    r = c.post("/api/agent/report", headers=AGENT, json=body)
    assert r.status_code == 200 and r.json()["drain_timeout_s"] == 1800       # unknown field: ignored by 2.2
    jid = job(c, "old", "shell")["id"]
    jobs = c.post("/api/agent/report", headers=AGENT, json=body | {"boot": False}).json()["jobs"]
    assert [j["id"] for j in jobs] == [jid]
    assert c.post(f"/api/agent/jobs/{jid}/update", headers=AGENT, json={"status": "running", "pid": 7, "append": "out "}).status_code == 200
    assert c.post(f"/api/agent/jobs/{jid}/update", headers=AGENT, json={"status": "done", "exit_code": 0, "append": "done"}).status_code == 200
    j = get_job(c, jid)
    assert j["status"] == "done" and j["output"] == "out done" and j["pid"] == 7
    n = c.get("/api/nodes/old", headers=UI).json()
    assert n["metrics"]["disks"][0]["name"] == "C:" and n["procs"][0]["pid"] == 1


def test_n1_a_23_standby_reads_output_a_22_leader_wrote(tmp_path):
    """A 2.2 leader replicates jobs.output and has no job_output / presence tables: the 2.3 standby shows that output."""
    clock = FakeClock()
    d = tmp_path / "s"
    d.mkdir()
    (d / "config.json").write_text(json.dumps(TOKENS | {"node_id": "s"}), encoding="utf-8")
    app = create_app(d, start_monitor=False, clock=clock, releases_dir=d / "rel")
    st = app.state.store
    page = {"now": clock(), "more": False, "deleted": [], "tables": {"jobs": [
        {"id": "0000000000a000001", "node_id": "n", "engine": "shell", "prompt": "p", "status": "done", "output": "legacy text",
         "created": clock(), "updated": clock(), "run_as": "user"}]}}
    with st.tx():
        st.upsert_rows("jobs", page["tables"]["jobs"])
        app.state.cluster._cleanup_chunks(page)
    c = TestClient(app)
    j = c.get("/api/jobs/0000000000a000001", headers={"Authorization": "Bearer ui-tok"}).json()
    assert j["output"] == "legacy text" and j["output_seq"] == 0


# ============================================================== review fixes (R-01 .. R-13)
def test_healthz_concurrent_probes_never_report_a_false_503(hub):
    """R-01: 8 probes at once, each with its own `now`; a write/read race would make 7 of them see another stamp."""
    import itertools
    import threading
    _, app, _ = hub
    ctx, rel, st = app.state.ctx, app.state.reliability, app.state.store
    n, nlock = itertools.count(), threading.Lock()

    def clk():
        with nlock:
            return 1_800_000_000 + next(n) * 0.001
    ctx.clock = clk
    barrier, orig_x = threading.Barrier(8, timeout=0.5), st.x

    def x(sql, args=()):
        r = orig_x(sql, args)
        if "healthz_probe" in sql:                 # widen the write -> read window of every probe
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return r
    st.x = x
    out, go = [], threading.Barrier(8)

    def probe():
        go.wait()
        out.append(rel.healthz()[0])
    ts = [threading.Thread(target=probe) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert out == [200] * 8


def c_status(f, name):
    return f.client(name).get("/cluster/status", headers=Fleet.cluster).json()


def test_reseed_pending_survives_a_restart_before_the_first_sync(tmp_path):
    """R-02: the empty reseeded copy restarts while no leader is reachable: still not eligible, alert still shown."""
    f = Fleet(tmp_path, names=("a", "b"))
    try:
        f.converge()
        f.client("a").post("/api/agent/report", headers=Fleet.agent, json={"node_id": "n1", "capacity": 1, "metrics": {}})
        f.converge(3)
        bdir = [d for d in tmp_path.iterdir() if d.name.startswith("b-")][0]

        def boot_b():
            f.kill("b")
            f.hubs["b"].state.store.db.close()
            app = create_app(bdir, start_monitor=False, clock=f.clock, client_factory=lambda: f.net.factory("b"),
                             releases_dir=bdir / "releases")
            f.hubs["b"] = f.net.apps["b"] = app
            f.net.down.discard("b")
            return app
        f.kill("b")
        f.hubs["b"].state.store.db.close()
        for sfx in ("-wal", "-shm"):
            (bdir / f"forge.db{sfx}").unlink(missing_ok=True)
        _corrupt(bdir / "forge.db")
        f.hubs["b"].state.store.db = sqlite3.connect(":memory:")     # boot_b closes it again
        app = boot_b()
        assert app.state.cluster.reseed_pending
        f.kill("a")                                     # the leader is gone before b could copy anything
        app = boot_b()                                  # ... and b restarts (power cut, watchdog)
        assert app.state.cluster.reseed_pending, "remembered across the restart"
        for _ in range(10):
            f.round(3)
        assert not app.state.cluster.eligible and not app.state.cluster.is_leader
        db = c_status(f, "b")["db"]
        assert db["quick_check"] == "reseeded" and db["reseed_pending"] is True
        assert [a["level"] for a in app.state.reliability.alerts(f.clock()) if a["kind"] == "db_reseeded"] == ["error"]
        f.net.down.discard("a")
        f.converge(6)
        assert app.state.cluster.eligible and not app.state.cluster.reseed_pending
        assert app.state.store.q("SELECT 1 FROM nodes WHERE id='n1'"), "data copied from the leader"
        assert not fr.reseed_marker(bdir / "forge.db").exists(), "cleared by the first full sync"
        assert boot_b().state.cluster.reseed_pending is False
    finally:
        f.close()


def test_prepare_db_never_moves_a_locked_or_unopenable_file(tmp_path):
    """R-03: 'database is locked' / 'unable to open' are transient: retried, then startup fails, nothing is moved."""
    db = tmp_path / "forge.db"
    con = sqlite3.connect(str(db), isolation_level=None)
    con.execute("CREATE TABLE t(x)")
    con.execute("INSERT INTO t VALUES(1)")
    con.execute("BEGIN EXCLUSIVE")                      # another process holds the file (rollback journal mode)
    naps = []
    try:
        with pytest.raises(fr.DbUnavailable):
            fr.prepare_db(db, {"seed_peers": [{"id": "x"}]}, attempts=3, lock_timeout=0.05, sleep=naps.append)
    finally:
        con.execute("ROLLBACK")
        con.close()
    assert len(naps) == 2 and naps[1] > naps[0], "retried with backoff"
    assert db.exists() and not list(tmp_path.glob("forge.db.corrupt-*")) and not fr.reseed_marker(db).exists()
    assert fr.prepare_db(db, {})["quick_check"] == "ok", "fine once the lock is gone"
    d2 = tmp_path / "d2"
    d2.mkdir()
    (d2 / "forge.db").mkdir()                           # cannot be opened as a database file at all
    with pytest.raises(fr.DbUnavailable):
        fr.prepare_db(d2 / "forge.db", {}, attempts=2, lock_timeout=0.05, sleep=lambda s: None)
    assert (d2 / "forge.db").is_dir() and not list(d2.glob("forge.db.corrupt-*"))


def test_quick_check_classifies_damage_as_corrupt(tmp_path):
    db = tmp_path / "x.db"
    db.write_bytes(b"this is not a database" * 100)
    ok, detail = fr.quick_check(db)
    assert ok is False and detail


def test_retire_offline_machine_with_queued_uninstall_is_not_reported_done(hub):
    """R-06: the system lane never picked the uninstall job up: no 'went silent' success; fails after 30 min."""
    c, app, clock = hub
    _with_uninstall(app)
    settle(app, clock)
    report(c, "w")                                      # user lane only; the machine then goes offline
    assert _retire(c, "w", uninstall=True).status_code == 202
    app.state.tick()
    v = c.get("/api/machines/w/retire", headers=UI).json()
    assert v["state"] == "uninstalling"
    assert app.state.store.q("SELECT status FROM jobs WHERE id=?", (v["job_id"],))[0]["status"] == "queued"
    clock.advance(301)
    app.state.tick()
    assert c.get("/api/machines/w/retire", headers=UI).json()["state"] == "uninstalling"
    clock.advance(1500)
    app.state.tick()
    v = c.get("/api/machines/w/retire", headers=UI).json()
    assert v["state"] == "failed" and "uninstall not performed" in v["error"]
    assert app.state.store.q("SELECT 1 FROM nodes WHERE id='w'"), "the machine was not removed"


def test_bad_version_node_id_is_validated(hub):
    """R-07: only a plain node id reaches the alert key/text and the notification URL."""
    c, app, _ = hub
    ctx, seen = app.state.ctx, []
    ctx.raise_alert = lambda kind, **kw: seen.append(("alert", kw["key"], kw["node_id"]))
    ctx.notify = lambda kind, title, body, **kw: seen.append(("notify", kw["url"], body))
    r = c.post("/cluster/bad-version", headers=CL, json={"version": "2.3.1", "node_id": "evil\n#/admin?x=" + "y" * 500})
    assert r.status_code == 200
    assert ("alert", "rolled_back:-:2.3.1", None) in seen
    assert [x[1] for x in seen if x[0] == "notify"] == ["#/machines"]
    seen.clear()
    c.post("/cluster/bad-version", headers=CL, json={"version": "2.3.2", "node_id": "Forge-01.lan"})
    assert ("alert", "rolled_back:forge-01.lan:2.3.2", "forge-01.lan") in seen
    assert [x[1] for x in seen if x[0] == "notify"] == ["#/machine/forge-01.lan"]


def test_pick_lan_prefers_the_default_route_and_drops_virtual_adapters():
    """R-08: vEthernet (172.16/12), Docker, VirtualBox host-only (192.168.56/24) addresses are not announced."""
    host = ["172.25.80.1", "192.168.56.1", "192.168.1.20", "100.101.1.2", "127.0.0.1", "169.254.3.4", "192.168.1.20"]
    assert fr.pick_lan("192.168.1.20", host) == ["192.168.1.20"]
    assert fr.pick_lan(None, host) == ["192.168.1.20"]
    assert fr.pick_lan("100.101.1.2", host) == ["192.168.1.20"], "a Tailscale exit-node route is not a LAN address"
    assert fr.pick_lan("172.20.0.5", []) == ["172.20.0.5"], "the default route is trusted even in 172.16/12"
    assert fr.pick_lan(None, ["172.17.0.1", "192.168.56.101"]) == []
    ts = {"dns": "forge-01.tail1.ts.net", "ips": ["100.101.1.2"]}
    assert fr.build_urls(8787, ts, fr.pick_lan(None, host)) == [
        "http://forge-01.tail1.ts.net:8787", "http://100.101.1.2:8787", "http://192.168.1.20:8787"]


def test_new_leader_gives_workers_the_full_lost_window_after_a_failover(hub):
    """R-09: the replicated nodes.last_seen is 55 s old when we take over; the worker reconnects 70 s later."""
    c, app, clock = hub
    st, cl = app.state.store, app.state.cluster
    jid = run_job(c)
    upd(c, jid, status="running")
    st.x("DELETE FROM presence WHERE node_id='w'")              # a new leader has no local presence rows
    st.x("UPDATE nodes SET last_seen=? WHERE id='w'", (clock() - 55,))
    cl.leader_since = clock()
    clock.advance(40)                                          # settled (>= 30 s grace), worker not back yet
    app.state.tick()
    assert get_job(c, jid)["status"] == "running"
    clock.advance(30)
    report(c, "w", cap=1)                                      # the worker found the new leader
    app.state.tick()
    assert get_job(c, jid)["status"] == "running"
    clock.advance(95)                                          # a worker that never comes back is still lost
    app.state.tick()
    assert get_job(c, jid)["status"] in ("failed", "queued")


def test_append_chunk_never_overwrites_when_jobs_row_lags(hub):
    """R-10: after a failover jobs.output_seq can lag the replicated job_output rows."""
    c, app, clock = hub
    st = app.state.store
    jid = run_job(c)
    upd(c, jid, status="running", append="a")
    upd(c, jid, append="b")
    st.x("UPDATE jobs SET output_seq=1 WHERE id=?", (jid,))
    assert st.append_chunk(jid, "c", clock()) == 3
    assert [x["text"] for x in st.job_chunks(jid)] == ["a", "b", "c"]


def test_cancelled_retire_keeps_a_manual_drain(hub):
    """R-11"""
    c, app, clock = hub
    st = app.state.store
    settle(app, clock)
    report(c, "w")
    st.x("INSERT INTO settings(node_id,drain,updated) VALUES('w',1,1)")
    assert _retire(c, "w").status_code == 202
    assert c.delete("/api/machines/w/retire", headers=UI).json() == {"ok": True}
    assert st.q("SELECT drain FROM settings WHERE node_id='w'")[0]["drain"] == 1


def test_nightly_backup_runs_off_the_tick_thread(tmp_path):
    """R-13: on a real server VACUUM INTO runs in a daemon thread; one attempt per day."""
    import threading
    c, app, clock = make_hub(tmp_path, {"backup_time": "00:00"})
    rel = app.state.reliability
    rel.backup_async = True
    started, gate, orig = threading.Event(), threading.Event(), rel.run_backup

    def slow(day):
        started.set()
        gate.wait(5)
        return orig(day)
    rel.run_backup = slow
    settle(app, clock)
    assert started.wait(5) and rel.backup_running(), "tick returned while the backup is still running"
    app.state.tick()                                            # no second backup while one runs
    gate.set()
    rel.backup_thread.join(10)
    assert len(list(rel.backup_dir().glob("forge-*.db"))) == 1
