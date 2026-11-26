"""Staggered rollout: update lease ordering, single holder, TTL, auto release, lone survivor, fleet-bad."""
import pytest

import forge_lease as fl
from cluster_net import Fleet

CL = Fleet.cluster
NOW = 1_000.0


# ---------------------------------------------------------------- pure
def test_build_order_is_canary_first_leader_last():
    peers = [{"id": "a", "priority": 1}, {"id": "b", "priority": 2}, {"id": "c", "priority": 3}, {"id": "d", "priority": 4}]
    assert fl.build_order(peers, "a") == ["d", "c", "b", "a"]
    assert fl.build_order(peers, "c") == ["d", "b", "a", "c"]          # failover: the new leader goes last
    assert fl.build_order(peers, None) == ["d", "c", "b", "a"]


def d(requester, running, down=None, lease=None, bad=(), version="2.3.0", order=("c", "b", "a")):
    return fl.decide(requester, version, list(order), running, down or {}, lease, set(bad), NOW)


def test_decide_canary_goes_first_then_waits_for_earlier_nodes():
    assert d("c", {})["granted"] is True
    r = d("b", {"c": "2.2.0"})
    assert r["granted"] is False and r["waiting_for"] == "c" and r["retry_after_s"] > 0
    assert d("b", {"c": "2.3.0"})["granted"] is True
    assert d("a", {"c": "2.3.0", "b": "2.2.0"})["waiting_for"] == "b"
    assert d("a", {"c": "2.3.1", "b": "2.3.0"})["granted"] is True       # leader last; newer counts as done


def test_decide_single_holder_and_same_holder_renews():
    lease = {"node_id": "c", "version": "2.3.0", "expires": NOW + 100}
    r = d("b", {"c": "2.3.0"}, lease=lease)
    assert r == {"granted": False, "holder": "c", "retry_after_s": 30, "reason": "lease held"}
    assert d("c", {}, lease=lease)["granted"] is True
    assert d("b", {"c": "2.3.0"}, lease={"node_id": "c", "version": "2.3.0", "expires": NOW - 1})["granted"] is True   # expired


def test_decide_skips_peers_unreachable_over_10_minutes_only():
    assert d("a", {"c": "2.3.0"}, down={"b": 599})["granted"] is False
    assert d("a", {"c": "2.3.0"}, down={"b": 600})["granted"] is True
    assert d("a", {}, down={"c": 5000, "b": 5000})["granted"] is True


def test_decide_fleet_bad_and_unknown_peer():
    r = d("c", {}, bad=["2.3.0"])
    assert r["granted"] is False and r["reason"] == "fleet-bad"
    assert d("c", {}, bad=["2.2.9"])["granted"] is True
    assert d("zz", {})["granted"] is False


# ---------------------------------------------------------------- 3 hubs
@pytest.fixture
def fleet(tmp_path):
    f = Fleet(tmp_path)
    f.converge(6)
    return f


def ask(fleet, via, node, version="2.3.0"):
    r = fleet.client(via).post("/cluster/update-lease", json={"node_id": node, "version": version}, headers=CL)
    assert r.status_code == 200, r.text
    return r.json()


def run_version(fleet, node, version):
    fleet.hubs[node].state.releases.running_version = version


def agent_on(fleet, node, version, via="a"):
    """That machine's agent reports `bundle_version` (lease release needs the agent on the new version too)."""
    r = fleet.client(via).post("/api/agent/report", headers=Fleet.agent,
                               json={"node_id": node, "metrics": {}, "capacity": 0, "bundle_version": version})
    assert r.status_code == 200, r.text


def rounds(fleet, n, secs=10, agents=()):
    for _ in range(n):
        for node, ver in agents:
            agent_on(fleet, node, ver)
        fleet.round(secs)


def test_rollout_order_across_the_cluster_and_standby_proxies_to_leader(fleet):
    assert fleet.leaders() == ["a"]
    assert ask(fleet, "b", "a")["granted"] is False                      # leader asks first: must wait
    assert ask(fleet, "c", "b")["waiting_for"] == "c"                    # via standby c -> forwarded to leader a
    g = ask(fleet, "b", "c")                                             # canary = highest priority number
    assert g["granted"] is True and g["holder"] == "c"
    # exactly one holder
    r = ask(fleet, "a", "b")
    assert r["granted"] is False and r["holder"] == "c"
    fleet.round(10)
    assert fleet.client("b").get("/cluster/status", headers=CL).json()["update_lease"]["holder"] == "c"   # replicated
    # canary updates; after 60 s healthy on the new version the lease frees itself and b may go
    run_version(fleet, "c", "2.3.0")
    rounds(fleet, 8, agents=[("c", "2.2.0")])
    assert fleet.hubs["a"].state.lease.view()["holder"] == "c"           # hub is on 2.3.0, its agent is not yet
    rounds(fleet, 3, agents=[("c", "2.3.0")])
    assert fleet.hubs["a"].state.lease.view()["holder"] == "c"           # < 60 s healthy: still held
    rounds(fleet, 4, agents=[("c", "2.3.0")])
    assert fleet.hubs["a"].state.lease.view() is None
    assert ask(fleet, "a", "b")["granted"] is True
    run_version(fleet, "b", "2.3.0")
    assert ask(fleet, "a", "a")["granted"] is False                       # b's lease is still active
    rounds(fleet, 8, agents=[("b", "2.3.0")])
    g = ask(fleet, "a", "a")
    assert g["granted"] is True and g["holder"] == "a"


def test_canary_flag_goes_first_and_release_needs_an_eligible_hub(fleet):
    for n in "abc":
        agent_on(fleet, n, "2.2.0")
    r = fleet.client("b").put("/api/nodes/b/settings", json={"canary": True}, headers=Fleet.ui)   # proxied to leader a
    assert r.status_code == 200 and r.json()["canary"] is True
    assert fleet.client("a").put("/api/nodes/b/settings", json={"canary": "yes"}, headers=Fleet.ui).status_code == 400
    assert ask(fleet, "a", "c")["waiting_for"] == "b"                    # the flagged canary goes before c now
    assert ask(fleet, "a", "b")["granted"] is True
    run_version(fleet, "b", "2.3.0")
    fleet.hubs["b"].state.cluster.eligible = False                       # restarted hub that has not re-joined yet
    fleet.hubs["b"].state.cluster.eligible_since = None
    held = fleet.hubs["a"].state
    held.cluster.probe_all(); held.cluster.probe_all()
    held.lease.tick()
    clock = fleet.clock
    for _ in range(8):
        clock.advance(10)
        agent_on(fleet, "b", "2.3.0")
        held.cluster.probe_all()
        held.lease.tick()
    assert held.lease.view()["holder"] == "b"                            # not eligible: never released
    # with the flag cleared the plain priority order is back (c first)
    assert fleet.client("a").put("/api/nodes/b/settings", json={"canary": False}, headers=Fleet.ui).json()["canary"] is False
    from forge_lease import build_order
    assert build_order([{"id": "a", "priority": 1}, {"id": "b", "priority": 2}, {"id": "c", "priority": 3}], "a") == ["c", "b", "a"]
    assert build_order([{"id": "a", "priority": 1}, {"id": "b", "priority": 2}, {"id": "c", "priority": 3}], "a", {"b"}) == ["b", "c", "a"]


def test_explicit_release_and_ttl_expiry(fleet):
    assert ask(fleet, "a", "c")["granted"] is True
    assert fleet.client("a").delete("/cluster/update-lease/b", headers=CL).json()["released"] is False
    assert ask(fleet, "a", "b")["holder"] == "c"
    assert fleet.client("b").delete("/cluster/update-lease/c", headers=CL).json() == {"ok": True, "released": True}
    assert ask(fleet, "a", "c")["granted"] is True
    for _ in range(7):                                                    # > 10 simulated minutes: lease expires
        fleet.round(100)
    assert fleet.hubs["a"].state.lease.view() is None


def test_unreachable_canary_is_skipped_after_10_minutes(fleet):
    fleet.kill("c")
    for _ in range(3):
        fleet.round(10)
    assert ask(fleet, "a", "b")["waiting_for"] == "c"
    for _ in range(7):
        fleet.round(100)
    assert ask(fleet, "a", "b")["granted"] is True


def test_lone_survivor_can_take_the_lease(fleet):
    fleet.kill("a")
    fleet.kill("c")
    for _ in range(3):
        fleet.round(10)
    for _ in range(7):
        fleet.round(100)
    assert fleet.leaders() == ["b"]
    assert ask(fleet, "b", "b")["granted"] is True      # both others unreachable > 10 min


def test_rollback_marks_version_fleet_bad_and_it_replicates(fleet):
    assert ask(fleet, "a", "c")["granted"] is True
    run_version(fleet, "c", "2.3.0")
    for _ in range(3):
        fleet.round(10)
    run_version(fleet, "c", "2.2.0")                                       # canary rolled back
    for _ in range(3):
        fleet.round(10)
    st = fleet.client("a").get("/cluster/status", headers=CL).json()
    assert st["bad_versions"] == ["2.3.0"] and st["update_lease"] is None
    for _ in range(3):
        fleet.round(10)
    for n in "abc":                                                        # standbys replicate it
        assert fleet.client(n).get("/cluster/status", headers=CL).json()["bad_versions"] == ["2.3.0"]
    r = ask(fleet, "b", "b")
    assert r["granted"] is False and r["reason"] == "fleet-bad"
    ui = fleet.client("a").get("/api/health", headers=Fleet.ui).json()
    assert ui["bad_versions"] == ["2.3.0"] and any(a["kind"] == "fleet_bad_version" for a in ui["alerts"])
    # a newer version is fine; an admin can clear the mark
    assert ask(fleet, "a", "c", "2.3.1")["granted"] is True
    assert fleet.client("b").delete("/cluster/bad-version/2.3.0", headers=CL).json()["cleared"] is True


def test_explicit_bad_version_report_and_validation(fleet):
    r = fleet.client("b").post("/cluster/bad-version", json={"version": "9.9.9", "node_id": "c", "reason": "watchdog rollback"}, headers=CL)
    assert r.json()["bad_versions"] == ["9.9.9"]
    assert fleet.client("a").post("/cluster/update-lease", json={"node_id": "c", "version": "nope"}, headers=CL).status_code == 422
    assert fleet.client("a").post("/cluster/update-lease", json={"node_id": "c", "version": "1.0.0"}, headers={}).status_code == 401
    assert fleet.client("a").get("/cluster/update-lease", headers=CL).json()["bad_versions"] == ["9.9.9"]


def test_lease_survives_leader_failover(fleet):
    assert ask(fleet, "a", "c")["granted"] is True
    for _ in range(2):
        fleet.round(10)
    fleet.kill("a")
    for _ in range(3):
        fleet.round(10)
    assert fleet.leaders() == ["b"]
    r = ask(fleet, "b", "b")
    assert r["granted"] is False and r["holder"] == "c"                   # b inherited the replicated lease
