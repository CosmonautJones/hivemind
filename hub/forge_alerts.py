"""Phase 3 workstream module, owner: workstream N (notifications). See CONTRACT-phase3.md §5 N.

N1 alert engine: every tick the settled leader turns the current conditions (core health checks + ctx.alert_sources)
   into rows of the replicated `alerts` table: one open row per key, auto-resolved after 2 ticks without the
   condition, reopened at most 1 h after a user ack. Other teams raise / resolve their own alerts through
   ctx.raise_alert / ctx.resolve_alert.
N2 notifiers: ntfy (JSON publish to a private random topic) and Web Push (VAPID, forge_webpush). Only these notify:
   a failed user-lane task, a finished task with notify=1, a machine down > machine_down_notify_s, and the
   auto-repair kinds (rolled_back, upkeep_failed, fleet_bad_version). Self-healing alerts notify only after
   alert_selfheal_grace_s. At most one notification per alert key per 6 h. Settings live in `prefs` (notify.*).
N3 dead-man switch: the leader GETs a healthchecks.io-style URL every deadman_every_s (<url path>/fail with send_fail
   while an infrastructure error alert is open: DEADMAN_FAIL_KINDS). The URL is masked in the settings response.
N4 morning brief: overnight alerts and finished tasks go into the roll-up prompt (p2.digest_extras) and into
   GET /api/brief/all (`overnight`).

HTTP to ntfy / push services / the dead-man URL goes through `Alerts.transport` (tests swap in a fake). Sends run on
one background worker thread so a slow notifier never delays the tick or a report; `Alerts.inline = True` (tests)
sends synchronously.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import queue
import re
import secrets
import sys
import threading
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import Body, Depends, HTTPException, Request

import forge_webpush as wp
from forge_projects import redact
from forge_store import jloads, new_id

# core condition thresholds (same values as hub/app.py, which keeps its copies for the node list)
ONLINE_SECS = 30
DISK_ALERT_PCT = 90
CLOCK_SKEW_ALERT_S = 5.0
WATCHDOG_STALE_SECS = 300

SELF_HEALING_KINDS = {"rollout_in_progress", "service_restarting"}
RULE_FOR_KIND = {"task_failed": "task_failed", "task_finished": "task_finished",
                 "node_offline": "machine_down", "machine_down": "machine_down",
                 "rolled_back": "autorepair", "upkeep_failed": "autorepair", "fleet_bad_version": "autorepair"}
RULE_KEYS = ("task_failed", "task_finished", "machine_down", "autorepair")
NOTIFY_DEDUP_S = 6 * 3600          # at most one notification per alert key per 6 h
ACK_REOPEN_S = 3600                # an acked condition that is still true reopens after 1 h
MISS_TICKS = 2                     # a condition gone for this many ticks is auto-resolved
TASK_ALERT_TTL_S = 24 * 3600       # task_failed / task_finished alerts resolve themselves after a day
RAISED_MAX_AGE_S = 7 * 86400       # any raised alert nobody resolves is closed after a week
RESOLVED_KEEP_S = 30 * 86400       # resolved rows are deleted (with tombstones) after 30 days
HOUSEKEEP_EVERY_S = 3600
OVERNIGHT_MAX = 30
PUSH_MAX_FAILS = 20                # consecutive HTTP rejections (not 404/410) before a sub is dropped ...
PUSH_FAIL_MIN_AGE_S = 24 * 3600    # ... and only when it has not worked for a day
PUSH_GONE = (404, 410)             # the only clear "subscription is dead" answers
PUSH_REJECT = (400, 401, 403, 413) # the push service refused this request (counted, not deleted at once)
MSG_TOUCH_MIN_S = 300              # a condition whose msg alone changes is rewritten at most every 5 min
# Push services used by the browsers U supports; config `push_host_allow` adds suffixes (self-hosted or test).
PUSH_HOST_SUFFIXES = ("fcm.googleapis.com", "push.services.mozilla.com", "push.apple.com", "notify.windows.com")
# Dead-man /fail is sent only for open error alerts of these kinds (infrastructure), not for a failed user task.
DEADMAN_FAIL_KINDS = ("node_offline", "machine_down", "hub_down", "fleet_bad_version", "rolled_back")
TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
SESSION_ID_RE = re.compile(r"^[0-9a-f]{64}$")
URL_RE = re.compile(r"^https?://[^\s/?#@]+(?:[/?#][^\s]*)?$", re.I)   # no userinfo (user:pass@host)
LEVELS = ("ok", "info", "warn", "error")
DEDUP_STATE = "suppressed:dedup"   # the only non-final notify_state (N-04)

DEFAULT_PREFS = {
    "notify.ntfy": {"enabled": False, "server": "https://ntfy.sh", "topic": None},
    "notify.webpush": {"enabled": True},
    "notify.rules": {"task_failed": True, "task_finished": True, "machine_down": True, "autorepair": True},
    "notify.deadman": {"enabled": False, "url": None, "send_fail": False},
}


def _disk_pct(metrics: dict) -> Optional[float]:
    disks = metrics.get("disks")
    disks = disks if isinstance(disks, list) else ([disks] if isinstance(disks, dict) else [])
    pcts = [100 * (1 - d["free_gb"] / d["total_gb"]) for d in disks if isinstance(d, dict) and d.get("total_gb")]
    return round(max(pcts), 1) if pcts else None


def _parse_ts(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str) or not v:
        return None
    s = re.sub(r"(\.\d{6})\d+", r"\1", v.strip().replace("Z", "+00:00"))
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).timestamp()


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _blocked_host(host: str, allow_private: bool) -> bool:
    """True for loopback / link-local / unspecified / multicast (and private, unless allowed) IP literals and localhost."""
    if not host or host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False                       # a DNS name; push endpoints are additionally allow-listed by suffix
    if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return True
    return ip.is_private and not allow_private


def outbound_url_ok(url: str, allow_private: bool = True) -> bool:
    """Settings URLs (ntfy server, dead-man URL): http(s), no userinfo, no loopback / link-local target.
    Private (LAN / tailnet) addresses stay allowed here: a self-hosted ntfy on the tailnet is a normal setup."""
    return bool(URL_RE.match(url)) and not _blocked_host(_host_of(url), allow_private)


def mask_url(url: Optional[str]) -> Optional[str]:
    """Hide the secret part of a ping URL (healthchecks.io puts the check secret in the path): scheme://host/…<last 4>."""
    if not url:
        return url
    try:
        p = urlsplit(url)
    except ValueError:
        return "…"
    rest = (p.path or "") + (("?" + p.query) if p.query else "")
    if len(rest.strip("/")) <= 4:
        return f"{p.scheme}://{p.netloc}{rest}"
    return f"{p.scheme}://{p.netloc}/…{rest[-4:]}"


def fail_url(url: str) -> str:
    """healthchecks.io style '<url>/fail', keeping any query string (?create=1 etc.)."""
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path.rstrip("/") + "/fail", p.query, p.fragment))


def alert_key(kind: str, node_id: Optional[str] = None, subject: Optional[str] = None) -> str:
    return f"{kind}:{node_id or '-'}" + (f":{subject}" if subject else "")


def default_action(kind: str, node_id: Optional[str], data: dict) -> Optional[dict]:
    job = data.get("job_id")
    if kind == "task_failed" and job:
        return {"label": "Retry", "kind": "retry_job", "target": {"job_id": job}}
    if kind == "task_finished" and job:
        return {"label": "Open task", "kind": "open_job", "target": {"job_id": job}}
    if kind in ("node_offline", "machine_down") and node_id:
        return {"label": "Wake machine", "kind": "wake_machine", "target": {"node_id": node_id}}
    if node_id:
        return {"label": "Open machine", "kind": "open_machine", "target": {"node_id": node_id}}
    return None


def hash_route(kind: str, node_id: Optional[str], data: dict) -> str:
    if data.get("job_id"):
        return f"#/task/{data['job_id']}"
    if node_id:
        return f"#/machine/{node_id}"
    return "#/today"


def overnight_since(now: float) -> float:
    """18:00 local time on the previous day."""
    d = datetime.fromtimestamp(now).astimezone() - timedelta(days=1)
    return d.replace(hour=18, minute=0, second=0, microsecond=0).timestamp()


class HttpTransport:
    """Real notifier HTTP (httpx). A fake with the same three methods replaces it in tests."""

    def __init__(self) -> None:
        self._client: Optional[httpx.Client] = None

    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(follow_redirects=False)
        return self._client

    def ntfy(self, server: str, msg: dict) -> None:
        r = self.client().post(server.rstrip("/") + "/", json=msg, timeout=10)
        if r.status_code >= 300:
            raise RuntimeError(f"ntfy answered {r.status_code}")

    def push(self, endpoint: str, headers: dict, body: bytes) -> int:
        return self.client().post(endpoint, headers=headers, content=body, timeout=10).status_code

    def get(self, url: str) -> int:
        return self.client().get(url, timeout=10).status_code


class Alerts:
    def __init__(self, ctx: Any) -> None:
        self.ctx, self.store = ctx, ctx.store
        self.transport: Any = HttpTransport()
        self.inline = False                       # True: send synchronously (tests)
        self._miss: dict[str, int] = {}           # condition key -> consecutive ticks without it
        self._sent_mem: dict[str, float] = {}     # direct ctx.notify keys without an alert row -> last sent
        self._msg_touched: dict[str, float] = {}  # alert id -> last time only its msg was rewritten (N-08)
        self._vapid_ready = False                 # leader created / found the VAPID keys (N-03)
        self._last_housekeep: Optional[float] = None
        self._deadman_next: Optional[float] = None
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------ config / prefs
    def cfg_s(self, name: str, default: float) -> float:
        try:
            return float(self.ctx.cfg.get(name, default))
        except (TypeError, ValueError):
            return default

    def pref(self, key: str) -> dict:
        r = self.store.q("SELECT value FROM prefs WHERE key=?", (key,))
        cur = jloads(r[0]["value"], {}) if r else {}
        base = dict(DEFAULT_PREFS.get(key, {}))
        return base | (cur if isinstance(cur, dict) else {})

    def set_pref(self, key: str, value: dict) -> None:
        now = self.ctx.clock()
        with self.store.lock:
            if self.store.q("SELECT 1 FROM prefs WHERE key=?", (key,)):
                self.store.touch("prefs", "key", key, "value=?", (json.dumps(value),))
            else:
                self.store.x("INSERT INTO prefs(key,value,updated) VALUES(?,?,?)", (key, json.dumps(value), now))

    def push_endpoint_ok(self, endpoint: str) -> bool:
        """https, no userinfo, not an IP literal / localhost, host under a known push service (or `push_host_allow`)."""
        if not URL_RE.match(endpoint) or not endpoint.lower().startswith("https://"):
            return False
        host = _host_of(endpoint)
        try:
            ipaddress.ip_address(host)
            return False
        except ValueError:
            pass
        if _blocked_host(host, allow_private=False):
            return False
        extra = self.ctx.cfg.get("push_host_allow") or []
        allow = PUSH_HOST_SUFFIXES + tuple(str(x).lower().strip(".") for x in (extra if isinstance(extra, list) else [])
                                           if x)
        return any(host == s or host.endswith("." + s) for s in allow)

    def public_url(self) -> Optional[str]:
        u = self.ctx.cfg.get("public_url")
        if u:
            return str(u).rstrip("/")
        lead = self.ctx.cluster.leader_id or self.ctx.node_id
        r = self.store.q("SELECT urls FROM peers WHERE id=?", (lead,))
        urls = [x for x in (jloads(r[0]["urls"], []) if r else []) if isinstance(x, str)]
        pick = next((x for x in urls if ".ts.net" in x.lower()), None)
        if not pick:
            pick = next((x for x in urls if "127.0.0.1" not in x and "localhost" not in x.lower()), None)
        return pick.rstrip("/") if pick else None

    # ------------------------------------------------------------ conditions (N1)
    def raw_conditions(self, now: float) -> list[dict]:
        """The core health checks (moved here from app.health_report, hook A-N1) + ctx.alert_sources."""
        from forge_bundle import parse_version
        ctx, alerts = self.ctx, []
        mine = ctx.releases.running_version
        lease = ctx.lease.current(now) if ctx.lease is not None else None
        holder = lease["node_id"] if lease else None
        for r in self.store.q("SELECT * FROM nodes_live ORDER BY id"):
            m, info = jloads(r["metrics"], {}), jloads(r["info"], {})
            online = now - (r["last_seen"] or 0) < ONLINE_SECS
            ver = m.get("bundle_version") or info.get("bundle_version")
            wd = m.get("watchdog") if isinstance(m.get("watchdog"), dict) else None
            wd_run = _parse_ts(wd.get("last_run")) if wd else None
            if not online:
                alerts.append({"level": "error", "kind": "node_offline", "node_id": r["id"],
                               "msg": f"{r['id']} is offline", "self_healing": holder == r["id"],
                               "since": r["last_seen"]})
                continue
            pct = _disk_pct(m)
            if pct is not None and pct >= DISK_ALERT_PCT:
                alerts.append({"level": "warn", "kind": "disk_full", "node_id": r["id"],
                               "msg": f"{r['id']} disk at {pct}%"})
            if ver and parse_version(ver) < parse_version(mine):
                alerts.append({"level": "warn", "kind": "version_behind", "node_id": r["id"],
                               "msg": f"{r['id']} runs {ver}, leader runs {mine}", "self_healing": holder is not None})
            skew = m.get("clock_skew_s")
            if isinstance(skew, (int, float)) and abs(skew) > CLOCK_SKEW_ALERT_S:
                alerts.append({"level": "warn", "kind": "clock_skew", "node_id": r["id"],
                               "msg": f"{r['id']} clock is {abs(skew):.0f}s {'ahead of' if skew > 0 else 'behind'} the hub"})
            if wd is not None and (wd_run is None or now - wd_run > WATCHDOG_STALE_SECS):
                alerts.append({"level": "warn", "kind": "watchdog_stale", "node_id": r["id"],
                               "msg": f"{r['id']} watchdog has not run for "
                                      f"{'a long time' if wd_run is None else str(int(now - wd_run)) + 's'}"})
        block = ctx.cluster.cluster_block(ctx.node_id)
        for p in block["peers"]:
            if not p["healthy"]:
                alerts.append({"level": "error", "kind": "hub_down", "node_id": p["id"], "msg": f"hub {p['id']} is unreachable"})
            elif p["version"] and parse_version(p["version"]) < parse_version(mine):
                alerts.append({"level": "warn", "kind": "version_behind", "node_id": p["id"], "subject": "hub",
                               "msg": f"hub {p['id']} runs {p['version']}, leader runs {mine}",
                               "self_healing": holder is not None})
        for v in (ctx.lease.bad_versions() if ctx.lease is not None else []):
            alerts.append({"level": "error", "kind": "fleet_bad_version", "node_id": None, "subject": v,
                           "msg": f"version {v} is marked fleet-bad (a canary rolled it back); watchdogs will not install it"})
        for extra in ctx.run_hooks(ctx.alert_sources, now):     # Phase 3 alert sources (S job_token, R db, F ...)
            alerts.extend(a for a in (extra or []) if isinstance(a, dict))
        for a in alerts:
            a["key"] = str(a.get("key") or alert_key(a.get("kind") or "?", a.get("node_id"), a.get("subject")))
            a["self_healing"] = bool(a.get("self_healing")) or a.get("kind") in SELF_HEALING_KINDS
        return alerts

    def open_rows(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in self.store.q("SELECT * FROM alerts WHERE resolved IS NULL ORDER BY opened"):
            out.setdefault(r["key"], dict(r))
        return out

    def view(self, row: dict) -> dict:
        data = jloads(row.get("data"), {})
        data = data if isinstance(data, dict) else {}
        action = data.get("action") if "action" in data else default_action(row["kind"], row["node_id"], data)
        return {"id": row["id"], "key": row["key"], "kind": row["kind"], "node_id": row["node_id"], "level": row["level"],
                "msg": row["msg"], "opened": row["opened"], "resolved": row["resolved"], "resolved_by": row["resolved_by"],
                "self_healing": bool(row["self_healing"]), "last_notified": row["last_notified"],
                "data": {k: v for k, v in data.items() if k != "action"}, "action": action}

    def current_conditions(self, now: float) -> list[dict]:
        """GET /api/health alerts[]: the 2.2 entries + id/opened/self_healing/action, then open raised alerts."""
        conds, rows = self.raw_conditions(now), self.open_rows()
        seen = set()
        for a in conds:
            row = rows.get(a["key"])
            seen.add(a["key"])
            a["id"] = row["id"] if row else None
            a["opened"] = row["opened"] if row else None
            if row:
                a["self_healing"] = bool(row["self_healing"]) or a["self_healing"]
            a.setdefault("action", self.view(row)["action"] if row else default_action(a["kind"], a.get("node_id"), a))
        for key, row in rows.items():
            if key not in seen and jloads(row["data"], {}).get("source") == "raised":
                v = self.view(row)
                conds.append({"level": v["level"], "kind": v["kind"], "node_id": v["node_id"], "msg": v["msg"],
                              "key": key, "id": v["id"], "opened": v["opened"], "self_healing": v["self_healing"],
                              "action": v["action"]})
        return conds

    def _insert(self, kind: str, key: str, node_id: Optional[str], level: str, msg: str, self_healing: bool,
                data: dict, now: float) -> str:
        aid = new_id(self.ctx.clock)
        self.store.x("INSERT INTO alerts(id,key,kind,node_id,level,msg,opened,self_healing,notify_count,data,updated) "
                     "VALUES(?,?,?,?,?,?,?,?,0,?,?)",
                     (aid, key, kind, node_id, level if level in LEVELS else "warn", str(msg)[:500], now,
                      int(bool(self_healing)), json.dumps(data), now))
        self._msg_touched[aid] = now
        return aid

    def _recently_acked(self, key: str, now: float) -> bool:
        return bool(self.store.q("SELECT 1 FROM alerts WHERE key=? AND resolved_by='user' AND resolved > ?",
                                 (key, now - ACK_REOPEN_S)))

    def _resolve_row(self, aid: str, by: str, now: float) -> bool:
        return bool(self.store.touch("alerts", "id", aid, "resolved=?, resolved_by=?", (now, by), "resolved IS NULL"))

    def sync_conditions(self, now: float) -> None:
        """One engine round (settled leader): open / update / auto-resolve condition alerts."""
        conds, rows = self.raw_conditions(now), self.open_rows()
        present = set()
        with self._lock:
            for a in conds:
                key = a["key"]
                if key in present:
                    continue
                present.add(key)
                self._miss.pop(key, None)
                row = rows.get(key)
                level = a.get("level") if a.get("level") in LEVELS else "warn"
                if row:
                    msg = str(a.get("msg") or "")[:500]
                    big = (row["level"], bool(row["self_healing"])) != (level, a["self_healing"])
                    small = (row["msg"] or "") != msg and now - self._msg_touched.get(row["id"], -1e18) >= MSG_TOUCH_MIN_S
                    if big or small:       # a msg that only embeds a changing number is rewritten at most every 5 min
                        self._msg_touched[row["id"]] = now
                        self.store.touch("alerts", "id", row["id"], "level=?, msg=?, self_healing=?",
                                         (level, msg, int(a["self_healing"])))
                    continue
                if self._recently_acked(key, now):
                    continue
                data = {"source": "condition"}
                if isinstance(a.get("data"), dict):
                    data.update(a["data"])
                if "action" in a:
                    data["action"] = a["action"]
                self._insert(a.get("kind") or "?", key, a.get("node_id"), level, a.get("msg") or "", a["self_healing"],
                             data, now)
            for key, row in rows.items():
                data = jloads(row["data"], {})
                if data.get("source") == "raised":
                    exp = data.get("expires") or ((row["opened"] or now) + RAISED_MAX_AGE_S)
                    if now >= exp:
                        self._resolve_row(row["id"], "auto", now)
                    continue
                if key in present:
                    continue
                self._miss[key] = self._miss.get(key, 0) + 1
                if self._miss[key] >= MISS_TICKS:
                    self._miss.pop(key, None)
                    self._resolve_row(row["id"], "auto", now)

    # ------------------------------------------------------------ slots for other teams
    def raise_alert(self, kind: str, *, key: Optional[str] = None, node_id: Optional[str] = None, level: str = "warn",
                    msg: str = "", self_healing: bool = False, data: Optional[dict] = None,
                    action: Optional[dict] = None, _expires: Optional[float] = None) -> Optional[str]:
        if not self.ctx.is_leader:
            return None
        now = self.ctx.clock()
        key = key or alert_key(kind, node_id)
        d = {"source": "raised"} | (data if isinstance(data, dict) else {})
        if action is not None:
            d["action"] = action
        if _expires is not None:
            d["expires"] = _expires
        level = level if level in LEVELS else "warn"
        with self._lock:
            row = self.open_rows().get(key)
            if row:
                old = jloads(row["data"], {})
                merged = old | {k: v for k, v in d.items() if k != "notify_state"}
                if (row["level"], row["msg"], bool(row["self_healing"]), old) != \
                        (level, str(msg)[:500], bool(self_healing), merged):
                    self.store.touch("alerts", "id", row["id"], "level=?, msg=?, self_healing=?, data=?",
                                     (level, str(msg)[:500], int(bool(self_healing)), json.dumps(merged)))
                aid = row["id"]
            elif self._recently_acked(key, now):
                return None            # the user acked this key < 1 h ago; a still-raised alert reopens after that
            else:
                aid = self._insert(kind, key, node_id, level, msg, self_healing, d, now)
        r = self.store.q("SELECT * FROM alerts WHERE id=?", (aid,))
        if r:
            self.consider(dict(r[0]), now)
        return aid

    def resolve_alert(self, key: str, *, by: str = "auto") -> bool:
        if not self.ctx.is_leader:
            return False
        now = self.ctx.clock()
        by = by if by in ("auto", "user") else "auto"
        n = 0
        for r in self.store.q("SELECT id FROM alerts WHERE key=? AND resolved IS NULL", (key,)):
            n += self._resolve_row(r["id"], by, now)
        return n > 0

    def notify(self, kind: str, title: str, body: str, *, node_id: Optional[str] = None, job_id: Optional[str] = None,
               url: Optional[str] = None) -> None:
        """Direct notification (R rollback, F upkeep failures): rule-filtered and deduplicated per key."""
        if not self.ctx.is_leader:
            return
        rule = RULE_FOR_KIND.get(kind)
        if not rule or not self.pref("notify.rules").get(rule, True):
            return
        now = self.ctx.clock()
        key = alert_key(kind, node_id, job_id)
        with self._lock:
            if self._notified_since(key, now - NOTIFY_DEDUP_S) or self._sent_mem.get(key, -1e18) > now - NOTIFY_DEDUP_S:
                return
            if not self.channels():
                return
            self._sent_mem[key] = now
            for r in self.store.q("SELECT id FROM alerts WHERE key=? AND resolved IS NULL", (key,)):
                self._stamp_sent(dict(r), now, {})
        self.dispatch({"title": title, "body": body, "url": url or hash_route(kind, node_id, {"job_id": job_id}),
                       "tag": key, "level": "error"})

    # ------------------------------------------------------------ notification decisions (N2)
    def _notified_since(self, key: str, since: float) -> bool:
        return bool(self.store.q("SELECT 1 FROM alerts WHERE key=? AND last_notified > ?", (key, since)))

    def _stamp(self, row: dict, data: dict, state: str) -> None:
        data = dict(data, notify_state=state)
        self.store.touch("alerts", "id", row["id"], "data=?", (json.dumps(data),))

    def _stamp_sent(self, row: dict, now: float, data: dict) -> None:
        cur = jloads(self.store.q("SELECT data FROM alerts WHERE id=?", (row["id"],))[0]["data"], {})
        self.store.touch("alerts", "id", row["id"], "last_notified=?, notify_count=COALESCE(notify_count,0)+1, data=?",
                         (now, json.dumps(cur | data | {"notify_state": "sent"})))

    def channels(self) -> list[str]:
        out = []
        n = self.pref("notify.ntfy")
        if n.get("enabled") and n.get("topic"):
            out.append("ntfy")
        if self.pref("notify.webpush").get("enabled") and wp.available() and \
                self.store.q("SELECT 1 FROM push_subs LIMIT 1"):
            out.append("webpush")
        return out

    def consider(self, row: dict, now: float) -> Optional[str]:
        """Decide once whether an open alert notifies. -> 'sent' | 'suppressed:<why>' | None (not yet / never)."""
        if row.get("resolved") is not None:
            return None
        data = jloads(row.get("data"), {})
        if data.get("notify_state") and data["notify_state"] != DEDUP_STATE:
            return None                # final decision already made (sent / rule / no_channel)
        rule = RULE_FOR_KIND.get(row["kind"])
        if not rule:
            return None
        delay = 0.0
        if rule == "machine_down":
            delay = self.cfg_s("machine_down_notify_s", 600)
        if row["self_healing"]:
            delay = max(delay, self.cfg_s("alert_selfheal_grace_s", 900))
        if now - (row["opened"] or now) < delay:
            return None
        with self._lock:
            if not self.pref("notify.rules").get(rule, True):
                state = "suppressed:rule"
            elif self._notified_since(row["key"], now - NOTIFY_DEDUP_S):
                # not final: re-evaluated each tick and sent once the 6 h window has passed while still open
                if data.get("notify_state") != DEDUP_STATE:
                    self._stamp(row, data, DEDUP_STATE)
                return DEDUP_STATE
            elif not self.channels():
                state = "suppressed:no_channel"
            else:
                state = "sent"
            if state != "sent":
                self._stamp(row, data, state)
                return state
            self._stamp_sent(row, now, {})
        self.dispatch(self.message(row, data, now))
        return "sent"

    def message(self, row: dict, data: dict, now: float) -> dict:
        kind, node = row["kind"], row["node_id"]
        if kind == "task_failed":
            title = f"Task failed on {node}"
        elif kind == "task_finished":
            title = f"Task finished on {node}"
        elif kind in ("node_offline", "machine_down"):
            title = f"{node} is down"
        elif kind == "rolled_back":
            title = f"Update rolled back on {node}" if node else "Update rolled back"
        elif kind == "upkeep_failed":
            title = f"Upkeep failed on {node}" if node else "Upkeep failed"
        elif kind == "fleet_bad_version":
            title = "Update marked bad"
        else:
            title = f"Hivemind: {kind}"
        body = row["msg"] or ""
        if kind in ("node_offline", "machine_down"):
            mins = int((now - (data.get("since") or row["opened"] or now)) // 60)
            body = f"{node} has been offline for {max(mins, 1)} min and is not recovering"
        return {"title": title, "body": body, "url": hash_route(kind, node, data), "tag": row["key"],
                "level": row["level"]}

    def consider_all(self, now: float) -> None:
        for row in self.open_rows().values():
            if RULE_FOR_KIND.get(row["kind"]):
                self.consider(row, now)

    # ------------------------------------------------------------ delivery
    def dispatch(self, msg: dict) -> None:
        if self.inline:
            self.deliver(msg)
            return
        self._q.put(("msg", msg))
        self._ensure_worker()

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._work, name="forge-notify", daemon=True)
                self._worker.start()

    def _work(self) -> None:
        while True:
            kind, arg = self._q.get()
            try:
                if kind == "msg":
                    self.deliver(arg)
                elif kind == "deadman":
                    self.deadman_ping(arg)
            except Exception:
                print("[forge_alerts] notifier failed:\n" + traceback.format_exc(), file=sys.stderr)

    def deliver(self, msg: dict, channel: str = "all") -> dict:
        """Send one message to every enabled channel. -> {ntfy?: "ok"|"<error>", webpush?: {sent, failed}}."""
        results: dict = {}
        n = self.pref("notify.ntfy")
        if channel in ("all", "ntfy") and n.get("topic") and (n.get("enabled") or channel == "ntfy" or msg.get("test")):
            results["ntfy"] = self.send_ntfy(n, msg)
        if channel in ("all", "webpush") and (self.pref("notify.webpush").get("enabled") or msg.get("test")):
            # "all" reports only a configured channel: Web Push needs the package and at least one device (N-07)
            if channel == "webpush" or (wp.available() and self.store.q("SELECT 1 FROM push_subs LIMIT 1")):
                results["webpush"] = self.send_push(msg)
        return results

    def send_ntfy(self, n: dict, msg: dict) -> str:
        base = self.public_url()
        body = {"topic": n["topic"], "title": msg.get("title") or "Hivemind", "message": msg.get("body") or "",
                "tags": ["rotating_light"] if msg.get("level") == "error" else ["bell"],
                "priority": 4 if msg.get("level") == "error" else 3}
        if base and msg.get("url"):
            body["click"] = f"{base}/{msg['url']}"
        try:
            self.transport.ntfy(n.get("server") or "https://ntfy.sh", body)
            return "ok"
        except Exception as e:   # transport errors are reported, never raised into the tick
            return f"{type(e).__name__}: {e}"[:200]

    def send_push(self, msg: dict) -> dict:
        subs = [dict(r) for r in self.store.q("SELECT * FROM push_subs ORDER BY created")]
        out = {"sent": 0, "failed": 0}
        if not subs or not wp.available():
            return out                 # no package on this hub: never count that against the devices (N-01)
        try:
            priv, _ = self.vapid_keys()
        except wp.WebPushError:
            return {"sent": 0, "failed": len(subs)}
        payload = {"title": msg.get("title") or "Hivemind", "body": msg.get("body") or "",
                   "url": msg.get("url") or "#/today", "tag": msg.get("tag") or "hivemind"}
        subject = str(self.ctx.cfg.get("vapid_subject") or "mailto:hivemind@example.com")
        now = self.ctx.clock()
        for s in subs:
            try:
                endpoint, headers, body = wp.build_request(s, payload, priv, subject, now=now, topic=payload["tag"],
                                                           urgency="high" if msg.get("level") == "error" else "normal")
                code = int(self.transport.push(endpoint, headers, body))
            except Exception as e:     # local build error, offline hub, timeout: not the device's fault
                print(f"[forge_alerts] push to {s['id']} failed: {e}", file=sys.stderr)
                out["failed"] += 1
                continue
            if 200 <= code < 300:
                out["sent"] += 1
                self.store.touch("push_subs", "id", s["id"], "last_ok=?, fails=0", (now,))
                continue
            out["failed"] += 1
            if code in PUSH_GONE:
                self.store.delete_row("push_subs", s["id"])           # expired / unsubscribed in the browser
            elif code in PUSH_REJECT:
                fails = (s["fails"] or 0) + 1
                quiet_since = s["last_ok"] or s["created"] or now
                if fails >= PUSH_MAX_FAILS and now - quiet_since > PUSH_FAIL_MIN_AGE_S:
                    self.store.delete_row("push_subs", s["id"])       # rejected for a day and 20 sends in a row
                else:
                    self.store.touch("push_subs", "id", s["id"], "fails=?", (fails,))
            # 429 / 5xx / anything else: the push service is having a moment, nothing is counted
        return out

    def vapid_keys(self) -> tuple[str, str]:
        """(private, public) base64url. Generated by the leader on first need; replicated through `secrets`."""
        rows = {r["name"]: r["value"] for r in self.store.q(
            "SELECT name,value FROM secrets WHERE name IN ('vapid_private_key','vapid_public_key')")}
        if rows.get("vapid_private_key") and rows.get("vapid_public_key"):
            return rows["vapid_private_key"], rows["vapid_public_key"]
        if not self.ctx.is_leader:
            raise wp.WebPushError("VAPID keys are created by the leader")
        priv, pub = wp.generate_vapid_keys()
        now = self.ctx.clock()
        with self.store.tx():
            for name, val in (("vapid_private_key", priv), ("vapid_public_key", pub)):
                self.store.x("INSERT OR REPLACE INTO secrets(name,value,prev_value,rotated,updated) VALUES(?,?,NULL,?,?)",
                             (name, val, now, now))
        self.ctx.event(None, "info", "Web Push keys created")
        return priv, pub

    # ------------------------------------------------------------ dead-man switch (N3)
    def deadman_tick(self, now: float) -> None:
        d = self.pref("notify.deadman")
        if not d.get("enabled") or not d.get("url"):
            self._deadman_next = None
            return
        if self._deadman_next is not None and now < self._deadman_next:
            return
        self._deadman_next = now + self.cfg_s("deadman_every_s", 300)
        marks = ",".join("?" * len(DEADMAN_FAIL_KINDS))
        fail = bool(d.get("send_fail")) and bool(self.store.q(
            f"SELECT 1 FROM alerts WHERE resolved IS NULL AND level='error' AND kind IN ({marks}) LIMIT 1",
            DEADMAN_FAIL_KINDS))
        url = fail_url(d["url"]) if fail else d["url"]
        if self.inline:
            self.deadman_ping((url, now))
        else:
            self._q.put(("deadman", (url, now)))
            self._ensure_worker()

    def deadman_ping(self, arg: tuple) -> None:
        url, now = arg
        state = self.pref("notify.deadman_state")
        state["last_ping"] = now
        try:
            code = int(self.transport.get(url))
            ok = 200 <= code < 300
            state["last_error"] = None if ok else f"HTTP {code}"
        except Exception as e:         # the error text may quote the URL: never store its secret path
            err = f"{type(e).__name__}: {e}"
            cands = {url, url.rstrip("/"), str(self.pref("notify.deadman").get("url") or "") or url}
            for u in sorted(cands, key=len, reverse=True):
                err = err.replace(u, mask_url(u) or "")
            ok, state["last_error"] = False, err[:200]
        if ok:
            state["last_ok"] = now
        self.set_pref("notify.deadman_state", state)

    # ------------------------------------------------------------ hooks
    def on_tick(self, now: float, settled: bool) -> None:
        if not settled:
            self._miss.clear()
            self._deadman_next = None
            return
        self.sync_conditions(now)
        self.consider_all(now)
        self.deadman_tick(now)
        self.ensure_vapid_keys()
        if self._last_housekeep is None or now - self._last_housekeep >= HOUSEKEEP_EVERY_S:
            self._last_housekeep = now
            for r in self.store.q("SELECT id FROM alerts WHERE resolved IS NOT NULL AND resolved < ?",
                                  (now - RESOLVED_KEEP_S,)):
                self.store.delete_row("alerts", r["id"])
                self._msg_touched.pop(r["id"], None)
            self.prune_push_subs(now)

    def ensure_vapid_keys(self) -> None:
        """Leader: create the VAPID keys up front so they replicate before a UI on a standby asks for them (N-03)."""
        if self._vapid_ready or not wp.available() or not self.pref("notify.webpush").get("enabled"):
            return
        try:
            self.vapid_keys()
            self._vapid_ready = True
        except Exception:
            print("[forge_alerts] VAPID key creation failed:\n" + traceback.format_exc(), file=sys.stderr)

    def prune_push_subs(self, now: float) -> None:
        """Drop subscriptions whose UI session was revoked, expired or deleted (N-05). A session row that is merely
        missing is given an hour, since the subscribe may have reached a hub before the session row replicated."""
        if self.ctx.ui_auth is None:
            return                     # no S sessions on this hub (2.2 ui_token): nothing to compare against
        rows = self.store.q("SELECT p.id, p.created, p.session_id, s.id AS sid, s.revoked, s.expires FROM push_subs p "
                            "LEFT JOIN ui_sessions s ON s.id = p.session_id WHERE p.session_id IS NOT NULL")
        for r in rows:
            if r["sid"] is None:       # only a real S session id (sha256 hex) can be "missing"; not a legacy sentinel
                dead = bool(SESSION_ID_RE.match(str(r["session_id"]))) and now - (r["created"] or now) > 3600
            else:
                dead = bool(r["revoked"]) or (r["expires"] is not None and r["expires"] < now)
            if dead:
                self.store.delete_row("push_subs", r["id"])

    def on_session_revoked(self, session_id: str) -> int:
        """Revoke hook for S (not wired by S yet; housekeeping covers it within an hour): drop that session's devices."""
        n = 0
        for r in self.store.q("SELECT id FROM push_subs WHERE session_id=?", (session_id,)):
            self.store.delete_row("push_subs", r["id"])
            n += 1
        return n

    def on_job_finished(self, job: dict, status: str, output: str, now: float) -> None:
        if not self.ctx.is_leader:
            return
        jid, node = job.get("id"), job.get("node_id")
        prompt = redact(job.get("prompt") or "", 80).replace("\n", " ")
        data = {"job_id": jid, "engine": job.get("engine"), "project_id": job.get("project_id")}
        if status == "failed" and (job.get("run_as") or "user") != "system" \
                and (job.get("source") or "") not in ("brief", "sync"):
            data["excerpt"] = redact((output or "")[-400:])
            self.raise_alert("task_failed", key=alert_key("task_failed", node, jid), node_id=node, level="error",
                             msg=f"Task failed on {node}: {prompt}", data=data, _expires=now + TASK_ALERT_TTL_S)
        elif status == "done" and job.get("notify"):
            self.raise_alert("task_finished", key=alert_key("task_finished", node, jid), node_id=node, level="info",
                             msg=f"Task finished on {node}: {prompt}", data=data, _expires=now + TASK_ALERT_TTL_S)

    # ------------------------------------------------------------ morning brief (N4)
    def overnight(self, now: float) -> dict:
        """Up to OVERNIGHT_MAX of each, preferring open / failed and then the most recent; shown oldest first."""
        since = overnight_since(now)
        a_where = "WHERE opened >= ? AND kind <> 'task_finished'"
        j_where = ("WHERE finished >= ? AND status IN ('done','failed') AND COALESCE(source,'') NOT IN ('brief','sync') "
                   "AND COALESCE(run_as,'user') <> 'system'")
        alerts = [{"kind": r["kind"], "node_id": r["node_id"], "msg": r["msg"], "opened": r["opened"],
                   "resolved": r["resolved"]} for r in self.store.q(
            f"SELECT kind,node_id,msg,opened,resolved FROM alerts {a_where} "
            "ORDER BY (resolved IS NULL) DESC, (level='error') DESC, opened DESC LIMIT ?", (since, OVERNIGHT_MAX))]
        finished = [{"id": r["id"], "prompt": (r["prompt"] or "")[:80], "status": r["status"], "node_id": r["node_id"],
                     "finished": r["finished"]} for r in self.store.q(
            f"SELECT id,prompt,status,node_id,finished FROM jobs {j_where} "
            "ORDER BY (status='failed') DESC, finished DESC LIMIT ?", (since, OVERNIGHT_MAX))]
        alerts.sort(key=lambda a: a["opened"] or 0)
        finished.sort(key=lambda j: j["finished"] or 0)
        n_a = self.store.q(f"SELECT COUNT(*) n FROM alerts {a_where}", (since,))[0]["n"]
        n_j = self.store.q(f"SELECT COUNT(*) n FROM jobs {j_where}", (since,))[0]["n"]
        return {"alerts": alerts, "finished": finished,
                "truncated": {"alerts": max(0, n_a - len(alerts)), "finished": max(0, n_j - len(finished))}}

    def digest_text(self, now: float) -> str:
        o = self.overnight(now)
        if not o["alerts"] and not o["finished"]:
            return "Overnight (since 18:00 yesterday): no alerts and no finished tasks."

        def hhmm(t: Optional[float]) -> str:
            return datetime.fromtimestamp(t).astimezone().strftime("%H:%M") if t else "?"
        lines = ["Overnight (since 18:00 yesterday). Add one short 'Overnight' line or bullet that mentions anything "
                 "that failed or is still open:"]
        for a in o["alerts"]:
            state = f"resolved {hhmm(a['resolved'])}" if a["resolved"] else "still open"
            lines.append(f"- alert {a['kind']} on {a['node_id'] or 'fleet'} at {hhmm(a['opened'])} ({state}): "
                         f"{redact(a['msg'], 160)}")
        for j in o["finished"]:
            lines.append(f"- task {j['status']} on {j['node_id']} at {hhmm(j['finished'])}: {redact(j['prompt'], 80)}")
        more = o["truncated"]["alerts"] + o["truncated"]["finished"]
        if more:
            lines.append(f"- ({more} older or resolved items not listed)")
        return "\n".join(lines)

    # ------------------------------------------------------------ settings (N2/N3)
    def settings_view(self) -> dict:
        n, w, d, rules = (self.pref("notify.ntfy"), self.pref("notify.webpush"), self.pref("notify.deadman"),
                          self.pref("notify.rules"))
        st = self.pref("notify.deadman_state")
        pub = None
        if wp.available():
            try:
                pub = self.vapid_keys()[1]
            except wp.WebPushError:
                pub = None
        server = (n.get("server") or "https://ntfy.sh").rstrip("/")
        return {"ntfy": {"enabled": bool(n.get("enabled")), "server": server, "topic": n.get("topic"),
                         "subscribe_url": f"{server}/{n['topic']}" if n.get("topic") else None},
                "webpush": {"enabled": bool(w.get("enabled")), "vapid_public_key": pub, "available": wp.available(),
                            "devices": self.store.q("SELECT COUNT(*) n FROM push_subs")[0]["n"]},
                "deadman": {"enabled": bool(d.get("enabled")), "url": mask_url(d.get("url")),
                            "send_fail": bool(d.get("send_fail")),
                            "last_ping": st.get("last_ping"), "last_ok": st.get("last_ok"),
                            "last_error": st.get("last_error")},
                "rules": {k: bool(rules.get(k, True)) for k in RULE_KEYS}}

    def update_settings(self, body: dict) -> dict:
        def section(name: str) -> dict:
            v = body.get(name)
            if v is None:
                return {}
            if not isinstance(v, dict):
                raise HTTPException(400, f"{name} must be an object")
            return v

        def flag(sec: dict, k: str, cur: dict, name: str) -> None:
            if k in sec:
                if not isinstance(sec[k], bool):
                    raise HTTPException(400, f"{name}.{k} must be true or false")
                cur[k] = sec[k]

        n, w, d, rules = (self.pref("notify.ntfy"), self.pref("notify.webpush"), self.pref("notify.deadman"),
                          self.pref("notify.rules"))
        sn, sw, sd, sr = section("ntfy"), section("webpush"), section("deadman"), section("rules")
        flag(sn, "enabled", n, "ntfy")
        if "server" in sn:
            s = str(sn["server"] or "").strip()
            if not outbound_url_ok(s) or len(s) > 200:
                raise HTTPException(400, "ntfy.server must be an http(s) URL without user:password@, "
                                         "not a loopback or link-local address")
            n["server"] = s.rstrip("/")
        if "topic" in sn:
            t = sn["topic"]
            if t in (None, ""):
                n["topic"] = None
            elif not isinstance(t, str) or not TOPIC_RE.match(t):
                raise HTTPException(400, "ntfy.topic must be 8-64 letters, digits, '-' or '_'")
            else:
                n["topic"] = t
        if n.get("enabled") and not n.get("topic"):
            raise HTTPException(400, "ntfy needs a topic before it can be enabled")
        flag(sw, "enabled", w, "webpush")
        flag(sd, "enabled", d, "deadman")
        flag(sd, "send_fail", d, "deadman")
        if "url" in sd:
            u = sd["url"]
            if u in (None, ""):
                d["url"] = None
            elif isinstance(u, str) and d.get("url") and u.strip() == mask_url(d["url"]):
                pass                   # the UI sent back the masked value it was shown: unchanged
            elif not isinstance(u, str) or not outbound_url_ok(u.strip()) or len(u) > 500:
                raise HTTPException(400, "deadman.url must be an http(s) URL without user:password@, "
                                         "not a loopback or link-local address")
            else:
                d["url"] = u.strip()
        if d.get("enabled") and not d.get("url"):
            raise HTTPException(400, "the dead-man switch needs a URL before it can be enabled")
        for k in sr:
            if k not in RULE_KEYS:
                raise HTTPException(400, f"unknown rule {k!r}")
        for k in RULE_KEYS:
            flag(sr, k, rules, "rules")
        for key, val in (("notify.ntfy", n), ("notify.webpush", w), ("notify.deadman", d), ("notify.rules", rules)):
            if val != self.pref(key) or not self.store.q("SELECT 1 FROM prefs WHERE key=?", (key,)):
                self.set_pref(key, val)
        self._deadman_next = None              # a changed URL is pinged on the next tick
        return self.settings_view()


def new_topic() -> str:
    return "hm-" + base64.b32encode(secrets.token_bytes(15)).decode("ascii").lower()[:20]


def current_conditions(ctx: Any, now: float) -> list[dict]:
    """Hook A-N1: the alert list of GET /api/health (2.2 shape + id, opened, self_healing, action, key)."""
    eng = getattr(ctx.app.state, "alerts", None)
    if eng is None:
        eng = Alerts(ctx)
        return eng.raw_conditions(now)
    return eng.current_conditions(now)


def register(app: Any, ctx: Any) -> None:
    """Add this workstream's routes to `app` and its hooks to `ctx` (forge_ctx.HubContext)."""
    eng = Alerts(ctx)
    app.state.alerts = eng
    ctx.raise_alert, ctx.resolve_alert, ctx.notify = eng.raise_alert, eng.resolve_alert, eng.notify
    ctx.on_tick.append(eng.on_tick)
    ctx.on_job_finished.append(eng.on_job_finished)
    if ctx.p2 is not None and hasattr(ctx.p2, "digest_extras"):
        ctx.p2.digest_extras.append(eng.digest_text)
    ui = [Depends(ctx.require_ui)]

    @app.get("/api/alerts", dependencies=ui)
    def list_alerts(open: int = 1, limit: int = 100, since: Optional[float] = None) -> dict:
        sql, args = "SELECT * FROM alerts WHERE 1=1", []
        if open:
            sql += " AND resolved IS NULL"
        if since is not None:
            sql += " AND updated > ?"
            args.append(since)
        sql += " ORDER BY opened DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        return {"alerts": [eng.view(dict(r)) for r in ctx.store.q(sql, tuple(args))]}

    @app.post("/api/alerts/{aid}/ack", dependencies=ui)
    def ack_alert(aid: str) -> dict:
        r = ctx.store.q("SELECT id FROM alerts WHERE id=?", (aid,))
        if not r:
            raise HTTPException(404, "no such alert")
        eng._resolve_row(aid, "user", ctx.clock())
        return {"ok": True}

    @app.get("/api/notify/settings", dependencies=ui)
    def get_notify_settings() -> dict:
        return eng.settings_view()

    @app.put("/api/notify/settings", dependencies=ui)
    def put_notify_settings(body: dict = Body(...)) -> dict:
        return eng.update_settings(body)

    @app.post("/api/notify/ntfy/topic", dependencies=ui)
    def ntfy_topic() -> dict:
        t = new_topic()
        server = (eng.pref("notify.ntfy").get("server") or "https://ntfy.sh").rstrip("/")
        return {"topic": t, "subscribe_url": f"{server}/{t}"}

    @app.post("/api/notify/test", dependencies=ui)
    def notify_test(body: Optional[dict] = Body(default=None)) -> dict:
        channel = (body or {}).get("channel") or "all"
        if channel not in ("ntfy", "webpush", "all"):
            raise HTTPException(400, "channel must be ntfy, webpush or all")
        msg = {"title": "Hivemind test", "body": "Notifications from your hub work.", "url": "#/settings",
               "tag": "test", "level": "info", "test": True}
        results = eng.deliver(msg, channel)
        if channel == "ntfy" and "ntfy" not in results:
            results["ntfy"] = "no ntfy topic configured"
        if channel == "webpush" and "webpush" not in results:
            results["webpush"] = {"sent": 0, "failed": 0}
        ok = bool(results) and all((v == "ok") if isinstance(v, str) else (v["failed"] == 0 and v["sent"] > 0)
                                   for v in results.values())
        return {"ok": ok, "results": results}

    @app.post("/api/push/subscribe", dependencies=ui)
    def push_subscribe(request: Request, body: dict = Body(...)) -> dict:
        endpoint = body.get("endpoint")
        keys = body.get("keys") if isinstance(body.get("keys"), dict) else {}
        if not isinstance(endpoint, str) or len(endpoint) > 2048 or not eng.push_endpoint_ok(endpoint):
            raise HTTPException(400, "endpoint must be an https URL of a known push service")
        if not wp.available():
            raise HTTPException(503, "Web Push needs the 'cryptography' package on the hub")
        p256dh, auth = keys.get("p256dh"), keys.get("auth")
        if not isinstance(p256dh, str) or not isinstance(auth, str) or not wp.valid_subscription_keys(p256dh, auth):
            raise HTTPException(400, "keys.p256dh / keys.auth are not a valid push subscription")
        label = str(body.get("label") or request.headers.get("user-agent") or "device")[:80]
        principal = getattr(request.state, "principal", None)
        session_id = principal.get("session_id") if isinstance(principal, dict) else None
        sid, now = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:32], ctx.clock()
        try:
            eng.vapid_keys()
        except wp.WebPushError:
            raise HTTPException(503, "the leader hub has not created the push keys yet; retry in a minute")
        with ctx.store.lock:
            if ctx.store.q("SELECT 1 FROM push_subs WHERE id=?", (sid,)):
                ctx.store.touch("push_subs", "id", sid, "p256dh=?, auth=?, label=?, session_id=?, fails=0",
                                (p256dh, auth, label, session_id))
            else:
                ctx.store.x("INSERT INTO push_subs(id,endpoint,p256dh,auth,session_id,label,created,last_ok,fails,updated) "
                            "VALUES(?,?,?,?,?,?,?,NULL,0,?)", (sid, endpoint, p256dh, auth, session_id, label, now, now))
        return {"ok": True, "id": sid}

    @app.delete("/api/push/subscribe", dependencies=ui)
    def push_unsubscribe(body: dict = Body(...)) -> dict:
        endpoint = body.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            raise HTTPException(400, "endpoint required")
        sid = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:32]
        removed = bool(ctx.store.q("SELECT 1 FROM push_subs WHERE id=?", (sid,)))
        if removed:
            ctx.store.delete_row("push_subs", sid)
        return {"ok": True, "removed": removed}

    # N4: replaces the core GET /api/brief/all (same body + `overnight`; CONTRACT-phase3.md §10 amendment N-1)
    @app.get("/api/brief/all", dependencies=ui)
    def brief_all_latest() -> dict:
        r = ctx.store.q("SELECT text,ts,model,job_id FROM briefs WHERE project_id='*' ORDER BY ts DESC LIMIT 1")
        row = dict(r[0]) if r else None
        return {"brief": row, **(row or {"text": None, "ts": None, "model": None, "job_id": None}),
                "overnight": eng.overnight(ctx.clock())}
