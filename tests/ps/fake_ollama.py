"""Fake ollama REST server for the agent smoke test. Ports 19000-19099 only.
usage: python -I fake_ollama.py <port> <log_jsonl>
  GET  /api/version   {"version": "0.0.0-fake"}            (keep-alive service health)
  GET  /api/tags      no models until a pull happened, then [{name: <pulled>}]
  POST /api/pull      NDJSON progress stream (connection closed at the end), then "success"
  POST /api/generate  {"response": "Brief: all good <check mark>", "done": true, "eval_count": 7}
Every request is appended to <log_jsonl>.
"""
import json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

port, log = int(sys.argv[1]), sys.argv[2]
assert 19000 <= port <= 19099
models = []


class H(BaseHTTPRequestHandler):
    def _log(self, body=None):
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"method": self.command, "path": self.path, "body": body}) + "\n")

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._log()
        if self.path == "/api/version":
            return self._json({"version": "0.0.0-fake"})
        if self.path == "/api/tags":
            return self._json({"models": [{"name": m, "model": m} for m in models]})
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        self._log(body)
        if self.path == "/api/pull":
            name = body.get("model") or body.get("name")
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            msgs = [{"status": "pulling manifest"}]
            msgs += [{"status": "pulling 0123abcd", "digest": "sha256:0123abcd", "total": 4000000, "completed": c}
                     for c in (0, 500000, 1000000, 2500000, 4000000)]
            msgs += [{"status": "verifying sha256 digest"}, {"status": "writing manifest"}, {"status": "success"}]
            for m in msgs:
                self.wfile.write((json.dumps(m) + "\n").encode())
                self.wfile.flush()
                time.sleep(0.05)
            models.append(name)
            self.close_connection = True
            return
        if self.path == "/api/generate":
            if body.get("model") not in models:
                return self._json({"error": f"model '{body.get('model')}' not found"}, 404)
            return self._json({"model": body["model"], "response": "Brief: all good ✓", "done": True, "eval_count": 7})
        self._json({"error": "not found"}, 404)

    def log_message(self, *a):
        pass


ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
