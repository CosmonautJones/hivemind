"""Tiny fake hub for the watchdog dry run (tests/ps/dry-run.ps1). Ports 19000-19299 only (never 8787).
usage: python -I fake_hub.py <port> <local_version> <peer_version> <peer_sha256>
  /healthz, /cluster/status, /cluster/bootstrap  -> the "local" hub (running local_version; a pre-2.2 hub:
                                                    no /cluster/update-lease -> 404)
  /peer/cluster/status                           -> a healthy LEADER advertising peer_version, bad_versions ["9.9.9"]
  /bad/cluster/status                            -> a standby advertising 9.9.9 (fleet-bad: must never be picked)
  POST /peer/cluster/update-lease {node_id, version} -> granted if node_id starts with "grant", else denied (holder canary-1)
  DELETE /peer/cluster/update-lease/<node>       -> {ok: true}
  POST /peer/cluster/bad-version {version}      -> {ok, bad_versions}
  POST /broken/cluster/update-lease              -> 500 (a leader that answers with an error)
"""
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

port, local_ver, peer_ver, peer_sha = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
assert 19000 <= port <= 19299 and port != 8787


class H(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/healthz":
            return self._json({"ok": True, "node_id": "dryrun-node", "role": "standby"})
        if p == "/cluster/status":
            return self._json({"node_id": "dryrun-node", "role": "standby", "leader_id": "atlas", "version": local_ver,
                               "running_version": local_ver, "bundle_sha256": "localsha"})
        if p == "/cluster/bootstrap":
            return self._json({"peers": [{"id": "atlas", "priority": 2, "urls": [f"http://127.0.0.1:{port}/peer"]},
                                         {"id": "nova", "priority": 3, "urls": [f"http://127.0.0.1:{port}/bad"]}]})
        if p == "/peer/cluster/status":
            return self._json({"node_id": "atlas", "role": "leader", "version": peer_ver, "bad_versions": ["9.9.9"],
                               "running_version": peer_ver, "bundle_sha256": peer_sha})
        if p == "/bad/cluster/status":
            return self._json({"node_id": "nova", "role": "standby", "version": "9.9.9",
                               "running_version": "9.9.9", "bundle_sha256": "cd" * 32})
        self._json({"detail": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/peer/cluster/update-lease" and self.headers.get("X-Cluster-Token"):
            if str(body.get("node_id", "")).startswith("grant"):
                return self._json({"granted": True, "holder": body["node_id"], "retry_after_s": 0})
            return self._json({"granted": False, "holder": "canary-1", "retry_after_s": 60})
        if self.path == "/broken/cluster/update-lease":
            return self._json({"detail": "lease bug"}, 500)
        if self.path == "/peer/cluster/bad-version" and body.get("version"):
            return self._json({"ok": True, "bad_versions": ["9.9.9", body["version"]]})
        self._json({"detail": "not found"}, 404)

    def do_DELETE(self):
        if self.path.startswith("/peer/cluster/update-lease/"):
            return self._json({"ok": True})
        self._json({"detail": "not found"}, 404)

    def log_message(self, *a):
        pass


HTTPServer(("127.0.0.1", port), H).serve_forever()
