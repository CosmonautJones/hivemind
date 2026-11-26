"""Mock hub for UI development. Serves hub/static/index.html + canned JSON shaped like the p2-hub (2.2.0) responses.

Run: python tools/mock_ui_server.py   (port MOCK_UI_PORT, default 19080; must be 19000-19099; never touches the live hub on 8787)
Toggles: POST /mock/503 and /mock/ok switch a "no leader" state; POST /mock/reset-transcript simulates a hub restart
(transcript seq numbers start again at 1, so the UI must notice the cursor going down). MOCK_DEMO=1 drops edge-case fixtures.
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("MOCK_UI_PORT", "19080"))
assert 19000 <= PORT <= 19099, "MOCK_UI_PORT must be in the test range 19000-19099"
INDEX = Path(__file__).resolve().parent.parent / "hub" / "static" / "index.html"
T0 = time.time()
LOCK = threading.Lock()
STATE = {"no_leader": False, "extra_jobs": [], "cancelled": set(), "priorities": {"forge-01": 1, "forge-02": 2, "forge-03": 3},
         "tx_reset_at": None}
OVERRIDES = {}   # project id -> {brief, name, pinned, group_name}: survives build_projects()
PROJECT_FIELDS = ("name", "pinned", "group_name")

STREAM = [
    "> claude --print 'refactor the build script'",
    "Reading build.ps1 ...",
    "Found 3 functions to extract.",
    "Writing tests/build.Tests.ps1",
    "Running Pester: 12 passed, 0 failed",
    "Updating README.md",
    "Committing changes ...",
    "Done: 4 files changed.",
]


def spark(base):
    now = time.time()
    return [{"ts": now - (60 - i) * 30, "cpu": max(0, min(100, base + (i * 7) % 23 - 10)), "mem": 40 + (i * 3) % 15} for i in range(60)]


def node(id_, host, online, cpu, mem, disk, ver, gpu=False, role_agents=(), addr=None):
    now = time.time()
    return {
        # the hub records the source address of the agent's report: 127.0.0.1 for the agent on the hub machine
        "id": id_, "online": online, "last_seen": now - (2 if online else 1800), "addr": addr or "192.168.1." + id_[-1],
        "info": {"hostname": host, "cpu": "Intel(R) Core(TM) i7-12700K CPU @ 3.60GHz", "cores": 20, "ram_gb": 32,
                 "gpus": ["RTX 3070"] if gpu else [], "os": "Windows 11 Pro", "model": "Custom", "user": "you",
                 "versions": {"claude": "2.1.0", "codex": "0.9"}, "wsl": ["Ubuntu"], "tools": ["claude", "codex"],
                 "agent_version": ver, "max_jobs": 2},
        "metrics": {"cpu": cpu, "mem_pct": mem, "mem_used_gb": round(32 * mem / 100, 1), "mem_total_gb": 32,
                    "disks": [{"name": "C:", "total_gb": 500, "free_gb": 500 - disk * 5}, {"name": "D:", "total_gb": 2000, "free_gb": 1400}],
                    "gpu": [{"name": "RTX 3070", "util": 35, "mem_used_mb": 2100, "mem_total_mb": 8192, "temp_c": 58}] if gpu else [],
                    "ips": [{"iface": "Loopback", "ip": "127.0.0.1"}, {"iface": "Ethernet", "ip": "192.168.1." + id_[-1]}],
                    "uptime_s": 86400 * 3 if online else None,
                    "battery": None, "procs_total": 212, "work_root": "D:\\forge", "work_root_free_gb": 1400,
                    "watchdog": {"last_run": now - 120, "actions": ["none needed"] if id_ != "forge-03" else ["restarted agent"]},
                    **({"services": [{"name": "t3", "ok": True, "since": now - 7200}, {"name": "ollama", "ok": id_ == "forge-01", "since": now - 3000}]} if id_ in ("forge-01", "forge-02") else {})} if online else {},
        "disk_pct": disk, "agents": [{"kind": k, "pid": 1000 + i, "started": None, "cmd": k + " --run", "job": None} for i, k in enumerate(role_agents)],
        "active_jobs": 1 if id_ == "forge-02" else 0, "capacity": 2, "spark": spark(cpu) if online else [],
        "system_lane": {"online": online and id_ != "forge-03", "last_seen": now - 5 if online else None, "capacity": 1, "version": ver},
    }


def all_jobs():
    now = time.time()
    sid = "019ac3f2b1a4f3c2d1"
    jobs = [
        {"id": sid, "node_id": "forge-02", "requested": "auto", "engine": "claude", "prompt": "Refactor the build script and add tests",
         "cwd": "D:\\forge\\repos\\app", "full_access": False, "status": "running", "created": now - 90, "started": now - 80,
         "finished": None, "exit_code": None, "tail": "", "source": "ui", "run_as": "user"},
        {"id": "019ac3e0aa11bb22cc", "node_id": "forge-01", "engine": "shell", "prompt": "Get-ChildItem D:\\forge | Measure-Object", "cwd": None,
         "full_access": True, "status": "done", "created": now - 900, "started": now - 890, "finished": now - 880, "exit_code": 0, "tail": "", "source": "ui", "run_as": "user"},
        {"id": "019ac3d0aa11bb22cd", "node_id": "forge-03", "engine": "codex", "prompt": "Explain why tests fail on CI", "cwd": None,
         "full_access": False, "status": "failed", "created": now - 4000, "started": now - 3990, "finished": now - 3900, "exit_code": 1, "tail": "", "source": "ui", "run_as": "user"},
        # an "any" job the hub could not place yet: engine stays "any"; req_engine:"any" is set on insert
        {"id": "019ac3c0aa11bb22ce", "node_id": "auto", "requested": "auto", "engine": "any", "req_engine": "any", "prompt": "Summarize open TODOs across the repo", "cwd": None,
         "full_access": False, "status": "queued", "created": now - 200, "started": None, "finished": None, "exit_code": None, "tail": "", "source": "ui", "run_as": "user",
         "wait_reason": "claude cooling down until " + time.strftime("%H:%M", time.localtime(now + 1500))},
        # a resolved "any" job: the hub rewrote engine/node and kept req_engine:"any"
        {"id": "019ac3c0aa11bb22d0", "node_id": "forge-01", "requested": "auto", "engine": "codex", "req_engine": "any", "prompt": "Tidy the changelog",
         "cwd": None, "full_access": False, "status": "done", "created": now - 1500, "started": now - 1490, "finished": now - 1400, "exit_code": 0,
         "tail": "", "source": "ui", "run_as": "user"},
        {"id": "019ac3c0aa11bb22cf", "node_id": "forge-01", "engine": "ollama", "model": "qwen2.5:14b", "prompt": "brief: hivemind", "cwd": None,
         "full_access": False, "status": "done", "created": now - 700, "started": now - 690, "finished": now - 650, "exit_code": 0, "tail": "", "source": "brief", "run_as": "user"},
    ] + STATE["extra_jobs"]
    for j in jobs:
        if j["id"] in STATE["cancelled"]:
            j.update(status="cancelled", finished=now)
    return jobs


def state():
    now = time.time()
    nodes = [
        node("forge-01", "forge-01", True, 18, 52, 40, "2.0.1", gpu=True, role_agents=("claude",), addr="127.0.0.1"),
        node("forge-02", "forge-02", True, 71, 66, 93, "2.0.1", role_agents=("claude", "claude", "codex")),
        node("forge-03", "forge-03", True, 9, 30, 55, "1.9.0"),
        node("forge-04", "closet-laptop", False, 0, 0, 60, "2.0.1"),
    ]
    jobs = [j for j in all_jobs() if j.get("source") != "brief"]     # the hub hides brief jobs from /api/state
    events = [
        {"id": "e1", "ts": now - 30, "node_id": "forge-02", "level": "warn", "msg": "disk 93% full"},
        {"id": "e2", "ts": now - 300, "node_id": "forge-04", "level": "error", "msg": "went offline"},
        {"id": "e3", "ts": now - 900, "node_id": "forge-01", "level": "ok", "msg": "job finished (exit 0)"},
        {"id": "e4", "ts": now - 2000, "node_id": None, "level": "info", "msg": "leader elected: forge-01"},
    ]
    p = STATE["priorities"]
    urls = {"forge-01": ["http://127.0.0.1:8787"], "forge-02": ["http://192.168.1.2:8787", "http://100.64.0.2:8787"],
            "forge-03": ["http://192.168.1.3:8787"]}
    peers = [{"id": k, "priority": v, "urls": urls.get(k, []), "role": "leader" if k == "forge-01" else "standby", "version": "2.0.1",
              "healthy": True, "last_ok": now - 2, "lag_s": 0 if k == "forge-01" else 0.8 + v / 10} for k, v in p.items()]
    return {"now": now, "nodes": nodes, "jobs": jobs[:40], "events": events,
            "cluster": {"leader_id": "forge-01", "viewing": "forge-02", "peers": peers}}


def health():
    now = time.time()
    s = state()
    return {"now": now, "alerts": [
        {"level": "error", "kind": "node_offline", "node_id": "forge-04", "msg": "forge-04 is offline"},
        {"level": "warn", "kind": "disk_full", "node_id": "forge-02", "msg": "forge-02 disk at 93%"},
        {"level": "warn", "kind": "version_behind", "node_id": "forge-03", "msg": "forge-03 runs 1.9.0, leader runs 2.0.1"},
        {"level": "error", "kind": "fleet_bad_version", "node_id": None,
         "msg": "version 2.2.0-rc1 is marked fleet-bad (a canary rolled it back); watchdogs will not install it"},
        {"level": "info", "kind": "bundle", "node_id": None, "msg": "bundle v2.0.1 published"}],   # older hubs only
        "nodes": [{"id": n["id"], "online": n["online"], "bundle_version": n["info"]["agent_version"], "system_lane": n["system_lane"],
                   "behind": n["id"] == "forge-03", "watchdog_last_run": now - 120, "watchdog_last_action": "none needed"} for n in s["nodes"]],
        "cluster": s["cluster"], "leader_id": "forge-01", "running_version": "2.0.1",
        "bundle_version": "2.0.1", "bad_versions": ["2.2.0-rc1"],
        "update_lease": {"holder": "forge-03", "version": "2.0.1", "expires_in_s": 500},
        "cooldowns": COOLDOWNS,
        "rollout": [{"node_id": "forge-01", "version": "2.0.1", "status": "current"},
                    {"node_id": "forge-02", "version": "2.0.1", "status": "current"},
                    {"node_id": "forge-03", "version": "1.9.0", "status": "updating"},
                    {"node_id": "forge-04", "version": None, "status": "unreachable"}]}


def detail(nid):
    s = state()
    n = next((x for x in s["nodes"] if x["id"] == nid), None)
    if not n:
        return None
    n = dict(n)
    n["history"] = n["spark"]
    n["procs"] = [{"name": "claude.exe", "pid": 4120, "cpu": 12.5, "mem_mb": 480}, {"name": "node.exe", "pid": 5310, "cpu": 3.1, "mem_mb": 210}]
    n["logs"] = [{"name": "agent.log", "ts": time.time() - 3, "text": "agent started\nreport ok\nreport ok\n"}]
    n["jobs"] = [j for j in s["jobs"] if j["node_id"] == nid]
    n["settings"] = {"work_root": None, "max_jobs": None}
    return n


def job_detail(jid):
    j = next((dict(x) for x in all_jobs() if x["id"] == jid), None)
    if not j:
        return None
    if str(j.get("prompt", "")).startswith("t3 pair"):
        j.update(status="done", started=j["created"], finished=j["created"] + 2, exit_code=0)
        j["output"] = "Pairing URL (valid 10 minutes):\nhttp://192.168.1.%s:3773/pair#token=Xk3-9fQ2LmPz7aVbT0\n" % j["node_id"][-1]
    elif jid == "019ac3f2b1a4f3c2d1":
        k = min(len(STREAM), int((time.time() - T0) / 2) % (len(STREAM) + 4) + 1)
        j["output"] = "\n".join(STREAM[:k])
    else:
        j["output"] = "output for " + jid + "\n<script>alert(1)</script>"
    return j


# ---------------------------------------------------------------- Phase 2 data
SESS = {}   # (tool, id) -> dict; replaced (never cleared in place) by build_projects()
PROJ = []
BRIEFS_ALL = {"text": "- hivemind: 3 live sessions, Phase 2 UI in progress.\n- nova: waiting on review.\n- Suggested next step: merge p2-ui.",
              "ts": T0, "model": "haiku", "job_id": "019ac3ff00brief"}
COOLDOWNS = {"claude": {"until": T0 + 1500 + 3600, "reason": "5-hour limit reached"}}


def _s(store, tool, sid, node, cwd, title, state, last, msgs, **kw):
    d = {"tool": tool, "session_id": sid, "node_id": node, "cwd": cwd, "title": title, "state": state,
         "last_activity": last, "started": last - 3600, "messages": msgs, "git_branch": "main", "model": "claude-sonnet-5", "pid": None}
    d.update(kw)
    store[(tool, sid)] = d
    return d


def build_projects():
    """Rebuild the fixtures with fresh timestamps into NEW objects, then swap them in under LOCK (no half-built state)."""
    global SESS
    now = time.time()
    ss = {}
    fc = [
        _s(ss, "claude", "live-term-1", "forge-01", "D:\\forge\\repos\\hivemind", "Build the Phase 2 UI: Projects view + live session viewer", "live", now - 4, 128, pid=4120, git_branch="p2-ui"),
        _s(ss, "claude", "live-remote-2", "forge-02", "D:\\forge\\repos\\hivemind", "Write chaos simulation tests for the scheduler", "live", now - 12, 54, remote_control=True, git_branch="p2-tests"),
        _s(ss, "codex", "codex-idle-3", "forge-01", "D:\\forge\\repos\\hivemind", "Review rollout lease ordering", "idle", now - 5400, 22, model="gpt-5-codex"),
        _s(ss, "t3", "t3-thread-4", "forge-02", "D:\\forge\\repos\\hivemind", "Chat: should leader take lease last?", "idle", now - 9000, 9),
        _s(ss, "forge", "019ac3f2b1a4f3c2d1", "forge-02", "D:\\forge\\repos\\app", "Refactor the build script and add tests", "live", now - 3, 1, status="running"),
        _s(ss, "claude", "done-5", "forge-03", "C:\\Users\\you\\hivemind", "Fix watchdog restart loop", "done", now - 86400 * 3, 310),
    ]
    tc = [
        _s(ss, "codex", "tc-1", "forge-01", "D:\\forge\\repos\\nova", "Add LRU eviction to nova", "idle", now - 7200, 41, model="gpt-5-codex", git_branch="lru"),
        _s(ss, "claude", "tc-2", "forge-03", "D:\\forge\\repos\\nova", "Benchmark cache hit ratio", "done", now - 86400 * 2, 77),
    ]
    xss = [_s(ss, "claude", "xss-1", "forge-01", "D:\\x", "<img src=x onerror=alert(1)> title", "done", now - 86400 * 5, 3)]
    sc = [_s(ss, "claude", "misc-1", "forge-01", "C:\\Users\\you", "quick question about robocopy flags", "done", now - 86400, 6)]
    commits = [{"sha": "a1b2c3d4e5f6a7b8", "author": "Hivemind contributors", "ts": now - 1800, "subject": "feat(hub): update lease endpoints"},
               {"sha": "b2c3d4e5f6a7b8c9", "author": "Hivemind contributors", "ts": now - 7200, "subject": "fix: <b>watchdog</b> restart loop"},
               {"sha": "c3d4e5f6a7b8c9d0", "author": "Claude", "ts": now - 86400, "subject": "test: rollout ordering"}]
    proj = [
        {"id": "p1", "key": "github.com/you/hivemind", "name": "hivemind", "group_name": "atlas", "pinned": True,
         "machines": ["forge-01", "forge-02", "forge-03"], "live_sessions": 3, "sessions_24h": 5, "last_activity": now - 3, "dirty_any": True, "branches": ["main", "p2-ui", "p2-tests"],
         "brief": {"text": "- Phase 2 UI is being built on branch p2-ui (live on forge-01).\n- Chaos simulation tests are running on forge-02.\n- Blocked: nothing.\n- Next: review the update-lease ordering and merge p2-ui.", "ts": T0 - 900, "model": "qwen2.5:14b"},
         "sessions": sorted(fc, key=lambda x: -x["last_activity"]), "commits": commits,
         "repos": [{"node_id": "forge-01", "cwd": "D:\\forge\\repos\\hivemind", "branch": "p2-ui", "dirty": True, "ahead": 3, "behind": 0},
                   {"node_id": "forge-02", "cwd": "D:\\forge\\repos\\hivemind", "branch": "p2-tests", "dirty": False, "ahead": 1, "behind": 2}],
         "briefs": [{"text": "- Phase 2 UI is being built.", "ts": T0 - 900, "model": "qwen2.5:14b"},
                    {"text": "- Earlier brief: scheduler work started.", "ts": T0 - 86400, "model": "qwen2.5:14b"}]},
        {"id": "p2", "key": "github.com/you/nova", "name": "nova", "group_name": "atlas", "pinned": False,
         "machines": ["forge-01", "forge-03"], "live_sessions": 0, "sessions_24h": 1, "last_activity": now - 7200, "dirty_any": False, "branches": ["main", "lru"],
         "brief": {"text": "LRU eviction is implemented; benchmarks show a 12% better hit ratio. Waiting on review.", "ts": T0 - 6000, "model": "qwen2.5:14b"},
         "sessions": tc, "commits": commits[:1], "repos": [{"node_id": "forge-01", "cwd": "D:\\forge\\repos\\nova", "branch": "lru", "dirty": False, "ahead": 2, "behind": 0}], "briefs": []},
        {"id": "p3", "key": "local:forge-01:d:\\x", "name": "<img src=x onerror=alert(1)>", "group_name": None, "pinned": False, "machines": ["forge-01"], "live_sessions": 0,
         "sessions_24h": 0, "last_activity": now - 86400 * 5, "dirty_any": False, "branches": [], "brief": None, "sessions": xss, "commits": [], "repos": [], "briefs": []},
        {"id": "p4", "key": "misc:forge-01", "name": "Scratch / misc", "group_name": "Misc", "pinned": False, "machines": ["forge-01"], "live_sessions": 0, "sessions_24h": 1,
         "last_activity": now - 86400, "dirty_any": False, "branches": [], "brief": None, "sessions": sc, "commits": [], "repos": [], "briefs": []},
        {"id": "p5", "key": "github.com/you/bare", "name": "bare-minimum-fields"},   # partial data from an older hub: almost everything missing
    ]
    if os.environ.get("MOCK_DEMO"):   # clean showcase data for README screenshots: drop the edge-case fixtures
        proj = [p for p in proj if p["id"] not in ("p3", "p5")]
    with LOCK:
        for p in proj:
            p.update(OVERRIDES.get(p["id"], {}))
        PROJ[:] = proj
        SESS = ss


build_projects()

BASE_EVENTS = [
    ("user", "text", "Build the Phase 2 UI: add a Projects tab and a live session viewer."),
    ("assistant", "thinking", "The user wants a Projects tab. I should read the spec and the current index.html first."),
    ("assistant", "text", "I'll start by reading the spec and the existing UI so I keep the Fleet, Jobs and Health tabs intact."),
    ("tool", "tool_call", "Read SPEC-phase2.md", "Read"),
    ("tool", "tool_result", "# Hivemind - Phase 2 spec\n...143 lines...", "Read"),
    ("tool", "tool_call", "Bash: git status --short", "Bash"),
    ("tool", "tool_result", " M hub/static/index.html\n?? tools/mock_ui_server.py", "Bash"),
    ("assistant", "text", "Plan:\n1. Add the Projects tab\n2. Session view with chat transcript\n3. Machine card actions\n\nExample of escaping:\n```html\n<script>alert('xss')</script>\n<div class=\"a&b\">ok</div>\n```\nThat must render as text."),
    ("user", "text", "Sounds good. Keep it simple, I use it on my phone too."),
    ("assistant", "text", "Understood: single column on mobile, big touch targets, and a prompt box that stays above the keyboard."),
    ("tool", "tool_call", "Edit hub/static/index.html (+412 -6)", "Edit"),
    ("tool", "tool_result", "The file hub/static/index.html has been updated.", "Edit"),
    ("assistant", "text", "A very_long_unbroken_token_" + "x" * 120 + " should wrap and not cause horizontal scroll."),
]
STREAM_EVENTS = [
    ("assistant", "text", "Running the tests now."),
    ("tool", "tool_call", "Bash: pytest -q tests/test_projects.py", "Bash"),
    ("tool", "tool_result", "........ [100%]\n8 passed in 0.42s", "Bash"),
    ("assistant", "text", "All green. Next I'll wire up the transcript poller."),
    ("user", "text", "Also show tool calls collapsed."),
    ("assistant", "thinking", "Tool calls should be one-line chips."),
    ("assistant", "text", "Done: tool calls render as one-line chips that expand on click."),
]


def ev_at(i):
    """Event with seq i+1 (hub seq numbers start at 1)."""
    e = BASE_EVENTS[i] if i < len(BASE_EVENTS) else STREAM_EVENTS[(i - len(BASE_EVENTS)) % len(STREAM_EVENTS)]
    d = {"seq": i + 1, "ts": time.time() - max(0, (400 - i * 3)), "role": e[0], "kind": e[1], "text": e[2], "collapsed": e[1] != "text"}
    if len(e) > 3:
        d["tool_name"] = e[3]
    return d


def transcript(tool, sid, since):
    """Same shape as the hub's Ephemeral.get: {events:[{seq,...}], cursor: last seq, done, available}."""
    s = SESS.get((tool, sid))
    if not s:
        return None
    t_from = STATE["tx_reset_at"] or T0
    total = len(BASE_EVENTS) + (int((time.time() - t_from) / 2) if s["state"] == "live" else 6)
    if STATE["tx_reset_at"] and s["state"] == "live":
        total = min(total, 3 + int((time.time() - t_from) / 2))   # after a "restart" the hub only has what was re-sent
    return {"events": [ev_at(i) for i in range(max(0, since), total)], "cursor": total, "done": s["state"] != "live", "available": True}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def body(self):
        """Parsed JSON body, or None after answering 400 (like the hub's 422) for malformed JSON."""
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            b = json.loads(raw)
        except ValueError:
            self.send(400, {"detail": "invalid json"})
            return None
        if not isinstance(b, dict):
            self.send(400, {"detail": "body must be a JSON object"})
            return None
        return b

    def query(self):
        q = self.path.split("?", 1)[1] if "?" in self.path else ""
        return dict(kv.split("=", 1) for kv in q.split("&") if "=" in kv)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            b = INDEX.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif p == "/api/state":
            self.send(503, {"detail": "no leader"}) if STATE["no_leader"] else self.send(200, state())
        elif p == "/api/health":
            self.send(200, health())
        elif p.startswith("/api/nodes/"):
            d = detail(p.split("/")[3])
            self.send(200, d) if d else self.send(404, {"detail": "no such node"})
        elif p == "/api/jobs":
            q = self.query()
            inc = q.get("include_brief", "").lower() in ("1", "true", "yes", "on")
            lim = max(1, min(int(q["limit"]) if q.get("limit", "").isdigit() else 40, 200))
            self.send(200, [j for j in all_jobs() if inc or j.get("source") != "brief"][:lim])
        elif p.startswith("/api/jobs/"):
            d = job_detail(p.split("/")[3])
            self.send(200, d) if d else self.send(404, {"detail": "no such job"})
        elif p == "/api/projects":
            build_projects()
            keys = ("id", "key", "name", "group_name", "pinned", "machines", "live_sessions", "sessions_24h", "last_activity", "brief", "dirty_any", "branches")
            with LOCK:
                self.send(200, [{k: x[k] for k in keys if k in x} for x in PROJ])
        elif p.startswith("/api/projects/"):
            build_projects()
            with LOCK:
                x = next((y for y in PROJ if y["id"] == p.split("/")[3]), None)
            self.send(200, x) if x else self.send(404, {"detail": "no such project"})
        elif p == "/api/brief/all":
            self.send(200, {"brief": BRIEFS_ALL})
        elif p == "/api/scheduler":
            self.send(200, {"cooldowns": COOLDOWNS, "queue_depth": 1, "engine_preference": ["claude", "codex", "ollama"],
                            "placement_preview": {"claude": [{"node_id": "forge-01", "score": 81.0}, {"node_id": "forge-03", "score": 76.5}],
                                                  "codex": [{"node_id": "forge-01", "score": 81.0}], "ollama": [{"node_id": "forge-01", "score": 121.0}],
                                                  "shell": [{"node_id": "forge-03", "score": 86.5}], "any": {"engine": "codex", "node_id": "forge-01"}}})
        elif p.startswith("/api/sessions/") and p.endswith("/transcript"):
            parts = p.split("/")
            s = self.query().get("since", "0")
            d = transcript(parts[3], parts[4], int(s) if s.isdigit() else 0)
            self.send(200, d) if d else self.send(404, {"detail": "no such session"})
        elif p == "/api/enroll":
            self.send(200, {"hub": "http://forge-01:8787", "command": "irm http://forge-01:8787/agent/install.ps1 | iex"})
        else:
            self.send(404, {"detail": "not found"})

    def do_POST(self):
        p = self.path.split("?")[0]
        b = self.body()
        if b is None:
            return
        if p == "/api/login":
            self.send(200, {"ok": True})
        elif p.startswith("/api/sessions/") and p.endswith("/watch"):
            s = SESS.get((p.split("/")[3], p.split("/")[4]))
            self.send(200, {"ok": True, "node_id": s["node_id"], "ttl_s": 30}) if s else self.send(404, {"detail": "no such session"})
        elif p == "/api/brief/all":
            self.send(200, {"id": "019ac3ff00brief", "projects": 2})
        elif p.startswith("/api/projects/") and p.endswith("/brief"):
            pid = p.split("/")[3]
            if not any(y["id"] == pid for y in PROJ):
                return self.send(404, {"detail": "no such project"})
            with LOCK:
                OVERRIDES.setdefault(pid, {})["brief"] = {"text": "- Fresh brief on demand.\n- Everything looks on track.", "ts": time.time(), "model": "haiku"}
            build_projects()
            self.send(200, {"id": "019ac3ff00brief", "existing": False})
        elif p == "/api/jobs":
            self.create_job(b)
        elif p.startswith("/api/jobs/") and p.endswith("/cancel"):
            STATE["cancelled"].add(p.split("/")[3])
            self.send(200, {"ok": True})
        elif p == "/mock/503":
            STATE["no_leader"] = True
            self.send(200, {})
        elif p == "/mock/ok":
            STATE["no_leader"] = False
            self.send(200, {})
        elif p == "/mock/reset-transcript":
            STATE["tx_reset_at"] = time.time()
            self.send(200, {"ok": True})
        else:
            self.send(404, {"detail": "not found"})

    def create_job(self, b):
        res = b.get("resume") if isinstance(b.get("resume"), dict) else None
        engine, node_id = b.get("engine"), b.get("node_id") or "auto"
        if res:
            s = SESS.get((res.get("tool"), res.get("session_id")))
            if not s:
                return self.send(404, {"detail": "no such session"})
            if res.get("tool") not in ("claude", "codex"):
                return self.send(400, {"detail": "only claude/codex sessions can be resumed"})
            if s["state"] == "live" and s.get("pid") and not b.get("force"):
                return self.send(409, {"detail": "session is live in a terminal"})
            engine, node_id = res["tool"], s["node_id"]
        if not engine:
            return self.send(400, {"detail": "engine required"})
        if not str(b.get("prompt") or "").strip() and engine != "action":
            return self.send(400, {"detail": "prompt required"})
        system = engine in ("action", "admin")
        req_any = engine == "any"
        if req_any:
            engine = "codex" if "claude" in COOLDOWNS else "claude"
        resolved = "forge-01" if node_id == "auto" else node_id
        jid = "019ac3ff%010x" % int(time.time() * 1000)
        row = {"id": jid, "node_id": resolved, "requested": "auto" if node_id == "auto" else None, "engine": engine,
               "req_engine": "any" if req_any else None, "prompt": b.get("prompt") or b.get("action"), "action": b.get("action"),
               "cwd": b.get("cwd"), "full_access": bool(b.get("full_access")) and not system, "status": "queued", "created": time.time(),
               "started": None, "finished": None, "exit_code": None, "tail": "", "source": b.get("source") or "ui",
               "run_as": "system" if system else "user", "resume": res, "project_id": b.get("project_id"), "wait_reason": None}
        STATE["extra_jobs"].insert(0, row)
        self.send(200, {k: row[k] for k in ("id", "node_id", "engine", "run_as", "status", "wait_reason")})

    def do_DELETE(self):
        parts = self.path.split("?")[0].split("/")
        if self.path.startswith("/api/scheduler/cooldowns/") and len(parts) == 5:
            return self.send(200, {"ok": True, "cleared": COOLDOWNS.pop(parts[4], None) is not None})
        if self.path.startswith("/api/peers/") and len(parts) == 4 and parts[3] in STATE["priorities"]:
            STATE["priorities"].pop(parts[3])
            return self.send(200, {"ok": True})
        self.send(404, {"detail": "not found"})

    def do_PATCH(self):
        b = self.body()
        if b is None:
            return
        parts = self.path.split("?")[0].split("/")
        if len(parts) != 4 or parts[1] != "api" or not parts[3]:
            return self.send(404, {"detail": "not found"})
        kind, oid = parts[2], parts[3]
        if kind == "projects":
            if not any(y["id"] == oid for y in PROJ):
                return self.send(404, {"detail": "no such project"})
            upd = {k: b[k] for k in PROJECT_FIELDS if k in b}
            if "name" in upd and not str(upd["name"] or "").strip():
                return self.send(400, {"detail": "name must not be empty"})
            if "pinned" in upd:
                upd["pinned"] = bool(upd["pinned"])
            with LOCK:
                OVERRIDES.setdefault(oid, {}).update(upd)
            build_projects()
            return self.send(200, {"ok": True})
        if kind == "peers":
            if oid not in STATE["priorities"]:
                return self.send(404, {"detail": "no such peer"})
            if not isinstance(b.get("priority"), int):
                return self.send(400, {"detail": "priority must be an integer"})
            STATE["priorities"][oid] = b["priority"]
            return self.send(200, {"ok": True})
        self.send(404, {"detail": "not found"})

    def do_PUT(self):
        if self.body() is None:
            return
        if self.path.startswith("/api/nodes/") and self.path.endswith("/settings"):
            return self.send(200, {"ok": True})
        self.send(404, {"detail": "not found"})


if __name__ == "__main__":
    print(f"mock hub on http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
