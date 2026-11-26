"""Forge Control hub: fleet status, logs and job dispatch for home worker machines.

Agents (one per machine) POST a heartbeat to /api/agent/report every few seconds and
receive queued jobs in the response (pull model: no inbound ports needed on nodes).
The UI (static/index.html) reads /api/state and /api/nodes/{id}, and queues jobs.

Phase 1: several hubs form a leader/standby cluster (see forge_cluster.py). Exactly one is leader;
standbys replicate its state and reverse-proxy API calls to it. See HUB-CONTRACT.md.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from forge_actions import ActionError, find_action, load_catalog, summary as action_summary, validate_args
from forge_bundle import VERSION_RE, BundleError, Releases, parse_version
from forge_cluster import Cluster
from forge_lease import UpdateLease
from forge_phase2 import KINDS, MODEL_RE, STALE_CANCEL_TTL, Phase2, to_ts
from forge_sched import agent_is_new, should_requeue
from forge_store import Clock, Store, jloads, new_id

BASE = Path(__file__).resolve().parent
ONLINE_SECS = 30
NODE_LOST_SECS = 90            # starting/running jobs on a node unseen this long are failed
LEADER_GRACE_SECS = 30         # a new leader waits this long before judging presence
WATCHDOG_STALE_SECS = 300
DISK_ALERT_PCT = 90
OUTPUT_CAP = 256_000
METRIC_KEEP_SECS = 24 * 3600
TOMBSTONE_KEEP_SECS = 7 * 86400
RETENTION_EVERY = 600
CLOCK_SKEW_ALERT_S = 5.0
TICK_SECS = 3
USER_ENGINES = {"claude", "codex", "shell", "ollama"}   # user lane only; "any" is a scheduling alias (see place_jobs)
SYSTEM_ENGINES = {"action", "admin"}              # SYSTEM lane only (run_as forced to "system")
ENGINES = USER_ENGINES | SYSTEM_ENGINES | {"any"}
ACTIVE = ("queued", "starting", "running")
# paths a standby answers itself instead of forwarding to the leader
LOCAL_PREFIXES = ("/cluster/", "/static/", "/agent/")
LOCAL_PATHS = {"/", "/healthz", "/api/login", "/api/logout", "/api/enroll", "/favicon.ico"}


class JobIn(BaseModel):
    node_id: str = "auto"
    engine: Optional[str] = None          # may be omitted for resume jobs (derived from resume.tool)
    prompt: str = Field(default="", max_length=20_000)
    cwd: Optional[str] = None
    full_access: bool = False
    source: str = Field(default="ui", max_length=64)
    run_as: Optional[str] = None          # "user" (default) | "system"
    action: Optional[str] = None          # engine "action": allowlisted action name (hub/actions.json)
    args: Optional[dict] = None           # engine "action": action args
    confirm: bool = False                 # required for engine "admin" and destructive actions
    model: Optional[str] = Field(default=None, max_length=100)   # ollama model / claude --model
    resume: Optional[dict] = None         # {tool, session_id}: continue an existing claude/codex session
    force: bool = False                   # resume: take over a session that is live in a terminal
    project_id: Optional[str] = Field(default=None, max_length=64)
    kind: Optional[str] = None            # "code" (default) | "text": only text jobs may resolve engine "any" to ollama


class JobUpdate(BaseModel):
    append: Optional[str] = None
    status: Optional[str] = None
    exit_code: Optional[int] = None
    pid: Optional[int] = None
    node_id: Optional[str] = Field(default=None, max_length=64)   # reporting node (2.2.1 agents): fences stale holders
    attempt: Optional[int] = None         # the dispatch item's `attempt`, echoed back


class Login(BaseModel):
    token: str


class JoinIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    urls: list[str] = Field(default_factory=list, max_length=8)


class PeerPatch(BaseModel):
    priority: int = Field(ge=1, le=999)


def default_node_id() -> str:
    return (os.environ.get("COMPUTERNAME") or socket.gethostname()).strip().lower()


def load_config(data_dir: Path) -> dict:
    data_dir.mkdir(parents=True, exist_ok=True)
    p = data_dir / "config.json"
    cfg = json.loads(p.read_text(encoding="utf-8-sig")) if p.exists() else {}
    changed = False
    for key, nbytes in (("ui_token", 18), ("agent_token", 24), ("cluster_token", 24)):
        if not cfg.get(key):
            cfg[key], changed = secrets.token_urlsafe(nbytes), True
    if changed:
        p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg


def as_list(v: Any) -> list:
    """Windows PowerShell 5.1 serializes a one-element array as a bare object; accept both."""
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


LIST_FIELDS = ("disks", "gpu", "ips")


def normalize_report(body: dict) -> dict:
    m = body.get("metrics")
    if isinstance(m, dict):
        for k in LIST_FIELDS:
            if k in m:
                m[k] = [x for x in as_list(m[k]) if isinstance(x, dict)]
    for k in ("procs", "agents"):
        if k in body:
            body[k] = [x for x in as_list(body[k]) if isinstance(x, dict)]
    info = body.get("info")
    if isinstance(info, dict):
        for k in ("gpus", "wsl", "tools"):
            if k in info:
                info[k] = as_list(info[k])
    return body


def _disk_pct(metrics: dict) -> Optional[float]:
    disks = as_list(metrics.get("disks"))
    pcts = [100 * (1 - d["free_gb"] / d["total_gb"]) for d in disks if isinstance(d, dict) and d.get("total_gb")]
    return round(max(pcts), 1) if pcts else None


def _job_row(r: Any, with_output: bool) -> dict:
    d = dict(r)
    d["run_as"] = d.get("run_as") or "user"
    if "args" in d:
        d["args"] = jloads(d["args"], None)
    if "resume" in d:
        d["resume"] = jloads(d["resume"], None)
    if not with_output:
        out = d.pop("output") or ""
        d["tail"] = out[-400:]
    return d


def _parse_ts(v: Any) -> Optional[float]:
    """Epoch seconds from a number or an ISO-8601 string (PowerShell 'o' format tolerated on py3.10)."""
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str) or not v:
        return None
    s = v.strip().replace("Z", "+00:00")
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).timestamp()


def _tools(info: dict) -> set:
    t = info.get("tools") or []
    return set(t.keys() if isinstance(t, dict) else t)


def default_client_factory() -> httpx.Client:
    return httpx.Client(follow_redirects=False)


def create_app(data_dir: Optional[Path] = None, start_monitor: bool = True, *,
               clock: Clock = time.time,
               client_factory: Callable[[], httpx.Client] = default_client_factory,
               release_dir: Optional[Path] = None, releases_dir: Optional[Path] = None,
               node_id: Optional[str] = None) -> FastAPI:
    """Build a hub. `clock` and `client_factory` (peer HTTP) are injectable for tests;
    `app.state.tick()` runs one election/replication/presence round."""
    data_dir = Path(data_dir or os.environ.get("FORGE_DATA") or BASE.parent / "data")
    release_dir = Path(release_dir or os.environ.get("FORGE_RELEASE_DIR") or BASE.parent)
    releases_dir = Path(releases_dir or os.environ.get("FORGE_RELEASES_DIR") or data_dir.parent / "releases")
    cfg = load_config(data_dir)
    me = (node_id or cfg.get("node_id") or default_node_id()).strip().lower()
    store = Store(data_dir / "forge.db", clock)
    releases = Releases(release_dir, releases_dir)
    cluster = Cluster(store, cfg, me, clock, client_factory, releases)
    actions_catalog = load_catalog()
    app = FastAPI(title="Forge Control", docs_url=None, redoc_url=None)
    app.state.store, app.state.cfg, app.state.cluster, app.state.releases = store, cfg, cluster, releases
    app.state.node_id = me
    p2 = Phase2(store, cfg, cluster, clock)
    lease = UpdateLease(store, cluster, clock, lambda: releases.running_version)
    app.state.p2, app.state.lease = p2, lease

    # ---------- auth ----------
    def require_ui(request: Request) -> None:
        tok = request.cookies.get("forge_token") or request.headers.get("authorization", "").removeprefix("Bearer ")
        if not tok or not secrets.compare_digest(tok, cfg["ui_token"]):
            raise HTTPException(401, "login required")

    def require_agent(request: Request) -> None:
        tok = request.headers.get("x-agent-token", "")
        if not secrets.compare_digest(tok, cfg["agent_token"]):
            raise HTTPException(401, "bad agent token")

    def require_cluster(request: Request) -> None:
        tok = request.headers.get("x-cluster-token", "")
        if not secrets.compare_digest(tok, cfg["cluster_token"]):
            raise HTTPException(401, "bad cluster token")

    # ---------- forwarding (standby -> leader) ----------
    def is_local_path(path: str) -> bool:
        return path in LOCAL_PATHS or path.startswith(LOCAL_PREFIXES)

    def to_response(r: httpx.Response) -> Response:
        out = Response(content=r.content, status_code=r.status_code)
        for k, v in r.headers.multi_items():
            if k.lower() not in ("content-length", "content-encoding", "transfer-encoding", "connection"):
                out.headers.append(k, v)
        return out

    def no_leader() -> JSONResponse:
        return JSONResponse({"detail": "no leader", "role": cluster.role,
                             "peers": [p["id"] for p in cluster.peers()]}, status_code=503)

    @app.middleware("http")
    async def forward_to_leader(request: Request, call_next):
        if cluster.is_leader or is_local_path(request.url.path):
            return await call_next(request)
        if "x-forge-via" in request.headers:
            return JSONResponse({"detail": "forward loop", "role": cluster.role}, status_code=503)
        raw = request.scope.get("raw_path")
        path = raw.decode("latin-1") if raw else request.url.path
        if request.url.query:
            path += "?" + request.url.query
        body = await request.body()
        resp = await run_in_threadpool(cluster.forward, request.method, path, dict(request.headers), body,
                                       request.client.host if request.client else None)
        return to_response(resp) if resp is not None else no_leader()

    def leader_only_or_forward(request: Request, body: bytes) -> Optional[Response]:
        """For /cluster/* write endpoints: None if we are leader, else the leader's proxied reply."""
        if cluster.is_leader:
            return None
        if "x-forge-via" in request.headers:
            return JSONResponse({"detail": "forward loop"}, status_code=503)
        path = request.url.path + (("?" + request.url.query) if request.url.query else "")
        resp = cluster.forward(request.method, path, dict(request.headers), body,
                               request.client.host if request.client else None)
        return to_response(resp) if resp is not None else no_leader()

    # ---------- presence / maintenance (leader duties) ----------
    def lose_job(j: Any, now: float, alive_until: float, why: str, requeue: bool = True) -> None:
        """A running job lost its agent. Requeue once if it barely started (< 1 KB output, < 60 s of life),
        else fail it. System-lane jobs are never requeued (not idempotent)."""
        ran = max(0.0, alive_until - (j["started"] or alive_until))
        live = "status IN ('starting','running') AND node_id=?"        # still where we saw it (sched_lock held too)
        if requeue and j["run_as"] != "system" and should_requeue(len(j["output"] or ""), ran, j["attempts"] or 0,
                                                       bool(j["cancel_requested"])):
            # prev_holder/requeued_at are replicated, so the fence survives a leader change: that node is told to
            # kill its copy on every report while the job is active elsewhere, and is not handed it back for a while
            sets, args = ["status='queued'", "started=NULL", "pid=NULL", "attempts=COALESCE(attempts,0)+1",
                          "wait_reason=NULL", "output=output||?", "prev_holder=?", "requeued_at=?"], \
                [f"\n[hub] {why}; job requeued\n", j["dispatched_to"] or j["node_id"], now]
            if j["requested"] == "auto":
                sets.append("node_id='auto'")
                if j["req_engine"] == "any":
                    sets.append("engine='any'")
            if store.touch("jobs", "id", j["id"], ", ".join(sets), tuple(args), live, (j["node_id"],)):
                store.event(j["node_id"], "warn", f"job {j['id'][:8]} ({j['engine']}) requeued: {why}")
        else:
            if store.touch("jobs", "id", j["id"], "status='failed', finished=?, output=output||?",
                           (now, f"\n[hub] {why}; job lost\n"), live, (j["node_id"],)):
                store.event(j["node_id"], "error", f"job {j['id'][:8]} ({j['engine']}) failed: {why}")

    def check_presence(now: Optional[float] = None) -> None:
        now = clock() if now is None else now
        for r in store.q("SELECT id,last_seen,online FROM nodes"):
            is_on = int(now - (r["last_seen"] or 0) < ONLINE_SECS)
            if is_on != r["online"]:
                store.touch("nodes", "id", r["id"], "online=?", (is_on,))
                store.event(r["id"], "ok" if is_on else "warn", "came online" if is_on else "went offline")
        with p2.sched_lock:
            for j in store.q(
                    "SELECT j.*, COALESCE(CASE WHEN j.run_as='system' THEN n.sys_last_seen ELSE n.last_seen END, 0) AS seen "
                    "FROM jobs j JOIN nodes n ON n.id=j.node_id "
                    "WHERE j.status IN ('starting','running') AND ? - COALESCE(CASE WHEN j.run_as='system' "
                    "THEN n.sys_last_seen ELSE n.last_seen END, 0) > ?", (now, NODE_LOST_SECS)):
                lose_job(j, now, j["seen"], "node offline")
        p2.place_jobs(now)

    _retention = {"last": None}

    def retention(now: Optional[float] = None) -> dict:
        """Leader only: prune old jobs / extra briefs (with tombstones, so standbys follow)."""
        now = clock() if now is None else now
        _retention["last"] = now
        return p2.retention(now)

    def trim(now: Optional[float] = None, settled: Optional[bool] = None) -> None:
        now = clock() if now is None else now
        store.x("DELETE FROM metrics WHERE ts < ?", (now - METRIC_KEEP_SECS,))
        store.x("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 2000)")
        store.x("DELETE FROM tombstones WHERE updated < ?", (now - TOMBSTONE_KEEP_SECS,))
        settled = (cluster.is_leader and not cluster.contested()) if settled is None else settled
        if settled and (_retention["last"] is None or now - _retention["last"] >= RETENTION_EVERY):
            retention(now)

    def tick() -> None:
        cluster.tick()
        now = clock()
        # a contested leader (a peer names another leader) keeps serving but makes no presence/brief judgments
        settled = cluster.is_leader and now - (cluster.leader_since or now) >= LEADER_GRACE_SECS and not cluster.contested()
        if settled:
            check_presence(now)
        if cluster.is_leader:
            lease.tick()
            if settled:
                p2.tick(now)
        trim(now, settled)

    app.state.check_presence, app.state.tick, app.state.trim = check_presence, tick, trim
    app.state.retention = retention

    if start_monitor:
        def loop() -> None:
            while True:
                try:
                    tick()
                except Exception:  # keep the monitor alive
                    pass
                time.sleep(TICK_SECS)
        threading.Thread(target=loop, daemon=True, name="forge-tick").start()

    # ---------- agent distribution files ----------
    legacy_agent = release_dir / "agent" / "legacy" / "forge-agent-v1.ps1"
    _ver_cache: dict = {}

    def agent_version() -> Optional[str]:
        """Version declared in the LEGACY (v1) agent script. v1 agents self-update when theirs differs."""
        try:
            mtime = legacy_agent.stat().st_mtime
        except OSError:
            return None
        if _ver_cache.get("mtime") != mtime:
            m = re.search(r"\$AgentVersion\s*=\s*'([^']+)'", legacy_agent.read_text(encoding="utf-8"))
            _ver_cache.update(mtime=mtime, ver=m.group(1) if m else None)
        return _ver_cache["ver"]

    def system_lane(r: Any, now: float) -> dict:
        seen = r["sys_last_seen"]
        return {"online": seen is not None and now - seen < ONLINE_SECS, "last_seen": seen,
                "capacity": r["sys_capacity"], "version": r["sys_version"]}

    def node_settings(nid: str) -> dict:
        r = store.q("SELECT work_root,max_jobs,canary FROM settings WHERE node_id=?", (nid,))
        return ({"work_root": r[0]["work_root"], "max_jobs": r[0]["max_jobs"], "canary": bool(r[0]["canary"])} if r
                else {"work_root": None, "max_jobs": None, "canary": False})

    def effective_settings(nid: str) -> dict:
        """What the agent is sent (canary is a hub-side rollout flag only)."""
        s = node_settings(nid)
        return {"work_root": s["work_root"], "max_jobs": s["max_jobs"]}

    # ---------- agent API ----------
    def dispatch(nid: str, lane: str, cap: int, boot: bool, now: float, agent_version: Optional[str] = None) -> tuple[list, list]:
        """Hand this lane's queued jobs to the agent. v1/pre-2.1 agents send no lane: they are the user lane
        and never see run_as='system' jobs. User-lane jobs are held back while their engine is cooling down,
        while engine is still 'any', and (ollama/resume) while the agent is older than 2.2.0."""
        with p2.sched_lock:
            return _dispatch(nid, lane, cap, boot, now, agent_version)

    def _dispatch(nid: str, lane: str, cap: int, boot: bool, now: float, agent_version: Optional[str]) -> tuple[list, list]:
        lane_sql = "run_as='system'" if lane == "system" else "COALESCE(run_as,'user')='user'"
        if boot:
            for j in store.q(f"SELECT * FROM jobs WHERE node_id=? AND status IN ('starting','running') AND {lane_sql}",
                             (nid,)):
                lose_job(j, now, now, "agent restarted", requeue=False)
        if cluster.contested():
            # Our copy may be stale (Cluster.contested): `boot` is still honoured above (the agent itself says its jobs
            # died), but nothing is handed out or fenced until the split brain resolves.
            return [], []
        stale = set(p2.stale_for(nid, now))      # jobs this node must kill: never hand them back to it meanwhile
        jobs = []
        if cap:
            cds = p2.cooldowns(now) if lane == "user" else {}
            for j in store.q(f"SELECT * FROM jobs WHERE node_id=? AND status='queued' AND {lane_sql} ORDER BY id", (nid,)):
                if len(jobs) >= cap:
                    break
                if j["id"] in stale or (j["prev_holder"] == nid and (j["requeued_at"] or 0) > now - STALE_CANCEL_TTL):
                    continue                     # this node may still run an old copy: let the cancel land first
                if lane == "user" and not p2.dispatchable(j, agent_version, cds):
                    continue
                if not store.touch("jobs", "id", j["id"], "status='starting', started=?, wait_reason=NULL, dispatched_to=?, "
                                   "prev_holder=CASE WHEN prev_holder=? THEN NULL ELSE prev_holder END",
                                   (now, nid, nid), "status='queued' AND node_id=?", (nid,)):
                    continue                     # taken or moved meanwhile
                item = {k: j[k] for k in ("id", "engine", "prompt", "cwd")} | {"full_access": bool(j["full_access"]),
                                                                               "attempt": j["attempts"] or 0}
                if lane == "system":
                    item |= {"run_as": "system", "action": j["action"], "args": jloads(j["args"], {})}
                else:
                    model = j["model"] or (p2.brief_model() if j["engine"] == "ollama" else None)
                    if model:
                        item["model"] = model
                    if j["resume"]:
                        item["resume"] = jloads(j["resume"], None)
                    if j["source"] == "brief":
                        item["source"] = "brief"
                jobs.append(item)
                store.event(nid, "info", f"job {j['id'][:8]} ({j['engine']}) dispatched"
                            + (" to system lane" if lane == "system" else ""))
        cancel = [r["id"] for r in store.q(
            "SELECT id FROM jobs WHERE node_id=? AND cancel_requested=1 AND status IN ('starting','running') "
            f"AND {lane_sql}", (nid,))]
        # fenced: requeued away from this node and still active (queued, or running somewhere else)
        stale |= {r["id"] for r in store.q(
            "SELECT id FROM jobs WHERE prev_holder=? AND status IN ('queued','starting','running') "
            f"AND NOT (status IN ('starting','running') AND dispatched_to=?) AND {lane_sql}", (nid, nid))}
        cancel += [jid for jid in sorted(stale) if jid not in cancel]     # unknown ids are a no-op for an agent
        return jobs, cancel

    def system_lane_report(nid: str, body: dict, now: float) -> dict:
        """SYSTEM-lane heartbeat (minimal body). Stored in nodes.sys_* so user-lane metrics are never touched."""
        cap = max(0, int(body.get("capacity") or 0))
        if not store.q("SELECT 1 FROM nodes WHERE id=?", (nid,)):
            store.x("INSERT INTO nodes(id,created,updated) VALUES(?,?,?)", (nid, now, now))
        prev = store.q("SELECT sys_last_seen FROM nodes WHERE id=?", (nid,))[0]["sys_last_seen"]
        if prev is None or now - prev >= ONLINE_SECS:
            store.event(nid, "ok", "system lane online")
        store.touch("nodes", "id", nid, "sys_last_seen=?, sys_capacity=?, sys_version=?",
                    (now, cap, str(body.get("agent_version") or "")[:32] or None))
        jobs, cancel = dispatch(nid, "system", cap, bool(body.get("boot")), now)
        return {"jobs": jobs, "cancel": cancel, "server_time": now, "lane": "system",
                "settings": effective_settings(nid), "hub_urls": cluster.hub_urls(),
                "bundle_version": releases.latest_version()}

    @app.post("/api/agent/report", dependencies=[Depends(require_agent)])
    def agent_report(body: dict, request: Request) -> dict:
        nid = str(body.get("node_id") or "").strip().lower()
        if not nid:
            raise HTTPException(400, "node_id required")
        now = clock()
        if body.get("lane") == "system":
            return system_lane_report(nid, body, now)
        body = normalize_report(body)
        metrics = body.get("metrics") or {}
        if body.get("bundle_version") and "bundle_version" not in metrics:
            metrics["bundle_version"] = body["bundle_version"]
        at = to_ts(body.get("agent_time"))
        if at is not None:                       # clock skew breaks session ages, cooldown resets and job timings
            metrics["clock_skew_s"] = round(at - now, 1)
        addr = request.client.host if request.client else None
        if "x-forge-via" in request.headers and request.headers.get("x-forwarded-for"):
            addr = request.headers["x-forwarded-for"].split(",")[0].strip()
        row = store.q("SELECT info FROM nodes WHERE id=?", (nid,))
        if not row:
            store.x("INSERT INTO nodes(id,created,last_seen,updated) VALUES(?,?,?,?)", (nid, now, now, now))
            store.event(nid, "ok", "joined the fleet")
        store.touch("nodes", "id", nid, "last_seen=?, addr=?, metrics=?, procs=?, agents=?, capacity=?",
                    (now, addr, json.dumps(metrics), json.dumps(body.get("procs") or []),
                     json.dumps(body.get("agents") or []), int(body.get("capacity") or 0)))
        if body.get("info"):
            store.touch("nodes", "id", nid, "info=?", (json.dumps(body["info"]),))
        av = str(body.get("agent_version") or "")[:32] or None
        if av and row and not body.get("info"):
            cur_info = jloads(row[0]["info"], {})
            if cur_info and cur_info.get("agent_version") != av:      # agent upgraded since it last sent `info`
                cur_info["agent_version"] = av
                store.touch("nodes", "id", nid, "info=?", (json.dumps(cur_info),))
        for name, text in (body.get("logs") or {}).items():
            store.x("INSERT OR REPLACE INTO logs(node_id,name,ts,text) VALUES(?,?,?,?)", (nid, name, now, str(text)[-60_000:]))
        gpus = metrics.get("gpu") or []
        store.x("INSERT INTO metrics VALUES(?,?,?,?,?,?)", (
            nid, now, metrics.get("cpu"), metrics.get("mem_pct"),
            max((g.get("util") or 0) for g in gpus) if gpus else None, _disk_pct(metrics)))
        p2.place_jobs(now)
        if not av:
            av = jloads(store.q("SELECT info FROM nodes WHERE id=?", (nid,))[0]["info"], {}).get("agent_version")
        jobs, cancel = dispatch(nid, "user", max(0, int(body.get("capacity") or 0)), bool(body.get("boot")), now, av)
        info = row[0]["info"] if row else "{}"
        return {"jobs": jobs, "cancel": cancel, "need_info": info in ("", "{}"), "server_time": now,
                "agent_version": agent_version(), "settings": effective_settings(nid),
                "hub_urls": cluster.hub_urls(), "bundle_version": releases.latest_version(),
                "watch": p2.eph.watch_list(nid, now)}

    @app.post("/api/agent/jobs/{jid}/update", dependencies=[Depends(require_agent)])
    def agent_job_update(jid: str, u: JobUpdate):
        if cluster.contested():          # stale copy: neither accept nor fence; the agent retries / fails over
            return JSONResponse({"detail": "leader contested", "role": cluster.role}, status_code=503)
        with p2.sched_lock:
            return _job_update(jid, u)

    def _job_update(jid: str, u: JobUpdate):
        r = store.q("SELECT * FROM jobs WHERE id=?", (jid,))
        if not r:
            raise HTTPException(404, "no such job")
        j, now = r[0], clock()
        reporter = (u.node_id or "").strip().lower() or None
        holder = j["dispatched_to"] or (j["node_id"] if j["node_id"] != "auto" else None)
        # Fencing: only the agent currently holding this dispatch may write to it. A node that slept through a requeue
        # (or lost the job to another node) gets 409 and is told to kill its copy on its next report.
        stale = (j["status"] not in ("starting", "running")
                 or (reporter is not None and holder is not None and reporter != holder)
                 or (u.attempt is not None and u.attempt != (j["attempts"] or 0)))
        if stale:
            who = reporter or j["dispatched_to"]          # never-dispatched job: nobody to fence
            first = who is not None and jid not in p2.stale_for(who, now)
            p2.add_stale(who, jid, now)
            if first and j["status"] in ACTIVE:
                store.event(reporter or holder, "warn", f"stale update for job {jid[:8]} rejected (job {j['status']}"
                            + (f" on {j['node_id']}" if j["node_id"] != (reporter or holder) else "") + ")")
            return JSONResponse({"detail": "job reassigned", "status": j["status"]}, status_code=409)
        out = j["output"] or ""
        if u.append:
            out = out + u.append
            if len(out) > OUTPUT_CAP:
                out = "[...truncated...]\n" + out[-OUTPUT_CAP:]
        sets, args = ["output=?"], [out]
        if u.pid is not None:
            sets.append("pid=?"); args.append(u.pid)
        if u.status:
            if u.status not in ("running", "done", "failed", "cancelled"):
                raise HTTPException(400, "bad status")
            sets.append("status=?"); args.append(u.status)
            if u.status != "running":
                sets.append("finished=?"); args.append(clock())
                lvl = {"done": "ok", "failed": "error", "cancelled": "warn"}[u.status]
                store.event(r[0]["node_id"], lvl, f"job {jid[:8]} ({r[0]['engine']}) {u.status}"
                            + (f" (exit {u.exit_code})" if u.exit_code is not None else ""))
        if u.exit_code is not None:
            sets.append("exit_code=?"); args.append(u.exit_code)
        store.touch("jobs", "id", jid, ", ".join(sets), tuple(args))
        p2.check_limit(j, u.append, u.status, u.exit_code, out, now)
        if u.status == "done":
            p2.on_job_done(j, out, now)
        return {"ok": True}

    # ---------- UI API ----------
    @app.post("/api/login")
    def login(body: Login, response: Response) -> dict:
        if not secrets.compare_digest(body.token.strip(), cfg["ui_token"]):
            raise HTTPException(401, "wrong token")
        response.set_cookie("forge_token", cfg["ui_token"], httponly=True, samesite="lax", max_age=60 * 24 * 3600)
        return {"ok": True}

    @app.post("/api/logout")
    def logout(response: Response) -> dict:
        response.delete_cookie("forge_token")
        return {"ok": True}

    @app.get("/api/state", dependencies=[Depends(require_ui)])
    def state(request: Request, include_brief: bool = False) -> dict:
        now = clock()
        running = {r["node_id"]: r["n"] for r in store.q(
            "SELECT node_id, COUNT(*) n FROM jobs WHERE status IN ('queued','starting','running') GROUP BY node_id")}
        settings = {r["node_id"]: {"work_root": r["work_root"], "max_jobs": r["max_jobs"], "canary": bool(r["canary"])}
                    for r in store.q("SELECT * FROM settings")}
        nodes = []
        for r in store.q("SELECT * FROM nodes ORDER BY id"):
            m = jloads(r["metrics"], {})
            nodes.append({
                "system_lane": system_lane(r, now),
                "id": r["id"], "online": now - (r["last_seen"] or 0) < ONLINE_SECS, "last_seen": r["last_seen"],
                "addr": r["addr"], "info": jloads(r["info"], {}), "metrics": m, "disk_pct": _disk_pct(m),
                "agents": jloads(r["agents"], []), "active_jobs": running.get(r["id"], 0), "capacity": r["capacity"],
                "bundle_version": m.get("bundle_version") or jloads(r["info"], {}).get("bundle_version"),
                "settings": settings.get(r["id"], {"work_root": None, "max_jobs": None, "canary": False}),
                "spark": [dict(x) for x in store.q(
                    "SELECT ts,cpu,mem FROM metrics WHERE node_id=? AND ts>? ORDER BY ts", (r["id"], now - 900))][::3],
            })
        jobs = [_job_row(r, False) for r in store.q(
            "SELECT * FROM jobs " + ("" if include_brief else "WHERE COALESCE(source,'')<>'brief' ") + "ORDER BY id DESC LIMIT 40")]
        events = [dict(r) for r in store.q("SELECT * FROM events ORDER BY id DESC LIMIT 60")]
        viewing = request.headers.get("x-forge-via") or me
        return {"now": now, "nodes": nodes, "jobs": jobs, "events": events,
                "cluster": cluster.cluster_block(viewing)}

    @app.get("/api/health", dependencies=[Depends(require_ui)])
    def health_report() -> dict:
        now = clock()
        alerts, nodes_out = [], []
        mine = releases.running_version
        for r in store.q("SELECT * FROM nodes ORDER BY id"):
            m, info = jloads(r["metrics"], {}), jloads(r["info"], {})
            online = now - (r["last_seen"] or 0) < ONLINE_SECS
            ver = m.get("bundle_version") or info.get("bundle_version")
            wd = m.get("watchdog") if isinstance(m.get("watchdog"), dict) else None
            wd_run = _parse_ts(wd.get("last_run")) if wd else None
            actions = (wd or {}).get("actions") or []
            nodes_out.append({"id": r["id"], "online": online, "bundle_version": ver, "system_lane": system_lane(r, now),
                              "behind": bool(ver and parse_version(ver) < parse_version(mine)),
                              "watchdog_last_run": wd_run, "watchdog_last_action": actions[-1] if actions else None})
            if not online:
                alerts.append({"level": "error", "kind": "node_offline", "node_id": r["id"],
                               "msg": f"{r['id']} is offline"})
                continue
            pct = _disk_pct(m)
            if pct is not None and pct >= DISK_ALERT_PCT:
                alerts.append({"level": "warn", "kind": "disk_full", "node_id": r["id"],
                               "msg": f"{r['id']} disk at {pct}%"})
            if ver and parse_version(ver) < parse_version(mine):
                alerts.append({"level": "warn", "kind": "version_behind", "node_id": r["id"],
                               "msg": f"{r['id']} runs {ver}, leader runs {mine}"})
            skew = m.get("clock_skew_s")
            if isinstance(skew, (int, float)) and abs(skew) > CLOCK_SKEW_ALERT_S:
                alerts.append({"level": "warn", "kind": "clock_skew", "node_id": r["id"],
                               "msg": f"{r['id']} clock is {abs(skew):.0f}s {'ahead of' if skew > 0 else 'behind'} the hub"})
            if wd is not None and (wd_run is None or now - wd_run > WATCHDOG_STALE_SECS):
                alerts.append({"level": "warn", "kind": "watchdog_stale", "node_id": r["id"],
                               "msg": f"{r['id']} watchdog has not run for {'a long time' if wd_run is None else str(int(now - wd_run)) + 's'}"})
        block = cluster.cluster_block(me)
        for p in block["peers"]:
            if not p["healthy"]:
                alerts.append({"level": "error", "kind": "hub_down", "node_id": p["id"], "msg": f"hub {p['id']} is unreachable"})
            elif p["version"] and parse_version(p["version"]) < parse_version(mine):
                alerts.append({"level": "warn", "kind": "version_behind", "node_id": p["id"],
                               "msg": f"hub {p['id']} runs {p['version']}, leader runs {mine}"})
        target = releases.latest_version()
        rollout = []
        for p in block["peers"]:
            v = p.get("version")
            status = ("unreachable" if not p["healthy"] else "missing" if not v
                      else "current" if parse_version(v) >= parse_version(target or v) else "updating")
            rollout.append({"node_id": p["id"], "version": v, "status": status})
        bad = lease.bad_versions()
        for v in bad:
            alerts.append({"level": "error", "kind": "fleet_bad_version", "node_id": None,
                           "msg": f"version {v} is marked fleet-bad (a canary rolled it back); watchdogs will not install it"})
        return {"now": now, "alerts": alerts, "nodes": nodes_out, "cluster": block, "leader_id": cluster.leader_id,
                "running_version": mine, "bundle_version": target, "rollout": rollout,
                "bad_versions": bad, "update_lease": lease.view(), "cooldowns": p2.scheduler_view(now)["cooldowns"]}

    @app.get("/api/nodes/{nid}", dependencies=[Depends(require_ui)])
    def node_detail(nid: str, minutes: int = 60) -> dict:
        r = store.q("SELECT * FROM nodes WHERE id=?", (nid,))
        if not r:
            raise HTTPException(404, "no such node")
        r = r[0]
        hist = [dict(x) for x in store.q(
            "SELECT ts,cpu,mem,gpu,disk FROM metrics WHERE node_id=? AND ts>? ORDER BY ts",
            (nid, clock() - minutes * 60))]
        step = max(1, len(hist) // 240)
        return {
            "id": nid, "online": clock() - (r["last_seen"] or 0) < ONLINE_SECS, "last_seen": r["last_seen"],
            "system_lane": system_lane(r, clock()),
            "addr": r["addr"], "info": jloads(r["info"], {}), "metrics": jloads(r["metrics"], {}),
            "procs": jloads(r["procs"], []), "agents": jloads(r["agents"], []), "history": hist[::step],
            "settings": effective_settings(nid),
            "logs": [dict(x) for x in store.q("SELECT name,ts,text FROM logs WHERE node_id=? ORDER BY name", (nid,))],
            "jobs": [_job_row(x, False) for x in store.q(
                "SELECT * FROM jobs WHERE node_id=? AND COALESCE(source,'')<>'brief' ORDER BY id DESC LIMIT 25", (nid,))],
        }

    @app.put("/api/nodes/{nid}/settings", dependencies=[Depends(require_ui)])
    def put_settings(nid: str, body: dict) -> dict:
        if not store.q("SELECT 1 FROM nodes WHERE id=?", (nid,)):
            raise HTTPException(404, "no such node")
        cur = node_settings(nid)
        if "work_root" in body:
            wr = body["work_root"]
            if wr is not None and not isinstance(wr, str):
                raise HTTPException(400, "work_root must be a string or null")
            cur["work_root"] = (wr or "").strip() or None
        if "max_jobs" in body:
            mj = body["max_jobs"]
            if mj is not None and (isinstance(mj, bool) or not isinstance(mj, int) or not 1 <= mj <= 64):
                raise HTTPException(400, "max_jobs must be an integer 1-64 or null")
            cur["max_jobs"] = mj
        if "canary" in body:
            if not isinstance(body["canary"], bool):
                raise HTTPException(400, "canary must be true or false")
            cur["canary"] = body["canary"]
        store.x("INSERT OR REPLACE INTO settings(node_id,work_root,max_jobs,canary,updated) VALUES(?,?,?,?,?)",
                (nid, cur["work_root"], cur["max_jobs"], int(cur["canary"]), clock()))
        return cur

    def admin_enabled() -> bool:
        return cfg.get("admin_lane_enabled", True) is not False

    @app.get("/api/actions", dependencies=[Depends(require_ui)])
    def list_actions() -> dict:
        return actions_catalog | {"admin_lane_enabled": admin_enabled()}

    @app.post("/api/jobs", dependencies=[Depends(require_ui)])
    def create_job(j: JobIn) -> dict:
        now = clock()
        engine, node_id, cwd, resume = j.engine, (j.node_id or "auto"), j.cwd, None
        if j.resume:
            tool, node_id, rcwd = p2.resolve_resume(j.resume, node_id, j.force, now)   # 400/404/409
            if engine in (None, "any"):
                engine = tool
            elif engine != tool:
                raise HTTPException(400, f"resume of a {tool} session needs engine {tool}")
            cwd, resume = rcwd or cwd, {"tool": tool, "session_id": str(j.resume["session_id"])}
        if not engine:
            raise HTTPException(400, "engine required")
        if engine not in ENGINES:
            raise HTTPException(400, f"engine must be one of {sorted(ENGINES)}")
        if j.run_as not in (None, "user", "system"):
            raise HTTPException(400, "run_as must be 'user' or 'system'")
        if j.model is not None and not MODEL_RE.match(j.model):
            raise HTTPException(400, "bad model name")
        if j.kind is not None and j.kind not in KINDS:
            raise HTTPException(400, f"kind must be one of {list(KINDS)}")
        system = engine in SYSTEM_ENGINES          # action/admin always run as SYSTEM
        if not system and j.run_as == "system":
            raise HTTPException(400, f"engine {engine} runs in the user lane only; use engine 'admin' or 'action'")
        auto = node_id == "auto"
        action, args, prompt = None, None, j.prompt
        if system:
            if auto:
                raise HTTPException(400, "system-lane jobs need a specific machine (not auto)")
            if engine == "action":
                spec = find_action(actions_catalog, j.action)
                if not spec:
                    raise HTTPException(400, f"unknown action {j.action!r}")
                try:
                    args = validate_args(spec, j.args)
                except ActionError as e:
                    raise HTTPException(400, str(e))
                action, prompt = spec["name"], action_summary(spec["name"], args)
                needs_confirm = bool(spec.get("destructive"))
            else:
                if j.action or j.args:
                    raise HTTPException(400, "action/args are only for engine 'action'")
                needs_confirm = True
            if needs_confirm and not j.confirm:
                raise HTTPException(400, "confirmation required")
        if not prompt.strip():
            raise HTTPException(400, "prompt required")
        if not auto and not store.q("SELECT 1 FROM nodes WHERE id=?", (node_id,)):
            raise HTTPException(404, "no such node")
        status, output, finished = "queued", "", None
        if system and not admin_enabled():
            status, finished = "failed", now
            output = "[hub] admin lane is disabled on this hub (config.json admin_lane_enabled=false); job not run\n"
        jid = p2.insert_job(node_id=node_id, engine=engine, prompt=prompt, cwd=None if system else (cwd or None),
                            full_access=j.full_access and not system, status=status, finished=finished, output=output,
                            source=j.source, requested="auto" if auto else None, run_as="system" if system else "user",
                            action=action, args=args, resume=resume, project_id=j.project_id, model=j.model,
                            req_engine="any" if engine == "any" else None, kind=j.kind)
        if system:
            what = f"ACTION {action}" if action else "ADMIN"
            store.event(node_id, "warn", f"{what} job {jid[:8]} queued for system lane (source {j.source})"
                        + (" - refused: admin lane disabled on hub" if status == "failed" else ""))
        else:
            store.event(node_id, "info", f"job {jid[:8]} ({engine}) queued" + (" (auto)" if auto else "")
                        + (f" [resume {resume['tool']}:{resume['session_id'][:8]}]" if resume else ""))
            p2.note_job_cwd(node_id, cwd, now, j.project_id)
            p2.place_jobs(now)
        row = store.q("SELECT node_id,engine,wait_reason FROM jobs WHERE id=?", (jid,))[0]
        return {"id": jid, "node_id": row["node_id"], "engine": row["engine"], "run_as": "system" if system else "user",
                "status": status, "wait_reason": row["wait_reason"]}

    @app.get("/api/jobs/{jid}", dependencies=[Depends(require_ui)])
    def get_job(jid: str) -> dict:
        r = store.q("SELECT * FROM jobs WHERE id=?", (jid,))
        if not r:
            raise HTTPException(404, "no such job")
        return _job_row(r[0], True)

    @app.post("/api/jobs/{jid}/cancel", dependencies=[Depends(require_ui)])
    def cancel_job(jid: str) -> dict:
        with p2.sched_lock:          # a queued job may be handed out concurrently: decide on the current status
            r = store.q("SELECT status FROM jobs WHERE id=?", (jid,))
            if not r:
                raise HTTPException(404, "no such job")
            if r[0]["status"] == "queued":
                store.touch("jobs", "id", jid, "status='cancelled', finished=?", (clock(),), "status='queued'")
            elif r[0]["status"] in ("starting", "running"):
                store.touch("jobs", "id", jid, "cancel_requested=1")
        return {"ok": True}

    @app.delete("/api/nodes/{nid}", dependencies=[Depends(require_ui)])
    def forget_node(nid: str) -> dict:
        store.delete_row("nodes", nid)
        store.delete_row("settings", nid)
        for t in ("sessions", "repos"):
            for r in store.q(f"SELECT id FROM {t} WHERE node_id=?", (nid,)):
                store.delete_row(t, r["id"])
        for t in ("metrics WHERE node_id", "logs WHERE node_id"):
            store.x(f"DELETE FROM {t}=?", (nid,))
        return {"ok": True}

    @app.get("/api/enroll", dependencies=[Depends(require_ui)])
    def enroll(request: Request) -> dict:
        hub = str(request.base_url).rstrip("/")
        return {"hub": hub, "command": f"irm '{hub}/agent/install.ps1?t={cfg['agent_token']}' | iex"}

    @app.patch("/api/peers/{pid}", dependencies=[Depends(require_ui)])
    def patch_peer(pid: str, body: PeerPatch) -> dict:
        if not store.q("SELECT 1 FROM peers WHERE id=?", (pid,)):
            raise HTTPException(404, "no such peer")
        store.touch("peers", "id", pid, "priority=?", (body.priority,))
        return {"id": pid, "priority": body.priority}

    @app.delete("/api/peers/{pid}", dependencies=[Depends(require_ui)])
    def delete_peer(pid: str) -> dict:
        if pid == me:
            raise HTTPException(400, "cannot remove the hub you are talking to; remove it from another hub")
        if not store.q("SELECT 1 FROM peers WHERE id=?", (pid,)):
            raise HTTPException(404, "no such peer")
        store.delete_row("peers", pid)
        return {"ok": True}

    # ---------- cluster API ----------
    @app.get("/cluster/status", dependencies=[Depends(require_cluster)])
    def cluster_status() -> dict:
        return cluster.status() | {"bad_versions": lease.bad_versions(), "update_lease": lease.view()}

    @app.get("/cluster/sync", dependencies=[Depends(require_cluster)])
    def cluster_sync(since: float = -1.0) -> dict:
        return cluster.sync_page(since)

    @app.get("/cluster/bootstrap")
    def cluster_bootstrap(t: str = "") -> dict:
        if not secrets.compare_digest(t, cfg["agent_token"]):
            raise HTTPException(401, "bad token")
        version, sha = releases.latest()
        return {"ui_token": cfg["ui_token"], "agent_token": cfg["agent_token"], "cluster_token": cfg["cluster_token"],
                "peers": [{"id": p["id"], "priority": p["priority"], "urls": p["urls"]} for p in cluster.peers()],
                "bundle_version": version, "bundle_sha256": sha}

    @app.post("/cluster/join", dependencies=[Depends(require_cluster)])
    async def cluster_join(request: Request):
        raw = await request.body()
        fwd = await run_in_threadpool(leader_only_or_forward, request, raw)
        if fwd is not None:
            return fwd
        try:
            body = JoinIn.model_validate_json(raw)
        except ValueError as e:
            raise HTTPException(422, str(e))
        urls = [u.strip().rstrip("/") for u in body.urls if re.match(r"^https?://[^\s/]+", u.strip())]
        return cluster.join(body.id, urls)

    def _bundle_auth(request: Request) -> None:
        tok = request.headers.get("x-cluster-token") or request.headers.get("x-agent-token") \
            or request.query_params.get("t") or ""
        if not (secrets.compare_digest(tok, cfg["cluster_token"]) or secrets.compare_digest(tok, cfg["agent_token"])):
            raise HTTPException(401, "bad token")

    @app.get("/cluster/bundle", dependencies=[Depends(_bundle_auth)])
    def cluster_bundle(version: Optional[str] = None) -> Response:
        got = releases.get(version)
        if not got:
            raise HTTPException(404, "no such bundle version")
        ver, data, sha = got
        return Response(data, media_type="application/zip",
                        headers={"X-Bundle-Version": ver, "X-Bundle-SHA256": sha,
                                 "Content-Disposition": f'attachment; filename="forge-{ver}.zip"'})

    @app.post("/cluster/publish", dependencies=[Depends(require_cluster)])
    async def cluster_publish(request: Request):
        data = await request.body()
        try:
            version, sha = await run_in_threadpool(releases.publish, data)
        except BundleError as e:
            return JSONResponse({"detail": str(e)}, status_code=e.status)
        store.event(me, "ok", f"bundle {version} published to {me}")
        return {"ok": True, "version": version, "sha256": sha}

    # ---------- Phase 2: update lease / fleet-bad (cluster token; standbys proxy writes to the leader) ----------
    async def _leader_json(request: Request):
        raw = await request.body()
        fwd = await run_in_threadpool(leader_only_or_forward, request, raw)
        if fwd is not None:
            return fwd, None
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            raise HTTPException(422, "invalid json")
        return None, body if isinstance(body, dict) else {}

    @app.get("/cluster/update-lease", dependencies=[Depends(require_cluster)])
    def lease_view() -> dict:
        return {"lease": lease.view(), "bad_versions": lease.bad_versions()}

    @app.post("/cluster/update-lease", dependencies=[Depends(require_cluster)])
    async def lease_request(request: Request):
        fwd, body = await _leader_json(request)
        if fwd is not None:
            return fwd
        nid, ver = str(body.get("node_id") or "").strip().lower(), str(body.get("version") or "").strip()
        if not nid or not VERSION_RE.match(ver):
            raise HTTPException(422, "node_id and version (x.y.z) required")
        return await run_in_threadpool(lease.request, nid, ver)

    @app.delete("/cluster/update-lease/{node_id}", dependencies=[Depends(require_cluster)])
    async def lease_release(node_id: str, request: Request):
        fwd, _ = await _leader_json(request)
        if fwd is not None:
            return fwd
        return {"ok": True, "released": lease.release(node_id)}

    @app.post("/cluster/bad-version", dependencies=[Depends(require_cluster)])
    async def bad_version_mark(request: Request):
        """Optional explicit report from a watchdog that rolled back: {version, node_id?, reason?}."""
        fwd, body = await _leader_json(request)
        if fwd is not None:
            return fwd
        ver = str(body.get("version") or "").strip()
        if not VERSION_RE.match(ver):
            raise HTTPException(422, "version (x.y.z) required")
        lease.mark_bad(ver, str(body.get("reason") or f"rolled back on {body.get('node_id') or 'a node'}")[:200])
        lease.release(str(body.get("node_id") or ""))
        return {"ok": True, "bad_versions": lease.bad_versions()}

    @app.delete("/cluster/bad-version/{version}", dependencies=[Depends(require_cluster)])
    async def bad_version_clear(version: str, request: Request):
        fwd, _ = await _leader_json(request)
        if fwd is not None:
            return fwd
        return {"ok": True, "cleared": lease.clear_bad(version), "bad_versions": lease.bad_versions()}

    # ---------- Phase 2: agent ingest ----------
    @app.post("/api/agent/sessions", dependencies=[Depends(require_agent)])
    def agent_sessions(body: dict) -> dict:
        return p2.ingest_sessions(body, clock())

    @app.post("/api/agent/commits", dependencies=[Depends(require_agent)])
    def agent_commits(body: dict) -> dict:
        return p2.ingest_commits(body, clock())

    @app.post("/api/agent/transcript", dependencies=[Depends(require_agent)])
    def agent_transcript(body: dict) -> dict:
        return p2.ingest_transcript(body, clock())

    # ---------- Phase 2: projects / sessions / briefs / scheduler (UI) ----------
    @app.get("/api/projects", dependencies=[Depends(require_ui)])
    def projects_list() -> list:
        return p2.list_projects(clock())

    @app.post("/api/projects/merge", dependencies=[Depends(require_ui)])
    def projects_merge(body: dict) -> dict:
        ids = body.get("ids")
        if not isinstance(ids, list):
            raise HTTPException(400, "ids must be a list")
        return p2.merge_projects(ids, clock())

    @app.get("/api/projects/{pid}", dependencies=[Depends(require_ui)])
    def project_get(pid: str) -> dict:
        return p2.get_project(pid, clock())

    @app.patch("/api/projects/{pid}", dependencies=[Depends(require_ui)])
    def project_patch(pid: str, body: dict) -> dict:
        return p2.patch_project(pid, body)

    @app.post("/api/projects/{pid}/brief", dependencies=[Depends(require_ui)])
    def project_brief(pid: str) -> dict:
        return p2.brief_now(pid, clock())

    @app.post("/api/brief/all", dependencies=[Depends(require_ui)])
    def brief_all() -> dict:
        return p2.brief_all(clock())

    @app.get("/api/brief/all", dependencies=[Depends(require_ui)])
    def brief_all_latest() -> dict:
        r = store.q("SELECT text,ts,model,job_id FROM briefs WHERE project_id='*' ORDER BY ts DESC LIMIT 1")
        row = dict(r[0]) if r else None
        return {"brief": row, **(row or {"text": None, "ts": None, "model": None, "job_id": None})}   # nested + flat

    @app.post("/api/sessions/{tool}/{sid}/watch", dependencies=[Depends(require_ui)])
    def session_watch(tool: str, sid: str) -> dict:
        return p2.watch(tool, sid, clock())

    @app.get("/api/sessions/{tool}/{sid}/transcript", dependencies=[Depends(require_ui)])
    def session_transcript(tool: str, sid: str, since: int = 0) -> dict:
        return p2.transcript(tool, sid, since, clock())

    @app.get("/api/scheduler", dependencies=[Depends(require_ui)])
    def scheduler() -> dict:
        return p2.scheduler_view(clock())

    @app.delete("/api/scheduler/cooldowns/{engine}", dependencies=[Depends(require_ui)])
    def scheduler_clear(engine: str) -> dict:
        return {"ok": True, "cleared": p2.clear_cooldown(engine)}

    @app.get("/api/jobs", dependencies=[Depends(require_ui)])
    def list_jobs(include_brief: bool = False, limit: int = 40) -> list:
        sql = "SELECT * FROM jobs " + ("" if include_brief else "WHERE COALESCE(source,'')<>'brief' ") + "ORDER BY id DESC LIMIT ?"
        return [_job_row(r, False) for r in store.q(sql, (max(1, min(limit, 200)),))]

    # ---------- agent distribution ----------
    @app.get("/agent/forge-agent.ps1", response_class=PlainTextResponse)
    def agent_script_legacy() -> str:
        """v1 agents self-update from here; it must keep serving the v1 script."""
        if not legacy_agent.exists():
            raise HTTPException(404, "legacy agent missing")
        return legacy_agent.read_text(encoding="utf-8")

    @app.get("/agent/v2/forge-agent.ps1", response_class=PlainTextResponse)
    def agent_script_v2() -> str:
        p = release_dir / "agent" / "forge-agent.ps1"
        if not p.exists():
            raise HTTPException(404, "agent script missing")
        return p.read_text(encoding="utf-8")

    @app.get("/agent/install.ps1", response_class=PlainTextResponse)
    def agent_installer(t: str, request: Request) -> str:
        if not secrets.compare_digest(t, cfg["agent_token"]):
            raise HTTPException(401, "bad token")
        tpl = (release_dir / "agent" / "install.ps1").read_text(encoding="utf-8")
        return tpl.replace("__HUB__", str(request.base_url).rstrip("/")).replace("__TOKEN__", t)

    # ---------- UI ----------
    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(BASE / "static" / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/healthz")
    def health() -> dict:
        return {"ok": True, "node_id": me, "role": cluster.role}

    return app


app = create_app() if os.environ.get("FORGE_NO_AUTOAPP") != "1" else None
