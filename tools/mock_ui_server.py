"""Mock hub for UI development. Serves hub/static (index.html, sw.js, icons) + canned JSON shaped like the 2.3 hub
(CONTRACT-phase3.md), so the UI can be built and browser-tested without a fleet.

Run: python tools/mock_ui_server.py   (port MOCK_UI_PORT, default 19210; must be 19210-19249 = workstream U's test range;
never touches the live hub on 8787)
Toggles (POST): /mock/503 and /mock/ok switch a "no leader" state; /mock/reset-transcript simulates a hub restart
(transcript seq numbers start again at 1); /mock/empty and /mock/full switch to a brand-new fleet (first run) and back;
/mock/nopin forgets the admin PIN (step-up answers "setup"); /mock/v22 makes the 2.3 endpoints answer 404 like a 2.2 leader
(and /mock/v23 undoes it). MOCK_DEMO=1 drops edge-case fixtures. The mock admin PIN is 123456.
"""
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hub"))
import forge_ui as fu  # noqa: E402  (pure helpers: fuzzy matching, Today buckets, alert actions; no FastAPI needed)

PORT = int(os.environ.get("MOCK_UI_PORT", "19210"))
assert 19210 <= PORT <= 19249, "MOCK_UI_PORT must be in workstream U's test range 19210-19249"
STATIC = ROOT / "hub" / "static"
INDEX = STATIC / "index.html"
T0 = time.time()
LOCK = threading.Lock()
STATE = {"no_leader": False, "extra_jobs": [], "cancelled": set(), "priorities": {"forge-01": 1, "forge-02": 2, "forge-03": 3},
         "tx_reset_at": None, "empty": False, "v22": False}
OVERRIDES = {}   # project id -> {brief, name, pinned, group_name, sync_nodes}: survives build_projects()
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
                    "job_token": "filtered" if id_ != "forge-03" else "full",
                    **({"services": [{"name": "t3", "ok": True, "state": "ok", "since": now - 7200},
                                     {"name": "ollama", "ok": id_ == "forge-01", "state": "ok" if id_ == "forge-01" else "starting", "since": now - 30}]}
                       if id_ in ("forge-01", "forge-02") else {})} if online else {},
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
    jn = joined_node()
    if jn:
        nodes.append(jn)
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
    n["settings"] = {"work_root": None, "max_jobs": None, "wu_window": None}
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
        j["output"] = "".join(x + "\n" for x in STREAM[:k])
        j["output_seq"] = k                  # R4: the live view continues with GET /api/jobs/{id}/output?since_seq=k
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
    for p in proj:
        if "sessions" in p:
            p.setdefault("sync_nodes", [])
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



# ---------------------------------------------------------------- Phase 3 (2.3) data
PIN = {"hash": hashlib.sha256(b"123456").hexdigest(), "until": 0.0, "fails": []}
DEVICES = {}
ENROLL = {}         # id -> {kind, code, created, expires, cancelled}
RUNS = {}           # maintenance runs: id -> run dict (progress is computed from time)
RETIRE = {}         # node id -> {state, started, ...}
ACKED = set()
NOTIFY = {"ntfy": {"enabled": False, "server": "https://ntfy.sh", "topic": None},
          "webpush": {"enabled": True, "vapid_public_key": "BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U", "devices": 0},
          "deadman": {"enabled": False, "url": None, "send_fail": False, "last_ping": None, "last_ok": None},
          "rules": {"task_failed": True, "task_finished": True, "machine_down": True, "autorepair": True}}
UPKEEP = {"schedule": {"enabled": True, "day": "sun", "time": "04:00", "actions": ["update_ai_tools", "update_t3", "update_ollama", "winget_upgrade"]},
          "windows_update": {"enabled": True, "window": "03:00-05:00", "stagger_min": 30}, "repo_sync_every_s": 1800}
TOKENS = {"agent": {"rotated": None, "grace_until": None}, "cluster": {"rotated": None, "grace_until": None}}
SUBS = set()
MAINT = [
    {"name": "update_app", "title": "Update an app", "description": "Upgrade one winget package, or all of them.", "steps": ["winget_upgrade"],
     "lane": "system", "destructive": False, "args": {"id": {"type": "string", "required": False, "label": "Package id (empty = all apps)", "max_len": 128}}},
    {"name": "install_app", "title": "Install an app", "description": "Install one winget package on every machine.", "steps": ["winget_install"],
     "lane": "system", "destructive": False, "args": {"id": {"type": "string", "required": True, "label": "Package id", "max_len": 128}}},
    {"name": "update_ai_tools", "title": "Update Claude Code & Codex", "description": "claude update, npm i -g @openai/codex@latest.",
     "steps": ["update_claude", "update_codex"], "lane": "user", "destructive": False, "args": {}},
    {"name": "restart_hivemind", "title": "Restart Hivemind", "description": "Restart the hub and the agent on each machine.",
     "steps": ["restart_task"], "lane": "system", "destructive": False, "args": {}},
    {"name": "free_disk", "title": "Free disk space", "description": "Empty temp folders, old scratch folders and the recycle bin.",
     "steps": ["free_disk"], "lane": "system", "destructive": False, "args": {}},
    {"name": "restart_machine", "title": "Restart machine", "description": "Reboot the machine (running tasks are stopped).",
     "steps": ["reboot"], "lane": "system", "destructive": True, "args": {}},
]


def maint_args_error(a, args):
    """Like F's arg check: required args present, strings within max_len, ints within min/max, enums valid."""
    args = args if isinstance(args, dict) else {}
    for k, r in (a.get("args") or {}).items():
        v = args.get(k)
        lab = r.get("label") or k
        if v in (None, ""):
            if r.get("required"):
                return f"{lab} is required"
            continue
        if r.get("enum") and v not in r["enum"]:
            return f"{lab}: pick one of {', '.join(r['enum'])}"
        if r.get("type") == "int":
            if not isinstance(v, int) or (r.get("min") is not None and v < r["min"]) or (r.get("max") is not None and v > r["max"]):
                return f"{lab}: out of range"
        elif r.get("type") == "string" and len(str(v)) > int(r.get("max_len") or 256):
            return f"{lab}: too long"
    return None


STEP_S = 4.0        # each machine of a mock maintenance run takes 4 s


def init_devices():
    now = time.time()
    DEVICES.clear()
    for i, (lbl, ip, age) in enumerate([("Chrome on Windows", "100.64.0.1", 3), ("Safari on iPhone", "100.64.0.7", 600), ("Old laptop", "192.168.1.40", 86400 * 20)]):
        did = hashlib.sha256(lbl.encode()).hexdigest()[:12]
        DEVICES[did] = {"id": did, "label": lbl, "created": now - 86400 * (30 - i * 5), "last_seen": now - age, "expires": now + 86400 * 80,
                        "current": i == 0, "ip": ip, "user_agent": lbl}


init_devices()


def nodes_now():
    return [] if STATE["empty"] else state()["nodes"]


def joined_node():
    """A machine added through the wizard shows up in /api/state once its code is 'joined'."""
    for e in ENROLL.values():
        if e["kind"] == "machine" and enroll_state(e)[0] == "joined":
            return node("forge-05", "studio-pc", True, 4, 21, 12, "2.3.0")
    return None


def enroll_state(e):
    now = time.time()
    if e.get("cancelled") or now > e["expires"]:
        return "expired", None
    t = now - e["created"]
    if e["kind"] == "device":
        return ("redeemed", None) if t > 6 else ("pending", None)
    if t > 10:
        return "joined", "forge-05"
    return ("redeemed", None) if t > 6 else ("pending", None)


def enroll_view(eid):
    e = ENROLL[eid]
    st, nid = enroll_state(e)
    out = {"id": eid, "kind": e["kind"], "state": st, "expires": e["expires"]}
    if nid:
        out["node_id"] = nid
    return out


def mock_alerts():
    """Alerts in the N1 shape (what /api/alerts returns once N lands)."""
    now = time.time()
    if STATE["empty"]:
        return []
    out = []
    for a in health()["alerts"]:
        if a["level"] not in ("warn", "error"):
            continue
        heal = a["kind"] == "version_behind"          # the mock fleet holds an update lease: version_behind heals itself
        out.append(fu.n1_alert(a, opened=now - 1800, self_healing=heal))
    out.append(fu.n1_alert({"kind": "task_failed", "node_id": "forge-03", "level": "warn", "opened": now - 3900,
                            "key": "task_failed:forge-03:019ac3d0aa11bb22cd", "msg": "Task failed: Explain why tests fail on CI",
                            "data": {"job_id": "019ac3d0aa11bb22cd"}}))
    out.append(fu.n1_alert({"kind": "repo_conflict", "node_id": "forge-02", "level": "warn", "opened": now - 7200,
                            "key": "repo_conflict:forge-02:p1", "msg": "hivemind on forge-02 has diverged from origin",
                            "data": {"project_id": "p1"}}))
    return [a for a in out if a["id"] not in ACKED]


def mock_today(q="", all_needs=False):
    now = time.time()
    if STATE["empty"]:
        return {"now": now, "status": fu.status_sentence(0, 0, []), "needs_you": {"items": [], "total": 0}, "happening": [],
                "recent": {"today": [], "yesterday": [], "earlier": []}, "brief": None, "first_run": {"machines": 0, "projects": 0}}
    build_projects()
    nodes = state()["nodes"]
    host = {n["id"]: n["info"]["hostname"] for n in nodes}
    needs = fu.filter_needs(mock_alerts(), now)
    happening, recent_rows = [], []
    with LOCK:
        projs = list(PROJ)
    for p in projs:
        for s in p.get("sessions", []):
            if s["tool"] == "forge":
                continue
            if s["state"] == "live":
                happening.append({"kind": "session", "id": s["session_id"], "tool": s["tool"], "title": s["title"], "node_id": s["node_id"],
                                  "project_id": p["id"], "since": s["started"], "state": "live"})
            else:
                recent_rows.append({"tool": s["tool"], "session_id": s["session_id"], "title": s["title"], "node_id": s["node_id"],
                                    "project_id": p["id"], "project_name": p["name"], "state": s["state"], "last_activity": s["last_activity"],
                                    "started": s["started"], "git_branch": s.get("git_branch"), "messages": s.get("messages")})
    for j in all_jobs():
        if j["status"] in ("queued", "starting", "running") and j.get("source") not in ("brief", "sync"):
            happening.append({"kind": "job", "id": j["id"], "tool": j["engine"], "title": j["prompt"] or j.get("action") or "", "node_id": j["node_id"],
                              "project_id": j.get("project_id"), "since": j.get("started") or j["created"], "state": j["status"]})
    happening.sort(key=lambda h: -(h["since"] or 0))
    qn = (q or "").strip().lower()
    if qn:
        recent_rows = [s for s in recent_rows if qn in " ".join([s["title"], s["project_name"] or "", s["node_id"], host.get(s["node_id"], "")]).lower()]
    recent_rows.sort(key=lambda s: -(s["last_activity"] or 0))
    recent = {"today": [], "yesterday": [], "earlier": []}
    for s in recent_rows:
        b = recent[fu.day_bucket(s["last_activity"], now)]
        if len(b) < fu.RECENT_MAX:
            b.append(s)
    online = sum(1 for n in nodes if n["online"])
    return {"now": now, "status": fu.status_sentence(len(nodes), online, needs), "needs_you": {"items": needs[:fu.NEEDS_ALL_MAX if all_needs else fu.NEEDS_MAX], "total": len(needs)},
            "happening": happening, "recent": recent, "brief": {k: BRIEFS_ALL[k] for k in ("text", "ts", "model")},
            "first_run": {"machines": len(nodes), "projects": len(projs)}}


def mock_ask(q, limit=8):
    text = (q or "").strip()[:200]
    if not text:
        return {"results": []}
    res = []
    for a in fu.NAMED_ACTIONS:
        res.append({"type": "action", "id": a["id"], "title": a["title"], "subtitle": a["subtitle"], "score": fu.best_score(text, a["title"], a["words"]),
                    "target": {"route": a["route"]}})
    for m in MAINT:
        res.append({"type": "maintenance", "id": m["name"], "title": m["title"], "subtitle": "Maintenance · all machines",
                    "score": fu.best_score(text, m["title"], m["name"].replace("_", " "), m["description"]), "target": {"route": "#/machines/maintenance", "action": m["name"]}})
    for n in nodes_now():
        res.append({"type": "machine", "id": n["id"], "title": n["info"]["hostname"], "subtitle": "Online" if n["online"] else "Offline",
                    "score": fu.best_score(text, n["info"]["hostname"], n["id"]), "target": {"node_id": n["id"]}})
    if not STATE["empty"]:
        build_projects()
        with LOCK:
            projs = list(PROJ)
        for p in projs:
            res.append({"type": "project", "id": p["id"], "title": p["name"], "subtitle": p.get("group_name") or "",
                        "score": fu.best_score(text, p["name"], p.get("group_name"), p.get("key")), "target": {"project_id": p["id"]}})
            for s in p.get("sessions", []):
                if s["tool"] != "forge":
                    res.append({"type": "session", "id": s["session_id"], "title": s["title"], "subtitle": f"{s['tool']} · {s['node_id']} · {p['name']}",
                                "score": fu.best_score(text, s["title"], p["name"]) * 0.95,
                                "target": {"tool": s["tool"], "session_id": s["session_id"], "project_id": p["id"]}})
    out = fu.ask_rank(res, max(1, min(limit, 50)) - 1)
    out.append(fu.run_entry(text))
    return {"results": out}


def rollout_order(targets):
    nodes = [n for n in nodes_now() if targets == "all" or n["id"] in targets]
    prio = STATE["priorities"]
    on = [n["id"] for n in nodes if n["online"]]
    on.sort(key=lambda i: (i == "forge-01", -prio.get(i, 99)))      # canary/highest number first, leader last
    skipped = [{"node_id": n["id"], "reason": "offline"} for n in nodes if not n["online"]]
    return on, skipped


def run_view(rid):
    r = dict(RUNS[rid])
    now = time.time()
    n = len(r["targets"])
    if r["status"] in ("cancelled",):
        return r
    done = min(n, int((now - r["started"]) / STEP_S))
    results = []
    status = "running"
    for i in range(done):
        nid = r["targets"][i]
        bad = r["action"] == "free_disk" and nid == "forge-02"        # one scripted failure, to show "stop on first failure"
        results.append({"node_id": nid, "job_id": f"{rid}-{i}", "status": "failed" if bad else "done",
                        "summary": "disk still 93% full" if bad else ("updated 3 packages" if r["action"] == "update_app" else "ok"),
                        "finished": r["started"] + (i + 1) * STEP_S})
        if bad:
            status = "failed"
            break
    if status != "failed" and done >= n:
        status = "done"
    r["results"], r["idx"], r["status"] = results, len(results), status
    r["current"] = {"node_id": r["targets"][len(results)], "job_id": f"{rid}-{len(results)}"} if status == "running" else None
    r["finished"] = r["started"] + len(results) * STEP_S if status != "running" else None
    return r


def output_chunks(jid, since):
    if jid != "019ac3f2b1a4f3c2d1":
        j = job_detail(jid)
        return {"chunks": [{"seq": 1, "ts": j["created"], "text": j["output"]}] if since < 1 and j else [], "next_seq": 2, "status": j["status"] if j else None,
                "truncated": False}
    k = min(len(STREAM), int((time.time() - T0) / 2) % (len(STREAM) + 4) + 1)
    chunks = [{"seq": i + 1, "ts": T0 + i * 2, "text": STREAM[i] + "\n"} for i in range(k) if i + 1 > since]
    return {"chunks": chunks, "next_seq": k + 1, "status": "running", "truncated": False}


V23_PREFIXES = ("/api/today", "/api/ask", "/api/alerts", "/api/enroll/", "/api/devices", "/api/tokens", "/api/stepup", "/api/security",
                "/api/notify", "/api/push", "/api/maintenance", "/api/upkeep", "/api/machines/", "/api/backups", "/api/claim")


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

    def send_file(self, path, ctype, extra=None):
        b = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
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
        from urllib.parse import unquote_plus
        return {unquote_plus(k): unquote_plus(v) for k, v in (kv.split("=", 1) for kv in q.split("&") if "=" in kv)}

    def v22_gone(self, p):
        if STATE["v22"] and self.command == "POST" and p == "/api/enroll":
            self.send(405, {"detail": "Method Not Allowed"})
            return True
        if STATE["v22"] and (p.startswith(V23_PREFIXES) or p in ("/manifest.webmanifest", "/sw.js")):
            self.send(404, {"detail": "Not Found"})
            return True
        return False

    # ---- S7 step-up (contract shape: 403 {"detail", "step_up"})
    def need_step_up(self):
        if not PIN["hash"]:
            self.send(403, {"detail": "admin PIN not set", "step_up": "setup"})
            return True
        if PIN["until"] < time.time():
            self.send(403, {"detail": "admin PIN required", "step_up": "required"})
            return True
        return False

    def do_GET(self):
        p = self.path.split("?")[0]
        if self.v22_gone(p):
            return
        q = self.query()
        if p in ("/", "/index.html"):
            self.send_file(INDEX, "text/html; charset=utf-8", {"Cache-Control": "no-store"})
        elif p == "/sw.js":
            self.send_file(STATIC / "sw.js", "application/javascript", {"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})
        elif p == "/manifest.webmanifest":
            b = json.dumps(fu.MANIFEST).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif p.startswith("/icons/"):
            name = p[len("/icons/"):]
            f = STATIC / "icons" / name
            self.send_file(f, "image/png") if fu.ICON_RE.match(name) and f.is_file() else self.send(404, {"detail": "no such icon"})
        elif p == "/healthz":
            self.send(200, {"ok": True, "node_id": "forge-01", "role": "leader", "version": "2.3.0", "running_version": "2.3.0", "db_ok": True,
                            "free_gb": 812.4, "checks": {"db_write": True, "disk": True}})
        elif p == "/api/state":
            if STATE["no_leader"]:
                return self.send(503, {"detail": "no leader"})
            s = state()
            if STATE["empty"]:
                s.update(nodes=[], jobs=[], events=[])
            self.send(200, s)
        elif p == "/api/health":
            self.send(200, health())
        elif p == "/api/today":
            self.send(503, {"detail": "no leader"}) if STATE["no_leader"] else self.send(200, mock_today(q.get("q", ""), q.get("all") == "1"))
        elif p == "/api/ask":
            lim = int(q["limit"]) if q.get("limit", "").isdigit() else 8
            self.send(200, mock_ask(q.get("q", ""), lim))
        elif p == "/api/alerts":
            al = mock_alerts()
            lim = int(q["limit"]) if q.get("limit", "").isdigit() else 100
            self.send(200, {"alerts": al[:lim]})
        elif p.startswith("/api/nodes/"):
            d = detail(p.split("/")[3])
            self.send(200, d) if d else self.send(404, {"detail": "no such node"})
        elif p == "/api/jobs":
            inc = q.get("include_brief", "").lower() in ("1", "true", "yes", "on")
            lim = max(1, min(int(q["limit"]) if q.get("limit", "").isdigit() else 40, 200))
            self.send(200, [j for j in all_jobs() if inc or j.get("source") != "brief"][:lim])
        elif re.fullmatch(r"/api/jobs/[^/]+/output", p):
            since = int(q["since_seq"]) if q.get("since_seq", "").lstrip("-").isdigit() else 0
            jid = p.split("/")[3]
            self.send(200, output_chunks(jid, since)) if job_detail(jid) else self.send(404, {"detail": "no such job"})
        elif p.startswith("/api/jobs/"):
            d = job_detail(p.split("/")[3])
            self.send(200, d) if d else self.send(404, {"detail": "no such job"})
        elif p == "/api/projects":
            if STATE["empty"]:
                return self.send(200, [])
            build_projects()
            keys = ("id", "key", "name", "group_name", "pinned", "machines", "live_sessions", "sessions_24h", "last_activity", "brief", "dirty_any",
                    "branches", "sync_nodes")
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
            s = q.get("since", "0")
            d = transcript(parts[3], parts[4], int(s) if s.isdigit() else 0)
            self.send(200, d) if d else self.send(404, {"detail": "no such session"})
        elif p == "/api/enroll":      # 2.2 shape (S replaces it with the POST shape)
            self.send(200, {"hub": "http://forge-01:8787", "command": "irm http://forge-01:8787/agent/install.ps1 | iex"})
        elif p.startswith("/api/enroll/"):
            eid = p.split("/")[3]
            self.send(200, enroll_view(eid)) if eid in ENROLL else self.send(404, {"detail": "no such code"})
        elif p == "/api/devices":
            self.send(200, {"devices": sorted(DEVICES.values(), key=lambda d: -d["last_seen"])})
        elif p == "/api/tokens":
            self.send(200, TOKENS)
        elif p == "/api/stepup":
            self.send(200, {"pin_set": bool(PIN["hash"]), "valid_until": PIN["until"] if PIN["until"] > time.time() else None})
        elif p == "/api/security/signing-key":
            self.send(200, {"pinned": True, "key_id": "3f9a1c0d5e7b2a84"})
        elif p == "/api/backups":
            now = time.time()
            self.send(200, {"backups": [{"file": time.strftime("forge-%Y%m%d.db", time.localtime(now - 86400 * i)), "size": 4_200_000 + i * 1000, "ts": now - 86400 * i - 3600}
                                        for i in range(3)], "last_backup": now - 3600})
        elif p == "/api/notify/settings":
            self.send(200, notify_view())
        elif p == "/api/actions":            # 2.2 allowlist (hub/actions.json); the 2.3 UI falls back to it on a 2.2 leader
            cat = json.loads((ROOT / "hub" / "actions.json").read_text(encoding="utf-8"))
            self.send(200, {**cat, "admin_lane_enabled": True})
        elif p == "/api/maintenance/actions":
            self.send(200, {"actions": MAINT})
        elif p == "/api/maintenance/runs":
            self.send(200, {"runs": sorted((run_view(r) for r in RUNS), key=lambda r: -r["created"])[:20]})
        elif p.startswith("/api/maintenance/runs/"):
            rid = p.split("/")[4]
            self.send(200, run_view(rid)) if rid in RUNS else self.send(404, {"detail": "no such run"})
        elif p == "/api/upkeep/settings":
            self.send(200, UPKEEP)
        elif re.fullmatch(r"/api/machines/[^/]+/retire", p):
            nid = p.split("/")[3]
            if not any(x["id"] == nid for x in nodes_now()):
                return self.send(404, {"detail": "no such node"})
            self.send(200, retire_view(nid) if nid in RETIRE else {"node_id": nid, "state": None, "started": None, "finished": None,
                                                                    "job_id": None, "error": None})
        else:
            self.send(404, {"detail": "Not Found"})

    def do_POST(self):
        p = self.path.split("?")[0]
        b = self.body()
        if b is None or self.v22_gone(p):
            return
        if p == "/api/login":
            self.send(200, {"ok": True, "session": {"id": "a1b2c3d4e5f6", "label": b.get("label") or "browser"}})
        elif p == "/api/claim":
            code = re.sub(r"[^A-Z0-9]", "", str(b.get("code") or "").upper())
            ok = code == "ABCDEFGH" or any(re.sub(r"[^A-Z0-9]", "", e["code"]) == code and e["kind"] == "device" and enroll_state(e)[0] != "expired"
                                           for e in ENROLL.values())
            self.send(200, {"ok": True, "session": {"id": "c1aim0000001", "label": b.get("label") or "device"}}) if ok else self.send(401, {"detail": "bad or expired code"})
        elif p == "/api/logout":
            self.send(200, {"ok": True})
        elif p == "/api/enroll":
            kind = b.get("kind") or "machine"
            if kind not in ("machine", "device"):
                return self.send(400, {"detail": "kind must be machine or device"})
            eid = secrets.token_hex(8)
            code = "".join(secrets.choice("0123456789ABCDEFGHJKMNPQRSTVWXYZ") for _ in range(8))
            code = code[:4] + "-" + code[4:]
            hub = "http://forge-01.tail1234.ts.net:8787"
            now = time.time()
            ENROLL[eid] = {"kind": kind, "code": code, "created": now, "expires": now + 600, "label": b.get("label")}
            out = {"id": eid, "code": code, "kind": kind, "expires": now + 600, "hub": hub}
            if kind == "machine":
                out["command"] = f"irm '{hub}/agent/install.ps1?code={code}' | iex"
            else:
                out["url"] = f"{hub}/?claim={code}"
            self.send(200, out)
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
        elif re.fullmatch(r"/api/alerts/[^/]+/ack", p):
            ACKED.add(urllib.parse.unquote(p.split("/")[3]))
            self.send(200, {"ok": True})
        elif p == "/api/stepup":
            PIN["fails"] = [t for t in PIN["fails"] if t > time.time() - 600]
            if len(PIN["fails"]) >= 5:
                return self.send(429, {"detail": "too many wrong PINs"})
            if not PIN["hash"] or hashlib.sha256(str(b.get("pin") or "").encode()).hexdigest() != PIN["hash"]:
                PIN["fails"].append(time.time())
                return self.send(403, {"detail": "wrong PIN"})
            PIN["until"] = time.time() + 600
            self.send(200, {"ok": True, "valid_until": PIN["until"]})
        elif p == "/api/tokens/rotate":
            if not b.get("confirm"):
                return self.send(400, {"detail": "confirmation required"})
            if self.need_step_up():
                return
            if not b.get("force"):
                return self.send(409, {"detail": "agents older than 2.3 would be locked out: forge-03"})
            now = time.time()
            for k in ("agent", "cluster"):
                TOKENS[k] = {"rotated": now, "grace_until": now + 600}
            self.send(200, {"ok": True, "rotated": ["agent", "cluster"], "grace_until": now + 600})
        elif p == "/api/notify/ntfy/topic":
            t = "hm-" + "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz234567") for _ in range(20))
            self.send(200, {"topic": t, "subscribe_url": f"{NOTIFY['ntfy']['server']}/{t}"})
        elif p == "/api/notify/test":
            res = {}
            if NOTIFY["ntfy"]["enabled"]:
                res["ntfy"] = "ok"
            res["webpush"] = {"sent": len(SUBS), "failed": 0}
            self.send(200, {"ok": True, "results": res})
        elif p == "/api/push/subscribe":
            if not b.get("endpoint") or not isinstance(b.get("keys"), dict):
                return self.send(400, {"detail": "endpoint and keys required"})
            SUBS.add(b["endpoint"])
            NOTIFY["webpush"]["devices"] = len(SUBS)
            self.send(200, {"ok": True, "id": hashlib.sha256(b["endpoint"].encode()).hexdigest()[:32]})
        elif p == "/api/maintenance/preview":
            a = next((m for m in MAINT if m["name"] == b.get("action")), None)
            if not a:
                return self.send(400, {"detail": "unknown action"})
            bad = maint_args_error(a, b.get("args"))
            if bad:
                return self.send(400, {"detail": bad})
            order, skipped = rollout_order(b.get("targets") or "all")
            self.send(200, {"order": order, "skipped": skipped, "destructive": a["destructive"], "needs_step_up": a["destructive"] or a["lane"] == "system",
                            "online": len(order)})
        elif p == "/api/maintenance/runs":
            a = next((m for m in MAINT if m["name"] == b.get("action")), None)
            if not a:
                return self.send(400, {"detail": "unknown action"})
            if not b.get("confirm"):
                return self.send(400, {"detail": "confirmation required"})
            bad = maint_args_error(a, b.get("args"))
            if bad:
                return self.send(400, {"detail": bad})
            if (a["destructive"] or a["lane"] == "system") and self.need_step_up():
                return
            order, _ = rollout_order(b.get("targets") or "all")
            if not order:
                return self.send(400, {"detail": "no online target"})
            rid = "run" + secrets.token_hex(5)
            now = time.time()
            RUNS[rid] = {"id": rid, "action": a["name"], "title": a["title"], "args": b.get("args") or {}, "targets": order, "idx": 0, "status": "running",
                         "current": None, "results": [], "source": "ui", "created": now, "started": now, "finished": None}
            self.send(200, {"id": rid, "order": order})
        elif re.fullmatch(r"/api/maintenance/runs/[^/]+/cancel", p):
            rid = p.split("/")[4]
            if rid not in RUNS:
                return self.send(404, {"detail": "no such run"})
            v = run_view(rid)
            RUNS[rid].update(status="cancelled", results=v["results"], current=None, finished=time.time())
            self.send(200, {"ok": True})
        elif re.fullmatch(r"/api/machines/[^/]+/wake", p):
            nid = p.split("/")[3]
            n = next((x for x in nodes_now() if x["id"] == nid), None)
            if not n:
                return self.send(404, {"detail": "no such node"})
            if n["online"]:
                return self.send(409, {"detail": "already online"})
            self.send(200, {"ok": True, "via": ["forge-02"], "mac": "3C-7C-3F-AA-BB-CC"})
        elif re.fullmatch(r"/api/machines/[^/]+/retire", p):
            nid = p.split("/")[3]
            if not any(x["id"] == nid for x in nodes_now()):
                return self.send(404, {"detail": "no such node"})
            if not b.get("confirm"):
                return self.send(400, {"detail": "confirmation required"})
            if self.need_step_up():
                return
            if nid in RETIRE and retire_view(nid)["state"] not in ("done", "failed"):
                return self.send(409, {"detail": "already retiring"})
            RETIRE[nid] = {"started": time.time(), "uninstall": bool(b.get("uninstall", True)), "rotate": bool(b.get("rotate_tokens"))}
            self.send(202, {"ok": True, "state": "draining"})
        elif p == "/mock/503":
            STATE["no_leader"] = True
            self.send(200, {})
        elif p == "/mock/ok":
            STATE["no_leader"] = False
            self.send(200, {})
        elif p == "/mock/reset-transcript":
            STATE["tx_reset_at"] = time.time()
            self.send(200, {"ok": True})
        elif p in ("/mock/empty", "/mock/full"):
            STATE["empty"] = p == "/mock/empty"
            self.send(200, {"ok": True})
        elif p in ("/mock/v22", "/mock/v23"):
            STATE["v22"] = p == "/mock/v22"
            self.send(200, {"ok": True})
        elif p == "/mock/nopin":
            PIN.update(hash=None, until=0.0)
            self.send(200, {"ok": True})
        else:
            self.send(404, {"detail": "Not Found"})

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
        if engine == "admin" and node_id == "auto":
            return self.send(400, {"detail": "admin jobs need a node_id"})
        destructive = engine == "action" and any(m["name"] == b.get("action") and m["destructive"] for m in MAINT)
        if (engine == "admin" or destructive) and self.need_step_up():
            return
        req_any = engine == "any"
        if req_any:
            engine = "codex" if "claude" in COOLDOWNS else "claude"
        resolved = "forge-01" if node_id == "auto" else node_id
        jid = "019ac3ff%010x" % int(time.time() * 1000)
        row = {"id": jid, "node_id": resolved, "requested": "auto" if node_id == "auto" else None, "engine": engine,
               "req_engine": "any" if req_any else None, "prompt": b.get("prompt") or b.get("action"), "action": b.get("action"),
               "cwd": b.get("cwd"), "full_access": bool(b.get("full_access")) and not system, "status": "queued", "created": time.time(),
               "started": None, "finished": None, "exit_code": None, "tail": "", "source": b.get("source") or "ui",
               "run_as": "system" if system else "user", "resume": res, "project_id": b.get("project_id"), "wait_reason": None,
               "notify": bool(b.get("notify"))}
        STATE["extra_jobs"].insert(0, row)
        self.send(200, {k: row[k] for k in ("id", "node_id", "engine", "run_as", "status", "wait_reason")})

    def do_DELETE(self):
        p = self.path.split("?")[0]
        parts = p.split("/")
        if self.v22_gone(p):
            return
        if p.startswith("/api/scheduler/cooldowns/") and len(parts) == 5:
            return self.send(200, {"ok": True, "cleared": COOLDOWNS.pop(parts[4], None) is not None})
        if p.startswith("/api/peers/") and len(parts) == 4 and parts[3] in STATE["priorities"]:
            STATE["priorities"].pop(parts[3])
            return self.send(200, {"ok": True})
        if p.startswith("/api/enroll/") and len(parts) == 4:
            if parts[3] not in ENROLL:
                return self.send(404, {"detail": "no such code"})
            ENROLL[parts[3]]["cancelled"] = True
            return self.send(200, {"ok": True})
        if p.startswith("/api/devices/") and len(parts) == 4:
            return self.send(200, {"ok": True}) if DEVICES.pop(parts[3], None) else self.send(404, {"detail": "no such device"})
        if p == "/api/push/subscribe":
            b = self.body()
            if b is None:
                return
            SUBS.discard(b.get("endpoint"))
            NOTIFY["webpush"]["devices"] = len(SUBS)
            return self.send(200, {"ok": True})
        if re.fullmatch(r"/api/machines/[^/]+/retire", p):
            nid = parts[3]
            if nid not in RETIRE or retire_view(nid)["state"] != "draining":
                return self.send(409, {"detail": "only a draining machine can be kept"})
            RETIRE.pop(nid)
            return self.send(200, {"ok": True})
        self.send(404, {"detail": "Not Found"})

    def do_PATCH(self):
        b = self.body()
        if b is None:
            return
        parts = self.path.split("?")[0].split("/")
        if len(parts) != 4 or parts[1] != "api" or not parts[3]:
            return self.send(404, {"detail": "Not Found"})
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
        if kind == "devices":
            d = DEVICES.get(oid)
            if not d:
                return self.send(404, {"detail": "no such device"})
            lbl = str(b.get("label") or "").strip()
            if not lbl or len(lbl) > 64:
                return self.send(400, {"detail": "label must be 1-64 characters"})
            d["label"] = lbl
            return self.send(200, d)
        self.send(404, {"detail": "Not Found"})

    def do_PUT(self):
        b = self.body()
        if b is None:
            return
        p = self.path.split("?")[0]
        if self.v22_gone(p):
            return
        if p.startswith("/api/nodes/") and p.endswith("/settings"):
            return self.send(200, {"ok": True})
        if p == "/api/stepup/pin":
            pin = str(b.get("pin") or "")
            if not re.fullmatch(r"\d{6}", pin):
                return self.send(400, {"detail": "the PIN must be exactly 6 digits"})
            if PIN["hash"] and PIN["until"] < time.time() and hashlib.sha256(str(b.get("old_pin") or "").encode()).hexdigest() != PIN["hash"]:
                return self.send(403, {"detail": "wrong PIN"})
            PIN["hash"] = hashlib.sha256(pin.encode()).hexdigest()
            return self.send(200, {"ok": True})
        if p == "/api/notify/settings":
            for k in ("ntfy", "deadman", "rules", "webpush"):
                if isinstance(b.get(k), dict):
                    upd = {x: y for x, y in b[k].items() if x not in ("subscribe_url", "vapid_public_key", "devices", "last_ping", "last_ok")}
                    if k == "ntfy" and upd.get("topic") and not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", str(upd["topic"])):
                        return self.send(400, {"detail": "bad topic"})
                    NOTIFY[k].update(upd)
            return self.send(200, notify_view())
        if p == "/api/upkeep/settings":
            for k in ("schedule", "windows_update"):
                if isinstance(b.get(k), dict):
                    UPKEEP[k].update(b[k])
            if isinstance(b.get("repo_sync_every_s"), int):
                UPKEEP["repo_sync_every_s"] = b["repo_sync_every_s"]
            return self.send(200, UPKEEP)
        m = re.fullmatch(r"/api/projects/([^/]+)/sync", p)
        if m:
            pid = m.group(1)
            if not any(y["id"] == pid for y in PROJ):
                return self.send(404, {"detail": "no such project"})
            nodes = b.get("nodes")
            if not isinstance(nodes, list) or not all(isinstance(x, str) for x in nodes):
                return self.send(400, {"detail": "nodes must be a list of node ids"})
            with LOCK:
                OVERRIDES.setdefault(pid, {})["sync_nodes"] = nodes
            build_projects()
            return self.send(200, {"ok": True, "sync_nodes": nodes})
        self.send(404, {"detail": "Not Found"})


def notify_view():
    n = dict(NOTIFY["ntfy"])
    n["subscribe_url"] = f"{n['server'].rstrip('/')}/{n['topic']}" if n.get("topic") else None
    return {"ntfy": n, "webpush": dict(NOTIFY["webpush"]), "deadman": dict(NOTIFY["deadman"]), "rules": dict(NOTIFY["rules"])}


def retire_view(nid):
    r = RETIRE[nid]
    t = time.time() - r["started"]
    st = "draining" if t < 8 else "uninstalling" if t < 14 and r["uninstall"] else "removing" if t < 18 else "done"
    return {"node_id": nid, "state": st, "started": r["started"], "finished": r["started"] + 18 if st == "done" else None,
            "job_id": "019ac3ffretire" if st in ("uninstalling", "removing", "done") and r["uninstall"] else None, "error": None}


if __name__ == "__main__":
    print(f"mock hub on http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
