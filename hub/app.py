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
from forge_bundle import BundleError, Releases, parse_version
from forge_cluster import Cluster
from forge_store import Clock, Store, jloads, new_id

BASE = Path(__file__).resolve().parent
ONLINE_SECS = 30
NODE_LOST_SECS = 90            # starting/running jobs on a node unseen this long are failed
LEADER_GRACE_SECS = 30         # a new leader waits this long before judging presence
WATCHDOG_STALE_SECS = 300
DISK_ALERT_PCT = 90
OUTPUT_CAP = 256_000
METRIC_KEEP_SECS = 24 * 3600
TICK_SECS = 3
USER_ENGINES = {"claude", "codex", "shell"}       # user lane only
SYSTEM_ENGINES = {"action", "admin"}              # SYSTEM lane only (run_as forced to "system")
ENGINES = USER_ENGINES | SYSTEM_ENGINES
ACTIVE = ("queued", "starting", "running")
# paths a standby answers itself instead of forwarding to the leader
LOCAL_PREFIXES = ("/cluster/", "/static/", "/agent/")
LOCAL_PATHS = {"/", "/healthz", "/api/login", "/api/logout", "/api/enroll", "/favicon.ico"}


class JobIn(BaseModel):
    node_id: str
    engine: str
    prompt: str = Field(default="", max_length=20_000)
    cwd: Optional[str] = None
    full_access: bool = False
    source: str = Field(default="ui", max_length=64)
    run_as: Optional[str] = None          # "user" (default) | "system"
    action: Optional[str] = None          # engine "action": allowlisted action name (hub/actions.json)
    args: Optional[dict] = None           # engine "action": action args
    confirm: bool = False                 # required for engine "admin" and destructive actions


class JobUpdate(BaseModel):
    append: Optional[str] = None
    status: Optional[str] = None
    exit_code: Optional[int] = None
    pid: Optional[int] = None


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
    def check_presence(now: Optional[float] = None) -> None:
        now = clock() if now is None else now
        for r in store.q("SELECT id,last_seen,online FROM nodes"):
            is_on = int(now - (r["last_seen"] or 0) < ONLINE_SECS)
            if is_on != r["online"]:
                store.touch("nodes", "id", r["id"], "online=?", (is_on,))
                store.event(r["id"], "ok" if is_on else "warn", "came online" if is_on else "went offline")
        for j in store.q(
                "SELECT j.id, j.node_id, j.engine FROM jobs j JOIN nodes n ON n.id=j.node_id "
                "WHERE j.status IN ('starting','running') AND ? - COALESCE(CASE WHEN j.run_as='system' "
                "THEN n.sys_last_seen ELSE n.last_seen END, 0) > ?", (now, NODE_LOST_SECS)):
            store.touch("jobs", "id", j["id"], "status='failed', finished=?, output=output||?",
                        (now, "\n[hub] node offline; job lost\n"))
            store.event(j["node_id"], "error", f"job {j['id'][:8]} ({j['engine']}) failed: node offline")
        resolve_auto(now)

    def trim(now: Optional[float] = None) -> None:
        now = clock() if now is None else now
        store.x("DELETE FROM metrics WHERE ts < ?", (now - METRIC_KEEP_SECS,))
        store.x("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 2000)")

    def tick() -> None:
        cluster.tick()
        now = clock()
        if cluster.is_leader and now - (cluster.leader_since or now) >= LEADER_GRACE_SECS:
            check_presence(now)
        trim(now)

    app.state.check_presence, app.state.tick, app.state.trim = check_presence, tick, trim

    if start_monitor:
        def loop() -> None:
            while True:
                try:
                    tick()
                except Exception:  # keep the monitor alive
                    pass
                time.sleep(TICK_SECS)
        threading.Thread(target=loop, daemon=True, name="forge-tick").start()

    # ---------- auto target resolution ----------
    def resolve_auto(now: Optional[float] = None) -> None:
        """Assign queued node_id='auto' jobs to the best online node (lowest cpu, then most free slots, then priority)."""
        now = clock() if now is None else now
        pending = store.q("SELECT id,engine FROM jobs WHERE node_id='auto' AND status='queued' "
                          "AND COALESCE(run_as,'user')='user' ORDER BY id")
        if not pending:
            return
        queued = {r["node_id"]: r["n"] for r in store.q(
            "SELECT node_id, COUNT(*) n FROM jobs WHERE status='queued' AND COALESCE(run_as,'user')='user' "
            "GROUP BY node_id")}
        nodes = []
        for r in store.q("SELECT id,info,metrics,capacity,last_seen FROM nodes"):
            if now - (r["last_seen"] or 0) < ONLINE_SECS:
                nodes.append((r["id"], _tools(jloads(r["info"], {})), jloads(r["metrics"], {}).get("cpu"), r["capacity"] or 0))
        for job in pending:
            best, best_key = None, None
            for nid, tools, cpu, cap in nodes:
                free = cap - queued.get(nid, 0)
                if free <= 0 or (job["engine"] != "shell" and job["engine"] not in tools):
                    continue
                key = (cpu if cpu is not None else 100.0, -free, cluster.priority_of(nid), nid)
                if best_key is None or key < best_key:
                    best, best_key = nid, key
            if best:
                store.touch("jobs", "id", job["id"], "node_id=?", (best,))
                queued[best] = queued.get(best, 0) + 1
                store.event(best, "info", f"job {job['id'][:8]} ({job['engine']}) auto-assigned to {best}")

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

    def effective_settings(nid: str) -> dict:
        r = store.q("SELECT work_root,max_jobs FROM settings WHERE node_id=?", (nid,))
        return {"work_root": r[0]["work_root"], "max_jobs": r[0]["max_jobs"]} if r else {"work_root": None, "max_jobs": None}

    # ---------- agent API ----------
    def dispatch(nid: str, lane: str, cap: int, boot: bool, now: float) -> tuple[list, list]:
        """Hand this lane's queued jobs to the agent. v1/pre-2.1 agents send no lane: they are the user lane
        and never see run_as='system' jobs."""
        lane_sql = "run_as='system'" if lane == "system" else "COALESCE(run_as,'user')='user'"
        if boot:
            for j in store.q(f"SELECT id FROM jobs WHERE node_id=? AND status IN ('starting','running') AND {lane_sql}",
                             (nid,)):
                store.touch("jobs", "id", j["id"], "status='failed', finished=?, output=output||?",
                            (now, "\n[hub] agent restarted; job lost\n"))
        jobs = []
        if cap:
            for j in store.q(f"SELECT * FROM jobs WHERE node_id=? AND status='queued' AND {lane_sql} ORDER BY id LIMIT ?",
                             (nid, cap)):
                store.touch("jobs", "id", j["id"], "status='starting', started=?", (now,))
                item = {k: j[k] for k in ("id", "engine", "prompt", "cwd")} | {"full_access": bool(j["full_access"])}
                if lane == "system":
                    item |= {"run_as": "system", "action": j["action"], "args": jloads(j["args"], {})}
                jobs.append(item)
                store.event(nid, "info", f"job {j['id'][:8]} ({j['engine']}) dispatched"
                            + (" to system lane" if lane == "system" else ""))
        cancel = [r["id"] for r in store.q(
            "SELECT id FROM jobs WHERE node_id=? AND cancel_requested=1 AND status IN ('starting','running') "
            f"AND {lane_sql}", (nid,))]
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
        for name, text in (body.get("logs") or {}).items():
            store.x("INSERT OR REPLACE INTO logs(node_id,name,ts,text) VALUES(?,?,?,?)", (nid, name, now, str(text)[-60_000:]))
        gpus = metrics.get("gpu") or []
        store.x("INSERT INTO metrics VALUES(?,?,?,?,?,?)", (
            nid, now, metrics.get("cpu"), metrics.get("mem_pct"),
            max((g.get("util") or 0) for g in gpus) if gpus else None, _disk_pct(metrics)))
        resolve_auto(now)
        jobs, cancel = dispatch(nid, "user", max(0, int(body.get("capacity") or 0)), bool(body.get("boot")), now)
        info = row[0]["info"] if row else "{}"
        return {"jobs": jobs, "cancel": cancel, "need_info": info in ("", "{}"), "server_time": now,
                "agent_version": agent_version(), "settings": effective_settings(nid),
                "hub_urls": cluster.hub_urls(), "bundle_version": releases.latest_version()}

    @app.post("/api/agent/jobs/{jid}/update", dependencies=[Depends(require_agent)])
    def agent_job_update(jid: str, u: JobUpdate) -> dict:
        r = store.q("SELECT output,status,node_id,engine FROM jobs WHERE id=?", (jid,))
        if not r:
            raise HTTPException(404, "no such job")
        out = r[0]["output"] or ""
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
    def state(request: Request) -> dict:
        now = clock()
        running = {r["node_id"]: r["n"] for r in store.q(
            "SELECT node_id, COUNT(*) n FROM jobs WHERE status IN ('queued','starting','running') GROUP BY node_id")}
        settings = {r["node_id"]: {"work_root": r["work_root"], "max_jobs": r["max_jobs"]}
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
                "settings": settings.get(r["id"], {"work_root": None, "max_jobs": None}),
                "spark": [dict(x) for x in store.q(
                    "SELECT ts,cpu,mem FROM metrics WHERE node_id=? AND ts>? ORDER BY ts", (r["id"], now - 900))][::3],
            })
        jobs = [_job_row(r, False) for r in store.q("SELECT * FROM jobs ORDER BY id DESC LIMIT 40")]
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
        return {"now": now, "alerts": alerts, "nodes": nodes_out, "cluster": block, "leader_id": cluster.leader_id,
                "running_version": mine, "bundle_version": target, "rollout": rollout}

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
                "SELECT * FROM jobs WHERE node_id=? ORDER BY id DESC LIMIT 25", (nid,))],
        }

    @app.put("/api/nodes/{nid}/settings", dependencies=[Depends(require_ui)])
    def put_settings(nid: str, body: dict) -> dict:
        if not store.q("SELECT 1 FROM nodes WHERE id=?", (nid,)):
            raise HTTPException(404, "no such node")
        cur = effective_settings(nid)
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
        store.x("INSERT OR REPLACE INTO settings(node_id,work_root,max_jobs,updated) VALUES(?,?,?,?)",
                (nid, cur["work_root"], cur["max_jobs"], clock()))
        return cur

    def admin_enabled() -> bool:
        return cfg.get("admin_lane_enabled", True) is not False

    @app.get("/api/actions", dependencies=[Depends(require_ui)])
    def list_actions() -> dict:
        return actions_catalog | {"admin_lane_enabled": admin_enabled()}

    @app.post("/api/jobs", dependencies=[Depends(require_ui)])
    def create_job(j: JobIn) -> dict:
        if j.engine not in ENGINES:
            raise HTTPException(400, f"engine must be one of {sorted(ENGINES)}")
        if j.run_as not in (None, "user", "system"):
            raise HTTPException(400, "run_as must be 'user' or 'system'")
        system = j.engine in SYSTEM_ENGINES          # action/admin always run as SYSTEM
        if not system and j.run_as == "system":
            raise HTTPException(400, f"engine {j.engine} runs in the user lane only; use engine 'admin' or 'action'")
        auto = j.node_id == "auto"
        action, args, prompt = None, None, j.prompt
        if system:
            if auto:
                raise HTTPException(400, "system-lane jobs need a specific machine (not auto)")
            if j.engine == "action":
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
        if not auto and not store.q("SELECT 1 FROM nodes WHERE id=?", (j.node_id,)):
            raise HTTPException(404, "no such node")
        jid, now = new_id(clock), clock()
        status, output, finished = "queued", "", None
        if system and not admin_enabled():
            status, finished = "failed", now
            output = "[hub] admin lane is disabled on this hub (config.json admin_lane_enabled=false); job not run\n"
        store.x(
            "INSERT INTO jobs(id,node_id,engine,prompt,cwd,full_access,status,created,finished,output,source,requested,"
            "run_as,action,args,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (jid, j.node_id, j.engine, prompt, None if system else (j.cwd or None), int(j.full_access and not system),
             status, now, finished, output, j.source, "auto" if auto else None, "system" if system else "user",
             action, json.dumps(args) if args is not None else None, now))
        if system:
            what = f"ACTION {action}" if action else "ADMIN"
            store.event(j.node_id, "warn", f"{what} job {jid[:8]} queued for system lane (source {j.source})"
                        + (" - refused: admin lane disabled on hub" if status == "failed" else ""))
        else:
            store.event(j.node_id, "info", f"job {jid[:8]} ({j.engine}) queued" + (" (auto)" if auto else ""))
        if auto:
            resolve_auto(now)
        return {"id": jid, "node_id": store.q("SELECT node_id FROM jobs WHERE id=?", (jid,))[0]["node_id"],
                "run_as": "system" if system else "user", "status": status}

    @app.get("/api/jobs/{jid}", dependencies=[Depends(require_ui)])
    def get_job(jid: str) -> dict:
        r = store.q("SELECT * FROM jobs WHERE id=?", (jid,))
        if not r:
            raise HTTPException(404, "no such job")
        return _job_row(r[0], True)

    @app.post("/api/jobs/{jid}/cancel", dependencies=[Depends(require_ui)])
    def cancel_job(jid: str) -> dict:
        r = store.q("SELECT status FROM jobs WHERE id=?", (jid,))
        if not r:
            raise HTTPException(404, "no such job")
        if r[0]["status"] == "queued":
            store.touch("jobs", "id", jid, "status='cancelled', finished=?", (clock(),))
        elif r[0]["status"] in ("starting", "running"):
            store.touch("jobs", "id", jid, "cancel_requested=1")
        return {"ok": True}

    @app.delete("/api/nodes/{nid}", dependencies=[Depends(require_ui)])
    def forget_node(nid: str) -> dict:
        store.delete_row("nodes", nid)
        store.delete_row("settings", nid)
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
        return cluster.status()

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
