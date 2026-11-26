"""Phase 3 workstream U (UX), hub side. See CONTRACT-phase3.md §5 "Workstream U".

Routes (all added in `register`):
- U9 PWA: GET /manifest.webmanifest, GET /sw.js, GET /icons/<file>.png (no auth; answered locally by standbys).
- U2 Today: GET /api/today?q=&all=0|1 (UI) -> {now, status, needs_you, happening, recent, brief, first_run}.
  `all=1` returns every "Needs you" item (up to 100) instead of the first 3; `total` is the same either way.
- U3 Ask:   GET /api/ask?q=&limit=8 (UI) -> {results: [...]}; the last entry is always "Run '<q>' on any free machine".

Everything else the new UI needs (enrollment, devices, alerts, maintenance, ...) belongs to the other workstreams.
The pure helpers (fuzzy_score, day_bucket, status_sentence, alert_action, ...) have no FastAPI dependency, so
tools/mock_ui_server.py imports them too and the mock answers exactly like the hub. Python 3.10 compatible.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

STATIC = Path(__file__).resolve().parent / "static"
ICON_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}\.png$")
ONLINE_SECS = 30
ACTIVE = ("queued", "starting", "running")
RECENT_MAX = 20                     # rows per Today/Yesterday/Earlier bucket
NEEDS_MAX = 3                       # "Needs you" cards on Today
FAILED_WINDOW_S = 12 * 3600         # pre-N: failed tasks from the last 12 h show up in "Needs you"
SELFHEAL_GRACE_S = 900              # N's alert_selfheal_grace_s default
NEEDS_ALL_MAX = 100                 # /api/today?all=1 ("See all")
INDEX_TTL_S = 2.0                   # Today/Ask reuse the project/session index for this long (keyed by ctx.clock)
ASK_SESSIONS_PER_PROJECT = 500      # Ask scores at most this many sessions per project (newest first)
THEME_BG = "#f5f6f8"
THEME_FG = "#4f46e5"

MANIFEST = {
    "name": "Hivemind", "short_name": "Hivemind", "start_url": "/#/today", "scope": "/", "display": "standalone",
    "background_color": THEME_BG, "theme_color": THEME_FG,
    "description": "Your machines, projects and AI sessions in one place.",
    "icons": [{"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
              {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
              {"src": "/icons/maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}],
}

# Friendly maintenance names (U5). F's forge_upkeep.MAINTENANCE wins when it exists; this is the fallback for Ask.
MAINTENANCE_FALLBACK = [
    {"name": "update_app", "title": "Update an app"},
    {"name": "update_ai_tools", "title": "Update Claude Code & Codex"},
    {"name": "restart_hivemind", "title": "Restart Hivemind"},
    {"name": "free_disk", "title": "Free disk space"},
    {"name": "restart_machine", "title": "Restart machine"},
]
# Named UI actions for Ask (route = U10 hash route)
NAMED_ACTIONS = [
    {"id": "add-machine", "title": "Add a machine", "subtitle": "Enroll a PC or pair a phone", "route": "#/add-machine",
     "words": "new enroll pair phone computer pc install"},
    {"id": "maintenance", "title": "Maintenance", "subtitle": "Update apps and tools on all machines", "route": "#/machines/maintenance",
     "words": "update upgrade restart reboot disk clean"},
    {"id": "advanced", "title": "Advanced", "subtitle": "Hub order, versions, auto-repair, devices, tokens",
     "route": "#/machines/advanced", "words": "hub order versions tokens devices rotate auto-repair watchdog"},
    {"id": "activity", "title": "Activity", "subtitle": "All tasks and events", "route": "#/activity", "words": "tasks jobs history log"},
    {"id": "settings", "title": "Settings", "subtitle": "Theme, notifications, admin PIN", "route": "#/settings",
     "words": "theme dark light notifications ntfy push pin sign out logout"},
    {"id": "brief", "title": "Brief me", "subtitle": "Roll-up across all active projects", "route": "#/today?brief=1",
     "words": "summary summarize brief morning"},
]


# ---------------------------------------------------------------- pure helpers (also used by the mock)
def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "").lower()).strip()


_SEP = " -_/\\.:#@"


def fuzzy_score(query: Any, text: Any) -> float:
    """0..1 match score. Same algorithm as UI.fuzzyScore in index.html (shared cases in tests/js/fuzzy_cases.json).
    1.0 exact, .95 prefix, .9 word start, .8 substring, .7 all words present, .3-.6 in-order letters, 0 no match."""
    q, t = _norm(query), _norm(text)
    if not q or not t:
        return 0.0
    if t == q:
        return 1.0
    if t.startswith(q):
        return 0.95
    i = t.find(q)
    if i > 0:
        return 0.9 if t[i - 1] in _SEP else 0.8
    words = q.split(" ")
    if len(words) > 1 and all(w in t for w in words):
        return 0.7
    # in-order letters ("frgctl" -> "hivemind"); score by how compact the match is
    pos, first, last = 0, -1, -1
    for ch in q.replace(" ", ""):
        j = t.find(ch, pos)
        if j < 0:
            return 0.0
        if first < 0:
            first = j
        last, pos = j, j + 1
    span = last - first + 1
    n = len(q.replace(" ", ""))
    return round(0.3 + 0.3 * (n / span), 4) if n >= 2 else 0.0


def best_score(query: str, *fields: Any) -> float:
    """Best fuzzy score over several fields; the first field (the title) counts fully, the others at 90 %."""
    best = 0.0
    for i, f in enumerate(fields):
        s = fuzzy_score(query, f) * (1.0 if i == 0 else 0.9)
        best = max(best, s)
    return round(best, 4)


def day_bucket(ts: Optional[float], now: float) -> str:
    """'today' | 'yesterday' | 'earlier' by LOCAL calendar day."""
    if not ts:
        return "earlier"
    d_now = datetime.fromtimestamp(now).astimezone().date()
    d = datetime.fromtimestamp(ts).astimezone().date()
    if d >= d_now:
        return "today"
    if d == d_now - timedelta(days=1):
        return "yesterday"
    return "earlier"


def status_sentence(machines: int, online: int, needs: list) -> dict:
    """The one Today sentence: {level: ok|warn|error, sentence}. `needs` = the open "Needs you" items."""
    if not machines:
        return {"level": "warn", "sentence": "No machines yet. Add one to get started."}
    if not needs:
        return {"level": "ok", "sentence": f"All good – {online} of {machines} machine{'' if machines == 1 else 's'} on"}
    lvl = "error" if any(a.get("level") == "error" for a in needs) else "warn"
    n = len(needs)
    return {"level": lvl, "sentence": f"{n} thing{'' if n == 1 else 's'} need{'s' if n == 1 else ''} you"}


def alert_action(a: dict) -> Optional[dict]:
    """Default one-button action for an alert that has none (N1 action shape)."""
    if isinstance(a.get("action"), dict):
        return a["action"]
    kind, nid, data = a.get("kind") or "", a.get("node_id"), a.get("data") if isinstance(a.get("data"), dict) else {}
    if kind in ("node_offline", "machine_down") and nid:
        return {"label": "Wake it", "kind": "wake_machine", "target": {"node_id": nid}}
    if kind in ("task_failed",) and data.get("job_id"):
        return {"label": "Retry", "kind": "retry_job", "target": {"job_id": data["job_id"]}}
    if kind == "repo_conflict" and data.get("project_id"):
        return {"label": "Open project", "kind": "open_project", "target": {"project_id": data["project_id"]}}
    if kind in ("fleet_bad_version", "rolled_back", "bundle_unsigned"):
        return {"label": "Details", "kind": "fix", "target": {"route": "#/machines/advanced"}}
    if nid:
        return {"label": "Open machine", "kind": "open_machine", "target": {"node_id": nid}}
    return None


def alert_key(kind: str, node_id: Optional[str], subject: Optional[str] = None) -> str:
    return f"{kind}:{node_id or '-'}" + (f":{subject}" if subject else "")


def n1_alert(a: dict, **extra: Any) -> dict:
    """Normalise any alert-ish dict to the N1 list shape."""
    kind = a.get("kind") or "alert"
    out = {"id": a.get("id") or a.get("key") or alert_key(kind, a.get("node_id")), "key": a.get("key") or alert_key(kind, a.get("node_id")),
           "kind": kind, "node_id": a.get("node_id"), "level": a.get("level") or "warn", "msg": a.get("msg") or "",
           "opened": a.get("opened"), "resolved": a.get("resolved"), "resolved_by": a.get("resolved_by"),
           "self_healing": bool(a.get("self_healing")), "last_notified": a.get("last_notified"),
           "data": a.get("data") if isinstance(a.get("data"), dict) else {}}
    out.update(extra)
    out["action"] = alert_action({**a, **out})
    return out


def filter_needs(alerts: Iterable[dict], now: float, grace_s: float = SELFHEAL_GRACE_S) -> list:
    """Open warn/error alerts that need a person: self-healing ones only after they outlast the grace period.
    Errors first, then newest first."""
    out = []
    for a in alerts:
        if not a or a.get("resolved") or a.get("level") not in ("warn", "error"):
            continue
        if a.get("self_healing") and (not a.get("opened") or now - float(a["opened"]) < grace_s):
            continue
        out.append(a)
    out.sort(key=lambda a: (a.get("level") != "error", -(a.get("opened") or 0)))
    return out


def ask_rank(results: list, limit: int) -> list:
    results = [r for r in results if r.get("score", 0) > 0]
    order = {"action": 0, "maintenance": 1, "project": 2, "machine": 3, "session": 4}
    results.sort(key=lambda r: (-r["score"], order.get(r["type"], 9), str(r.get("title") or "").lower()))
    return results[:max(0, limit)]


def run_entry(q: str) -> dict:
    text = q.strip()
    short = text if len(text) <= 60 else text[:59] + "…"
    return {"type": "run", "id": "run", "title": f"Run '{short}' on any free machine", "subtitle": "Opens a preview first",
            "score": 0.0, "target": {"prompt": text, "engine": "any", "node_id": "auto"}}


def _jl(s: Any, default: Any) -> Any:
    if isinstance(s, (dict, list)):
        return s
    try:
        return json.loads(s) if s else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- hub-backed builders
class UiData:
    """Reads what Today / Ask need from the store. Every table read tolerates missing optional columns."""

    def __init__(self, ctx: Any):
        self.ctx = ctx
        self._health: Optional[Callable] = None
        self._ix: Optional[tuple] = None          # (clock value, index)

    # ---- alerts (N table when N is active, else the 2.2 /api/health list + recent failed tasks)
    def _n_active(self) -> bool:
        fn = getattr(self.ctx, "raise_alert", None)
        return bool(fn) and getattr(fn, "__name__", "") != "_noop"

    def _health_fn(self) -> Optional[Callable]:
        if self._health is None:
            for r in self.ctx.app.router.routes:
                if getattr(r, "path", None) == "/api/health" and "GET" in (getattr(r, "methods", None) or ()):
                    self._health = r.endpoint
                    break
        return self._health

    @staticmethod
    def _n_list_fn() -> Optional[Callable]:
        """N's own open-alert list (CONTRACT §N1 `forge_alerts.list_open(ctx, now)`), so Today has exactly the
        GET /api/alerts shape. None while N has not shipped it."""
        try:
            import forge_alerts
        except Exception:
            return None
        fn = getattr(forge_alerts, "list_open", None)
        return fn if callable(fn) else None

    def alerts(self, now: float) -> tuple:
        if self._n_active():
            grace = float(self.ctx.cfg.get("alert_selfheal_grace_s") or SELFHEAL_GRACE_S)
            fn = self._n_list_fn()
            if fn is not None:
                got = self.ctx.safe_call(fn, self.ctx, now)
                if isinstance(got, list):
                    return [n1_alert(a) for a in got if isinstance(a, dict)], grace
            rows = self.ctx.store.q("SELECT * FROM alerts WHERE resolved IS NULL ORDER BY opened DESC LIMIT 200")
            out = []
            for r in rows:
                d = dict(r)
                data = _jl(d.get("data"), {})
                data = data if isinstance(data, dict) else {}
                act = _jl(d.get("action"), None) if d.get("action") else data.get("action")   # a column, if N adds one
                out.append(n1_alert(d, data=data, action=act if isinstance(act, dict) else None))
            return out, grace
        out = []
        fn = self._health_fn()
        h = self.ctx.safe_call(fn) if fn else None
        if isinstance(h, dict):
            lease = h.get("update_lease") or {}
            rolling = bool(lease.get("holder")) or any(r.get("status") == "updating" for r in h.get("rollout") or [])
            for a in h.get("alerts") or []:
                if not isinstance(a, dict):
                    continue
                heal = a.get("kind") == "version_behind" and rolling
                out.append(n1_alert(a, self_healing=heal or bool(a.get("self_healing"))))
        out.extend(self.failed_tasks(now))
        return out, SELFHEAL_GRACE_S

    def failed_tasks(self, now: float) -> list:
        """Pre-N "task failed" items: user-lane UI tasks that failed in the last 12 h and were not run again since
        (no later job with the same prompt, engine and project, on any machine)."""
        rows = self.ctx.store.q(
            "SELECT f.id,f.node_id,f.engine,f.prompt,f.finished,f.source,f.run_as FROM jobs f WHERE f.status='failed' AND f.finished>? "
            "AND COALESCE(f.source,'') NOT IN ('brief','sync') AND COALESCE(f.run_as,'user')='user' "
            "AND NOT EXISTS (SELECT 1 FROM jobs r WHERE r.id<>f.id AND r.created>f.created "
            "AND COALESCE(r.prompt,'')=COALESCE(f.prompt,'') AND COALESCE(r.engine,'')=COALESCE(f.engine,'') "
            "AND COALESCE(r.project_id,'')=COALESCE(f.project_id,'')) "
            "ORDER BY f.finished DESC LIMIT 5",
            (now - FAILED_WINDOW_S,))
        out = []
        for r in rows:
            title = re.sub(r"\s+", " ", str(r["prompt"] or "")).strip()
            title = title if len(title) <= 70 else title[:69] + "…"
            out.append(n1_alert({"kind": "task_failed", "node_id": r["node_id"], "level": "warn", "opened": r["finished"],
                                 "key": alert_key("task_failed", r["node_id"], r["id"]),
                                 "msg": f"Task failed: {title}" if title else "A task failed",
                                 "data": {"job_id": r["id"]}}))
        return out

    # ---- nodes / sessions
    def nodes(self, now: float) -> list:
        out = []
        for r in self.ctx.store.q("SELECT id,last_seen,info FROM nodes_live ORDER BY id"):
            info = _jl(r["info"], {})
            out.append({"id": r["id"], "hostname": info.get("hostname") or r["id"],
                        "online": now - (r["last_seen"] or 0) < ONLINE_SECS})
        return out

    def index(self, now: float) -> dict:
        """p2's project/session index, reused for INDEX_TTL_S: Today polls every 5 s per tab and Ask runs per keystroke."""
        c = self._ix
        if c is not None and 0 <= now - c[0] < INDEX_TTL_S:
            return c[1]
        ix = self.ctx.p2._index(now)
        self._ix = (now, ix)
        return ix

    # ---- Today
    def today(self, now: float, q: str = "", all_needs: bool = False) -> dict:
        nodes = self.nodes(now)
        host = {n["id"]: n["hostname"] for n in nodes}
        alerts, grace = self.alerts(now)
        needs = filter_needs(alerts, now, grace)
        ix = self.index(now)
        projects = ix["projects"]
        happening, sessions = [], []
        for pid, ss in ix["sessions"].items():
            for s in ss:
                row = {"tool": s["tool"], "session_id": s["session_id"], "title": s["title"] or "", "node_id": s["node_id"],
                       "project_id": pid, "project_name": projects[pid]["name"], "state": s["state"],
                       "last_activity": s["last_activity"], "started": s["started"], "git_branch": s.get("git_branch"),
                       "messages": s.get("messages")}
                if s["state"] == "live":
                    happening.append({"kind": "session", "id": s["session_id"], "tool": s["tool"], "title": row["title"],
                                      "node_id": s["node_id"], "project_id": pid, "since": s["started"] or s["last_activity"],
                                      "state": "live"})
                else:
                    sessions.append(row)
        for r in self.ctx.store.q(
                "SELECT id,node_id,engine,req_engine,prompt,status,created,started,project_id,source,run_as,action FROM jobs "
                "WHERE status IN ('queued','starting','running') AND COALESCE(source,'') NOT IN ('brief','sync') "
                "ORDER BY created DESC LIMIT 30"):
            title = re.sub(r"\s+", " ", str(r["prompt"] or r["action"] or "")).strip()[:140]
            happening.append({"kind": "job", "id": r["id"], "tool": r["engine"], "title": title, "node_id": r["node_id"],
                              "project_id": r["project_id"], "since": r["started"] or r["created"], "state": r["status"]})
        happening.sort(key=lambda h: -(h["since"] or 0))
        qn = _norm(q)
        if qn:
            sessions = [s for s in sessions if qn in _norm(" ".join([s["title"], s["project_name"] or "", s["node_id"] or "",
                                                                     host.get(s["node_id"], "")]))]
        sessions.sort(key=lambda s: -(s["last_activity"] or 0))
        recent: dict = {"today": [], "yesterday": [], "earlier": []}
        for s in sessions:
            b = recent[day_bucket(s["last_activity"], now)]
            if len(b) < RECENT_MAX:
                b.append(s)
        br = self.ctx.store.q("SELECT text,ts,model FROM briefs WHERE project_id='*' ORDER BY ts DESC LIMIT 1")
        online = sum(1 for n in nodes if n["online"])
        return {"now": now, "status": status_sentence(len(nodes), online, needs),
                "needs_you": {"items": needs[:NEEDS_ALL_MAX if all_needs else NEEDS_MAX], "total": len(needs)},
                "happening": happening[:30], "recent": recent,
                "brief": dict(br[0]) if br else None,
                "first_run": {"machines": len(nodes), "projects": len(projects)}}

    # ---- Ask
    def maintenance_catalog(self) -> list:
        try:
            import forge_upkeep  # F (may not define it yet)
            cat = getattr(forge_upkeep, "MAINTENANCE", None)
        except Exception:
            cat = None
        if isinstance(cat, dict):
            return [{"name": k, "title": (v or {}).get("title") or k, "description": (v or {}).get("description") or ""}
                    for k, v in cat.items()]
        if isinstance(cat, list) and cat:
            return [{"name": c.get("name"), "title": c.get("title") or c.get("name"), "description": c.get("description") or ""}
                    for c in cat if isinstance(c, dict) and c.get("name")]
        return [dict(m, description="") for m in MAINTENANCE_FALLBACK]

    def ask(self, now: float, q: str, limit: int = 8) -> dict:
        limit = max(1, min(int(limit or 8), 50))
        text = (q or "").strip()[:200]
        if not text:
            return {"results": []}
        res: list = []
        for a in NAMED_ACTIONS:
            res.append({"type": "action", "id": a["id"], "title": a["title"], "subtitle": a["subtitle"],
                        "score": best_score(text, a["title"], a["words"]), "target": {"route": a["route"]}})
        for m in self.maintenance_catalog():
            res.append({"type": "maintenance", "id": m["name"], "title": m["title"], "subtitle": "Maintenance · all machines",
                        "score": best_score(text, m["title"], m["name"].replace("_", " "), m.get("description")),
                        "target": {"route": "#/machines/maintenance", "action": m["name"]}})
        nodes = self.nodes(now)
        host = {n["id"]: n["hostname"] for n in nodes}
        for n in nodes:
            res.append({"type": "machine", "id": n["id"], "title": n["hostname"],
                        "subtitle": ("Online" if n["online"] else "Offline") + (f" · {n['id']}" if n["id"] != n["hostname"] else ""),
                        "score": best_score(text, n["hostname"], n["id"]), "target": {"node_id": n["id"]}})
        ix = self.index(now)
        for pid, p in ix["projects"].items():
            res.append({"type": "project", "id": pid, "title": p["name"] or "Project",
                        "subtitle": " · ".join(x for x in [p.get("group_name"), f"{len(ix['sessions'][pid])} sessions"] if x),
                        "score": best_score(text, p["name"], p.get("group_name"), p.get("key")), "target": {"project_id": pid}})
            for s in ix["sessions"][pid][:ASK_SESSIONS_PER_PROJECT]:
                res.append({"type": "session", "id": s["session_id"], "title": s["title"] or "(untitled)",
                            "subtitle": f"{s['tool']} · {host.get(s['node_id'], s['node_id'])} · {p['name']}",
                            "score": best_score(text, s["title"], p["name"]) * 0.95,
                            "target": {"tool": s["tool"], "session_id": s["session_id"], "project_id": pid}})
        out = ask_rank(res, limit - 1)
        out.append(run_entry(text))
        return {"results": out}


# ---------------------------------------------------------------- routes
def register(app: Any, ctx: Any) -> None:
    """Add the U routes to `app` and the PWA paths to the standby-local set."""
    from fastapi import Depends, HTTPException, Query
    from fastapi.responses import FileResponse, JSONResponse

    data = UiData(ctx)
    app.state.ui_data = data
    ctx.local_paths.update({"/manifest.webmanifest", "/sw.js"})
    if "/icons/" not in ctx.local_prefixes:
        ctx.local_prefixes.append("/icons/")

    @app.get("/manifest.webmanifest")
    def manifest() -> JSONResponse:
        return JSONResponse(MANIFEST, media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})

    @app.get("/sw.js")
    def service_worker() -> FileResponse:
        return FileResponse(STATIC / "sw.js", media_type="application/javascript",
                            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})

    @app.get("/icons/{name}")
    def icon(name: str) -> FileResponse:
        p = STATIC / "icons" / name
        if not ICON_RE.match(name) or not p.is_file():
            raise HTTPException(404, "no such icon")
        return FileResponse(p, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/api/today", dependencies=[Depends(ctx.require_ui)])
    def today(q: str = Query(default="", max_length=200), all: int = Query(default=0, ge=0, le=1)) -> dict:
        return data.today(ctx.clock(), q, all_needs=bool(all))

    @app.get("/api/ask", dependencies=[Depends(ctx.require_ui)])
    def ask(q: str = Query(default="", max_length=200), limit: int = Query(default=8, ge=1, le=50)) -> dict:
        return data.ask(ctx.clock(), q, limit)
