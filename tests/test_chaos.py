"""Chaos simulation: 4 in-process hubs + 30 fake agents + 500 jobs over 30 simulated minutes with random
leader/standby kills (fresh restart), hub-link cuts and agent crashes, on a fake clock.

Invariants: no job lost without a terminal status; no job held by two agents at once; the cluster is never
leaderless for >= 60 simulated seconds; all hubs converge on the same job table.
Time is scaled: one round = 40 simulated seconds (two hub ticks), ONLINE_SECS widened to 60 so agents stay online at
that cadence. Agents may also sleep (laptop lid) and resume still running their jobs: the hub must fence them."""
import random

import pytest

import app as app_mod
import forge_phase2
from cluster_net import Fleet

STEP = 40
CHAOS_ROUNDS = 45                     # 30 simulated minutes
N_AGENTS, N_JOBS = 30, 500
TERMINAL = {"done", "failed", "cancelled"}
UI, AGENT = Fleet.ui, Fleet.agent


class SimAgent:
    def __init__(self, i: int, rng: random.Random):
        self.id, self.rng = f"agent{i:02d}", rng
        self.running: dict[str, float] = {}       # job id -> finish time
        self.attempt: dict[str, int] = {}
        self.paused = False
        self.down_until = 0.0
        self.boot = True

    def crash(self, now: float, rounds: int) -> None:
        self.running.clear()                      # the process died: its jobs are gone, hub must notice
        self.down_until, self.boot = now + rounds * STEP, True

    def pause(self, now: float, rounds: int) -> None:
        """Laptop sleep: silent for a while, then resumes with its jobs still running (no boot)."""
        self.down_until = now + rounds * STEP
        self.paused = True


class Sim:
    def __init__(self, fleet: Fleet, seed: int = 20260101):
        self.f, self.rng = fleet, random.Random(seed)
        self.agents = [SimAgent(i, self.rng) for i in range(N_AGENTS)]
        self.created: list[str] = []
        self.to_create = N_JOBS
        self.held: dict[str, str] = {}            # job id -> agent currently running it
        self.dispatches: dict[str, int] = {}
        self.violations: list[str] = []
        self.fenced = 0
        self.pauses = True
        self.done_ok: dict[str, int] = {}         # job id -> times the hub ACCEPTED a 'done' for it
        self.dead_hubs: dict[str, float] = {}     # hub -> restart time
        self.cuts: list[tuple[frozenset, float]] = []
        self.leaderless = self.max_leaderless = 0
        self.leaders_seen: set[str] = set()

    # ---- helpers
    def hub_order(self):
        return [n for n in self.f.names if n not in self.f.net.down]      # agents try hubs in priority order

    def post(self, path, json, headers):
        for n in self.hub_order()[:2]:                                     # first live hub, then the next
            r = self.f.client(n).post(path, json=json, headers=headers)
            if r.status_code < 500:
                return r
        return None

    # ---- one simulated round
    def faults(self, rnd: int, chaos: bool) -> None:
        now = self.f.clock.t
        for n, back in list(self.dead_hubs.items()):
            if now >= back:
                self.f.start(n)                                            # comes back with an empty database
                for _ in range(3):                                         # real hubs tick every 3 s: it sees its peers
                    self.f.round(3)                                        # (and the claimed leader) before SOLO_WAIT
                del self.dead_hubs[n]
        self.cuts = [(c, t) for c, t in self.cuts if now < t]
        self.f.net.cut = {c for c, _ in self.cuts}
        if not chaos:
            return
        live = self.f.live()
        if len(live) >= 3 and self.rng.random() < 0.09 and self.f.leaders():
            victim = self.f.leaders()[0]                                  # kill the leader
            self.f.kill(victim)
            self.dead_hubs[victim] = now + self.rng.randint(2, 4) * STEP
        elif len(live) >= 3 and self.rng.random() < 0.05:
            victim = self.rng.choice(live)
            self.f.kill(victim)
            self.dead_hubs[victim] = now + self.rng.randint(1, 3) * STEP
        if self.rng.random() < 0.08 and self.f.leaders():
            # A one-round partition between two hubs, neither of which outranks the leader. (A cut between the leader
            # and a HIGHER-priority hub creates two leaders that both accept writes; Phase 1 has no quorum, so those
            # writes can be lost on heal. That is a documented gap, not something this test pins down.)
            rank = self.f.names.index(self.f.leaders()[0])
            ok = [n for n in self.f.live() if self.f.names.index(n) >= rank]
            if len(ok) >= 2:
                self.cuts.append((frozenset(self.rng.sample(ok, 2)), now + STEP))
                self.f.net.cut = {c for c, _ in self.cuts}
        for a in self.agents:
            if a.down_until <= now and self.rng.random() < 0.02:
                a.crash(now, self.rng.randint(1, 4))
            elif a.down_until <= now and self.pauses and self.rng.random() < 0.02:
                a.pause(now, self.rng.randint(1, 4))

    def submit(self, rnd: int, chaos: bool) -> None:
        want = min(self.to_create, round(N_JOBS / CHAOS_ROUNDS * 1.05) if chaos else 25)
        for _ in range(want):
            kind = self.rng.random()
            body = {"node_id": "auto", "prompt": "work", "engine": "shell" if kind < 0.6 else "claude" if kind < 0.8 else "any"}
            r = self.post("/api/jobs", body, UI)
            if r is not None and r.status_code == 200:
                self.created.append(r.json()["id"])
                self.to_create -= 1

    def agents_step(self) -> None:
        now = self.f.clock.t
        for a in self.agents:
            if a.down_until > now:
                continue
            for jid, fin in list(a.running.items()):
                if fin <= now:
                    r = self.post(f"/api/agent/jobs/{jid}/update", {"status": "done", "exit_code": 0, "append": "ok",
                                                                    "node_id": a.id, "attempt": a.attempt.get(jid, 0)}, AGENT)
                    if r is not None and r.status_code in (200, 409):      # 409: the hub gave it to someone else
                        if r.status_code == 200:
                            self.done_ok[jid] = self.done_ok.get(jid, 0) + 1
                        del a.running[jid]
                        if self.held.get(jid) == a.id:
                            self.held.pop(jid, None)
                        if r.status_code == 409:
                            self.fenced += 1
            body = {"node_id": a.id, "metrics": {"cpu": self.rng.randint(1, 90)}, "capacity": 2 - len(a.running),
                    "info": {"tools": ["claude", "codex"], "agent_version": "2.2.0"}, "boot": a.boot}
            r = self.post("/api/agent/report", body, AGENT)
            if r is None or r.status_code != 200:
                continue
            a.boot = False
            # a sleeper counts as awake only once a hub hears from it again (until then it cannot be told to stop)
            a.paused = False
            for j in r.json()["jobs"]:
                jid = j["id"]
                if jid in self.held and self.held[jid] != a.id and jid in self.agent(self.held[jid]).running \
                        and not self.agent(self.held[jid]).paused:
                    # (a holder that is asleep is indistinguishable from a dead one; it must be fenced when it wakes,
                    # which the done-accepted-once invariant checks)
                    self.violations.append(f"{jid} dispatched to {a.id} while {self.held[jid]} still runs it")
                self.dispatches[jid] = self.dispatches.get(jid, 0) + 1
                self.held[jid] = a.id
                a.running[jid] = now + self.rng.randint(20, 150)
                a.attempt[jid] = j.get("attempt", 0)
            for jid in r.json().get("cancel") or []:          # hub fenced us off this job: kill our copy
                if jid in a.running:
                    del a.running[jid]
                    if self.held.get(jid) == a.id:
                        self.held.pop(jid, None)

    def agent(self, aid: str) -> SimAgent:
        return self.agents[int(aid[5:])]

    def round(self, rnd: int, chaos: bool = True) -> None:
        self.faults(rnd, chaos)
        self.submit(rnd, chaos)
        self.agents_step()
        # hubs tick every 3 s in production; two ticks per 40 s round (37 s + 3 s) keep hub-to-hub views about as fresh
        # relative to agent traffic as they really are (a single tick made every peer status a whole round stale)
        self.f.round(STEP - 3)
        self.f.round(3)
        if self.f.leaders():
            self.leaderless = 0
            self.leaders_seen |= set(self.f.leaders())
        else:
            self.leaderless += STEP
            self.max_leaderless = max(self.max_leaderless, self.leaderless)

    def jobs_table(self, hub: str):
        return {r["id"]: (r["status"], r["node_id"]) for r in self.f.hubs[hub].state.store.q("SELECT id,status,node_id FROM jobs")}


def test_chaos_simulation_invariants(tmp_path, monkeypatch, request):
    monkeypatch.setattr(app_mod, "ONLINE_SECS", 60)
    monkeypatch.setattr(forge_phase2, "ONLINE_SECS", 60)
    fleet = Fleet(tmp_path, names=("a", "b", "c", "d"), fast=True)
    request.addfinalizer(fleet.close)
    fleet.converge(6)
    assert fleet.leaders() == ["a"]
    sim = Sim(fleet)

    for rnd in range(CHAOS_ROUNDS):                       # 30 simulated minutes of faults
        sim.round(rnd)
    sim.faults(CHAOS_ROUNDS, chaos=False)

    # heal everything, then let the system drain
    for n in list(sim.dead_hubs):
        sim.dead_hubs[n] = 0
    sim.cuts.clear()
    for a in sim.agents:
        a.down_until = 0
    for rnd in range(60):
        sim.round(CHAOS_ROUNDS + rnd, chaos=False)
        if not sim.to_create and rnd > 3 and len(sim.created) == N_JOBS:
            leader = fleet.leaders()[0] if fleet.leaders() else None
            if leader and all(s in TERMINAL for s, _ in sim.jobs_table(leader).values()) and not any(a.running for a in sim.agents):
                break
    for _ in range(4):                                    # quiet rounds so replication settles
        sim.round(0, chaos=False)

    # ---- invariants
    assert sim.violations == []                                              # no job on two agents at once
    assert all(n == 1 for n in sim.done_ok.values()), "a job's completion was accepted twice"
    assert sim.fenced > 0, "chaos never produced a stale holder (sleeping agent) to fence"
    assert len(sim.created) == N_JOBS
    assert fleet.leaders() and len(fleet.leaders()) == 1
    assert sim.max_leaderless < 60, f"leaderless for {sim.max_leaderless}s simulated"
    leader = fleet.leaders()[0]
    table = sim.jobs_table(leader)
    missing = [j for j in sim.created if j not in table]
    assert not missing, f"jobs lost (absent from the leader): {missing[:3]}"
    stuck = {j: table[j][0] for j in sim.created if table[j][0] not in TERMINAL}
    assert not stuck, f"jobs without a terminal status: {dict(list(stuck.items())[:3])}"
    for n in fleet.live():                                                   # all hubs converge
        assert sim.jobs_table(n) == table, f"hub {n} diverged"
    attempts = {r["id"]: r["attempts"] or 0 for r in fleet.hubs[leader].state.store.q("SELECT id,attempts FROM jobs")}
    assert all(sim.dispatches.get(j, 0) <= 1 + attempts[j] for j in sim.created if j in sim.dispatches)
    done = sum(1 for s, _ in table.values() if s == "done")
    assert done >= N_JOBS * 0.6, f"only {done} of {N_JOBS} jobs completed"
    assert len(sim.leaders_seen) >= 2, "chaos never forced a leader change"


# ---------------------------------------------------------------- partition safety (2.2.1)
def _jobs(fleet, hub):
    return {r["id"]: (r["status"], r["prompt"]) for r in fleet.hubs[hub].state.store.q("SELECT id,status,prompt FROM jobs")}


def test_partition_then_heal_keeps_writes_from_both_leaders(tmp_path, request):
    """Leader a is cut off from b and c: b leads the other side. Both accept writes. After the heal a stays leader,
    merges b's rows (last-write-wins), b steps down; no job created or changed on either side is lost."""
    f = Fleet(tmp_path, names=("a", "b", "c"), fast=True)
    request.addfinalizer(f.close)
    f.converge(6)
    assert f.leaders() == ["a"]
    pre = [f.client("a").post("/api/jobs", json={"node_id": "auto", "engine": "shell", "prompt": f"pre{i}"}, headers=UI).json()["id"]
           for i in range(4)]
    f.converge(3)
    f.net.cut = {frozenset(("a", "b")), frozenset(("a", "c"))}
    f.converge(4)
    assert sorted(f.leaders()) == ["a", "b"]                         # split brain (no quorum in Phase 1)
    side_a = [f.client("a").post("/api/jobs", json={"node_id": "auto", "engine": "shell", "prompt": f"A{i}"}, headers=UI).json()["id"]
              for i in range(3)]
    side_b = [f.client("b").post("/api/jobs", json={"node_id": "auto", "engine": "shell", "prompt": f"B{i}"}, headers=UI).json()["id"]
              for i in range(3)]
    f.client("b").post(f"/api/jobs/{pre[0]}/cancel", headers=UI)      # a change made only on b's side
    f.client("a").post(f"/api/jobs/{pre[1]}/cancel", headers=UI)      # and one only on a's side
    f.converge(3)
    f.net.cut = set()                                                  # heal
    f.converge(15)
    assert f.leaders() == ["a"]
    assert f.hubs["a"].state.cluster.merges >= 1
    for n in "abc":
        t = _jobs(f, n)
        missing = [j for j in pre + side_a + side_b if j not in t]
        assert not missing, f"hub {n} lost {missing}"
        assert t[pre[0]][0] == "cancelled" and t[pre[1]][0] == "cancelled", f"hub {n}: {t[pre[0]]}, {t[pre[1]]}"
    assert _jobs(f, "a") == _jobs(f, "b") == _jobs(f, "c")


def test_leader_change_resets_sync_cursor_and_new_leader_merges_every_peer(tmp_path):
    f = Fleet(tmp_path, names=("a", "b", "c"))
    f.converge(6)
    b, c = f.hubs["b"].state.cluster, f.hubs["c"].state.cluster
    assert b.sync_cursor is not None and c.sync_cursor is not None
    # a row only c has (e.g. written while c was briefly a partitioned leader)
    f.hubs["c"].state.store.x("INSERT INTO jobs(id,node_id,engine,prompt,status,created,updated) VALUES(?,?,?,?,?,?,?)",
                              ("zzonlyonc", "auto", "shell", "orphan", "cancelled", f.clock(), f.clock()))
    f.kill("a")
    f.converge(3)
    assert f.leaders() == ["b"]
    assert b.merges >= 1 and f.hubs["b"].state.store.q("SELECT 1 FROM jobs WHERE id='zzonlyonc'")
    f.converge(2)
    assert c.sync_cursor is not None and c.leader_id == "b"


@pytest.mark.parametrize("skew", [30.0, -30.0])
def test_chaos_with_one_hub_clock_skewed(tmp_path, monkeypatch, request, skew):
    """One hub's clock runs 30 s ahead/behind. Leader kills + agent faults; invariant: no job lost, hubs converge."""
    monkeypatch.setattr(app_mod, "ONLINE_SECS", 60)
    monkeypatch.setattr(forge_phase2, "ONLINE_SECS", 60)
    fleet = Fleet(tmp_path, names=("a", "b", "c"), fast=True, skew={"b": skew})
    request.addfinalizer(fleet.close)
    fleet.converge(6)
    sim = Sim(fleet, seed=4242 + int(skew))
    sim.to_create = 150
    for rnd in range(15):
        sim.round(rnd)
    sim.faults(15, chaos=False)
    for n in list(sim.dead_hubs):
        sim.dead_hubs[n] = 0
    sim.cuts.clear()
    for a in sim.agents:
        a.down_until = 0
    for rnd in range(40):
        sim.round(15 + rnd, chaos=False)
        leader = fleet.leaders()[0] if len(fleet.leaders()) == 1 else None
        if not sim.to_create and leader and all(s in TERMINAL for s, _ in sim.jobs_table(leader).values()) \
                and not any(a.running for a in sim.agents):
            break
    for _ in range(4):
        sim.round(0, chaos=False)
    assert len(fleet.leaders()) == 1
    leader = fleet.leaders()[0]
    table = sim.jobs_table(leader)
    assert len(sim.created) == 150
    missing = [j for j in sim.created if j not in table]
    assert not missing, f"jobs lost: {missing[:3]}"
    stuck = {j: table[j][0] for j in sim.created if table[j][0] not in TERMINAL}
    assert not stuck, f"jobs without a terminal status: {dict(list(stuck.items())[:3])}"
    for n in fleet.live():
        assert sim.jobs_table(n) == table, f"hub {n} diverged"
    assert sim.violations == [] and all(n == 1 for n in sim.done_ok.values())


def test_contested_leader_hands_out_and_fences_nothing_but_honours_boot(tmp_path, request):
    """Only the a-c and b-c links are cut: c sees only d, elects itself, but d still names the better leader a.
    c is 'contested': it must not dispatch or fence from its possibly stale copy, yet it still fails the jobs of an
    agent that says it rebooted. a (the better side) is not contested and keeps working."""
    f = Fleet(tmp_path, names=("a", "b", "c", "d"), fast=True)
    request.addfinalizer(f.close)
    f.converge(6)
    agent = {"node_id": "w", "metrics": {}, "capacity": 2, "info": {"tools": ["claude"], "agent_version": "2.2.0"}}
    f.client("a").post("/api/agent/report", json=agent, headers=AGENT)
    run = f.client("a").post("/api/jobs", json={"node_id": "w", "engine": "shell", "prompt": "r"}, headers=UI).json()["id"]
    assert [j["id"] for j in f.client("a").post("/api/agent/report", json=agent, headers=AGENT).json()["jobs"]] == [run]
    queued = f.client("a").post("/api/jobs", json={"node_id": "w", "engine": "shell", "prompt": "q"}, headers=UI).json()["id"]
    f.converge(2)
    f.net.cut = {frozenset(("a", "c")), frozenset(("b", "c"))}
    f.converge(3)
    c = f.hubs["c"].state.cluster
    assert c.is_leader and c.contested() and not f.hubs["a"].state.cluster.contested()
    r = f.client("c").post("/api/agent/report", json=agent | {"capacity": 5}, headers=AGENT)
    assert r.status_code == 200 and r.json()["jobs"] == [] and r.json()["cancel"] == []
    assert f.client("c").post(f"/api/agent/jobs/{run}/update", json={"status": "done", "exit_code": 0}, headers=AGENT).status_code == 503
    r = f.client("c").post("/api/agent/report", json=agent | {"boot": True}, headers=AGENT)
    assert r.status_code == 200
    st = {x["id"]: x["status"] for x in f.hubs["c"].state.store.q("SELECT id,status FROM jobs")}
    assert st[run] == "failed" and st[queued] == "queued"
    # heal: c steps down, a merges the boot failure, everyone converges
    f.net.cut = set()
    f.converge(12)
    assert f.leaders() == ["a"]
    for n in "abcd":
        st = {x["id"]: x["status"] for x in f.hubs[n].state.store.q("SELECT id,status FROM jobs")}
        assert st[run] == "failed" and st[queued] == "queued", n
    status = f.client("c").get("/cluster/status", headers=Fleet.cluster).json()
    assert status["stepdown_seq"] >= 1
    assert f.client("a").get("/cluster/status", headers=Fleet.cluster).json()["merged"].get("c", 0) >= status["stepdown_seq"]


# ---------------------------------------------------------------- 2.3 R4: job output chunks under failover / partition
def _outputs(fleet, hub):
    st = fleet.hubs[hub].state.store
    return {r["id"]: st.job_output_text(r["id"]) for r in st.q("SELECT id FROM jobs")}


def test_job_output_chunks_survive_failover_and_partition_heal(tmp_path, request):
    """Output appended through the old leader, then through the new one while the old one is cut off, then both sides
    after the heal: every hub ends with the same concatenated output and no chunk is lost or duplicated."""
    f = Fleet(tmp_path, names=("a", "b", "c"), fast=True)
    request.addfinalizer(f.close)
    f.converge(6)
    beat = {"node_id": "w", "capacity": 2, "metrics": {}}
    f.client("a").post("/api/agent/report", headers=AGENT, json=beat)
    jid = f.client("a").post("/api/jobs", json={"node_id": "w", "engine": "shell", "prompt": "p"}, headers=UI).json()["id"]
    f.client("a").post("/api/agent/report", headers=AGENT, json=beat)
    f.client("a").post(f"/api/agent/jobs/{jid}/update", headers=AGENT, json={"status": "running", "append": "one "})
    f.converge(2)
    f.kill("a")                                                       # leader dies: b takes over
    f.converge(4)
    assert f.leaders() == ["b"]
    f.client("b").post("/api/agent/report", headers=AGENT, json=beat)
    f.client("b").post(f"/api/agent/jobs/{jid}/update", headers=AGENT, json={"append": "two "})
    f.converge(2)
    f.net.down.discard("a")                                           # a comes back (same database) and leads again
    f.converge(15)
    assert f.leaders() == ["a"]
    f.client("a").post(f"/api/agent/jobs/{jid}/update", headers=AGENT, json={"status": "done", "exit_code": 0, "append": "three"})
    f.converge(4)
    outs = [_outputs(f, n)[jid] for n in "abc"]
    assert outs == ["one two three"] * 3, outs
    seqs = {n: [r["seq"] for r in f.hubs[n].state.store.q("SELECT seq FROM job_output WHERE job_id=? ORDER BY seq", (jid,))]
            for n in "abc"}
    assert seqs["a"] == seqs["b"] == seqs["c"] == [1, 2, 3]
