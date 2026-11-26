"""Phase 2 hub logic: smart scheduler (placement, cooldowns, requeue), session/project index, briefings,
live transcripts. Wired into the FastAPI app by hub/app.py (which owns auth, forwarding and job endpoints).
Everything here runs on the LEADER; standbys proxy API calls to it and replicate the persistent tables."""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import HTTPException

import forge_projects as fp
import forge_sched as fs
from forge_store import Clock, Store, jloads, new_id

ONLINE_SECS = 30
BRIEF_EVERY = 30 * 60
BRIEF_MAX_PER_LOOP = 6
BRIEF_MODEL_DEFAULT = "qwen2.5:14b"
CLAUDE_BRIEF_MODEL = "haiku"
BRIEF_WINDOW_DEFAULT = "00:00-07:00"      # local time; the automatic brief loop only runs inside it
BRIEF_DIGEST_DEFAULT = "07:30"            # local time of the daily morning roll-up
BRIEF_DIGEST_LATE_S = 3 * 3600            # a hub that comes up later than digest+3h skips that day's digest
BRIEF_GPU_MAX = 30                        # the loop waits while the brief node's GPU is busier than this (%)
BRIEFS_KEEP = 20                          # briefs kept per project
JOB_KEEP_DAYS = 30                        # finished jobs older than this are pruned...
JOB_KEEP_MIN = 200                        # ...except the newest 200 jobs, which are always kept
STALE_CANCEL_TTL = 60.0                   # how long a node keeps being told to kill a job it no longer holds
                                          # (and is not handed that job again)
ACTIVE = ("queued", "starting", "running")
KINDS = ("code", "text")
_HHMM = re.compile(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\-]{0,99}$")
BRIEF_NOISE = re.compile(r"^\s*(pulling |verifying |writing manifest|success\b|\[ollama\]|\[forge\])", re.I)


def to_ts(v: Any) -> Optional[float]:
    """Epoch seconds from a number or ISO-8601 string (PowerShell 'o' format tolerated on py3.10)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) / 1000.0 if v > 1e11 else float(v)       # tolerate epoch milliseconds
    if not isinstance(v, str) or not v.strip():
        return None
    s = re.sub(r"(\.\d{6})\d+", r"\1", v.strip().replace("Z", "+00:00"))
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


def _num(v: Any) -> Optional[float]:
    try:
        return None if isinstance(v, bool) else float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def clean_brief_text(output: str) -> str:
    lines = [ln for ln in (output or "").splitlines() if not BRIEF_NOISE.match(ln)]
    return "\n".join(lines).strip()[:4000]


def parse_hhmm(v: Any) -> Optional[int]:
    """'07:30' -> minutes after midnight, None if malformed."""
    m = _HHMM.match(str(v or ""))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def parse_window(v: Any) -> Optional[tuple[int, int]]:
    """'00:00-07:00' -> (0, 420) in minutes; 'always' -> (0, 1440); None if malformed. end < start wraps midnight."""
    s = str(v or "").strip().lower()
    if s in ("always", "any", "*"):
        return (0, 24 * 60)
    a, sep, b = s.partition("-")
    lo, hi = parse_hhmm(a), parse_hhmm(b)
    if not sep or lo is None or hi is None:
        return None
    return (0, 24 * 60) if lo == hi else (lo, hi)


def local_dt(now: float) -> datetime:
    return datetime.fromtimestamp(now).astimezone()


def in_window(window: tuple[int, int], now: float) -> bool:
    d = local_dt(now)
    m = d.hour * 60 + d.minute
    lo, hi = window
    return lo <= m < hi if lo <= hi else (m >= lo or m < hi)


class Phase2:
    def __init__(self, store: Store, cfg: dict, cluster, clock: Clock):
        self.store, self.cfg, self.cluster, self.clock = store, cfg, cluster, clock
        self.eph = fp.Ephemeral()
        self._last_brief: Optional[float] = None
        self.t3_status: dict[str, str] = {}
        # One lock for every check-then-act on job placement/ownership (place_jobs, dispatch, lost-job handling, agent
        # job updates). Agent reports run concurrently in the server threadpool; without it two reports could both
        # place or hand out the same queued job, or relabel a job another node is already running.
        self.sched_lock = threading.RLock()
        self._stale: dict[str, dict[str, float]] = {}      # node -> {job id: expires}: jobs that node must kill

    # ================================================================ stale holders (fencing)
    def add_stale(self, node_id: Optional[str], jid: str, now: float) -> None:
        if node_id and node_id != "auto":
            with self.sched_lock:
                self._stale.setdefault(node_id, {})[jid] = now + STALE_CANCEL_TTL

    def stale_for(self, node_id: str, now: float) -> list[str]:
        with self.sched_lock:
            d = self._stale.get(node_id) or {}
            for k in [k for k, exp in d.items() if exp <= now]:
                del d[k]
            return list(d)

    # ================================================================ nodes / scheduler
    def nodes(self, now: float) -> dict[str, dict]:
        out = {}
        for r in self.store.q("SELECT id,info,metrics,capacity,last_seen FROM nodes"):
            info, m = jloads(r["info"], {}), jloads(r["metrics"], {})
            gpus = [g for g in (m.get("gpu") or []) if isinstance(g, dict)]
            caps = info.get("capabilities")
            out[r["id"]] = {
                "id": r["id"], "online": now - (r["last_seen"] or 0) < ONLINE_SECS, "tools": _tools(info),
                "agent_version": info.get("agent_version"), "cpu": m.get("cpu"), "mem_pct": m.get("mem_pct"),
                "gpu": bool(gpus), "gpu_mem": max((g.get("mem_total_mb") or 0) for g in gpus) if gpus else 0,
                "gpu_util": max((_num(g.get("util")) or 0) for g in gpus) if gpus else None,
                "free_gb": m.get("work_root_free_gb"), "capacity": r["capacity"] or 0,
                "caps": set(caps.keys() if isinstance(caps, dict) else caps) if isinstance(caps, (list, dict)) else None}
        return out

    def cooldowns(self, now: float) -> dict[str, float]:
        return fs.active_cooldowns(((r["engine"], r["until"]) for r in self.store.q("SELECT engine,until FROM cooldowns")), now)

    def preference(self) -> list[str]:
        p = self.cfg.get("engine_preference")
        return [e for e in p if e in fs.DEFAULT_PREFERENCE] if isinstance(p, list) and p else list(fs.DEFAULT_PREFERENCE)

    @staticmethod
    def has_engine(n: dict, engine: str) -> bool:
        if engine == "shell":
            return True
        if engine == "ollama":
            return "ollama" in n["tools"] and fs.agent_is_new(n["agent_version"])
        return engine in n["tools"]

    @staticmethod
    def needs_new_agent(engine: str, resume: Any) -> bool:
        return engine == "ollama" or bool(resume)

    def dispatchable(self, job: Any, agent_version: Optional[str], cds: dict[str, float]) -> bool:
        """May this queued job be handed to an agent right now? (engine resolved, not cooling, agent new enough)"""
        if job["engine"] == "any" or job["engine"] in cds:
            return False
        if self.needs_new_agent(job["engine"], job["resume"]) and not fs.agent_is_new(agent_version):
            return False
        return True

    def place_jobs(self, now: float) -> None:
        """Resolve node_id='auto' and engine='any' for queued user-lane jobs; maintain wait_reason.
        Serialized with dispatch() (sched_lock) so concurrent agent reports never place or relabel a job twice."""
        with self.sched_lock:
            self._place_jobs(now)

    def _place_jobs(self, now: float) -> None:
        cds = self.cooldowns(now)
        marks = ",".join("?" * len(cds)) if cds else "''"
        # only jobs that may need work: unresolved node/engine, a wait_reason to refresh, resume/ollama (agent gate)
        # or an engine that is cooling down (explicit engine on a fixed node is otherwise left to dispatch)
        rows = self.store.q(
            "SELECT id,node_id,engine,req_engine,requested,kind,resume,wait_reason FROM jobs WHERE status='queued' "
            "AND COALESCE(run_as,'user')='user' AND (node_id='auto' OR engine='any' OR wait_reason IS NOT NULL "
            f"OR resume IS NOT NULL OR engine='ollama' OR engine IN ({marks})) ORDER BY id", tuple(cds))
        if not rows:
            return
        nodes, pref = self.nodes(now), self.preference()
        queued = {r["node_id"]: r["n"] for r in self.store.q(
            "SELECT node_id, COUNT(*) n FROM jobs WHERE status='queued' AND COALESCE(run_as,'user')='user' GROUP BY node_id")}
        active = {r["node_id"]: r["n"] for r in self.store.q(
            "SELECT node_id, COUNT(*) n FROM jobs WHERE status IN ('queued','starting','running') GROUP BY node_id")}
        leader = self.cluster.leader_id
        for job in rows:
            auto = job["node_id"] == "auto"
            # an 'any' job stays an 'any' job after it was resolved: if its engine starts cooling down before dispatch
            # it moves to the next engine instead of waiting like an explicit-engine job
            wants_any = job["engine"] == "any" or job["req_engine"] == "any"
            engines = fs.any_engines(pref, job["kind"]) if wants_any else [job["engine"]]
            if wants_any and not auto and job["engine"] in cds and job["requested"] == "auto":
                auto = True                                       # re-place from scratch (the node may lack the new engine)
                queued[job["node_id"]] = queued.get(job["node_id"], 1) - 1
                active[job["node_id"]] = active.get(job["node_id"], 1) - 1
            if auto:
                cand = [n for n in nodes.values() if n["online"] and n["capacity"] - queued.get(n["id"], 0) > 0]
            else:
                cand = [nodes[job["node_id"]]] if job["node_id"] in nodes else []
            avail, best = {}, {}
            for e in engines:
                if e in cds:
                    continue
                # explicit engine on a fixed node: the agent decides (as in 2.1); otherwise need the tool
                ok = cand if not (auto or wants_any) else [n for n in cand if self.has_engine(n, e)]
                if ok:
                    avail[e] = True
                    best[e] = min(ok, key=lambda n: fs.rank_key(
                        fs.score_node(n["cpu"], n["mem_pct"], active.get(n["id"], 0), n["id"] == leader, n["gpu"], e,
                                      n["free_gb"]),
                        n["capacity"] - queued.get(n["id"], 0), self.cluster.priority_of(n["id"]), n["id"]))
            chosen = fs.pick_engine(engines, avail, cds)
            reason: Optional[str] = None
            if chosen is None:
                cooling = [e for e in engines if e in cds]
                if cooling:
                    first = min(cooling, key=lambda e: cds[e])
                    reason = fs.wait_reason_cooldown(first, cds[first])
                elif auto or wants_any:
                    reason = "waiting for a capable node"
                else:
                    reason = "node has not reported yet"
            else:
                node = best[chosen]
                sets, args = [], []
                if auto:
                    if node["id"] != job["node_id"]:
                        sets.append("node_id=?"); args.append(node["id"])
                        self.store.event(node["id"], "info", f"job {job['id'][:8]} ({chosen}) auto-assigned to {node['id']}")
                    queued[node["id"]] = queued.get(node["id"], 0) + 1
                    active[node["id"]] = active.get(node["id"], 0) + 1
                if wants_any and (chosen != job["engine"] or job["req_engine"] != "any"):
                    sets.append("engine=?"); args.append(chosen)
                    sets.append("req_engine=?"); args.append("any")
                if self.needs_new_agent(chosen, job["resume"]) and not fs.agent_is_new(node["agent_version"]):
                    reason = "node agent is older than 2.2.0 (needed for ollama/resume jobs)"
                if reason != job["wait_reason"]:
                    sets.append("wait_reason=?"); args.append(reason)
                if sets:     # conditional: only a job that is still queued where we saw it
                    self.store.touch("jobs", "id", job["id"], ", ".join(sets), tuple(args),
                                     "status='queued' AND node_id=?", (job["node_id"],))
                continue
            if reason != job["wait_reason"]:
                self.store.touch("jobs", "id", job["id"], "wait_reason=?", (reason,), "status='queued'")

    def set_cooldown(self, engine: str, until: float, reason: str, now: float) -> bool:
        cur = self.store.q("SELECT until FROM cooldowns WHERE engine=?", (engine,))
        if cur and cur[0]["until"] and cur[0]["until"] > now and until <= cur[0]["until"] + 60:
            return False
        self.store.x("INSERT OR REPLACE INTO cooldowns(engine,until,reason,updated) VALUES(?,?,?,?)",
                     (engine, until, reason[:200], now))
        self.store.event(None, "warn", f"{engine} usage limit hit; {fs.wait_reason_cooldown(engine, until)}")
        return True

    def clear_cooldown(self, engine: str) -> bool:
        if not self.store.q("SELECT 1 FROM cooldowns WHERE engine=?", (engine,)):
            return False
        self.store.delete_row("cooldowns", engine)
        self.store.event(None, "info", f"{engine} cooldown cleared manually")
        return True

    def check_limit(self, job: Any, appended: Optional[str], status: Optional[str], exit_code: Optional[int],
                    full_output: str, now: float) -> None:
        """Cooldown detection. Only a claude/codex (non-brief) job that ENDED with a non-zero exit code counts, and only a
        CLI-style limit message in the last 20 lines of its output (fs.limit_message). Streamed output is never checked:
        a job that prints "added rate limit middleware" must not put the engine on cooldown fleet-wide."""
        engine = job["engine"]
        if engine not in ("claude", "codex") or job["source"] == "brief":
            return
        if status not in ("done", "failed") or exit_code in (None, 0):
            return
        text = fs.limit_message(full_output)
        if text:
            until = fs.cooldown_until(text, now) or now + fs.DEFAULT_COOLDOWN_S
            self.set_cooldown(engine, until, text.strip()[-150:], now)

    def scheduler_view(self, now: float) -> dict:
        cds = self.cooldowns(now)
        nodes = self.nodes(now)
        leader = self.cluster.leader_id
        active = {r["node_id"]: r["n"] for r in self.store.q(
            "SELECT node_id, COUNT(*) n FROM jobs WHERE status IN ('queued','starting','running') GROUP BY node_id")}
        preview: dict[str, list] = {}
        for e in ("claude", "codex", "ollama", "shell"):
            ranked = []
            for n in nodes.values():
                if n["online"] and n["capacity"] > 0 and self.has_engine(n, e):
                    sc = fs.score_node(n["cpu"], n["mem_pct"], active.get(n["id"], 0), n["id"] == leader, n["gpu"], e,
                                       n["free_gb"])
                    ranked.append((fs.rank_key(sc, n["capacity"], self.cluster.priority_of(n["id"]), n["id"]), n["id"], sc))
            preview[e] = [{"node_id": nid, "score": round(sc, 1)} for _, nid, sc in sorted(ranked)[:3]]
        def pick(kind: str) -> Optional[dict]:
            return next(({"engine": e, "node_id": preview[e][0]["node_id"]} for e in fs.any_engines(self.preference(), kind)
                         if e not in cds and preview.get(e)), None)
        depth = self.store.q("SELECT COUNT(*) n FROM jobs WHERE status='queued'")[0]["n"]
        return {"cooldowns": {e: {"until": u, "reason": (self.store.q("SELECT reason FROM cooldowns WHERE engine=?", (e,)) or
                                                          [{"reason": None}])[0]["reason"]} for e, u in cds.items()},
                "queue_depth": depth, "placement_preview": preview | {"any": pick("code"), "any_text": pick("text")},
                "engine_preference": self.preference(), "briefs": self.brief_settings(now)}

    # ================================================================ job insert
    def insert_job(self, *, node_id: str, engine: str, prompt: str, cwd: Optional[str] = None, full_access: bool = False,
                   source: str = "ui", requested: Optional[str] = None, run_as: str = "user", action: Optional[str] = None,
                   args: Optional[dict] = None, status: str = "queued", output: str = "", finished: Optional[float] = None,
                   resume: Optional[dict] = None, project_id: Optional[str] = None, model: Optional[str] = None,
                   req_engine: Optional[str] = None, kind: Optional[str] = None) -> str:
        jid, now = new_id(self.clock), self.clock()
        kind = kind if kind in KINDS else ("text" if source == "brief" else "code")
        self.store.x(
            "INSERT INTO jobs(id,node_id,engine,prompt,cwd,full_access,status,created,finished,output,source,requested,"
            "run_as,action,args,resume,project_id,model,req_engine,kind,attempts,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
            (jid, node_id, engine, prompt, cwd, int(full_access), status, now, finished, output, source, requested,
             run_as, action, json.dumps(args) if args is not None else None, json.dumps(resume) if resume else None,
             project_id, model, req_engine, kind, now))
        return jid

    def resolve_resume(self, resume: dict, node_id: str, force: bool, now: float) -> tuple[str, str, Optional[str]]:
        """Validate a resume request -> (engine, node_id, cwd). 404 unknown session, 409 live in a terminal."""
        tool, sid = str((resume or {}).get("tool") or ""), str((resume or {}).get("session_id") or "")
        if tool not in ("claude", "codex"):
            raise HTTPException(400, "resume.tool must be 'claude' or 'codex'")
        r = self.store.q("SELECT * FROM sessions WHERE id=?", (f"{tool}:{sid}",))
        if not sid or not r:
            raise HTTPException(404, "no such session")
        s = r[0]
        if node_id not in ("auto", "", s["node_id"]):
            raise HTTPException(400, f"session lives on {s['node_id']}; resume must run there")
        nodes = self.nodes(now)
        online = bool(nodes.get(s["node_id"], {}).get("online"))
        if fp.effective_state(s["state"], s["last_activity"], now, online) == "live" and s["pid"] and not force:
            raise HTTPException(409, "session is live in a terminal")
        return tool, s["node_id"], s["cwd"]

    # ================================================================ projects index
    def project_for_key(self, key: str) -> Optional[str]:
        a = self.store.q("SELECT project_id FROM project_alias WHERE id=?", (key,))
        if a:
            return a[0]["project_id"]
        r = self.store.q("SELECT id FROM projects WHERE key=? ORDER BY id LIMIT 1", (key,))
        return r[0]["id"] if r else None

    def ensure_project(self, key: str, cwd: Optional[str], now: float) -> str:
        pid = self.project_for_key(key)
        if pid:
            return pid
        pid = new_id(self.clock)
        self.store.x("INSERT INTO projects(id,key,name,group_name,pinned,created,updated) VALUES(?,?,?,?,0,?,?)",
                     (pid, key, fp.project_name(key, cwd), None, now, now))
        return pid

    def key_for(self, node_id: str, cwd: Optional[str], git_remote: Optional[str] = None) -> str:
        return fp.project_key(node_id, cwd, git_remote)

    def valid_project(self, pid: Optional[str]) -> bool:
        return bool(pid) and pid != "*" and bool(self.store.q("SELECT 1 FROM projects WHERE id=?", (pid,)))

    def note_job_cwd(self, node_id: str, cwd: Optional[str], now: float, project_id: Optional[str] = None) -> None:
        """Auto-create a project for a job with a cwd (git remote known via the repos/sessions tables), unless the
        job names an existing project (UI 'New task here' / Continue): it belongs there."""
        if not cwd or node_id == "auto" or self.valid_project(project_id):
            return
        self.ensure_project(self._key_from_repos(node_id, cwd), cwd, now)

    def _key_from_repos(self, node_id: str, cwd: str) -> str:
        norm = fp._cwd_norm(cwd)
        r = self.store.q("SELECT project_key FROM repos WHERE node_id=? AND lower(cwd)=?", (node_id, norm))
        if not r:
            r = self.store.q("SELECT project_key FROM repos WHERE id=?", (f"{node_id}|{norm}",))
        if not r:      # commits are scanned every few minutes; a session in that folder may already know the remote
            r = self.store.q("SELECT project_key FROM sessions WHERE node_id=? AND lower(rtrim(replace(cwd,'/','\\'),'\\'))=? "
                             "AND project_key IS NOT NULL ORDER BY last_activity DESC LIMIT 1", (node_id, norm))
        return r[0]["project_key"] if r else fp.project_key(node_id, cwd, None)

    # ---- ingestion (agent -> hub)
    def ingest_sessions(self, body: dict, now: float) -> dict:
        nid = str(body.get("node_id") or "").strip().lower()
        if not nid:
            raise HTTPException(400, "node_id required")
        if isinstance(body.get("t3"), str):
            self.t3_status[nid] = body["t3"]
        n_in = n_written = 0
        for s in body.get("sessions") or []:
            if not isinstance(s, dict):
                continue
            tool, sid = str(s.get("tool") or ""), str(s.get("session_id") or "")
            if tool not in ("claude", "codex", "t3") or not sid or len(sid) > 200:
                continue
            last = to_ts(s.get("last_activity"))
            if last is not None and now - last > fp.KEEP_DAYS * 86400:
                continue
            n_in += 1
            cwd = str(s.get("cwd") or "") or None
            remote = fp.normalize_remote(s.get("git_remote"))
            key = fp.project_key(nid, cwd, s.get("git_remote"))
            self.ensure_project(key, cwd, now)
            state = s.get("state") if s.get("state") in ("live", "idle", "done") else fp.session_state(last, now, bool(s.get("pid")))
            row = (f"{tool}:{sid}", tool, sid, nid, key, cwd, remote, str(s.get("git_branch") or "")[:200] or None,
                   fp.clean_title(s.get("title")), to_ts(s.get("started")), last, _int(s.get("messages")) if s.get("messages") is not None else None,
                   _int(s.get("tokens")) if s.get("tokens") is not None else None, state, int(bool(s.get("remote_control"))),
                   _int(s.get("pid")) or None, str(s.get("model") or "")[:100] or None)
            cur = self.store.q("SELECT id,tool,session_id,node_id,project_key,cwd,git_remote,git_branch,title,started,"
                               "last_activity,messages,tokens,state,remote_control,pid,model FROM sessions WHERE id=?", (row[0],))
            if cur and self._session_unchanged(tuple(cur[0]), row):
                continue
            self.store.x("INSERT OR REPLACE INTO sessions(id,tool,session_id,node_id,project_key,cwd,git_remote,git_branch,"
                         "title,started,last_activity,messages,tokens,state,remote_control,pid,model,updated) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (*row, now))
            n_written += 1
        for sid in body.get("removed") or []:
            sid = str(sid)
            ids = [sid] if ":" in sid and sid.split(":", 1)[0] in ("claude", "codex", "t3") else \
                [f"{t}:{sid}" for t in ("claude", "codex", "t3")]
            for i in ids:
                if self.store.q("SELECT 1 FROM sessions WHERE id=? AND node_id=?", (i, nid)):
                    self.store.delete_row("sessions", i)
        return {"ok": True, "accepted": n_in, "changed": n_written}

    SESSION_LAST = 10                 # index of last_activity in the session row tuple
    SESSION_ACTIVITY_SLACK = 60.0     # a session row is rewritten (and replicated) for activity moves > 60 s only

    @classmethod
    def _session_unchanged(cls, cur: tuple, new: tuple) -> bool:
        i = cls.SESSION_LAST
        if cur[:i] != new[:i] or cur[i + 1:] != new[i + 1:]:
            return False
        a, b = cur[i], new[i]
        if a is None or b is None:
            return a == b
        return abs(b - a) <= cls.SESSION_ACTIVITY_SLACK

    def ingest_commits(self, body: dict, now: float) -> dict:
        nid = str(body.get("node_id") or "").strip().lower()
        if not nid:
            raise HTTPException(400, "node_id required")
        changed = 0
        for r in body.get("repos") or []:
            if not isinstance(r, dict) or not r.get("cwd"):
                continue
            cwd = str(r["cwd"])
            remote = fp.normalize_remote(r.get("git_remote"))
            key = fp.project_key(nid, cwd, r.get("git_remote"))
            self.ensure_project(key, cwd, now)
            commits = []
            for c in (r.get("commits") or [])[:10]:
                if isinstance(c, dict) and c.get("sha"):
                    commits.append({"sha": str(c["sha"])[:40], "author": fp.redact(c.get("author"), 80),
                                    "ts": to_ts(c.get("ts")), "subject": fp.redact(c.get("subject"), 200)})
            row = (f"{nid}|{fp._cwd_norm(cwd)}", nid, cwd, remote, key, str(r.get("branch") or "")[:200] or None,
                   int(bool(r.get("dirty"))), _int(r.get("ahead")), _int(r.get("behind")), json.dumps(commits))
            cur = self.store.q("SELECT id,node_id,cwd,git_remote,project_key,branch,dirty,ahead,behind,commits "
                               "FROM repos WHERE id=?", (row[0],))
            if cur and tuple(cur[0]) == row:
                continue
            self.store.x("INSERT OR REPLACE INTO repos(id,node_id,cwd,git_remote,project_key,branch,dirty,ahead,behind,"
                         "commits,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (*row, now))
            changed += 1
        return {"ok": True, "changed": changed}

    # ---- views
    def _index(self, now: float) -> dict:
        projects = {r["id"]: dict(r) for r in self.store.q("SELECT * FROM projects")}
        key2pid = {p["key"]: pid for pid, p in projects.items()}
        for a in self.store.q("SELECT id,project_id FROM project_alias"):
            if a["project_id"] in projects:
                key2pid[a["id"]] = a["project_id"]
        nodes = self.nodes(now)
        sessions: dict[str, list] = {pid: [] for pid in projects}
        for r in self.store.q("SELECT * FROM sessions ORDER BY last_activity DESC"):
            pid = key2pid.get(r["project_key"])
            if pid:
                d = dict(r)
                d["state"] = fp.effective_state(r["state"], r["last_activity"], now, bool(nodes.get(r["node_id"], {}).get("online")))
                d["remote_control"] = bool(d["remote_control"])
                sessions[pid].append(d)
        repos: dict[str, list] = {pid: [] for pid in projects}
        for r in self.store.q("SELECT * FROM repos"):
            pid = key2pid.get(r["project_key"])
            if pid:
                d = dict(r)
                d["commits"], d["dirty"] = jloads(r["commits"], []), bool(r["dirty"])
                repos[pid].append(d)
        jobs: dict[str, list] = {pid: [] for pid in projects}
        keycache: dict[tuple, Optional[str]] = {}
        for r in self.store.q("SELECT id,node_id,engine,prompt,cwd,status,created,started,finished,updated,source,project_id "
                              "FROM jobs WHERE cwd IS NOT NULL AND cwd<>'' AND created>? AND COALESCE(run_as,'user')='user' "
                              "AND node_id<>'auto' ORDER BY created DESC", (now - fp.KEEP_DAYS * 86400,)):
            pid = r["project_id"] if r["project_id"] in projects else None      # the project the UI started it from
            if pid is None:
                ck = (r["node_id"], fp._cwd_norm(r["cwd"]))
                if ck not in keycache:
                    keycache[ck] = key2pid.get(self._key_from_repos(r["node_id"], r["cwd"]))
                pid = keycache[ck]
            if pid:
                d = dict(r)
                d.pop("project_id", None)
                jobs[pid].append(d)
        briefs: dict[str, list] = {}
        for r in self.store.q("SELECT * FROM briefs ORDER BY ts DESC"):
            briefs.setdefault(r["project_id"], []).append(dict(r))
        return {"projects": projects, "key2pid": key2pid, "sessions": sessions, "repos": repos, "jobs": jobs,
                "briefs": briefs}

    @staticmethod
    def _forge_session(j: dict, now: float) -> dict:
        live = j["status"] in ACTIVE
        return {"tool": "forge", "session_id": j["id"], "node_id": j["node_id"], "cwd": j["cwd"],
                "title": fp.redact(j["prompt"], 140), "state": "live" if live else "done", "started": j["started"] or j["created"],
                "last_activity": j["updated"] or j["finished"] or j["created"], "messages": None, "pid": None,
                "remote_control": False, "model": j["engine"], "status": j["status"], "source": j["source"]}

    def _summary(self, pid: str, ix: dict, now: float, detail: bool = False) -> dict:
        p = ix["projects"][pid]
        sess = ix["sessions"][pid] + [self._forge_session(j, now) for j in ix["jobs"][pid] if j["source"] != "brief"]
        sess.sort(key=lambda s: s["last_activity"] or 0, reverse=True)
        repos = ix["repos"][pid]
        commits_ts = [c["ts"] for r in repos for c in r["commits"] if c.get("ts")]
        last = max([s["last_activity"] or 0 for s in sess] + commits_ts + [0]) or None
        br = ix["briefs"].get(pid)
        out = {"id": pid, "key": p["key"], "name": p["name"], "group_name": p["group_name"], "pinned": bool(p["pinned"]),
               "machines": sorted({s["node_id"] for s in sess if s["node_id"]} | {r["node_id"] for r in repos}),
               "live_sessions": sum(1 for s in sess if s["state"] == "live"),
               "sessions_24h": sum(1 for s in sess if s["last_activity"] and now - s["last_activity"] < 86400),
               "last_activity": last,
               "brief": {"text": br[0]["text"], "ts": br[0]["ts"], "model": br[0]["model"]} if br else None,
               "dirty_any": any(r["dirty"] for r in repos),
               "ahead": max((r["ahead"] or 0) for r in repos) if repos else 0,
               "behind": max((r["behind"] or 0) for r in repos) if repos else 0,
               "branches": sorted({r["branch"] for r in repos if r["branch"]} | {s["git_branch"] for s in ix["sessions"][pid] if s.get("git_branch")})}
        if detail:
            seen, commits = set(), []
            for c in sorted((c for r in repos for c in r["commits"]), key=lambda c: c.get("ts") or 0, reverse=True):
                if c["sha"] not in seen:
                    seen.add(c["sha"])
                    commits.append(c)
            out |= {"sessions": sess, "commits": commits[:50],
                    "repos": [{"node_id": r["node_id"], "cwd": r["cwd"], "branch": r["branch"], "dirty": r["dirty"],
                               "ahead": r["ahead"], "behind": r["behind"]} for r in repos],
                    "briefs": [{k: b[k] for k in ("id", "text", "ts", "model", "job_id")} for b in (br or [])[:10]]}
        return out

    def list_projects(self, now: float) -> list[dict]:
        ix = self._index(now)
        out = [self._summary(pid, ix, now) for pid in ix["projects"]]
        out.sort(key=lambda p: (not p["pinned"], -(p["last_activity"] or 0)))
        return out

    def get_project(self, pid: str, now: float) -> dict:
        ix = self._index(now)
        if pid not in ix["projects"]:
            raise HTTPException(404, "no such project")
        return self._summary(pid, ix, now, detail=True)

    def patch_project(self, pid: str, body: dict) -> dict:
        if not self.store.q("SELECT 1 FROM projects WHERE id=?", (pid,)):
            raise HTTPException(404, "no such project")
        sets, args = [], []
        if body.get("name") is not None:
            name = str(body["name"]).strip()[:120]
            if not name:
                raise HTTPException(400, "name must not be empty")
            sets.append("name=?"); args.append(name)
        if "group_name" in body:
            g = body["group_name"]
            sets.append("group_name=?"); args.append((str(g).strip()[:80] or None) if g is not None else None)
        if body.get("pinned") is not None:
            sets.append("pinned=?"); args.append(int(bool(body["pinned"])))
        if sets:
            self.store.touch("projects", "id", pid, ", ".join(sets), tuple(args))
        return self.get_project(pid, self.clock())

    def merge_projects(self, ids: list, now: float) -> dict:
        ids = [str(i) for i in dict.fromkeys(ids)]
        if len(ids) < 2:
            raise HTTPException(400, "need at least two project ids")
        rows = {r["id"]: r for r in self.store.q(f"SELECT * FROM projects WHERE id IN ({','.join('?' * len(ids))})", tuple(ids))}
        if len(rows) != len(ids):
            raise HTTPException(404, "no such project")
        target = ids[0]
        for other in ids[1:]:
            keys = [rows[other]["key"]] + [a["id"] for a in self.store.q("SELECT id FROM project_alias WHERE project_id=?", (other,))]
            for k in keys:
                if k != rows[target]["key"]:
                    self.store.x("INSERT OR REPLACE INTO project_alias(id,project_id,updated) VALUES(?,?,?)", (k, target, now))
            for b in self.store.q("SELECT id FROM briefs WHERE project_id=?", (other,)):
                self.store.touch("briefs", "id", b["id"], "project_id=?", (target,))
            if rows[other]["pinned"] and not rows[target]["pinned"]:
                self.store.touch("projects", "id", target, "pinned=1")
            self.store.delete_row("projects", other)
        return self.get_project(target, now)

    # ================================================================ briefs
    def brief_model(self) -> str:
        return str(self.cfg.get("brief_model") or BRIEF_MODEL_DEFAULT)

    def pick_brief_node(self, now: float) -> Optional[str]:
        """Configured `brief_node`, else the node with the most GPU memory that has ollama + the briefs capability.
        Prefers online nodes; an offline best node is still chosen (the brief waits, no fallback to Claude)."""
        nodes = self.nodes(now)

        def capable(n: dict) -> bool:
            return "ollama" in n["tools"] and fs.agent_is_new(n["agent_version"]) and (n["caps"] is None or "briefs" in n["caps"])
        want = self.cfg.get("brief_node")
        if want and want != "auto":
            return want if want in nodes and capable(nodes[want]) else None
        cands = [n for n in nodes.values() if capable(n)]
        if not cands:
            return None
        return min(cands, key=lambda n: (not n["online"], -n["gpu_mem"], self.cluster.priority_of(n["id"]), n["id"]))["id"]

    def project_ctx(self, pid: str, ix: dict, now: float) -> dict:
        sess = ix["sessions"][pid] + [self._forge_session(j, now) for j in ix["jobs"][pid] if j["source"] != "brief"]
        sess.sort(key=lambda s: s["last_activity"] or 0, reverse=True)
        commits = sorted({c["sha"]: c for r in ix["repos"][pid] for c in r["commits"]}.values(), key=lambda c: c.get("ts") or 0, reverse=True)
        return {"name": ix["projects"][pid]["name"], "now": now, "sessions": sess, "commits": commits,
                "repos": ix["repos"][pid],
                "jobs": [{"title": j["prompt"], "status": j["status"], "ts": j["updated"] or j["created"]}
                         for j in ix["jobs"][pid] if j["source"] != "brief"]}

    def _activity(self, pid: str, ix: dict) -> float:
        a = [s["last_activity"] or 0 for s in ix["sessions"][pid]]
        a += [c["ts"] for r in ix["repos"][pid] for c in r["commits"] if c.get("ts")]
        a += [j["updated"] or j["created"] or 0 for j in ix["jobs"][pid] if j["source"] != "brief"]
        return max(a + [0])

    # ---- schedule (hub config: brief_window, brief_digest_time, brief_gpu_max)
    def brief_window(self) -> tuple[str, tuple[int, int]]:
        raw = self.cfg.get("brief_window")
        w = parse_window(raw) if raw is not None else None
        return (str(raw), w) if w else (BRIEF_WINDOW_DEFAULT, parse_window(BRIEF_WINDOW_DEFAULT))

    def brief_digest_time(self) -> tuple[Optional[str], Optional[int]]:
        raw = self.cfg.get("brief_digest_time", BRIEF_DIGEST_DEFAULT)
        if raw in (None, "", False, "off"):
            return None, None                                 # digest disabled
        m = parse_hhmm(raw)
        return (str(raw).strip(), m) if m is not None else (BRIEF_DIGEST_DEFAULT, parse_hhmm(BRIEF_DIGEST_DEFAULT))

    def brief_gpu_max(self) -> float:
        v = _num(self.cfg.get("brief_gpu_max"))
        return v if v is not None and 0 < v <= 100 else BRIEF_GPU_MAX

    def _digest_due(self, now: float) -> Optional[float]:
        """Epoch of today's digest time if the digest is due now (and not done yet), else None."""
        _, minutes = self.brief_digest_time()
        if minutes is None:
            return None
        d = local_dt(now)
        due = d.replace(hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0)
        if now < due.timestamp():
            due -= timedelta(days=1)                          # still before today's time: yesterday's slot
        due_ts = due.timestamp()
        if not 0 <= now - due_ts < BRIEF_DIGEST_LATE_S:
            return None
        done = self.store.q("SELECT 1 FROM jobs WHERE source='brief' AND project_id='*' AND created>=? LIMIT 1", (due_ts,))
        return None if done else due_ts

    def brief_settings(self, now: float) -> dict:
        wname, w = self.brief_window()
        dname, _ = self.brief_digest_time()
        node = self.pick_brief_node(now)
        util = self.nodes(now).get(node, {}).get("gpu_util") if node else None
        return {"brief_window": wname, "in_window": in_window(w, now), "brief_digest_time": dname,
                "brief_gpu_max": self.brief_gpu_max(), "brief_node": node, "brief_node_gpu_util": util,
                "brief_model": self.brief_model(), "interval_s": BRIEF_EVERY, "last_loop": self._last_brief}

    def brief_tick(self, now: float) -> int:
        """Automatic briefs (leader). Inside `brief_window` (local, default 00:00-07:00) and only while the brief node's GPU
        is below `brief_gpu_max` (30 %): every 30 min, enqueue ollama brief jobs for projects with activity since their
        last brief attempt. Separately, once a day at `brief_digest_time` (07:30) an all-projects roll-up is queued.
        On-demand briefs (UI 'Brief me') ignore all of this."""
        self.digest_tick(now)
        if not in_window(self.brief_window()[1], now):
            return 0
        if self._last_brief is not None and now - self._last_brief < BRIEF_EVERY:
            return 0
        node = self.pick_brief_node(now)
        if not node:
            self._last_brief = now
            return 0
        util = self.nodes(now)[node].get("gpu_util")
        if util is not None and util >= self.brief_gpu_max():
            return 0                                          # GPU busy: look again next tick, interval not consumed
        self._last_brief = now
        ix = self._index(now)
        pending = {r["project_id"] for r in self.store.q(
            "SELECT project_id FROM jobs WHERE source='brief' AND status IN ('queued','starting','running')")}
        attempts: dict[str, float] = {}
        for r in self.store.q("SELECT project_id, MAX(created) c FROM jobs WHERE source='brief' GROUP BY project_id"):
            attempts[r["project_id"]] = r["c"] or 0
        todo = []
        for pid in ix["projects"]:
            act = self._activity(pid, ix)
            last = max(attempts.get(pid, 0), (ix["briefs"].get(pid) or [{"ts": 0}])[0]["ts"] or 0)
            if pid not in pending and act > last and now - act < fp.KEEP_DAYS * 86400:
                todo.append((act, pid))
        n = 0
        for act, pid in sorted(todo, reverse=True)[:BRIEF_MAX_PER_LOOP]:
            self.insert_job(node_id=node, engine="ollama", prompt=fp.build_brief_prompt(self.project_ctx(pid, ix, now)),
                            source="brief", project_id=pid, model=self.brief_model())
            n += 1
        if n:
            self.store.event(node, "info", f"{n} project brief job(s) queued for {node} ({self.brief_model()})")
        return n

    def brief_now(self, pid: str, now: float) -> dict:
        """UI 'Brief me': Claude haiku on an Auto node."""
        ix = self._index(now)
        if pid not in ix["projects"]:
            raise HTTPException(404, "no such project")
        ex = self.store.q("SELECT id FROM jobs WHERE source='brief' AND project_id=? AND engine='claude' "
                          "AND status IN ('queued','starting','running')", (pid,))
        if ex:
            return {"id": ex[0]["id"], "existing": True}
        jid = self.insert_job(node_id="auto", engine="claude", prompt=fp.build_brief_prompt(self.project_ctx(pid, ix, now)),
                              source="brief", project_id=pid, model=CLAUDE_BRIEF_MODEL, requested="auto")
        self.place_jobs(now)
        return {"id": jid, "existing": False}

    def _rollup_prompt(self, now: float) -> tuple[str, int]:
        ix = self._index(now)
        act = []
        for pid, p in ix["projects"].items():
            s = self._summary(pid, ix, now)
            if s["last_activity"] and now - s["last_activity"] < 7 * 86400:
                act.append(s)
        act.sort(key=lambda s: -(s["last_activity"] or 0))
        prompt = fp.build_rollup_prompt([{"name": s["name"], "last_activity": s["last_activity"], "live": s["live_sessions"],
                                          "sessions_24h": s["sessions_24h"], "brief": (s["brief"] or {}).get("text")}
                                         for s in act], now)
        return prompt, len(act)

    def brief_all(self, now: float) -> dict:
        prompt, n = self._rollup_prompt(now)
        jid = self.insert_job(node_id="auto", engine="claude", prompt=prompt, source="brief", project_id="*",
                              model=CLAUDE_BRIEF_MODEL, requested="auto")
        self.place_jobs(now)
        return {"id": jid, "projects": n}

    def digest_tick(self, now: float) -> Optional[str]:
        """Daily morning digest: one roll-up job (ollama on the brief node, like the overnight loop) at brief_digest_time.
        Skipped when a roll-up was already queued since that time (e.g. the user pressed 'Brief me (all)')."""
        if self._digest_due(now) is None:
            return None
        node = self.pick_brief_node(now)
        if not node:
            return None
        prompt, n = self._rollup_prompt(now)
        jid = self.insert_job(node_id=node, engine="ollama", prompt=prompt, source="brief", project_id="*",
                              model=self.brief_model())
        self.store.event(node, "info", f"morning digest queued for {node} ({n} active projects)")
        return jid

    def on_job_done(self, job: Any, output: str, now: float) -> None:
        if job["source"] == "brief" and job["project_id"]:
            text = clean_brief_text(output)
            if text:
                bid = new_id(self.clock)
                self.store.x("INSERT INTO briefs(id,project_id,text,ts,model,job_id,updated) VALUES(?,?,?,?,?,?,?)",
                             (bid, job["project_id"], text, now, job["model"] or job["engine"], job["id"], now))
                self.prune_briefs(job["project_id"])

    def prune_briefs(self, project_id: Optional[str] = None) -> int:
        """Keep the newest BRIEFS_KEEP briefs per project (deletes replicate as tombstones)."""
        pids = [project_id] if project_id else [r["project_id"] for r in self.store.q(
            "SELECT project_id FROM briefs GROUP BY project_id HAVING COUNT(*) > ?", (BRIEFS_KEEP,))]
        n = 0
        for pid in pids:
            for r in self.store.q("SELECT id FROM briefs WHERE project_id=? ORDER BY ts DESC, id DESC LIMIT -1 OFFSET ?",
                                  (pid, BRIEFS_KEEP)):
                self.store.delete_row("briefs", r["id"])
                n += 1
        return n

    # ================================================================ transcripts
    def session_row(self, tool: str, sid: str) -> Optional[dict]:
        if tool == "forge":
            r = self.store.q("SELECT id,node_id FROM jobs WHERE id=?", (sid,))
            return dict(r[0]) if r else None
        r = self.store.q("SELECT node_id FROM sessions WHERE id=?", (f"{tool}:{sid}",))
        return dict(r[0]) if r else None

    def watch(self, tool: str, sid: str, now: float) -> dict:
        row = self.session_row(tool, sid)
        if not row:
            raise HTTPException(404, "no such session")
        if tool != "forge":
            self.eph.add_watch(tool, sid, row["node_id"], now)
        return {"ok": True, "node_id": row["node_id"], "ttl_s": int(fp.WATCH_TTL)}

    def transcript(self, tool: str, sid: str, since: int, now: float) -> dict:
        if tool == "forge":
            r = self.store.q("SELECT prompt,output,status,created,finished FROM jobs WHERE id=?", (sid,))
            if not r:
                raise HTTPException(404, "no such session")
            j = r[0]
            evs = [{"seq": 1, "ts": j["created"], "role": "user", "kind": "text", "text": fp.redact(j["prompt"], fp.EVENT_TEXT_MAX),
                    "collapsed": False}]
            out = fp.redact(j["output"] or "")      # redact BEFORE chunking: a token across a chunk edge must not escape
            for i in range(0, len(out), fp.EVENT_TEXT_MAX):
                evs.append({"seq": len(evs) + 1, "ts": j["finished"] or j["created"], "role": "assistant", "kind": "text",
                            "text": out[i:i + fp.EVENT_TEXT_MAX], "collapsed": False})
            return {"events": [e for e in evs if e["seq"] > since], "cursor": len(evs),
                    "done": j["status"] not in ACTIVE, "available": True}
        if not self.session_row(tool, sid):
            raise HTTPException(404, "no such session")
        return self.eph.get(tool, sid, since)

    def ingest_transcript(self, body: dict, now: float) -> dict:
        tool, sid = str(body.get("tool") or ""), str(body.get("session_id") or "")
        if tool not in ("claude", "codex", "t3") or not sid:
            raise HTTPException(400, "tool/session_id required")
        n = self.eph.add_events(tool, sid, body.get("cursor"), body.get("events") or [], bool(body.get("done")), now)
        return {"ok": True, "accepted": n}

    # ================================================================ maintenance (leader tick)
    def tick(self, now: float) -> None:
        self.eph.gc(now)
        self.brief_tick(now)

    def retention(self, now: float) -> dict:
        """Leader housekeeping (deletes replicate as tombstones): terminal jobs older than JOB_KEEP_DAYS except the newest
        JOB_KEEP_MIN, briefs beyond BRIEFS_KEEP per project."""
        old = self.store.q(
            "SELECT id FROM jobs WHERE created < ? AND status NOT IN ('queued','starting','running') "
            "AND id NOT IN (SELECT id FROM jobs ORDER BY id DESC LIMIT ?)", (now - JOB_KEEP_DAYS * 86400, JOB_KEEP_MIN))
        with self.store.tx():
            for r in old:
                self.store.delete_row("jobs", r["id"])
            briefs = self.prune_briefs()
        return {"jobs": len(old), "briefs": briefs}
