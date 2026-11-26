"""Leader/standby clustering: peer probing, election, pull replication, request forwarding."""
from __future__ import annotations

import json
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

import httpx

from forge_bundle import Releases
from forge_store import REPLICATED, Clock, Store, jloads

PROBE_TIMEOUT = 2.0
FORWARD_TIMEOUT = 10.0
SOLO_WAIT = 15.0          # no healthy leader seen for this long after start -> self-elect eligible
HOLD_DOWN = 20.0          # a lower-priority peer must be eligible this long before it takes over
SYNC_PAGE = 2000
LAST_OK_WRITE_EVERY = 30.0
MERGE_RIVAL_EVERY = 10.0  # leader: merge-pull from a peer that still claims leadership at most this often
FORCE_WAIT_MAX = 60.0     # demoted hub: force-resync anyway if the leader has not confirmed merging us by then
HOP_BY_HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "upgrade",
              "te", "trailer", "proxy-authorization", "proxy-authenticate", "x-forwarded-for"}

ClientFactory = Callable[[], httpx.Client]


class PeerState:
    """Local, in-memory observation of one remote hub."""

    def __init__(self) -> None:
        self.probes: deque = deque(maxlen=2)
        self.url: Optional[str] = None
        self.status: dict = {}
        self.eligible_since: Optional[float] = None
        self.last_ok: Optional[float] = None

    @property
    def healthy(self) -> bool:
        return len(self.probes) == 2 and all(self.probes)


class Cluster:
    def __init__(self, store: Store, cfg: dict, node_id: str, clock: Clock, client_factory: ClientFactory,
                 releases: Releases):
        self.store, self.cfg, self.id, self.clock = store, cfg, node_id, clock
        self.releases = releases
        self._factory, self._client = client_factory, None
        self.lock = threading.RLock()
        self.started = clock()
        self.ps: dict[str, PeerState] = {}
        self.eligible = False
        self.eligible_since: Optional[float] = None
        self.leader_id: Optional[str] = None
        self.leader_since: Optional[float] = None
        self.sync_cursor: Optional[float] = None
        self._force_sync = False      # after stepping down: re-pull everything and let the leader's rows win
        self._last_peer_write: dict[str, float] = {}
        self._merge_pending: set[str] = set()     # peers to merge-pull from (leader side; see tick)
        self._last_merge: dict[str, float] = {}
        self.merges = 0                           # merge pulls done (tests/diagnostics)
        self.stepdown_seq = 0                     # bumped each time we stop being leader
        self.merged_seq: dict[str, int] = {}      # leader: peer -> its stepdown_seq covered by our last merge of it
        self._force_wait_since: Optional[float] = None
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="probe")
        self._seed(cfg.get("seed_peers") or [])
        self._ensure_self()
        if not self._others():           # alone in the cluster: leader right away
            self._set_eligible(self.started)
            self.leader_id, self.leader_since = self.id, self.started

    # ------------------------------------------------------------ peers table
    def peers(self) -> list[dict]:
        rows = [dict(r) | {"urls": jloads(r["urls"], [])} for r in self.store.q("SELECT * FROM peers")]
        return sorted(rows, key=lambda r: (r["priority"] if r["priority"] is not None else 999, r["id"]))

    def _others(self) -> list[dict]:
        return [p for p in self.peers() if p["id"] != self.id]

    def priority_of(self, nid: str) -> int:
        r = self.store.q("SELECT priority FROM peers WHERE id=?", (nid,))
        return r[0]["priority"] if r and r[0]["priority"] is not None else 999

    def _seed(self, seeds: list) -> None:
        for s in seeds:
            pid = str(s.get("id") or "").lower()
            if pid and not self.store.q("SELECT 1 FROM peers WHERE id=?", (pid,)):
                self.store.x("INSERT INTO peers(id,priority,urls,updated) VALUES(?,?,?,?)",
                             (pid, int(s.get("priority") or 99), json.dumps(s.get("urls") or []), 0.001))

    def _ensure_self(self) -> None:
        if not self.store.q("SELECT 1 FROM peers WHERE id=?", (self.id,)):
            top = self.store.q("SELECT MAX(priority) m FROM peers")[0]["m"] or 0
            self.store.x("INSERT INTO peers(id,priority,urls,updated) VALUES(?,?,?,?)",
                         (self.id, top + 1, "[]", 0.001))

    def join(self, pid: str, urls: list[str]) -> dict:
        """Upsert a peer; priority = existing or max+1. Must run on the leader."""
        pid = pid.strip().lower()
        row = self.store.q("SELECT priority FROM peers WHERE id=?", (pid,))
        if row:
            self.store.touch("peers", "id", pid, "urls=?", (json.dumps(urls),))
            prio = row[0]["priority"]
        else:
            prio = (self.store.q("SELECT MAX(priority) m FROM peers")[0]["m"] or 0) + 1
            self.store.x("INSERT INTO peers(id,priority,urls,updated) VALUES(?,?,?,?)",
                         (pid, prio, json.dumps(urls), self.clock()))
            self.store.event(pid, "ok", f"hub peer {pid} joined (priority {prio})")
        return {"id": pid, "priority": prio, "urls": urls}

    def hub_urls(self) -> list[str]:
        seen: list[str] = []
        for p in self.peers():
            for u in p["urls"]:
                if u not in seen:
                    seen.append(u)
        return seen

    # ------------------------------------------------------------ state
    def _set_eligible(self, now: float) -> None:
        if not self.eligible:
            self.eligible, self.eligible_since = True, now

    @property
    def is_leader(self) -> bool:
        return self.leader_id == self.id

    @property
    def role(self) -> str:
        if self.is_leader:
            return "leader"
        return "standby" if self.eligible else "starting"

    def status(self) -> dict:
        version, sha = self.releases.latest()
        with self.lock:
            merged = dict(self.merged_seq)
        return {"node_id": self.id, "role": self.role, "leader_id": self.leader_id, "eligible": self.eligible,
                "version": version, "running_version": self.releases.running_version, "bundle_sha256": sha,
                "sync_cursor": self.sync_cursor if not self.is_leader else self.clock(), "now": self.clock(),
                # step-down handshake (clock-free): we bump stepdown_seq when we stop leading; a leader that merge-pulled
                # us publishes the seq it saw in merged[our id]; only then do we let its copy overwrite ours
                "stepdown_seq": self.stepdown_seq, "merged": merged}

    # ------------------------------------------------------------ http
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = self._factory()
        return self._client

    def _hdrs(self) -> dict:
        return {"X-Cluster-Token": self.cfg["cluster_token"]}

    def _probe(self, p: dict) -> tuple[Optional[str], dict]:
        for url in p["urls"]:
            try:
                r = self.client().get(url.rstrip("/") + "/cluster/status", headers=self._hdrs(), timeout=PROBE_TIMEOUT)
                st = r.json() if r.status_code == 200 else None
            except (httpx.HTTPError, ValueError):
                continue
            if isinstance(st, dict) and st.get("node_id") == p["id"]:
                return url, st
        return None, {}

    def probe_all(self) -> None:
        others = self._others()
        results = list(self._pool.map(self._probe, others)) if len(others) > 1 else [self._probe(p) for p in others]
        now = self.clock()
        with self.lock:
            live = {p["id"] for p in others}
            for gone in [k for k in self.ps if k not in live]:
                del self.ps[gone]
            for p, (url, st) in zip(others, results):
                s = self.ps.setdefault(p["id"], PeerState())
                s.probes.append(url is not None)
                if url is not None:
                    if s.status.get("role") == "leader" and st.get("role") != "leader":
                        self._merge_pending.add(p["id"])       # it stepped down: rescue what it wrote while leading
                    s.url, s.status, s.last_ok = url, st, now
                else:
                    s.status = {}
                if s.healthy and st.get("eligible"):
                    s.eligible_since = s.eligible_since if s.eligible_since is not None else now
                else:
                    s.eligible_since = None

    # ------------------------------------------------------------ election
    def compute_leader(self, now: float) -> Optional[str]:
        with self.lock:
            cand: dict[str, int] = {}
            if self.eligible:
                cand[self.id] = self.priority_of(self.id)
            for pid, s in self.ps.items():
                if s.healthy and s.status.get("eligible"):
                    cand[pid] = self.priority_of(pid)
            if not cand:
                return None

            def key(i: str) -> tuple:
                return (cand[i], i)

            def since(i: str) -> Optional[float]:
                return self.eligible_since if i == self.id else self.ps[i].eligible_since

            best = min(cand, key=key)
            cur = self.leader_id
            if cur in cand and best != cur:
                better = [i for i in cand if key(i) < key(cur)
                          and since(i) is not None and now - since(i) >= HOLD_DOWN]
                return min(better, key=key) if better else cur
            return best

    def tick(self) -> None:
        """One election + replication round. Safe to call from tests with an injected clock."""
        self.probe_all()
        now = self.clock()
        with self.lock:
            if not self.eligible:
                claimed = any(s.healthy and s.status.get("role") == "leader" for s in self.ps.values())
                if not self._others() or (now - self.started >= SOLO_WAIT and not claimed):
                    self._set_eligible(now)
            prev = self.leader_id
            new = self.compute_leader(now)
            if new != prev:
                # Leader changed: the old cursor is in the old leader's clock (hubs may be skewed) -> full resync.
                self.sync_cursor = None
        if new == self.id and prev != self.id:
            # Partition safety: take over with the union of what every reachable hub has (last-write-wins on
            # `updated`), so rows a losing/partitioned leader wrote are not lost.
            for pid in self.healthy_peers():
                self.merge_from(pid)
            with self.lock:
                self.leader_id, self.leader_since = new, now
                self._merge_pending.clear()
            self.store.event(self.id, "ok", f"{self.id} is now leader")
        else:
            with self.lock:
                if new != prev and prev == self.id:
                    self.store.event(self.id, "warn", f"{self.id} stepped down; leader is {new}")
                    # The new leader merges our rows when it sees us step down (or when it took over); after that its
                    # copy is authoritative: full forced resync so our stamps cannot shadow its later writes. Until it
                    # confirms the merge (status.merged[us] >= our stepdown_seq) we only pull last-write-wins.
                    self.stepdown_seq += 1
                    self.sync_cursor, self._force_sync, self._force_wait_since = None, True, now
                self.leader_id = new
                if new != self.id:
                    self.leader_since = None
        if new and new != self.id:
            if self.sync_from(new):
                with self.lock:
                    self._set_eligible(self.clock())
        elif new == self.id:
            self._merge_rivals(now)
            self._record_peer_observations()

    def contested(self) -> bool:
        """True while we lead but a healthy peer names a BETTER-priority leader (we are the losing side of a split
        brain: only our link to the real leader is cut, or a healed partition before our hold-down step-down).
        Our copy may be stale, so such a leader keeps serving reads/new jobs but must not dispatch, fence agents or
        judge presence (agents report to the other leader, so to us every node looks silent, and those judgments
        would be merged back after the heal). The better-priority side is never contested, so it keeps working."""
        if not self.is_leader:
            return False
        with self.lock:
            mine = (self.priority_of(self.id), self.id)
            for s in self.ps.values():
                lead = s.status.get("leader_id") if s.healthy else None
                if lead and lead != self.id and (self.priority_of(lead), lead) < mine:
                    return True
            return False

    def healthy_peers(self) -> list[str]:
        with self.lock:
            return [pid for pid, s in self.ps.items() if s.healthy and s.url]

    def _merge_rivals(self, now: float) -> None:
        """Leader: merge-pull from peers that just stepped down, and (at most every MERGE_RIVAL_EVERY s) from peers that
        still claim to be leader (a healed partition: the rival writes until it notices us and steps down)."""
        with self.lock:
            todo = set(self._merge_pending)
            self._merge_pending.clear()
            for pid, s in self.ps.items():
                if not s.healthy:
                    continue
                if s.status.get("role") == "leader" and now - self._last_merge.get(pid, -1e18) >= MERGE_RIVAL_EVERY:
                    todo.add(pid)
                seq = s.status.get("stepdown_seq")
                if isinstance(seq, int) and seq > self.merged_seq.get(pid, 0) and s.status.get("role") != "leader":
                    todo.add(pid)                       # it stepped down since our last merge of it (handshake)
        for pid in sorted(todo):
            s = self.ps.get(pid)
            if s and s.healthy:
                self.merge_from(pid)

    # ------------------------------------------------------------ replication
    def sync_page(self, since: float) -> dict:
        """Leader side of GET /cluster/sync."""
        now = self.clock()
        tables, capped = {}, []
        for t in REPLICATED:
            rows = self.store.changed_since(t, since, SYNC_PAGE)
            tables[t] = rows
            if len(rows) >= SYNC_PAGE:
                capped.append(rows[-1]["updated"])
        deleted = [{"table": r["tbl"], "id": r["id"], "updated": r["updated"]} for r in self.store.q(
            "SELECT tbl,id,updated FROM tombstones WHERE updated > ?", (since,))]
        out: dict[str, Any] = {"now": now, "tables": tables, "deleted": deleted, "more": bool(capped)}
        if capped:
            out["next_since"] = min(capped)
        return out

    def sync_from(self, pid: str) -> bool:
        """Standby: incremental pull from the leader (full + forced after we stepped down, once the leader has merged
        our rows or FORCE_WAIT_MAX passed)."""
        force = False
        with self.lock:
            if self._force_sync:
                s = self.ps.get(pid)
                merged = ((s.status.get("merged") or {}) if s else {}).get(self.id, -1)
                waited = self.clock() - (self._force_wait_since or self.clock())
                force = (isinstance(merged, int) and merged >= self.stepdown_seq) or waited >= FORCE_WAIT_MAX
                if not force:
                    self.sync_cursor = None         # keep pulling everything (LWW) until the forced pass can run
            since = (self.sync_cursor - 1.0) if self.sync_cursor is not None else -1.0
        ok = self._pull(pid, since, force, track=True)
        if ok and not force and self._force_sync:
            with self.lock:
                self.sync_cursor = None
        return ok

    def merge_from(self, pid: str) -> bool:
        """Leader: one full last-write-wins merge pull (since 0) from a peer. Never forced, never moves sync_cursor."""
        with self.lock:
            s = self.ps.get(pid)
            seq = (s.status.get("stepdown_seq") if s else None) or 0
        ok = self._pull(pid, -1.0, False, track=False)
        with self.lock:
            self._last_merge[pid] = self.clock()
            if ok:
                self.merges += 1
                self.merged_seq[pid] = max(int(seq) if isinstance(seq, int) else 0, self.merged_seq.get(pid, 0))
        return ok

    def _pull(self, pid: str, since: float, force: bool, track: bool) -> bool:
        """Pull /cluster/sync pages from a peer. track=True: standby replication (moves sync_cursor). track=False: a
        leader's merge pull; rows it takes over are re-stamped to its own clock so its standbys pull them too."""
        s = self.ps.get(pid)
        if not s or not s.url:
            return False
        try:
            for _ in range(100):
                r = self.client().get(s.url.rstrip("/") + "/cluster/sync", params={"since": since},
                                      headers=self._hdrs(), timeout=FORWARD_TIMEOUT)
                if r.status_code != 200:
                    return False
                data = r.json()
                restamp = None if track else self.clock()
                with self.store.tx():
                    for t in REPLICATED:
                        self.store.upsert_rows(t, data["tables"].get(t) or [], force=force, restamp=restamp)
                    self.store.apply_deleted(data.get("deleted") or [], force=force, restamp=restamp)
                if data.get("more"):
                    since = data["next_since"] - 0.001
                    continue
                if track:
                    with self.lock:
                        self.sync_cursor = data["now"]
                        if force:
                            self._force_sync = False
                return True
        except (httpx.HTTPError, ValueError, KeyError):
            return False
        return False

    def _record_peer_observations(self) -> None:
        """Leader writes what it observed (version/role/last_ok) into the replicated peers table."""
        now = self.clock()
        ver = self.releases.latest()[0]
        obs = {self.id: (ver, "leader", now)}
        for pid, s in self.ps.items():
            if s.status:
                obs[pid] = (s.status.get("version"), s.status.get("role"), s.last_ok)
        for pid, (v, role, last_ok) in obs.items():
            row = self.store.q("SELECT version,role,last_ok FROM peers WHERE id=?", (pid,))
            if not row:
                continue
            stale = now - self._last_peer_write.get(pid, 0) >= LAST_OK_WRITE_EVERY
            if row[0]["version"] != v or row[0]["role"] != role or stale:
                self._last_peer_write[pid] = now
                self.store.touch("peers", "id", pid, "version=?, role=?, last_ok=?", (v, role, last_ok))

    # ------------------------------------------------------------ forwarding
    def leader_urls(self) -> list[str]:
        lead = self.leader_id
        if not lead or lead == self.id or lead not in self.ps:
            return []
        urls = [p["urls"] for p in self.peers() if p["id"] == lead]
        first = self.ps[lead].url
        return ([first] if first else []) + [u for u in (urls[0] if urls else []) if u != first]

    def forward(self, method: str, path_qs: str, headers: dict, body: bytes, client_host: Optional[str]
                ) -> Optional[httpx.Response]:
        """Reverse-proxy one request to the leader. None if there is no reachable leader."""
        out = {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}
        xff = headers.get("x-forwarded-for")
        out["X-Forwarded-For"] = f"{xff}, {client_host}" if xff and client_host else (client_host or xff or "")
        out["X-Forge-Via"] = self.id
        for url in self.leader_urls():
            try:
                return self.client().request(method, url.rstrip("/") + path_qs, headers=out, content=body,
                                             timeout=FORWARD_TIMEOUT)
            except httpx.HTTPError:
                continue
        return None

    # ------------------------------------------------------------ views
    @staticmethod
    def _best_url(urls: list, working: Optional[str] = None) -> Optional[str]:
        """The peer URL the UI should link to: first non-loopback configured URL (Tailscale/LAN), preferring the one
        this hub actually reaches it on; loopback only when nothing else is known."""
        def remote(u: str) -> bool:
            return "127.0.0.1" not in u and "localhost" not in u.lower() and "[::1]" not in u
        if working and remote(working):
            return working
        return next((u for u in urls if remote(u)), urls[0] if urls else None)

    def cluster_block(self, viewing: str) -> dict:
        now = self.clock()
        out = []
        with self.lock:
            for p in self.peers():
                pid = p["id"]
                if pid == self.id:
                    out.append({"id": pid, "priority": p["priority"], "urls": p["urls"], "url": self._best_url(p["urls"]),
                                "role": self.role,
                                "version": self.releases.latest()[0], "healthy": True, "last_ok": now, "lag_s": 0.0})
                    continue
                s = self.ps.get(pid)
                st = s.status if s else {}
                cur = st.get("sync_cursor")
                lag = 0.0 if st.get("role") == "leader" else (max(0.0, round(now - cur, 1)) if cur else None)
                out.append({"id": pid, "priority": p["priority"], "urls": p["urls"],
                            "url": self._best_url(p["urls"], s.url if s else None),
                            "role": st.get("role") or p.get("role") or "down",
                            "version": st.get("version") or p.get("version"),
                            "healthy": bool(s and s.healthy), "last_ok": (s.last_ok if s else None) or p.get("last_ok"),
                            "lag_s": lag})
        return {"leader_id": self.leader_id, "viewing": viewing, "peers": out}
