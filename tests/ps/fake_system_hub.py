"""Fake hub for the SYSTEM-lane smoke test (tests/ps/agent-system-smoke.ps1). Ports 19000-19099 only.
usage: python -I fake_system_hub.py <port> <work_root> <log_jsonl>
Hands the system-lane agent one job per report (while it has capacity):
  sys-act   engine action winget_install {id: Git.Git}         -> agent dry-run prints the argv, exit 0
  sys-bad   engine action with args the HUB would reject       -> agent must refuse (defense in depth)
  sys-adm   engine admin  'Write-Output "admin-ok $env:USERNAME"; exit 4'
  sys-usr   engine shell (user lane only)                      -> agent must refuse
Every POST body is appended to <log_jsonl>.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

port, work_root, log = int(sys.argv[1]), sys.argv[2], sys.argv[3]
assert 19000 <= port <= 19099
QUEUE = [
    {"id": "sys-act", "engine": "action", "run_as": "system", "action": "winget_install", "args": {"id": "Git.Git"},
     "prompt": "winget_install", "cwd": None, "full_access": False},
    {"id": "sys-bad", "engine": "action", "run_as": "system", "action": "winget_install",
     "args": {"id": "Git.Git & calc"}, "prompt": "x", "cwd": None, "full_access": False},
    {"id": "sys-adm", "engine": "admin", "run_as": "system", "action": None, "args": {},
     "prompt": "Write-Output \"admin-ok $env:USERNAME\"; exit 4", "cwd": None, "full_access": False},
    {"id": "sys-usr", "engine": "shell", "prompt": "Write-Output 'should not run'", "cwd": None, "full_access": False},
]


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"path": self.path, "token": self.headers.get("X-Agent-Token"), "body": body}) + "\n")
        resp = {"ok": True}
        if self.path == "/api/agent/report":
            jobs = []
            if QUEUE and int(body.get("capacity") or 0) > 0:
                jobs = [QUEUE.pop(0)]
            resp = {"jobs": jobs, "cancel": [], "lane": "system", "settings": {"work_root": work_root, "max_jobs": 5},
                    "hub_urls": [f"http://127.0.0.1:{port}", "http://peer-a.invalid:8787"], "bundle_version": "2.1.0"}
        b = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


HTTPServer(("127.0.0.1", port), H).serve_forever()
