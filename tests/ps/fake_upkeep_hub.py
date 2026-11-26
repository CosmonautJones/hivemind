"""Fake hub for the workstream F agent smoke test (tests/ps/p3-upkeep-smoke.ps1). F's ports 19250-19279 only.
usage: python -I fake_upkeep_hub.py <port> <repo_cwd> <log_jsonl> <wol_port>
User-lane report responses hand out one job per report (while the agent has capacity):
  u-git   engine action git_sync {cwd: <repo_cwd>} run_as user -> runs for real on a scratch repo, ##RESULT uptodate
  u-bad   engine action git_sync {cwd: "x"} run_as user           -> the agent must refuse (defense in depth)
  u-sys   engine action reboot run_as system                       -> wrong lane, the agent must refuse
The first report response also carries wol: [{mac, broadcast: 127.0.0.1, port: <wol_port>}] (the test listens there).
GET /api/tags (fake ollama model list) and GET /health (a healthy keep-alive service). Every POST is logged.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

port, repo, log, wol_port = int(sys.argv[1]), sys.argv[2], sys.argv[3], int(sys.argv[4])
assert (19250 <= port <= 19279 and 19250 <= wol_port <= 19279) or (19280 <= port <= 19299 and 19280 <= wol_port <= 19299)   # F range, or the lead integration range
QUEUE = [
    {"id": "u-git", "engine": "action", "run_as": "user", "action": "git_sync", "args": {"cwd": repo},
     "prompt": "git_sync", "cwd": None, "full_access": False, "attempt": 0},
    {"id": "u-bad", "engine": "action", "run_as": "user", "action": "git_sync", "args": {"cwd": "x"},
     "prompt": "git_sync", "cwd": None, "full_access": False, "attempt": 0},
    {"id": "u-sys", "engine": "action", "run_as": "system", "action": "reboot", "args": {},
     "prompt": "reboot", "cwd": None, "full_access": False, "attempt": 0},
]
STATE = {"wol_sent": False}


class H(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/api/tags":
            return self._send({"models": [{"name": "qwen2.5:14b"}]})
        if self.path == "/health":
            return self._send({"ok": True})
        return self._send({"detail": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"path": self.path, "body": body}) + "\n")
        resp = {"ok": True}
        if self.path == "/api/agent/report":
            jobs = []
            if QUEUE and int(body.get("capacity") or 0) > 0 and not body.get("boot"):
                jobs = [QUEUE.pop(0)]
            resp = {"jobs": jobs, "cancel": [], "settings": {"work_root": None, "max_jobs": 2},
                    "hub_urls": [f"http://127.0.0.1:{port}"], "bundle_version": "2.3.0", "need_info": False}
            if not STATE["wol_sent"]:
                STATE["wol_sent"] = True
                resp["wol"] = [{"mac": "AA-BB-CC-DD-EE-01", "broadcast": "127.0.0.1", "port": wol_port}]
        self._send(resp)

    def log_message(self, *a):
        pass


HTTPServer(("127.0.0.1", port), H).serve_forever()
