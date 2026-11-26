"""Phase 3 workstream F (first run + upkeep), hub side. See CONTRACT-phase3.md section 5 "Workstream F".

F2 maintenance runs: named actions rolled out one machine at a time (canary first, descending priority number, leader
   last; forge_lease.build_order), stop on the first failure, offline machines skipped. Weekly schedule from prefs
   `upkeep.schedule`.
F3 repo sync: projects.sync_nodes; the leader queues a user-lane `git_sync {cwd}` (source "sync") per listed node and
   repo every `upkeep.repo_sync_every_s`; diverged / error -> alert `repo_conflict`, ff / uptodate resolves it.
F4 Windows Update: prefs `upkeep.windows_update` {enabled, window, stagger_min} + settings.wu_window override; slot =
   window start + stagger x rank (rollout order, leader last); result in settings.wu_last; reboot only when required,
   another hub is healthy + eligible and the window is still open, else alert `reboot_pending`.
F5 Wake-on-LAN: agents report metrics.macs; POST /api/machines/{id}/wake hands `wol` items to up to 2 online agents on
   the same IPv4 subnet (report response, 60 s TTL, leader memory).
F6 (a) misc project keys live in forge_projects; here the leader merges pre-2.3 per-folder scratch/temp projects into
   misc:<node> once. (b) brief node pre-pull: one `ollama_pull {brief_model}` per day inside brief_window when the
   brief node reports the model missing.

Every fleet decision runs only where `settled` (ctx.settled_leader) is true. Python 3.10 compatible, no import-time I/O.
"""
from __future__ import annotations

import ipaddress
import json
import re
import threading
from typing import Any, Optional

from fastapi import Depends, HTTPException, Request

import forge_projects as fp
import forge_sched as fs
from forge_actions import ActionError, action_lane, find_action, summary as action_summary, validate_args
from forge_lease import build_order
from forge_phase2 import ACTIVE, ONLINE_SECS, USER_ACTION_AGENT, local_dt, parse_hhmm, parse_window
from forge_store import jloads, new_id

# ---------------------------------------------------------------- catalog of friendly maintenance actions (U5)
# steps: (catalog action, fixed args or None = the run's validated args)
MAINTENANCE: dict[str, dict] = {
    "update_app": {"title": "Update an app", "description": "Upgrade one app with winget on every machine.",
                   "steps": [("winget_upgrade", None)],
                   "args": {"id": {"type": "string", "required": True, "pattern": r"^[A-Za-z0-9][A-Za-z0-9.\-_]*$",
                                   "max_len": 128, "label": "Package id (e.g. Git.Git)"}}},
    "update_ai_tools": {"title": "Update Claude Code & Codex", "description": "claude update / npm, then the Codex CLI.",
                        "steps": [("update_claude", {}), ("update_codex", {})], "args": {}},
    "restart_hivemind": {"title": "Restart Hivemind", "description": "Restart the hub and the agent on each machine.",
                         "steps": [("restart_task", {"name": "ForgeHub"}), ("restart_task", {"name": "ForgeAgent"})],
                         "args": {}},
    "free_disk": {"title": "Free disk space", "description": "Delete old temp files, scratch folders untouched for 14 days, "
                                                             "and every user's recycle bin.",
                  "steps": [("free_disk", {})], "args": {}},
    "restart_machine": {"title": "Restart machine", "description": "Reboot (60 s warning).",
                        "steps": [("reboot", {"delay_s": 60})], "args": {}},
}
RUN_ACTIVE = ("pending", "running")
RUN_STATES = ("pending", "running", "done", "failed", "cancelled")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SCHEDULE_DEFAULT = {"enabled": True, "day": "sun", "time": "04:00",
                    "actions": ["update_ai_tools", "update_t3", "update_ollama", "winget_upgrade"]}
WU_DEFAULT = {"enabled": True, "window": "03:00-05:00", "stagger_min": 30}
REPO_SYNC_DEFAULT_S = 1800
SCHEDULE_LATE_MIN = 180            # a weekly run still starts up to 3 h after its time (leader restarts, failover)
WOL_TTL_S = 60.0
RUN_PICKUP_TIMEOUT_S = 1800        # a run step nobody picked up (machine went away) is skipped after 30 min
WU_PICKUP_TIMEOUT_S = 6 * 3600     # a nightly windows_update still queued after 6 h is cancelled ("missed")
# F2: a reboot / hub or agent restart step is finished only when the machine (or that process) is back
WAIT_TIMEOUT_S = {"reboot": 30 * 60, "hub": 10 * 60, "agent": 10 * 60}
HUB_RESTART_GRACE_S = 60           # a restarted hub counts as back once healthy again after this long
AGENT_RESTART_GRACE_S = 120        # fallback when the agent's boot report was not seen (leader changed meanwhile)
REBOOT_SETTLE_S = 15 * 60          # a queued post-update reboot that has not happened after delay + this -> pending
REBOOT_DELAY_S = 60
# system-lane actions a 2.2 SYSTEM agent does not know (it validates against its own bundled actions.json)
SYSTEM_ACTIONS_23 = frozenset({"windows_update", "free_disk", "update_ollama", "forge_uninstall"})
MAC_RE = re.compile(r"^[0-9A-F]{2}(-[0-9A-F]{2}){5}$")
CONFLICT_RESULTS = ("diverged", "error")
OK_RESULTS = ("ff", "uptodate")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


# ================================================================ pure helpers (unit-tested)
def parse_result(text: Optional[str]) -> Optional[dict]:
    """The last `##RESULT <json>` line of an action's output, or None."""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("##RESULT "):
            try:
                v = json.loads(line[len("##RESULT "):])
            except ValueError:
                return None
            return v if isinstance(v, dict) else None
    return None


def summarize(action: str, res: Optional[dict], status: str) -> str:
    if not res:
        return status
    if action == "windows_update":
        s = f"{int(res.get('installed') or 0)} installed, {int(res.get('failed') or 0)} failed"
        return s + (", reboot required" if res.get("reboot_required") else "")
    return str(res.get("summary") or res.get("result") or status)[:200]


def normalize_mac(v: Any) -> Optional[str]:
    s = re.sub(r"[^0-9A-Fa-f]", "", str(v or "")).upper()
    if len(s) != 12 or s in ("000000000000", "FFFFFFFFFFFF"):
        return None
    return "-".join(s[i:i + 2] for i in range(0, 12, 2))


def clean_macs(v: Any) -> list:
    """metrics.macs (one-element arrays arrive as an object from PS 5.1) -> [{iface, mac, ip, prefix}]."""
    out = []
    for m in (v if isinstance(v, list) else [v] if isinstance(v, dict) else []):
        if not isinstance(m, dict):
            continue
        mac = normalize_mac(m.get("mac"))
        if not mac:
            continue
        ip, prefix = m.get("ip"), m.get("prefix")
        try:
            ipaddress.IPv4Address(str(ip))
            prefix = int(prefix)
            if not 0 < prefix <= 32:
                raise ValueError
        except (ValueError, TypeError):
            ip, prefix = None, None
        out.append({"iface": str(m.get("iface") or "")[:64], "mac": mac, "ip": ip, "prefix": prefix})
    return out[:8]


def networks(macs: list) -> list:
    out = []
    for m in macs:
        if m.get("ip") and m.get("prefix"):
            try:
                out.append(ipaddress.IPv4Network(f"{m['ip']}/{m['prefix']}", strict=False))
            except ValueError:
                pass
    return out


def wol_items(macs: list) -> list:
    """One magic packet per (mac, directed broadcast) + the limited broadcast, deduplicated."""
    items, seen = [], set()
    for m in macs:
        nets = networks([m])
        for bc in ([str(n.broadcast_address) for n in nets if n.prefixlen < 31] + ["255.255.255.255"]):
            if (m["mac"], bc) not in seen:
                seen.add((m["mac"], bc))
                items.append({"mac": m["mac"], "broadcast": bc, "port": 9})
    return items


def normalize_wu_window(v: Any) -> Optional[str]:
    """settings.wu_window: "HH:MM-HH:MM" | "off" | None. Raises HTTPException(400)."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, str):
        s = v.strip().lower()
        if s == "off":
            return "off"
        a, sep, b = s.partition("-")
        if sep and _HHMM_RE.match(a) and _HHMM_RE.match(b) and a != b:
            return f"{a}-{b}"
    raise HTTPException(400, 'wu_window must be "HH:MM-HH:MM", "off" or null')


def window_offset(window: tuple, now: float) -> tuple:
    """(minutes since the window opened, window length in minutes, local date the window opened)."""
    lo, hi = window
    length = (hi - lo) % 1440 or 1440
    d = local_dt(now)
    since = (d.hour * 60 + d.minute - lo) % 1440
    opened = local_dt(now - since * 60).date().isoformat()
    return since, length, opened


def wu_slot_offset(rank: int, stagger: int, length: int, n: int = 1) -> int:
    """Slot of the rank-th machine inside a window of `length` minutes shared by `n` machines. Never wraps around:
    when n x stagger does not fit, the stagger shrinks so the last machine (the leader) still starts last."""
    length, stagger = max(1, length), max(0, stagger)
    if n > 1:
        stagger = min(stagger, (length - 1) // (n - 1))
    return min(rank * stagger, length - 1)


def model_present(models: list, want: str) -> bool:
    names = {str(m).lower() for m in models}
    w = want.lower()
    return w in names or (":" not in w and f"{w}:latest" in names)


# ================================================================ module state
class Upkeep:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.lock = threading.RLock()
        self.wol: dict[str, tuple] = {}          # node -> (items, expires)  (leader memory)
        self.models: dict[str, list] = {}        # node -> reported ollama model names
        self.macs_cache: dict[str, str] = {}     # node -> json of the stored macs (skip unchanged writes)
        self.last: dict[str, float] = {}         # throttles: wu, schedule, sync, prepull, misc
        self.boots: dict[str, dict] = {}         # node -> {lane: time of its last boot report} (leader memory)

    # ---------------------------------------------------------------- small store helpers
    @property
    def store(self):
        return self.ctx.store

    def get_pref(self, key: str, default: Any) -> Any:
        r = self.store.q("SELECT value FROM prefs WHERE key=?", (key,))
        return jloads(r[0]["value"], default) if r else default

    def set_pref(self, key: str, value: Any) -> None:
        raw = json.dumps(value)
        if not self.store.touch("prefs", "key", key, "value=?", (raw,)):
            self.store.x("INSERT INTO prefs(key,value,updated) VALUES(?,?,?)", (key, raw, self.ctx.clock()))

    def schedule(self) -> dict:
        v = self.get_pref("upkeep.schedule", None)
        return SCHEDULE_DEFAULT | (v if isinstance(v, dict) else {})

    def wu_prefs(self) -> dict:
        v = self.get_pref("upkeep.windows_update", None)
        return WU_DEFAULT | (v if isinstance(v, dict) else {})

    def repo_sync_every(self) -> int:
        v = self.get_pref("upkeep.repo_sync_every_s", REPO_SYNC_DEFAULT_S)
        return int(v) if isinstance(v, int) and not isinstance(v, bool) and v >= 300 else REPO_SYNC_DEFAULT_S

    def setting(self, nid: str, col: str) -> Any:
        r = self.store.q(f"SELECT {col} FROM settings WHERE node_id=?", (nid,))
        return r[0][col] if r else None

    def set_setting(self, nid: str, col: str, val: Any) -> None:
        if not self.store.touch("settings", "node_id", nid, f"{col}=?", (val,)):
            self.store.x(f"INSERT INTO settings(node_id,{col},updated) VALUES(?,?,?)", (nid, val, self.ctx.clock()))

    def metrics(self, nid: str) -> dict:
        """Latest user-lane metrics: presence (R4, local) when present, else the replicated nodes row."""
        r = self.store.q("SELECT metrics FROM presence WHERE node_id=?", (nid,))
        m = jloads(r[0]["metrics"], {}) if r else {}
        if not m:
            r = self.store.q("SELECT metrics FROM nodes_live WHERE id=?", (nid,))
            m = jloads(r[0]["metrics"], {}) if r else {}
        return m if isinstance(m, dict) else {}

    def node_view(self, now: float) -> dict:
        """node -> {online, sys_online, agent_version, sys_version} (p2.nodes + the system lane heartbeat)."""
        nodes = self.ctx.p2.nodes(now)
        out = {}
        for r in self.store.q("SELECT id, sys_last_seen, sys_version FROM nodes_live"):
            n = nodes.get(r["id"]) or {}
            out[r["id"]] = {"online": bool(n.get("online")), "agent_version": n.get("agent_version"),
                            "sys_version": r["sys_version"],
                            "sys_online": r["sys_last_seen"] is not None and now - r["sys_last_seen"] < ONLINE_SECS}
        return out

    def node_boot(self, nid: str) -> Optional[str]:
        """info.boot (LastBootUpTime) the user-lane agent last reported; changes when the machine rebooted."""
        r = self.store.q("SELECT info FROM nodes WHERE id=?", (nid,))
        b = (jloads(r[0]["info"], {}) or {}).get("boot") if r else None
        return str(b) if b else None

    def active_jobs(self, nid: str, exclude: tuple = ()) -> list:
        rows = self.store.q("SELECT id, action FROM jobs WHERE node_id=? AND status IN ('queued','starting','running')", (nid,))
        return [dict(r) for r in rows if r["id"] not in exclude]

    def wu_last(self, nid: str) -> dict:
        v = jloads(self.setting(nid, "wu_last"), {})
        return v if isinstance(v, dict) else {}

    def reboot_imminent(self, nid: str, now: float) -> bool:
        """A post-update reboot was queued for this machine and has not happened yet (do not start work there)."""
        last = self.wu_last(nid)
        if last.get("reboot") != "queued":
            return False
        if any(j["action"] == "reboot" for j in self.active_jobs(nid)):
            return True
        return now - float(last.get("reboot_ts") or 0) < REBOOT_DELAY_S + REBOOT_SETTLE_S

    def order(self, ids: list) -> list:
        """Rollout order: canaries first, then descending priority number (non-hub workers = 999), leader last."""
        c = self.ctx.cluster
        peers = [{"id": i, "priority": c.priority_of(i)} for i in dict.fromkeys(ids)]
        return build_order(peers, c.leader_id, self.ctx.lease.canaries())

    def job_text(self, jid: str) -> str:
        if self.ctx.job_output is not None:
            try:
                t = self.ctx.job_output(jid)
                if t:
                    return t
            except Exception:
                pass
        r = self.store.q("SELECT output FROM jobs WHERE id=?", (jid,))
        return (r[0]["output"] or "") if r else ""

    def cancel_queued(self, jid: str, why: str) -> bool:
        with self.ctx.p2.sched_lock:
            return bool(self.store.touch("jobs", "id", jid, "status='cancelled', finished=?, output=COALESCE(output,'')||?",
                                         (self.ctx.clock(), f"[hub] {why}; job cancelled\n"), "status='queued'"))

    # ---------------------------------------------------------------- queueing actions
    def queue_action(self, nid: str, action: str, args: Optional[dict], *, source: str, run_id: Optional[str] = None,
                     project_id: Optional[str] = None) -> str:
        """Insert a validated catalog action job for one machine (lane from the catalog). Returns the job id."""
        ctx = self.ctx
        spec = find_action(ctx.actions_catalog, action)
        if spec is None:
            raise ActionError(f"unknown action {action!r}")
        norm = validate_args(spec, args or {})
        lane = action_lane(spec)
        status, output, finished = "queued", "", None
        if lane == "system" and ctx.cfg.get("admin_lane_enabled", True) is False:
            status, finished = "failed", ctx.clock()
            output = "[hub] admin lane is disabled on this hub (config.json admin_lane_enabled=false); job not run\n"
        jid = ctx.p2.insert_job(node_id=nid, engine="action", prompt=action_summary(action, norm), status=status,
                                finished=finished, output=output, source=source, run_as=lane, action=action,
                                args=norm, project_id=project_id, run_id=run_id)
        if source != "sync":
            ctx.event(nid, "info", f"ACTION {action} job {jid[:8]} queued for {lane} lane (source {source})")
        if lane == "user":
            ctx.p2.place_jobs(ctx.clock())
        return jid

    # ---------------------------------------------------------------- F2: maintenance actions
    def maintenance_def(self, name: str) -> Optional[dict]:
        """Friendly action, or a raw catalog action name, as {name, title, description, steps, args}."""
        if name in MAINTENANCE:
            return {"name": name} | MAINTENANCE[name]
        spec = find_action(self.ctx.actions_catalog, name)
        if spec is None or name == "forge_uninstall":         # uninstall only through R's retire flow
            return None
        return {"name": name, "title": name.replace("_", " ").capitalize(), "description": spec.get("description", ""),
                "steps": [(name, None)], "args": spec.get("args") or {}}

    def describe(self, d: dict) -> dict:
        specs = [find_action(self.ctx.actions_catalog, s) for s, _ in d["steps"]]
        lanes = {action_lane(s) for s in specs}
        return {"name": d["name"], "title": d["title"], "description": d["description"],
                "steps": [s for s, _ in d["steps"]], "lane": "system" if "system" in lanes else "user",
                "destructive": any(bool((s or {}).get("destructive")) for s in specs), "args": d["args"]}

    def step_args(self, d: dict, args: Any) -> list:
        """Validate the run's args, then each step's args. -> [(action, args)]. Raises ActionError."""
        run_args = validate_args({"name": d["name"], "args": d["args"]}, args or {})
        out = []
        for action, fixed in d["steps"]:
            spec = find_action(self.ctx.actions_catalog, action)
            out.append((action, validate_args(spec, dict(fixed) if fixed is not None else run_args)))
        return out

    def skip_reason(self, nid: str, lanes: set, view: dict, actions: Any = ()) -> Optional[str]:
        n = view.get(nid)
        if n is None:
            return "unknown machine"
        if "user" in lanes:
            if not n["online"]:
                return "offline"
            if fs.parse_version(n["agent_version"]) < USER_ACTION_AGENT:
                return "needs agent 2.3"
        if "system" in lanes:
            if not n["sys_online"]:
                return "offline"
            if SYSTEM_ACTIONS_23 & set(actions) and fs.parse_version(n.get("sys_version")) < USER_ACTION_AGENT:
                return "needs agent 2.3"         # a 2.2 SYSTEM agent fails these as unknown actions
        return None

    def resolve_targets(self, targets: Any) -> list:
        ids = [r["id"] for r in self.store.q("SELECT id FROM nodes ORDER BY id")]
        if targets == "all":
            return ids
        if not isinstance(targets, list) or not targets or not all(isinstance(t, str) for t in targets):
            raise HTTPException(400, 'targets must be "all" or a list of machine ids')
        unknown = [t for t in targets if t not in ids]
        if unknown:
            raise HTTPException(400, f"unknown machine(s): {', '.join(unknown)}")
        return list(dict.fromkeys(targets))

    def plan(self, d: dict, targets: Any, now: float) -> dict:
        desc = self.describe(d)
        lanes = {action_lane(find_action(self.ctx.actions_catalog, s)) for s, _ in d["steps"]}
        view = self.node_view(now)
        full = self.order(self.resolve_targets(targets))
        order, skipped = [], []
        acts = [s for s, _ in d["steps"]]
        for nid in full:
            why = self.skip_reason(nid, lanes, view, acts)
            (skipped.append({"node_id": nid, "reason": why}) if why else order.append(nid))
        return {"order": order, "skipped": skipped, "targets": full, "destructive": desc["destructive"],
                "needs_step_up": desc["destructive"] or desc["lane"] == "system",
                "online": sum(1 for n in view.values() if n["online"]), "lanes": lanes}

    def create_run(self, action: str, args: Any, targets: Any, *, source: str, created_by: Optional[str] = None,
                   check_overlap: bool = True) -> dict:
        d = self.maintenance_def(action)
        if d is None:
            raise HTTPException(400, f"unknown action {action!r}")
        try:
            run_args = validate_args({"name": d["name"], "args": d["args"]}, args or {})
            self.step_args(d, run_args)
        except ActionError as e:
            raise HTTPException(400, str(e))
        now = self.ctx.clock()
        pl = self.plan(d, targets, now)
        with self.lock:
            if check_overlap:
                for r in self.store.q("SELECT id,targets FROM maintenance_runs WHERE status IN ('pending','running')"):
                    if set(jloads(r["targets"], [])) & set(pl["targets"]):
                        raise HTTPException(409, f"another maintenance run ({r['id']}) is active on these machines")
            rid = new_id(self.ctx.clock)
            self.store.x("INSERT INTO maintenance_runs(id,action,args,targets,idx,status,current_job,results,source,created,"
                         "started,finished,stop_on_failure,created_by,updated) VALUES(?,?,?,?,0,'pending',NULL,'[]',?,?,NULL,"
                         "NULL,1,?,?)", (rid, d["name"], json.dumps(run_args), json.dumps(pl["targets"]), source, now,
                                         created_by, now))
        self.ctx.event(None, "info", f"maintenance {d['title']} queued ({source}): {', '.join(pl['order']) or 'no machine online'}")
        return {"id": rid, "order": pl["order"]}

    def run_view(self, r: Any) -> dict:
        d = self.maintenance_def(r["action"])
        cur = None
        if r["current_job"]:
            j = self.store.q("SELECT node_id FROM jobs WHERE id=?", (r["current_job"],))
            cur = {"node_id": j[0]["node_id"] if j else None, "job_id": r["current_job"]}
        return {"id": r["id"], "action": r["action"], "title": d["title"] if d else r["action"],
                "args": jloads(r["args"], {}), "targets": jloads(r["targets"], []), "idx": r["idx"], "status": r["status"],
                "current": cur, "results": jloads(r["results"], []), "source": r["source"], "created": r["created"],
                "started": r["started"], "finished": r["finished"]}

    def cancel_run(self, rid: str) -> dict:
        with self.lock:
            r = self.store.q("SELECT * FROM maintenance_runs WHERE id=?", (rid,))
            if not r:
                raise HTTPException(404, "no such run")
            if r[0]["status"] not in RUN_ACTIVE:
                raise HTTPException(409, f"run is {r[0]['status']}")
            jid = r[0]["current_job"]
            if jid:
                with self.ctx.p2.sched_lock:
                    self.store.touch("jobs", "id", jid, "status='cancelled', finished=?", (self.ctx.clock(),), "status='queued'")
                    self.store.touch("jobs", "id", jid, "cancel_requested=1", (), "status IN ('starting','running')")
            self.store.touch("maintenance_runs", "id", rid, "status='cancelled', finished=?", (self.ctx.clock(),))
        return {"ok": True}

    def step_runs(self, now: float) -> None:
        """Leader tick: advance every active run by at most one job each (one machine at a time per run). One broken
        run (e.g. its stored args no longer validate after a catalog change) is failed; it never blocks the others."""
        with self.lock:
            rows = self.store.q("SELECT * FROM maintenance_runs WHERE status IN ('pending','running') ORDER BY created, id")
            claimed: set = set()
            for r in sorted(rows, key=lambda x: x["status"] != "running"):   # running runs claim their machines first
                targets = jloads(r["targets"], [])
                targets = targets if isinstance(targets, list) else []
                if r["status"] == "pending" and set(targets) & claimed:
                    claimed |= set(targets)
                    continue                     # FIFO: wait for the earlier run on these machines
                claimed |= set(targets)
                try:
                    self._advance(r, targets, now)
                except Exception as e:           # noqa: BLE001 - isolate one bad run from the rest of the tick
                    self._fail_run(r, f"internal error: {type(e).__name__}: {e}", now)

    def _fail_run(self, r: Any, why: str, now: float) -> None:
        results = jloads(r["results"], [])
        results = (results if isinstance(results, list) else []) + [
            {"node_id": None, "job_id": None, "step": None, "status": "failed", "summary": why[:300], "finished": now}]
        self.store.touch("maintenance_runs", "id", r["id"], "status='failed', results=?, finished=?",
                         (json.dumps(results), now), "status IN ('pending','running')")
        self.ctx.event(None, "warn", f"maintenance run {r['id']} ({r['action']}) failed: {why[:300]}")

    @staticmethod
    def wait_kind(action: str, args: Any) -> Optional[str]:
        """Steps that take the machine (or a Forge process) away: the run waits for it to come back."""
        if action == "reboot":
            return "reboot"
        if action == "restart_task":
            return {"ForgeHub": "hub", "ForgeAgent": "agent"}.get(str((args or {}).get("name") or ""))
        return None

    def boot_seen(self, nid: str, after: float, lane: Optional[str] = None) -> bool:
        b = self.boots.get(nid) or {}
        return any(ts > after for ln, ts in b.items() if lane is None or ln == lane)

    def wait_state(self, w: dict, now: float) -> str:
        """A finished reboot / restart step -> 'ok' (the machine or process is back), 'wait' or 'timeout'."""
        nid, kind, since = w.get("node_id"), w.get("kind"), float(w.get("since") or now)
        n = self.node_view(now).get(nid) or {}
        if kind == "reboot":
            boot = self.node_boot(nid)
            back = bool(n.get("online") and boot and boot != w.get("boot")) or \
                (self.boot_seen(nid, since + 5) and bool(n.get("online") or n.get("sys_online")))
        elif kind == "agent":
            back = bool(n.get("online")) and (self.boot_seen(nid, since, "user") or now - since >= AGENT_RESTART_GRACE_S)
        elif kind == "hub":
            c = self.ctx.cluster
            peer = nid == c.id or any(p["id"] == nid for p in c.peers())
            back = now - since >= HUB_RESTART_GRACE_S and (not peer or self.ctx.lease._healthy(nid))
        else:
            back = True
        if back:
            return "ok"
        return "timeout" if now - since > WAIT_TIMEOUT_S.get(kind or "", 600) else "wait"

    def _alert_failed(self, d: dict, r: Any, node: Optional[str], job_id: Optional[str], summary: str) -> None:
        title, key = d["title"], f"upkeep_failed:{node}:{r['action']}"
        self.ctx.raise_alert("upkeep_failed", key=key, node_id=node, level="warn", msg=f"{title} failed on {node}: {summary}",
                             data={"run_id": r["id"], "job_id": job_id},
                             action={"label": "Open task", "kind": "open_job", "target": {"job_id": job_id}})
        self.ctx.notify("upkeep_failed", f"{title} failed on {node}", summary, node_id=node, job_id=job_id,
                        url=f"#/task/{job_id}")

    def _advance(self, r: Any, targets: list, now: float) -> None:
        d = self.maintenance_def(r["action"])
        rid, idx, status, cur = r["id"], int(r["idx"] or 0), r["status"], r["current_job"]
        results = jloads(r["results"], [])
        started = r["started"] or now
        if d is None:
            self.store.touch("maintenance_runs", "id", rid, "status='failed', finished=?", (now,))
            return
        try:
            steps = self.step_args(d, jloads(r["args"], {}))
        except ActionError as e:
            self._fail_run(r, f"arguments no longer valid: {e}", now)
            return
        nsteps = max(1, len(steps))
        lanes = {action_lane(find_action(self.ctx.actions_catalog, s)) for s, _ in steps}
        acts = [s for s, _ in steps]
        view: Optional[dict] = None
        while True:
            w = results[-1].get("wait") if results and isinstance(results[-1], dict) else None
            if not cur and isinstance(w, dict) and "back" not in w:
                st = self.wait_state(w, now)
                if st == "wait":
                    break                        # machine / process not back yet: next tick
                w["back"] = now if st == "ok" else False
                if st == "timeout":
                    summary = {"reboot": "machine did not come back after the restart",
                               "hub": "hub did not come back after the restart",
                               "agent": "agent did not come back after the restart"}.get(w.get("kind"), "did not come back")
                    results[-1].update(status="failed", summary=summary)
                    self._alert_failed(d, r, w.get("node_id"), results[-1].get("job_id"), summary)
                    if r["stop_on_failure"]:
                        status = "failed"
                        break
                continue
            if cur:
                j = self.store.q("SELECT id,node_id,status,action,created FROM jobs WHERE id=?", (cur,))
                if j and j[0]["status"] == "queued" and now - (j[0]["created"] or now) > RUN_PICKUP_TIMEOUT_S \
                        and self.cancel_queued(cur, "not picked up within 30 min"):
                    node = targets[idx // nsteps] if idx // nsteps < len(targets) else j[0]["node_id"]
                    results.append({"node_id": node, "job_id": cur, "step": steps[idx % nsteps][0], "status": "skipped",
                                    "summary": "machine did not pick up the job", "finished": now})
                    cur, idx = None, (idx // nsteps + 1) * nsteps
                    continue
                if j and j[0]["status"] in ACTIVE:
                    break                        # still running: next tick
                jstatus = j[0]["status"] if j else "failed"
                node = targets[idx // nsteps] if idx // nsteps < len(targets) else (j[0]["node_id"] if j else None)
                action, sargs = steps[idx % nsteps]
                res = parse_result(self.job_text(cur)) if j else None
                summary = summarize(action, res, jstatus) if j else "job lost"
                results.append({"node_id": node, "job_id": cur, "step": action, "status": jstatus, "summary": summary,
                                "finished": now})
                key = f"upkeep_failed:{node}:{r['action']}"
                cur = None
                if jstatus != "done":
                    self._alert_failed(d, r, node, results[-1]["job_id"], summary)
                    if r["stop_on_failure"] or jstatus == "cancelled":
                        status = "failed" if jstatus != "cancelled" else "cancelled"
                        idx += 1
                        break
                else:
                    self.ctx.resolve_alert(key)
                    kind = self.wait_kind(action, sargs)
                    if kind:                     # one machine at a time: wait until it (or that process) is back
                        results[-1]["wait"] = {"kind": kind, "node_id": node, "since": now, "boot": self.node_boot(node)}
                idx += 1
                continue
            if idx >= len(targets) * nsteps:
                status = "done" if status != "failed" else status
                break
            node, (action, args) = targets[idx // nsteps], steps[idx % nsteps]
            if view is None:
                view = self.node_view(now)
            why = self.skip_reason(node, lanes, view, acts)
            if why:
                results.append({"node_id": node, "job_id": None, "step": None, "status": "skipped", "summary": why,
                                "finished": now})
                idx = (idx // nsteps + 1) * nsteps          # skip every step of this machine
                continue
            if self.reboot_imminent(node, now):
                break                            # a post-update reboot is about to hit this machine: wait for it
            try:
                cur = self.queue_action(node, action, args, source="upkeep", run_id=rid)
            except ActionError as e:
                results.append({"node_id": node, "job_id": None, "step": action, "status": "failed", "summary": str(e),
                                "finished": now})
                status = "failed"
                break
            status = "running"
            break
        finished = now if status in ("done", "failed", "cancelled") else None
        if status != r["status"] or idx != r["idx"] or cur != r["current_job"] or json.dumps(results) != (r["results"] or "[]"):
            self.store.touch("maintenance_runs", "id", rid,
                             "status=?, idx=?, current_job=?, results=?, started=?, finished=?",
                             (status if status != "pending" else "running", idx, cur, json.dumps(results), started, finished),
                             "status IN ('pending','running')")
            if finished:
                n_ok = sum(1 for x in results if x["status"] == "done")
                self.ctx.event(None, "ok" if status == "done" else "warn",
                               f"maintenance {d['title']} {status} ({n_ok} step(s) ok, "
                               f"{sum(1 for x in results if x['status'] == 'skipped')} machine(s) skipped)")

    def schedule_tick(self, now: float) -> None:
        sch = self.schedule()
        if not sch.get("enabled") or sch.get("day") not in DAYS:
            return
        at = parse_hhmm(sch.get("time"))
        if at is None:
            return
        d = local_dt(now)
        since = d.hour * 60 + d.minute - at
        if DAYS[d.weekday()] != sch["day"] or not 0 <= since < SCHEDULE_LATE_MIN:
            return
        today = d.date().isoformat()
        if self.get_pref("upkeep.schedule_last", None) == today:
            return
        self.set_pref("upkeep.schedule_last", today)
        for a in sch.get("actions") or []:
            try:
                self.create_run(a, {}, "all", source="schedule", created_by="schedule", check_overlap=False)
            except HTTPException as e:
                self.ctx.event(None, "warn", f"weekly upkeep: {a} not started: {e.detail}")

    # ---------------------------------------------------------------- F3: repo sync
    def project_keys(self, pid: str) -> list:
        r = self.store.q("SELECT key FROM projects WHERE id=?", (pid,))
        keys = [r[0]["key"]] if r else []
        return keys + [a["id"] for a in self.store.q("SELECT id FROM project_alias WHERE project_id=?", (pid,))]

    def sync_tick(self, now: float) -> int:
        view, n = self.node_view(now), 0
        for p in self.store.q("SELECT id, sync_nodes FROM projects WHERE sync_nodes IS NOT NULL AND sync_nodes NOT IN ('', '[]')"):
            nodes = [x for x in jloads(p["sync_nodes"], []) if isinstance(x, str)]
            keys = self.project_keys(p["id"])
            if not nodes or not keys:
                continue
            marks = ",".join("?" * len(keys))
            for repo in self.store.q(f"SELECT node_id, cwd FROM repos WHERE project_key IN ({marks})", tuple(keys)):
                nid, cwd = repo["node_id"], repo["cwd"]
                if nid not in nodes or self.skip_reason(nid, {"user"}, view):
                    continue
                pending = self.store.q("SELECT id, args, status, created FROM jobs WHERE node_id=? AND action='git_sync' AND "
                                       "status IN ('queued','starting','running')", (nid,))
                stale = [j for j in pending if j["status"] == "queued" and now - (j["created"] or now) > 2 * self.repo_sync_every()]
                for j in stale:
                    self.cancel_queued(j["id"], "repo sync not picked up")
                pending = [j for j in pending if j not in stale]
                if any((jloads(j["args"], {}) or {}).get("cwd", "").lower() == (cwd or "").lower() for j in pending):
                    continue
                try:
                    self.queue_action(nid, "git_sync", {"cwd": cwd}, source="sync", project_id=p["id"])
                    n += 1
                except ActionError as e:
                    self.ctx.event(nid, "warn", f"repo sync skipped for {cwd}: {e}")
        return n

    def on_sync_finished(self, job: dict, status: str, output: str) -> None:
        # only a result the agent reported counts: a cancelled job, a job lost while the machine slept or a timeout
        # (no ##RESULT line) says nothing about the repository and must not raise repo_conflict
        res = parse_result(output) or {}
        result = res.get("result") if status in ("done", "failed") else None
        nid, pid = job.get("node_id"), job.get("project_id")
        cwd = (jloads(job.get("args"), {}) or {}).get("cwd") if isinstance(job.get("args"), str) else (job.get("args") or {}).get("cwd")
        key = f"repo_conflict:{nid}:{pid or cwd}"
        if result in CONFLICT_RESULTS:
            name = None
            if pid:
                r = self.store.q("SELECT name FROM projects WHERE id=?", (pid,))
                name = r[0]["name"] if r else None
            self.ctx.raise_alert("repo_conflict", key=key, node_id=nid, level="warn",
                                 msg=f"{name or cwd} on {nid} could not be synced ({result})" +
                                     (f": {res.get('detail')}" if res.get("detail") else ""),
                                 data={"project_id": pid, "cwd": cwd, "result": result, "job_id": job.get("id")},
                                 action={"label": "Open project", "kind": "open_project", "target": {"project_id": pid}})
        elif result in OK_RESULTS:
            self.ctx.resolve_alert(key)

    # ---------------------------------------------------------------- F4: Windows Update windows
    def wu_plan(self, now: float) -> list:
        prefs = self.wu_prefs()
        base = parse_window(prefs.get("window")) or parse_window(WU_DEFAULT["window"])
        stagger = int(prefs.get("stagger_min") or 0)
        ids = [r["id"] for r in self.store.q("SELECT id FROM nodes ORDER BY id")]
        order = self.order(ids)
        out = []
        for rank, nid in enumerate(order):
            own = self.setting(nid, "wu_window")
            if own == "off":
                out.append({"node_id": nid, "window": "off", "slot": None, "rank": rank})
                continue
            window = parse_window(own) if own else base
            off = 0 if own else wu_slot_offset(rank, stagger, window_offset(window, now)[1], len(order))
            slot = (window[0] + off) % 1440
            out.append({"node_id": nid, "window": own or prefs.get("window"), "slot": f"{slot // 60:02d}:{slot % 60:02d}",
                        "rank": rank, "_window": window, "_off": off})
        return out

    def run_busy(self, nid: str, now: float) -> bool:
        """A maintenance run has a step running on this machine, or waits for it to come back from a restart."""
        for r in self.store.q("SELECT current_job, results FROM maintenance_runs WHERE status IN ('pending','running')"):
            if r["current_job"] and any(j["id"] == r["current_job"] for j in self.active_jobs(nid)):
                return True
            res = jloads(r["results"], [])
            w = res[-1].get("wait") if isinstance(res, list) and res and isinstance(res[-1], dict) else None
            if isinstance(w, dict) and w.get("node_id") == nid and "back" not in w:
                return True
        return False

    def wu_tick(self, now: float) -> int:
        if not self.wu_prefs().get("enabled"):
            return 0
        view, n = self.node_view(now), 0
        for p in self.wu_plan(now):
            nid = p["node_id"]
            last = self.wu_last(nid)
            last = self._wu_followup(nid, last, view, now) or last
            if p["slot"] is None:
                continue
            since, length, opened = window_offset(p["_window"], now)
            if since >= length or since < p["_off"] or last.get("date") == opened:
                continue
            if self.skip_reason(nid, {"system"}, view, ("windows_update",)):
                continue                         # offline / 2.2 SYSTEM agent: retried every minute while the window is open
            if any(j["action"] in ("windows_update", "reboot") for j in self.active_jobs(nid)):
                continue
            if self.run_busy(nid, now) or self.reboot_imminent(nid, now):
                continue                         # a maintenance step / restart is in progress there: try again later
            jid = self.queue_action(nid, "windows_update", {"reboot": "never"}, source="upkeep")
            self.set_setting(nid, "wu_last", json.dumps({"date": opened, "state": "queued", "job_id": jid, "queued": now,
                                                          "reboot": last.get("reboot")}))
            n += 1
        return n

    def _wu_followup(self, nid: str, last: dict, view: dict, now: float) -> Optional[dict]:
        """Resolve what a missed hook / a reboot left behind. Returns the updated wu_last (None = unchanged)."""
        # a result the job_finished hook missed (leader change): pick it up from the job row
        if last.get("state") == "queued" and last.get("job_id"):
            j = self.store.q("SELECT * FROM jobs WHERE id=?", (last["job_id"],))
            if not j:
                new = last | {"state": "lost"}
            elif j[0]["status"] == "queued" and now - (last.get("queued") or now) > WU_PICKUP_TIMEOUT_S:
                self.cancel_queued(last["job_id"], "Windows Update window missed (machine did not pick it up)")
                new = last | {"state": "missed"}
            elif j[0]["status"] not in ACTIVE:
                self.on_wu_finished(dict(j[0]), j[0]["status"], self.job_text(last["job_id"]), now)
                return self.wu_last(nid)
            else:
                return None
            self.set_setting(nid, "wu_last", json.dumps(new))
            return new
        reboot = last.get("reboot")
        if reboot not in ("pending", "queued", "waiting"):
            return None
        up = self.metrics(nid).get("uptime_s")
        if (view.get(nid) or {}).get("online") and isinstance(up, (int, float)) and now - up > (last.get("ts") or now):
            self.ctx.resolve_alert(f"reboot_pending:{nid}")
            new = last | {"reboot": "done"}
        elif reboot == "waiting":
            # reboot deferred until the machine's jobs finish; give up (pending) when the window closes
            new = self._wu_try_reboot(nid, last, now)
            if new is None:
                return None
        elif reboot == "queued":
            j = self.store.q("SELECT status FROM jobs WHERE id=?", (last.get("reboot_job"),)) if last.get("reboot_job") else []
            js = j[0]["status"] if j else None
            overdue = now - float(last.get("reboot_ts") or 0) > REBOOT_DELAY_S + REBOOT_SETTLE_S
            if js in ("failed", "cancelled") or (js not in ACTIVE and overdue):
                new = last | {"reboot": "pending"}
                why = f"the reboot job {js}" if js in ("failed", "cancelled") else "the queued reboot did not happen"
                self._raise_reboot_pending(nid, why)
            else:
                return None
        else:
            return None
        self.set_setting(nid, "wu_last", json.dumps(new))
        return new

    def _raise_reboot_pending(self, nid: str, why: str) -> None:
        self.ctx.raise_alert("reboot_pending", key=f"reboot_pending:{nid}", node_id=nid, level="info",
                             msg=f"{nid} needs a restart to finish Windows updates ({why})",
                             action={"label": "Open machine", "kind": "open_machine", "target": {"node_id": nid}})

    def other_hub_healthy(self, nid: str, now: Optional[float] = None) -> bool:
        """Another hub is healthy + eligible and is not itself about to update or restart."""
        lease = self.ctx.lease
        now = self.ctx.clock() if now is None else now
        for p in self.ctx.cluster.peers():
            pid = p["id"]
            if pid == nid or not (lease._healthy(pid) and lease._eligible(pid)):
                continue
            if any(j["action"] in ("windows_update", "reboot") for j in self.active_jobs(pid)):
                continue
            if self.wu_last(pid).get("reboot") == "queued" and self.reboot_imminent(pid, now):
                continue
            return True
        return False

    def _wu_try_reboot(self, nid: str, last: dict, now: float, exclude: tuple = ()) -> Optional[dict]:
        """Queue the post-update reboot when it is safe. -> updated wu_last, or None when still waiting."""
        plan = next((p for p in self.wu_plan(now) if p["node_id"] == nid and p["slot"]), None)
        inside = bool(plan) and window_offset(plan["_window"], now)[0] < window_offset(plan["_window"], now)[1]
        if not inside:
            self._raise_reboot_pending(nid, "outside its update window")
            return last | {"reboot": "pending"}
        if not self.other_hub_healthy(nid, now):
            self._raise_reboot_pending(nid, "no other healthy hub")
            return last | {"reboot": "pending"}
        if self.active_jobs(nid, exclude) or self.run_busy(nid, now):
            if last.get("reboot") != "waiting":
                self.ctx.event(nid, "info", f"{nid}: restart for Windows updates waits for its running jobs")
            return last | {"reboot": "waiting"} if last.get("reboot") != "waiting" else None
        try:
            jid = self.queue_action(nid, "reboot", {"delay_s": REBOOT_DELAY_S}, source="upkeep")
        except ActionError as e:
            self._raise_reboot_pending(nid, f"reboot not queued: {e}")
            return last | {"reboot": "pending"}
        j = self.store.q("SELECT status FROM jobs WHERE id=?", (jid,))
        if j and j[0]["status"] in ("failed", "cancelled"):         # e.g. admin lane disabled on this hub
            self._raise_reboot_pending(nid, "the reboot job could not run (admin lane disabled?)")
            return last | {"reboot": "pending", "reboot_job": jid}
        self.ctx.event(nid, "warn", f"{nid}: rebooting in {REBOOT_DELAY_S} s to finish Windows updates")
        return last | {"reboot": "queued", "reboot_job": jid, "reboot_ts": now}

    def on_wu_finished(self, job: dict, status: str, output: str, now: float) -> None:
        nid = job.get("node_id")
        res = parse_result(output) or {}
        last = self.wu_last(nid)
        failed = int(res.get("failed") or 0) if isinstance(res.get("failed"), (int, float)) else 0
        reboot_required = bool(res.get("reboot_required")) and status == "done"
        last.update({"state": "done", "ts": now, "status": status, "job_id": job.get("id"),
                     "installed": int(res.get("installed") or 0) if isinstance(res.get("installed"), (int, float)) else 0,
                     "failed": failed, "reboot_required": reboot_required,
                     "kb": [str(k)[:32] for k in (res.get("kb") or []) if isinstance(k, (str, int))][:50]})
        if status != "done" or failed:
            self.ctx.raise_alert("windows_update_failed", key=f"windows_update_failed:{nid}", node_id=nid, level="warn",
                                 msg=f"Windows Update on {nid}: {summarize('windows_update', res, status)}",
                                 data={"job_id": job.get("id")},
                                 action={"label": "Open task", "kind": "open_job", "target": {"job_id": job.get("id")}})
        else:
            self.ctx.resolve_alert(f"windows_update_failed:{nid}")
        if reboot_required:
            if job.get("source") != "upkeep":
                last["reboot"] = "pending"
                self._raise_reboot_pending(nid, "started by hand")
            else:
                last = self._wu_try_reboot(nid, last | {"reboot": None}, now, exclude=(job.get("id"),)) or last
        self.set_setting(nid, "wu_last", json.dumps(last))

    # ---------------------------------------------------------------- F5: Wake-on-LAN
    def known_macs(self, nid: str) -> list:
        v = self.get_pref(f"upkeep.macs.{nid}", None)
        if isinstance(v, dict) and v.get("macs"):
            return clean_macs(v["macs"])
        return clean_macs(self.metrics(nid).get("macs"))

    def wake(self, nid: str, now: float) -> dict:
        if not self.store.q("SELECT 1 FROM nodes WHERE id=?", (nid,)):
            raise HTTPException(404, "no such machine")
        view = self.node_view(now)
        if view.get(nid, {}).get("online"):
            raise HTTPException(409, "already online")
        macs = self.known_macs(nid)
        if not macs:
            raise HTTPException(422, "no MAC known")
        nets = networks(macs)
        via = []
        for other in self.order([o for o in view if o != nid]):
            v = view[other]
            if not v["online"] or fs.parse_version(v["agent_version"]) < USER_ACTION_AGENT:
                continue
            ips = [ipaddress.IPv4Address(m["ip"]) for m in self.known_macs(other) if m.get("ip")]
            ips += [ipaddress.IPv4Address(x["ip"]) for x in (self.metrics(other).get("ips") or [])
                    if isinstance(x, dict) and _is_ipv4(x.get("ip"))]
            if any(ip in n for ip in ips for n in nets):
                via.append(other)
            if len(via) == 2:
                break
        if not via:
            raise HTTPException(503, "no online machine on the same network")
        items = wol_items(macs)
        with self.lock:
            for v in via:
                self.wol[v] = (items, now + WOL_TTL_S)
        self.ctx.event(nid, "info", f"wake-on-LAN for {nid} ({macs[0]['mac']}) sent via {', '.join(via)}")
        return {"ok": True, "via": via, "mac": macs[0]["mac"]}

    # ---------------------------------------------------------------- F6b: brief model pre-pull
    def prepull_tick(self, now: float) -> Optional[str]:
        p2 = self.ctx.p2
        _, window = p2.brief_window()
        lo, hi = window
        since, length, opened = window_offset(window, now)
        if since >= length:
            return None
        node = p2.pick_brief_node(now)
        models = self.models.get(node) if node else None
        if not node or models is None:
            return None                          # unknown model list (agent < 2.3 or not reported yet)
        model = p2.brief_model()
        last = self.get_pref("upkeep.prepull_last", {}) or {}
        if last.get("date") == opened or model_present(models, model):
            return None
        if self.skip_reason(node, {"user"}, self.node_view(now)):
            return None
        jid = self.queue_action(node, "ollama_pull", {"model": model}, source="upkeep")
        self.set_pref("upkeep.prepull_last", {"date": opened, "node_id": node, "model": model, "job_id": jid})
        return jid

    # ---------------------------------------------------------------- F6a: merge pre-2.3 scratch/temp projects
    def merge_misc_projects(self, now: float) -> int:
        p2, n = self.ctx.p2, 0
        for r in self.store.q("SELECT id, key FROM projects WHERE key LIKE 'local:%'"):
            _, nid, cwd = (r["key"].split(":", 2) + ["", ""])[:3]
            if not nid or not cwd or not fp.is_misc_cwd(cwd):
                continue
            try:
                target = p2.ensure_project(f"misc:{nid}", None, now)
                if target != r["id"]:
                    p2.merge_projects([target, r["id"]], now)
                    n += 1
            except HTTPException:
                continue
        if n:
            self.ctx.event(None, "info", f"{n} scratch/temp folder project(s) merged into 'Scratch / misc'")
        return n

    # ---------------------------------------------------------------- hooks
    def on_tick(self, now: float, settled: bool) -> None:
        if not settled:
            return
        self.ctx.safe_call(self.step_runs, now)
        for name, every, fn in (("misc", 3600, self.merge_misc_projects), ("schedule", 60, self.schedule_tick),
                                ("wu", 60, self.wu_tick), ("prepull", 300, self.prepull_tick),
                                ("sync", self.repo_sync_every(), self.sync_tick)):
            if now - self.last.get(name, -1e18) >= every:
                self.last[name] = now
                self.ctx.safe_call(fn, now)
        with self.lock:
            for k in [k for k, (_, exp) in self.wol.items() if exp <= now]:
                del self.wol[k]

    def on_system_report(self, nid: str, body: dict, resp: dict, now: float) -> None:
        if body.get("boot"):
            self.boots.setdefault(nid, {})["system"] = now

    def on_report(self, nid: str, body: dict, resp: dict, now: float) -> None:
        if body.get("boot"):
            self.boots.setdefault(nid, {})["user"] = now
        m = body.get("metrics") if isinstance(body.get("metrics"), dict) else {}
        if "macs" in m:
            macs = clean_macs(m.get("macs"))
            raw = json.dumps(macs)
            if macs and self.macs_cache.get(nid) != raw:
                stored = self.get_pref(f"upkeep.macs.{nid}", None)
                if not (isinstance(stored, dict) and json.dumps(clean_macs(stored.get("macs"))) == raw):
                    self.set_pref(f"upkeep.macs.{nid}", {"macs": macs, "ts": now})
                self.macs_cache[nid] = raw
        if "ollama_models" in m:
            om = m.get("ollama_models")
            om = om if isinstance(om, list) else [om] if isinstance(om, str) else []
            self.models[nid] = [str(x)[:128] for x in om if isinstance(x, str)][:500]
        with self.lock:
            w = self.wol.pop(nid, None)
        if w and w[1] > now:
            resp["wol"] = w[0]
        # user-lane action jobs: the core dispatch item has no action/args (A-F1 / CONTRACT 7.3)
        for item in resp.get("jobs") or []:
            if isinstance(item, dict) and item.get("engine") == "action":
                r = self.store.q("SELECT action, args, run_as FROM jobs WHERE id=?", (item.get("id"),))
                if r:
                    item.update(run_as=r[0]["run_as"] or "user", action=r[0]["action"], args=jloads(r[0]["args"], {}))

    def on_job_finished(self, job: dict, status: str, output: str, now: float) -> None:
        action = job.get("action")
        if action == "git_sync":
            self.on_sync_finished(job, status, output)
        elif action == "windows_update":
            self.on_wu_finished(job, status, output, now)

    def state_extras(self, state: dict, now: float) -> None:
        wu = {r["node_id"]: (r["wu_window"], jloads(r["wu_last"], None))
              for r in self.store.q("SELECT node_id, wu_window, wu_last FROM settings")}
        for n in state.get("nodes") or []:
            win, last = wu.get(n.get("id"), (None, None))
            if isinstance(n.get("settings"), dict):
                n["settings"]["wu_window"] = win
            n["upkeep"] = {"wu_last": last, "wake": bool(self.known_macs(n["id"])) if not n.get("online") else False}

    # ---------------------------------------------------------------- settings API
    def settings_view(self, now: float) -> dict:
        return {"schedule": self.schedule(), "windows_update": self.wu_prefs(), "repo_sync_every_s": self.repo_sync_every(),
                "wu_slots": [{k: v for k, v in p.items() if not k.startswith("_")} for p in self.wu_plan(now)]}

    def put_settings(self, body: dict, now: float) -> dict:
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be an object")
        writes: list = []                    # validate every section first, then write them together
        if "schedule" in body:
            s = body["schedule"]
            if not isinstance(s, dict):
                raise HTTPException(400, "schedule must be an object")
            cur = self.schedule()
            if "enabled" in s:
                if not isinstance(s["enabled"], bool):
                    raise HTTPException(400, "schedule.enabled must be true or false")
                cur["enabled"] = s["enabled"]
            if "day" in s:
                if s["day"] not in DAYS:
                    raise HTTPException(400, f"schedule.day must be one of {list(DAYS)}")
                cur["day"] = s["day"]
            if "time" in s:
                if not isinstance(s["time"], str) or not _HHMM_RE.match(s["time"]):
                    raise HTTPException(400, "schedule.time must be HH:MM")
                cur["time"] = s["time"]
            if "actions" in s:
                acts = s["actions"]
                if not isinstance(acts, list) or not all(isinstance(a, str) for a in acts):
                    raise HTTPException(400, "schedule.actions must be a list of action names")
                for a in acts:
                    d = self.maintenance_def(a)
                    if d is None:
                        raise HTTPException(400, f"unknown action {a!r}")
                    try:
                        self.step_args(d, {})
                    except ActionError as e:
                        raise HTTPException(400, f"{a} needs arguments and cannot be scheduled: {e}")
                    if self.describe(d)["destructive"]:
                        raise HTTPException(400, f"{a} is destructive and cannot be scheduled")
                cur["actions"] = list(dict.fromkeys(acts))
            writes.append(("upkeep.schedule", cur))
        if "windows_update" in body:
            w = body["windows_update"]
            if not isinstance(w, dict):
                raise HTTPException(400, "windows_update must be an object")
            cur = self.wu_prefs()
            if "enabled" in w:
                if not isinstance(w["enabled"], bool):
                    raise HTTPException(400, "windows_update.enabled must be true or false")
                cur["enabled"] = w["enabled"]
            if "window" in w:
                win = normalize_wu_window(w["window"]) if w["window"] != "off" else None
                if not win:
                    raise HTTPException(400, "windows_update.window must be HH:MM-HH:MM")
                cur["window"] = win
            if "stagger_min" in w:
                v = w["stagger_min"]
                if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 240:
                    raise HTTPException(400, "windows_update.stagger_min must be an integer 0-240")
                cur["stagger_min"] = v
            writes.append(("upkeep.windows_update", cur))
        if "repo_sync_every_s" in body:
            v = body["repo_sync_every_s"]
            if isinstance(v, bool) or not isinstance(v, int) or not 300 <= v <= 86400:
                raise HTTPException(400, "repo_sync_every_s must be an integer 300-86400")
            writes.append(("upkeep.repo_sync_every_s", v))
        for k, v in writes:
            self.set_pref(k, v)
        return self.settings_view(now)


def _is_ipv4(v: Any) -> bool:
    try:
        ipaddress.IPv4Address(str(v))
        return True
    except ValueError:
        return False


# ================================================================ registration
def register(app: Any, ctx: Any) -> None:
    """Add workstream F's routes to `app` and its hooks to `ctx` (forge_ctx.HubContext)."""
    up = Upkeep(ctx)
    app.state.upkeep = up
    ui = [Depends(ctx.require_ui)]
    ctx.on_tick.append(up.on_tick)
    ctx.on_report.append(up.on_report)
    ctx.on_system_report.append(up.on_system_report)
    ctx.on_job_finished.append(up.on_job_finished)
    ctx.state_extras.append(up.state_extras)

    @app.get("/api/maintenance/actions", dependencies=ui)
    def maintenance_actions() -> dict:
        return {"actions": [up.describe(up.maintenance_def(n)) for n in MAINTENANCE]}

    @app.post("/api/maintenance/preview", dependencies=ui)
    def maintenance_preview(body: dict) -> dict:
        d = up.maintenance_def(str(body.get("action") or ""))
        if d is None:
            raise HTTPException(400, f"unknown action {body.get('action')!r}")
        try:
            up.step_args(d, body.get("args"))
        except ActionError as e:
            raise HTTPException(400, str(e))
        pl = up.plan(d, body.get("targets", "all"), ctx.clock())
        return {k: pl[k] for k in ("order", "skipped", "destructive", "needs_step_up", "online")}

    @app.post("/api/maintenance/runs", dependencies=ui)
    def maintenance_run(body: dict, request: Request) -> dict:
        d = up.maintenance_def(str(body.get("action") or ""))
        if d is None:
            raise HTTPException(400, f"unknown action {body.get('action')!r}")
        try:
            up.step_args(d, body.get("args"))
        except ActionError as e:
            raise HTTPException(400, str(e))
        if body.get("confirm") is not True:
            raise HTTPException(400, "confirmation required")
        desc = up.describe(d)
        if desc["destructive"] or desc["lane"] == "system":
            ctx.require_step_up(request)
        who = (getattr(request.state, "principal", None) or {}).get("label") if hasattr(request, "state") else None
        return up.create_run(d["name"], body.get("args"), body.get("targets", "all"), source="ui", created_by=who)

    @app.get("/api/maintenance/runs", dependencies=ui)
    def maintenance_runs(limit: int = 20) -> dict:
        rows = ctx.store.q("SELECT * FROM maintenance_runs ORDER BY created DESC, id DESC LIMIT ?", (max(1, min(limit, 100)),))
        return {"runs": [up.run_view(r) for r in rows]}

    @app.get("/api/maintenance/runs/{rid}", dependencies=ui)
    def maintenance_run_get(rid: str) -> dict:
        r = ctx.store.q("SELECT * FROM maintenance_runs WHERE id=?", (rid,))
        if not r:
            raise HTTPException(404, "no such run")
        return up.run_view(r[0])

    @app.post("/api/maintenance/runs/{rid}/cancel", dependencies=ui)
    def maintenance_run_cancel(rid: str) -> dict:
        return up.cancel_run(rid)

    @app.get("/api/upkeep/settings", dependencies=ui)
    def upkeep_settings() -> dict:
        return up.settings_view(ctx.clock())

    @app.put("/api/upkeep/settings", dependencies=ui)
    def upkeep_settings_put(body: dict) -> dict:
        return up.put_settings(body, ctx.clock())

    @app.put("/api/projects/{pid}/sync", dependencies=ui)
    def project_sync(pid: str, body: dict) -> dict:
        if not ctx.store.q("SELECT 1 FROM projects WHERE id=?", (pid,)):
            raise HTTPException(404, "no such project")
        nodes = body.get("nodes")
        if not isinstance(nodes, list) or not all(isinstance(n, str) for n in nodes):
            raise HTTPException(400, "nodes must be a list of machine ids")
        known = {r["id"] for r in ctx.store.q("SELECT id FROM nodes")}
        unknown = [n for n in nodes if n not in known]
        if unknown:
            raise HTTPException(400, f"unknown machine(s): {', '.join(unknown)}")
        nodes = list(dict.fromkeys(nodes))
        ctx.store.touch("projects", "id", pid, "sync_nodes=?", (json.dumps(nodes),))
        return {"ok": True, "sync_nodes": nodes}

    @app.post("/api/machines/{nid}/wake", dependencies=ui)
    def machine_wake(nid: str) -> dict:
        return up.wake(nid, ctx.clock())
