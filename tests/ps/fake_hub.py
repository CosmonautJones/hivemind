"""Tiny fake hub for the watchdog dry run (tests/ps/dry-run.ps1). Never use port 8787.
usage: python -I fake_hub.py <port> <local_version> <peer_version> <peer_sha256>
  /healthz, /cluster/status, /cluster/bootstrap  -> the "local" hub (running local_version)
  /peer/cluster/status                           -> a healthy peer advertising peer_version
"""
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

port, local_ver, peer_ver, peer_sha = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
assert port != 8787


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
            return self._json({"node_id": "dryrun-node", "role": "standby", "version": local_ver,
                               "running_version": local_ver, "bundle_sha256": "localsha"})
        if p == "/cluster/bootstrap":
            return self._json({"peers": [{"id": "atlas", "priority": 2,
                                          "urls": [f"http://127.0.0.1:{port}/peer"]}]})
        if p == "/peer/cluster/status":
            return self._json({"node_id": "atlas", "role": "leader", "version": peer_ver,
                               "running_version": peer_ver, "bundle_sha256": peer_sha})
        self._json({"detail": "not found"}, 404)

    def log_message(self, *a):
        pass


HTTPServer(("127.0.0.1", port), H).serve_forever()
