"""Mock hub for UI development. Serves hub/static/index.html + canned JSON on port 18790.

Run: python tools/mock_ui_server.py   (never touches the live hub on 8787)
Query ?mode flags are not needed; POST /mock/503 and /mock/ok toggle a "no leader" state.
"""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 18790
INDEX = Path(__file__).resolve().parent.parent / "hub" / "static" / "index.html"
T0 = time.time()
STATE = {"no_leader": False, "extra_jobs": [], "cancelled": set(), "priorities": {"forge-01": 1, "forge-02": 2, "forge-03": 3}}

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


def node(id_, host, online, cpu, mem, disk, ver, gpu=False, role_agents=()):
    now = time.time()
    return {
        "id": id_, "online": online, "last_seen": now - (2 if online else 1800), "addr": "192.168.1." + id_[-1],
        "info": {"hostname": host, "cpu": "Intel(R) Core(TM) i7-12700K CPU @ 3.60GHz", "cores": 20, "ram_gb": 32,
                 "gpus": ["RTX 3070"] if gpu else [], "os": "Windows 11 Pro", "model": "Custom", "user": "you",
                 "versions": {"claude": "2.1.0", "codex": "0.9"}, "wsl": ["Ubuntu"], "tools": ["claude", "codex"],
                 "agent_version": ver, "max_jobs": 2},
        "metrics": {"cpu": cpu, "mem_pct": mem, "mem_used_gb": round(32 * mem / 100, 1), "mem_total_gb": 32,
                    "disks": [{"name": "C:", "total_gb": 500, "free_gb": 500 - disk * 5}, {"name": "D:", "total_gb": 2000, "free_gb": 1400}],
                    "gpu": [{"name": "RTX 3070", "util": 35, "mem_used_mb": 2100, "mem_total_mb": 8192, "temp_c": 58}] if gpu else [],
                    "ips": [{"iface": "Ethernet", "ip": "192.168.1." + id_[-1]}], "uptime_s": 86400 * 3 if online else None,
                    "battery": None, "procs_total": 212, "work_root": "D:\\forge", "work_root_free_gb": 1400,
                    "watchdog": {"last_run": now - 120, "actions": ["none needed"] if id_ != "forge-03" else ["restarted agent"]},
                    **({"services": [{"name": "t3", "ok": True, "since": now - 7200}, {"name": "ollama", "ok": id_ == "forge-01", "since": now - 3000}]} if id_ in ("forge-01", "forge-02") else {})} if online else {},
        "disk_pct": disk, "agents": [{"kind": k, "pid": 1000 + i, "started": None, "cmd": k + " --run", "job": None} for i, k in enumerate(role_agents)],
        "active_jobs": 1 if id_ == "forge-02" else 0, "capacity": 2, "spark": spark(cpu) if online else [],
    }


def state():
    now = time.time()
    nodes = [
        node("forge-01", "forge-01", True, 18, 52, 40, "2.0.1", gpu=True, role_agents=("claude",)),
        node("forge-02", "forge-02", True, 71, 66, 93, "2.0.1", role_agents=("claude", "claude", "codex")),
        node("forge-03", "forge-03", True, 9, 30, 55, "1.9.0"),
        node("forge-04", "closet-laptop", False, 0, 0, 60, "2.0.1"),
    ]
    sid = "019ac3f2b1a4f3c2d1"
    jobs = [
        {"id": sid, "node_id": "forge-02", "requested": "auto", "engine": "claude", "prompt": "Refactor the build script and add tests",
         "cwd": "D:\\forge\\repos\\app", "full_access": False, "status": "running", "created": now - 90, "started": now - 80,
         "finished": None, "exit_code": None, "tail": "", "source": "ui"},
        {"id": "019ac3e0aa11bb22cc", "node_id": "forge-01", "engine": "shell", "prompt": "Get-ChildItem D:\\forge | Measure-Object", "cwd": None,
         "full_access": True, "status": "done", "created": now - 900, "started": now - 890, "finished": now - 880, "exit_code": 0, "tail": "", "source": "ui"},
        {"id": "019ac3d0aa11bb22cd", "node_id": "forge-03", "engine": "codex", "prompt": "Explain why tests fail on CI", "cwd": None,
         "full_access": False, "status": "failed", "created": now - 4000, "started": now - 3990, "finished": now - 3900, "exit_code": 1, "tail": "", "source": "ui"},
        {"id": "019ac3c0aa11bb22ce", "node_id": "auto", "requested": "auto", "engine": "any", "prompt": "Summarize open TODOs across the repo", "cwd": None,
         "full_access": False, "status": "queued", "created": now - 200, "started": None, "finished": None, "exit_code": None, "tail": "", "source": "ui",
         "wait_reason": "claude cooling down until " + time.strftime("%H:%M", time.localtime(now + 1500))},
        {"id": "019ac3c0aa11bb22cf", "node_id": "forge-01", "engine": "ollama", "prompt": "brief: hivemind", "cwd": None,
         "full_access": False, "status": "done", "created": now - 700, "started": now - 690, "finished": now - 650, "exit_code": 0, "tail": "", "source": "brief"},
    ] + STATE["extra_jobs"]
    for j in jobs:
        if j["id"] in STATE["cancelled"]:
            j.update(status="cancelled", finished=now)
    events = [
        {"id": "e1", "ts": now - 30, "node_id": "forge-02", "level": "warn", "msg": "disk 93% full"},
        {"id": "e2", "ts": now - 300, "node_id": "forge-04", "level": "error", "msg": "went offline"},
        {"id": "e3", "ts": now - 900, "node_id": "forge-01", "level": "ok", "msg": "job finished (exit 0)"},
        {"id": "e4", "ts": now - 2000, "node_id": None, "level": "info", "msg": "leader elected: forge-01"},
    ]
    p = STATE["priorities"]
    peers = [{"id": k, "priority": v, "role": "leader" if k == "forge-01" else "standby", "version": "2.0.1",
              "healthy": k != "forge-03" or True, "last_ok": now - 2, "lag_s": 0 if k == "forge-01" else 0.8 + v / 10} for k, v in p.items()]
    return {"now": now, "nodes": nodes, "jobs": jobs, "events": events,
            "cluster": {"leader_id": "forge-01", "viewing": "forge-02", "peers": peers, "bad_versions": ["2.2.0-rc1"]}}


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
    s = state()
    j = next((dict(x) for x in s["jobs"] if x["id"] == jid), None)
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
SESS = {}   # (tool, id) -> dict
PROJ = []
BRIEFS_ALL = {"text": "- hivemind: 3 live sessions, Phase 2 UI in progress.\n- nova: waiting on review.\n- Suggested next step: merge p2-ui.", "ts": T0, "model": "haiku"}
COOLDOWNS = {"claude": {"until": T0 + 1500 + 3600, "reason": "5-hour limit reached"}}


def _s(tool, sid, node, cwd, title, state, last, msgs, **kw):
    d = {"tool": tool, "session_id": sid, "node_id": node, "cwd": cwd, "title": title, "state": state,
         "last_activity": last, "started": last - 3600, "messages": msgs, "git_branch": "main", "model": "claude-sonnet-5"}
    d.update(kw)
    SESS[(tool, sid)] = d
    return d


def build_projects():
    now = time.time()
    SESS.clear()
    fc = [
        _s("claude", "live-term-1", "forge-01", "D:\\forge\\repos\\hivemind", "Build the Phase 2 UI: Projects view + live session viewer", "live", now - 4, 128, pid=4120, git_branch="p2-ui"),
        _s("claude", "live-remote-2", "forge-02", "D:\\forge\\repos\\hivemind", "Write chaos simulation tests for the scheduler", "live", now - 12, 54, remote_control=True, git_branch="p2-tests"),
        _s("codex", "codex-idle-3", "forge-01", "D:\\forge\\repos\\hivemind", "Review rollout lease ordering", "idle", now - 5400, 22, model="gpt-5-codex"),
        _s("t3", "t3-thread-4", "forge-02", "D:\\forge\\repos\\hivemind", "Chat: should leader take lease last?", "idle", now - 9000, 9),
        _s("forge", "019ac3f2b1a4f3c2d1", "forge-02", "D:\\forge\\repos\\app", "Refactor the build script and add tests", "live", now - 3, 1),
        _s("claude", "done-5", "forge-03", "C:\\Users\\you\\hivemind", "Fix watchdog restart loop", "done", now - 86400 * 3, 310),
    ]
    tc = [
        _s("codex", "tc-1", "forge-01", "D:\\forge\\repos\\nova", "Add LRU eviction to nova", "idle", now - 7200, 41, model="gpt-5-codex", git_branch="lru"),
        _s("claude", "tc-2", "forge-03", "D:\\forge\\repos\\nova", "Benchmark cache hit ratio", "done", now - 86400 * 2, 77),
    ]
    xss = [_s("claude", "xss-1", "forge-01", "D:\\x", "<img src=x onerror=alert(1)> title", "done", now - 86400 * 5, 3)]
    sc = [_s("claude", "misc-1", "forge-01", "C:\\Users\\you", "quick question about robocopy flags", "done", now - 86400, 6)]
    commits = [{"sha": "a1b2c3d4e5f6a7b8", "author": "Hivemind contributors", "ts": now - 1800, "subject": "feat(hub): update lease endpoints"},
               {"sha": "b2c3d4e5f6a7b8c9", "author": "Hivemind contributors", "ts": now - 7200, "subject": "fix: <b>watchdog</b> restart loop"},
               {"sha": "c3d4e5f6a7b8c9d0", "author": "Claude", "ts": now - 86400, "subject": "test: rollout ordering"}]
    PROJ[:] = [
        {"id": "p1", "key": "github.com/you/hivemind", "name": "hivemind", "group_name": "atlas", "pinned": 1,
         "machines": ["forge-01", "forge-02", "forge-03"], "live_sessions": 3, "sessions_24h": 5, "last_activity": now - 3, "dirty_any": True, "branches": ["main", "p2-ui", "p2-tests"],
         "brief": {"text": "- Phase 2 UI is being built on branch p2-ui (live on forge-01).\n- Chaos simulation tests are running on forge-02.\n- Blocked: nothing.\n- Next: review the update-lease ordering and merge p2-ui.", "ts": now - 900, "model": "qwen2.5:14b"},
         "sessions": sorted(fc, key=lambda x: -x["last_activity"]), "commits": commits,
         "repos": [{"node_id": "forge-01", "cwd": "D:\\forge\\repos\\hivemind", "branch": "p2-ui", "dirty": True, "ahead": 3, "behind": 0},
                   {"node_id": "forge-02", "cwd": "D:\\forge\\repos\\hivemind", "branch": "p2-tests", "dirty": False, "ahead": 1, "behind": 2}],
         "briefs": [{"text": "- Earlier brief: scheduler work started.", "ts": now - 86400, "model": "qwen2.5:14b"}]},
        {"id": "p2", "key": "github.com/you/nova", "name": "nova", "group_name": "atlas", "pinned": 0,
         "machines": ["forge-01", "forge-03"], "live_sessions": 0, "sessions_24h": 1, "last_activity": now - 7200, "dirty_any": False, "branches": ["main", "lru"],
         "brief": {"text": "LRU eviction is implemented; benchmarks show a 12% better hit ratio. Waiting on review.", "ts": now - 6000, "model": "qwen2.5:14b"},
         "sessions": tc, "commits": commits[:1], "repos": [{"node_id": "forge-01", "cwd": "D:\\forge\\repos\\nova", "branch": "lru", "dirty": False, "ahead": 2, "behind": 0}], "briefs": []},
        {"id": "p3", "key": "local:forge-01:d:\\x", "name": "<img src=x onerror=alert(1)>", "group_name": None, "pinned": 0, "machines": ["forge-01"], "live_sessions": 0,
         "sessions_24h": 0, "last_activity": now - 86400 * 5, "dirty_any": False, "branches": [], "brief": None, "sessions": xss, "commits": [], "repos": [], "briefs": []},
        {"id": "p4", "key": "misc:forge-01", "name": "Scratch / misc", "group_name": "Misc", "pinned": 0, "machines": ["forge-01"], "live_sessions": 0, "sessions_24h": 1,
         "last_activity": now - 86400, "dirty_any": False, "branches": [], "brief": None, "sessions": sc, "commits": [], "repos": [], "briefs": []},
        {"id": "p5", "key": "github.com/you/bare", "name": "bare-minimum-fields"},   # partial data from an older hub: almost everything missing
    ]
    if os.environ.get("MOCK_DEMO"):   # clean showcase data for README screenshots: drop the edge-case fixtures
        PROJ[:] = [p for p in PROJ if p["id"] not in ("p3", "p5")]


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
    e = BASE_EVENTS[i] if i < len(BASE_EVENTS) else STREAM_EVENTS[(i - len(BASE_EVENTS)) % len(STREAM_EVENTS)]
    d = {"ts": time.time() - max(0, (400 - i * 3)), "role": e[0], "kind": e[1], "text": e[2], "collapsed": e[1] != "text"}
    if len(e) > 3:
        d["tool_name"] = e[3]
    return d


def transcript(tool, sid, since):
    s = SESS.get((tool, sid))
    if not s:
        return None
    total = len(BASE_EVENTS) + (int((time.time() - T0) / 2) if s["state"] == "live" else 6)
    start = 0 if since is None else min(max(since, 0), total)
    return {"events": [ev_at(i) for i in range(start, total)], "cursor": total, "done": s["state"] != "live",
            "session": {"state": s["state"], "pid": s.get("pid"), "messages": s["messages"] + max(0, total - len(BASE_EVENTS))}}


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
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

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
            self.send(200, {"alerts": [
                {"level": "error", "node_id": "forge-04", "kind": "offline", "msg": "offline for 30 min"},
                {"level": "warn", "node_id": "forge-02", "kind": "disk", "msg": "disk 93% full on C:"},
                {"level": "warn", "node_id": "forge-03", "kind": "version", "msg": "agent v1.9.0 is behind v2.0.1"},
                {"level": "info", "node_id": None, "kind": "bundle", "msg": "bundle v2.0.1 published"}],
                "bundle_version": "2.0.1", "update_lease": {"holder": "forge-03", "version": "2.0.1", "expires": time.time() + 500},
                "rollout": [{"node_id": "forge-01", "version": "2.0.1", "status": "current"},
                            {"node_id": "forge-02", "version": "2.0.1", "status": "current"},
                            {"node_id": "forge-03", "version": "1.9.0", "status": "updating"},
                            {"node_id": "forge-04", "version": "2.0.1", "status": "offline"}]})
        elif p.startswith("/api/nodes/"):
            d = detail(p.split("/")[3])
            self.send(200, d) if d else self.send(404, {"detail": "not found"})
        elif p.startswith("/api/jobs/"):
            d = job_detail(p.split("/")[3])
            self.send(200, d) if d else self.send(404, {"detail": "not found"})
        elif p == "/api/projects":
            build_projects()
            keys = ("id", "key", "name", "group_name", "pinned", "machines", "live_sessions", "sessions_24h", "last_activity", "brief", "dirty_any", "branches")
            self.send(200, [{k: x[k] for k in keys if k in x} for x in PROJ])
        elif p.startswith("/api/projects/"):
            build_projects()
            x = next((y for y in PROJ if y["id"] == p.split("/")[3]), None)
            self.send(200, x) if x else self.send(404, {"detail": "not found"})
        elif p == "/api/brief/all":
            self.send(200, BRIEFS_ALL)
        elif p == "/api/scheduler":
            self.send(200, {"cooldowns": COOLDOWNS, "queue_depth": 1, "placement_preview": {"claude": "forge-01"}})
        elif p.startswith("/api/sessions/") and p.endswith("/transcript"):
            parts = p.split("/")
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            since = None
            for kv in q.split("&"):
                if kv.startswith("since=") and kv[6:].isdigit():
                    since = int(kv[6:])
            d = transcript(parts[3], parts[4], since)
            self.send(200, d) if d else self.send(404, {"detail": "unknown session"})
        elif p == "/api/enroll":
            self.send(200, {"hub": "http://forge-01:8787", "command": "irm http://forge-01:8787/agent/install.ps1 | iex"})
        else:
            self.send(404, {"detail": "not found"})

    def do_POST(self):
        p = self.path.split("?")[0]
        b = self.body()
        if p == "/api/login":
            self.send(200, {"ok": True})
        elif p.startswith("/api/sessions/") and p.endswith("/watch"):
            self.send(200, {"ok": True, "ttl": 30})
        elif p == "/api/brief/all":
            self.send(200, {"id": "019ac3ff00brief"})
        elif p.startswith("/api/projects/") and p.endswith("/brief"):
            x = next((y for y in PROJ if y["id"] == p.split("/")[3]), None)
            if x:
                x["brief"] = {"text": "- Fresh brief on demand.\n- Everything looks on track.", "ts": time.time(), "model": "haiku"}
            self.send(200, {"id": "019ac3ff00brief"})
        elif p == "/api/jobs" and b.get("resume") and (SESS.get((b["resume"].get("tool"), b["resume"].get("session_id"))) or {}).get("pid") and not b.get("force"):
            self.send(409, {"detail": "session is live in a terminal"})
        elif p == "/api/jobs":
            jid = "019ac3ff%010x" % int(time.time() * 1000)
            STATE["extra_jobs"].insert(0, {"id": jid, "node_id": "forge-01" if b.get("node_id") == "auto" else b.get("node_id"),
                "requested": "auto" if b.get("node_id") == "auto" else None, "engine": b.get("engine"), "prompt": b.get("prompt"),
                "cwd": b.get("cwd"), "full_access": b.get("full_access"), "status": "queued", "created": time.time(), "started": None,
                "finished": None, "exit_code": None, "tail": "", "source": "ui"})
            self.send(200, {"id": jid})
        elif p.endswith("/cancel"):
            STATE["cancelled"].add(p.split("/")[3])
            self.send(200, {"ok": True})
        elif p == "/mock/503":
            STATE["no_leader"] = True
            self.send(200, {})
        elif p == "/mock/ok":
            STATE["no_leader"] = False
            self.send(200, {})
        else:
            self.send(404, {"detail": "not found"})

    def do_DELETE(self):
        if self.path.startswith("/api/scheduler/cooldowns/"):
            COOLDOWNS.pop(self.path.split("/")[4], None)
        self.send(200, {"ok": True})

    def do_PATCH(self):
        b = self.body()
        if self.path.startswith("/api/projects/"):
            x = next((y for y in PROJ if y["id"] == self.path.split("/")[3]), None)
            return self.send(200, {"ok": True}) if x else self.send(404, {"detail": "not found"})
        STATE["priorities"][self.path.split("/")[3]] = b.get("priority")
        self.send(200, {"ok": True})

    def do_PUT(self):
        self.body()
        self.send(200, {"ok": True})


if __name__ == "__main__":
    print(f"mock hub on http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
