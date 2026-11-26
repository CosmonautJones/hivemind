"""Fake hub for the agent smoke test (tests/ps/agent-smoke.ps1). Ports 19000-19299 only (never 8787).
usage: python -I fake_agent_hub.py <port> <work_root> <log_jsonl> [<session_id>]
Every report gets settings + hub_urls. Jobs are handed out one at a time (the next one only after the
previous one reported a final status):
  job-0001  shell  'hello from forge job', exit 3
  job-0002  claude resume {tool: claude, session_id}   (cwd null: the agent must use the session's own cwd)
  job-0003  ollama model tiny:1b, brief, kind text     (against the fake ollama REST server)
  job-0004  shell sleeping 60 s, timeout_s 3           (the agent must kill it and report failed)
  job-0005  shell part1 / sleep 10 / part2             (the smoke test kills the worker mid-job: re-attach)
With a session_id, every report response also carries watch: [{tool: claude, session_id, since: <last cursor>}]
(2.2 live transcript; the cursor is whatever the agent last POSTed to /api/agent/transcript).
Every POST body is appended to <log_jsonl>.
"""
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

port, work_root, log = int(sys.argv[1]), sys.argv[2], sys.argv[3]
sid = sys.argv[4] if len(sys.argv) > 4 else None
assert 19000 <= port <= 19299 and port != 8787
JOBS = [
    {"id": "job-0001", "engine": "shell", "prompt": "Write-Output 'hello from forge job'; exit 3", "cwd": None, "full_access": False},
    {"id": "job-0002", "engine": "claude", "prompt": "please continue the docs", "cwd": None, "full_access": False,
     "resume": {"tool": "claude", "session_id": sid}},
    {"id": "job-0003", "engine": "ollama", "model": "tiny:1b", "prompt": "Summarize: project is fine.", "cwd": None,
     "full_access": False, "source": "brief", "kind": "text"},
    {"id": "job-0004", "engine": "shell", "prompt": "Start-Sleep -Seconds 60; Write-Output never", "cwd": None,
     "full_access": False, "timeout_s": 3},
    {"id": "job-0005", "engine": "shell", "prompt": "Write-Output 'part1'; Start-Sleep -Seconds 10; Write-Output 'part2'",
     "cwd": None, "full_access": False},
]
if not sid:
    JOBS = JOBS[:1]
state = {"next": 0, "waiting": None, "cursor": None}


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"path": self.path, "token": self.headers.get("X-Agent-Token"), "body": body}) + "\n")
        resp = {"ok": True}
        if self.path.startswith("/api/agent/jobs/") and body.get("status") in ("done", "failed", "cancelled"):
            if state["waiting"] and self.path == f"/api/agent/jobs/{state['waiting']}/update":
                state["waiting"] = None
        if self.path == "/api/agent/transcript" and body.get("session_id") == sid and body.get("cursor") is not None:
            state["cursor"] = body["cursor"]
        if self.path == "/api/agent/report":
            jobs = []
            if state["waiting"] is None and state["next"] < len(JOBS) and int(body.get("capacity") or 0) > 0:
                j = JOBS[state["next"]]
                state["next"] += 1
                state["waiting"] = j["id"]
                jobs = [j]
            resp = {"jobs": jobs, "cancel": [], "need_info": False, "settings": {"work_root": work_root, "max_jobs": 3},
                    "hub_urls": [f"http://127.0.0.1:{port}", "http://peer-a.invalid:8787"], "bundle_version": "2.0.0"}
            if sid:
                resp["watch"] = [{"tool": "claude", "session_id": sid, "since": state["cursor"]}]
        b = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


HTTPServer(("127.0.0.1", port), H).serve_forever()
