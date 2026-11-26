"""Staggered rollout: the cluster-wide update lease (leader-side, state in the replicated `leases` and
`bad_versions` tables so a new leader inherits it).

Order: peers flagged `canary` (node settings, PUT /api/nodes/{id}/settings {"canary": true}) first, then the other
non-leader peers by DESCENDING priority number (lowest priority first), leader LAST. With no canary flag set this is
the plain priority order.
A peer may take the lease only when every earlier peer already runs the target version, or has been
unreachable for > 10 min (skipped). One holder at a time; TTL 10 min; auto-released once, for 60 s straight, the
holder's hub is healthy, reports `running_version >= version` and `eligible` in /cluster/status, AND that machine's
agent reports a `bundle_version >= version` (and is online). A holder that reached the version and then went back
below it is a rollback: the version becomes fleet-bad."""
from __future__ import annotations

import json
from typing import Callable, Optional

from forge_sched import parse_version
from forge_store import Clock, Store

LEASE_TTL = 600.0
HEALTHY_RELEASE_S = 60.0
SKIP_DOWN_S = 600.0
RETRY_S = 30.0
LEASE_ID = "lease"
AGENT_ONLINE_S = 30.0


def build_order(peers: list[dict], leader_id: Optional[str], canaries: Optional[set] = None) -> list[str]:
    """peers: [{id, priority}] -> ids: flagged canaries first, then by descending priority number, leader last."""
    canaries = canaries or set()
    rest = sorted((p for p in peers if p["id"] != leader_id),
                  key=lambda p: (p["id"] not in canaries, -(p["priority"] or 0), p["id"]))
    ids = [p["id"] for p in rest]
    if leader_id and any(p["id"] == leader_id for p in peers):
        ids.append(leader_id)
    return ids


def decide(requester: str, version: str, order: list[str], running: dict[str, Optional[str]],
           down_for: dict[str, float], lease: Optional[dict], bad: set, now: float) -> dict:
    """Pure grant decision. `lease` = active lease {node_id, version, expires} or None; `running[id]` = last known
    running_version; `down_for[id]` = seconds unreachable (0 if healthy)."""
    if version in bad:
        return {"granted": False, "holder": None, "retry_after_s": 300, "reason": "fleet-bad"}
    if requester not in order:
        return {"granted": False, "holder": None, "retry_after_s": RETRY_S, "reason": "unknown peer"}
    if lease and lease.get("node_id") and lease["expires"] > now and lease["node_id"] != requester:
        remaining = lease["expires"] - now
        return {"granted": False, "holder": lease["node_id"], "retry_after_s": int(min(RETRY_S, max(5.0, remaining))),
                "reason": "lease held"}
    target = parse_version(version)
    for pid in order[:order.index(requester)]:
        done = running.get(pid) is not None and parse_version(running[pid]) >= target
        if done or down_for.get(pid, 0.0) >= SKIP_DOWN_S:
            continue
        return {"granted": False, "holder": None, "retry_after_s": int(RETRY_S), "reason": "waiting for earlier node",
                "waiting_for": pid}
    return {"granted": True, "holder": requester, "retry_after_s": 0}


class UpdateLease:
    def __init__(self, store: Store, cluster, clock: Clock, running_version: Callable[[], str]):
        self.store, self.cluster, self.clock, self._running = store, cluster, clock, running_version
        self.last_running: dict[str, Optional[str]] = {}
        self.down_since: dict[str, float] = {}
        self.ok_since: dict[str, float] = {}

    # ---- state access
    def bad_versions(self) -> list[str]:
        return [r["version"] for r in self.store.q("SELECT version FROM bad_versions ORDER BY ts")]

    def current(self, now: Optional[float] = None) -> Optional[dict]:
        now = self.clock() if now is None else now
        r = self.store.q("SELECT * FROM leases WHERE id=?", (LEASE_ID,))
        if r and r[0]["node_id"] and (r[0]["expires"] or 0) > now:
            return dict(r[0])
        return None

    def view(self) -> Optional[dict]:
        now = self.clock()
        c = self.current(now)
        return {"holder": c["node_id"], "version": c["version"], "expires_in_s": int(c["expires"] - now)} if c else None

    def _release(self, why: str) -> None:
        r = self.current()
        if r:
            self.store.touch("leases", "id", LEASE_ID, "node_id=NULL, expires=0")
            self.store.event(r["node_id"], "info", f"update lease released ({why})")

    def mark_bad(self, version: str, reason: str) -> None:
        if version in self.bad_versions():
            return
        now = self.clock()
        self.store.x("INSERT OR REPLACE INTO bad_versions(version,ts,reason,updated) VALUES(?,?,?,?)",
                     (version, now, reason, now))
        self.store.event(self.cluster.id, "error", f"version {version} marked fleet-bad: {reason}")

    def clear_bad(self, version: str) -> bool:
        if version not in self.bad_versions():
            return False
        self.store.delete_row("bad_versions", version)
        return True

    # ---- observation (leader)
    def observe(self) -> None:
        now, c = self.clock(), self.cluster
        with c.lock:
            for p in c._others():
                s = c.ps.get(p["id"])
                if s is not None and s.healthy:
                    self.last_running[p["id"]] = s.status.get("running_version") or self.last_running.get(p["id"])
                    self.down_since.pop(p["id"], None)
                else:
                    self.down_since.setdefault(p["id"], s.last_ok if s is not None and s.last_ok else now)
        self.last_running[c.id] = self._running()
        self.down_since.pop(c.id, None)

    def _healthy(self, pid: str) -> bool:
        if pid == self.cluster.id:
            return True
        s = self.cluster.ps.get(pid)
        return bool(s and s.healthy)

    def _eligible(self, pid: str) -> bool:
        if pid == self.cluster.id:
            return bool(self.cluster.eligible)
        s = self.cluster.ps.get(pid)
        return bool(s and s.healthy and s.status.get("eligible"))

    def canaries(self) -> set:
        return {r["node_id"] for r in self.store.q("SELECT node_id FROM settings WHERE canary=1")}

    def agent_version(self, nid: str, now: float) -> Optional[str]:
        """bundle_version reported by that machine's (user-lane) agent, None if it is not reporting."""
        r = self.store.q("SELECT last_seen,metrics,info FROM nodes WHERE id=?", (nid,))
        if not r or now - (r[0]["last_seen"] or 0) >= AGENT_ONLINE_S:
            return None
        try:
            m, info = json.loads(r[0]["metrics"] or "{}"), json.loads(r[0]["info"] or "{}")
        except ValueError:
            return None
        v = (m.get("bundle_version") if isinstance(m, dict) else None) or (info.get("bundle_version") if isinstance(info, dict) else None)
        return str(v) if v else None

    def tick(self) -> None:
        """Leader duty: expire / auto-release the lease, detect a canary rollback."""
        self.observe()
        now = self.clock()
        r = self.store.q("SELECT * FROM leases WHERE id=?", (LEASE_ID,))
        if not r or not r[0]["node_id"]:
            return
        r = r[0]
        if (r["expires"] or 0) <= now:
            self._release("expired")
            return
        holder, ver = r["node_id"], r["version"]
        run = self.last_running.get(holder)
        healthy = self._healthy(holder)
        if healthy and run and parse_version(run) >= parse_version(ver):
            if not r["reached"]:
                self.store.touch("leases", "id", LEASE_ID, "reached=1")
            av = self.agent_version(holder, now)
            if not (self._eligible(holder) and av and parse_version(av) >= parse_version(ver)):
                self.ok_since.pop(holder, None)          # hub is up but not yet a full member / agent not on it yet
                return
            since = self.ok_since.setdefault(holder, now)
            if now - since >= HEALTHY_RELEASE_S:
                self._release(f"{holder} healthy on {ver}")
                self.ok_since.pop(holder, None)
        else:
            self.ok_since.pop(holder, None)
            if r["reached"] and healthy and run and parse_version(run) < parse_version(ver):
                self.mark_bad(ver, f"{holder} rolled back from {ver} to {run}")
                self._release("rollback")

    # ---- endpoints (leader)
    def request(self, node_id: str, version: str) -> dict:
        self.observe()
        now, c = self.clock(), self.cluster
        node_id = node_id.strip().lower()
        order = build_order(c.peers(), c.leader_id, self.canaries())
        down = {pid: now - t for pid, t in self.down_since.items()}
        out = decide(node_id, version, order, self.last_running, down, self.current(now), set(self.bad_versions()), now)
        if out["granted"]:
            cur = self.store.q("SELECT * FROM leases WHERE id=?", (LEASE_ID,))
            same = bool(cur and cur[0]["node_id"] == node_id and cur[0]["version"] == version and
                        (cur[0]["expires"] or 0) > now)
            granted_at = cur[0]["granted"] if same else now
            reached = cur[0]["reached"] if same else 0
            self.store.x("INSERT OR REPLACE INTO leases(id,node_id,version,granted,expires,reached,updated) "
                         "VALUES(?,?,?,?,?,?,?)", (LEASE_ID, node_id, version, granted_at, now + LEASE_TTL, reached, now))
            if not same:
                self.store.event(node_id, "info", f"update lease granted to {node_id} for {version}")
                self.ok_since.pop(node_id, None)
        return out

    def release(self, node_id: str) -> bool:
        r = self.current()
        if r and r["node_id"] == node_id.strip().lower():
            self._release(f"released by {node_id}")
            return True
        return False
