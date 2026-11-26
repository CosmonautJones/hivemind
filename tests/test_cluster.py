"""Election, replication and forwarding across 3 in-process hubs (fake clock + fake transport)."""
import pytest

import forge_cluster
from app import create_app
from cluster_net import FakeClock, Fleet

UI, AGENT = Fleet.ui, Fleet.agent


@pytest.fixture
def fleet(tmp_path):
    return Fleet(tmp_path)


def roles(f):
    return {n: f.hubs[n].state.cluster.role for n in f.live()}


def report(client, node="w1", **extra):
    body = {"node_id": node, "metrics": {"cpu": 10}, "capacity": 2, "info": {"tools": ["claude"]}} | extra
    return client.post("/api/agent/report", json=body, headers=AGENT)


# ---------------------------------------------------------------- election
def test_lowest_priority_hub_wins_and_exactly_one_leader(fleet):
    # Arrange / Act
    fleet.converge()
    # Assert
    assert fleet.leaders() == ["a"]
    assert roles(fleet) == {"a": "leader", "b": "standby", "c": "standby"}
    assert {fleet.hubs[n].state.cluster.leader_id for n in "abc"} == {"a"}


def test_nobody_leads_before_peers_are_seen_healthy(fleet):
    fleet.round()  # a single probe round: peers are not healthy yet, nobody eligible yet
    assert fleet.leaders() == []
    assert set(roles(fleet).values()) == {"starting"}


def test_leader_killed_next_priority_promotes_within_three_rounds(fleet):
    fleet.converge()
    fleet.kill("a")
    for _ in range(3):
        fleet.round()
    assert fleet.leaders() == ["b"]
    assert fleet.hubs["c"].state.cluster.leader_id == "b"


def test_returning_priority_one_node_syncs_then_takes_over_after_hold_down(fleet):
    fleet.converge()
    fleet.kill("a")
    fleet.converge(4)
    jid = fleet.client("b").post("/api/jobs", json={"node_id": "auto", "engine": "shell", "prompt": "while a was away"},
                                 headers=UI).json()["id"]
    # a comes back with an empty database
    fleet.start("a")
    for _ in range(4):
        fleet.round()
    # a is synced and eligible but must NOT lead yet (hold-down); b keeps leadership
    assert fleet.leaders() == ["b"]
    assert fleet.hubs["a"].state.cluster.eligible
    assert fleet.hubs["a"].state.store.q("SELECT 1 FROM jobs WHERE id=?", (jid,))
    fleet.converge(10)  # well past the 20 s hold-down
    assert fleet.leaders() == ["a"]
    assert roles(fleet) == {"a": "leader", "b": "standby", "c": "standby"}
    assert fleet.hubs["a"].state.store.q("SELECT 1 FROM jobs WHERE id=?", (jid,))


def test_no_two_leaders_after_convergence_and_only_transient_overlap_during_handover(fleet):
    fleet.converge()
    fleet.kill("a")
    for _ in range(3):
        fleet.round()
    fleet.start("a")
    for _ in range(15):
        fleet.round()
        assert len(fleet.leaders()) <= 2
    assert fleet.leaders() == ["a"]


def test_lone_hub_is_leader_immediately(tmp_path):
    app = create_app(tmp_path, start_monitor=False, clock=FakeClock(), releases_dir=tmp_path / "r")
    assert app.state.cluster.role == "leader"


def test_cluster_status_requires_cluster_token(fleet):
    c = fleet.client("a")
    assert c.get("/cluster/status").status_code == 401
    assert c.get("/cluster/status", headers=UI).status_code == 401
    st = c.get("/cluster/status", headers=Fleet.cluster).json()
    assert {"node_id", "role", "leader_id", "eligible", "version", "bundle_sha256", "sync_cursor", "now"} <= set(st)


# ---------------------------------------------------------------- replication
def test_rows_created_on_leader_appear_on_standbys(fleet):
    fleet.converge()
    report(fleet.client("a"))
    jid = fleet.client("a").post("/api/jobs", json={"node_id": "w1", "engine": "shell", "prompt": "x"},
                                 headers=UI).json()["id"]
    fleet.round()
    for n in "bc":
        st = fleet.hubs[n].state.store
        assert st.q("SELECT 1 FROM nodes WHERE id='w1'")
        assert st.q("SELECT status FROM jobs WHERE id=?", (jid,))[0]["status"] == "queued"
        assert st.q("SELECT 1 FROM events WHERE msg LIKE ?", ("%joined the fleet%",))
        assert len(st.q("SELECT 1 FROM peers")) == 3


def test_metrics_and_logs_are_not_replicated(fleet):
    fleet.converge()
    report(fleet.client("a"), logs={"agent.log": "hello"})
    fleet.round()
    assert fleet.hubs["a"].state.store.q("SELECT 1 FROM metrics")
    assert not fleet.hubs["b"].state.store.q("SELECT 1 FROM metrics")
    assert not fleet.hubs["b"].state.store.q("SELECT 1 FROM logs")


def test_conflict_newer_updated_wins_on_upsert(fleet):
    st = fleet.hubs["b"].state.store
    st.x("INSERT INTO settings(node_id,work_root,max_jobs,updated) VALUES('n','LOCAL',2,200.0)")

    def row(wr, upd):
        st.upsert_rows("settings", [{"node_id": "n", "work_root": wr, "max_jobs": 1, "updated": upd}])
        return st.q("SELECT work_root FROM settings WHERE node_id='n'")[0]["work_root"]

    assert row("OLDER", 100.0) == "LOCAL"
    assert row("NEWER", 300.0) == "NEWER"
    assert row("SAME", 300.0) == "SAME"  # equal timestamps: incoming wins (>=)


def test_deletes_propagate_via_tombstones(fleet):
    fleet.converge()
    report(fleet.client("a"))
    fleet.round()
    assert fleet.hubs["b"].state.store.q("SELECT 1 FROM nodes WHERE id='w1'")
    fleet.client("a").delete("/api/nodes/w1", headers=UI)
    fleet.round()
    assert not fleet.hubs["b"].state.store.q("SELECT 1 FROM nodes WHERE id='w1'")


def test_sync_pages_until_caught_up(fleet, monkeypatch):
    fleet.converge()
    monkeypatch.setattr(forge_cluster, "SYNC_PAGE", 3)
    st = fleet.hubs["a"].state.store
    for i in range(10):
        fleet.clock.advance(0.5)
        st.event("w1", "info", f"e{i}")
    fleet.round()
    n = fleet.hubs["b"].state.store.q("SELECT COUNT(*) n FROM events WHERE msg LIKE 'e%'")[0]["n"]
    assert n == 10


def test_sync_requires_cluster_token(fleet):
    assert fleet.client("a").get("/cluster/sync?since=0", headers=AGENT).status_code == 401
    body = fleet.client("a").get("/cluster/sync?since=-1", headers=Fleet.cluster).json()
    assert {"peers", "nodes", "jobs", "events", "settings"} <= set(body["tables"])      # Phase 2 adds more tables
    assert {"projects", "project_alias", "sessions", "repos", "briefs", "cooldowns", "bad_versions", "leases"}         <= set(body["tables"]) and "transcripts" not in body["tables"]


# ---------------------------------------------------------------- forwarding
def test_standby_proxies_state_and_job_creation_to_leader(fleet):
    fleet.converge()
    report(fleet.client("a"))
    b = fleet.client("b")
    state = b.get("/api/state", headers=UI)
    assert state.status_code == 200
    cl = state.json()["cluster"]
    assert cl["leader_id"] == "a" and cl["viewing"] == "b"
    assert [p["id"] for p in cl["peers"]] == ["a", "b", "c"]
    assert all(k in cl["peers"][0] for k in ("priority", "role", "version", "healthy", "last_ok", "lag_s"))
    jid = b.post("/api/jobs", json={"node_id": "w1", "engine": "shell", "prompt": "via b"}, headers=UI).json()["id"]
    assert fleet.hubs["a"].state.store.q("SELECT prompt FROM jobs WHERE id=?", (jid,))[0]["prompt"] == "via b"


def test_forwarded_agent_report_uses_forwarded_for_as_addr(fleet):
    fleet.converge()
    r = report(fleet.client("b"))  # agent talks to standby b; the TestClient peer host is "testclient"
    assert r.status_code == 200
    assert fleet.hubs["a"].state.store.q("SELECT addr FROM nodes WHERE id='w1'")[0]["addr"] == "testclient"


def test_forwarding_preserves_auth_and_status_codes(fleet):
    fleet.converge()
    b = fleet.client("b")
    assert b.get("/api/state").status_code == 401
    assert b.get("/api/jobs/nope", headers=UI).status_code == 404


def test_503_when_leader_unreachable(fleet):
    fleet.converge()
    fleet.kill("a")  # b still believes a leads until its next probe round
    r = fleet.client("b").get("/api/state", headers=UI)
    assert r.status_code == 503 and r.json()["detail"] == "no leader"
    assert set(r.json()) >= {"role", "peers"}


def test_503_before_any_election_and_local_paths_still_work(fleet):
    b = fleet.client("b")
    assert b.get("/api/state", headers=UI).status_code == 503
    assert b.get("/healthz").json()["ok"] is True
    assert b.get("/").status_code == 200
    assert b.get("/cluster/status", headers=Fleet.cluster).status_code == 200
    assert b.post("/api/login", json={"token": "ui-tok"}).status_code == 200


def test_forward_loop_is_refused(fleet):
    fleet.converge()
    r = fleet.client("b").get("/api/state", headers=UI | {"X-Forge-Via": "c"})
    assert r.status_code == 503


def test_cluster_join_on_standby_is_applied_on_leader(fleet):
    fleet.converge()
    r = fleet.client("c").post("/cluster/join", json={"id": "D", "urls": ["http://d:8787/"]}, headers=Fleet.cluster)
    assert r.status_code == 200 and r.json() == {"id": "d", "priority": 4, "urls": ["http://d:8787"]}
    assert fleet.hubs["a"].state.store.q("SELECT priority FROM peers WHERE id='d'")
    fleet.round()
    assert fleet.hubs["c"].state.store.q("SELECT priority FROM peers WHERE id='d'")
