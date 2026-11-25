"""Fake hub for the agent smoke test (tests/ps/agent-smoke.ps1). Never port 8787.
usage: python -I fake_agent_hub.py <port> <work_root> <log_jsonl>
First report gets one shell job (id "job-0001"); every report gets settings + hub_urls.
Every POST body is appended to <log_jsonl>.
"""
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

port, work_root, log = int(sys.argv[1]), sys.argv[2], sys.argv[3]
assert port != 8787
state = {"sent": False}


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"path": self.path, "token": self.headers.get("X-Agent-Token"), "body": body}) + "\n")
        resp = {"ok": True}
        if self.path == "/api/agent/report":
            jobs = []
            if not state["sent"]:
                state["sent"] = True
                jobs = [{"id": "job-0001", "engine": "shell", "prompt": "Write-Output 'hello from forge job'; exit 3",
                         "cwd": None, "full_access": False}]
            resp = {"jobs": jobs, "cancel": [], "need_info": False, "settings": {"work_root": work_root, "max_jobs": 3},
                    "hub_urls": [f"http://127.0.0.1:{port}", "http://peer-a.invalid:8787"], "bundle_version": "2.0.0"}
        b = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


HTTPServer(("127.0.0.1", port), H).serve_forever()
