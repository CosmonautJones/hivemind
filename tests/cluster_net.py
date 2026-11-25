"""Test harness: several in-process hubs talking through a fake httpx transport and a fake clock."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi.testclient import TestClient

from app import create_app

TOKENS = {"ui_token": "ui-tok", "agent_token": "agent-tok", "cluster_token": "cluster-tok"}


class FakeClock:
    def __init__(self, t: float = 1_700_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, secs: float) -> None:
        self.t += secs


class Net:
    """Routes http://<name>:8787 requests to in-process apps; `down` hosts refuse connections."""

    def __init__(self) -> None:
        self.apps: dict[str, object] = {}
        self.down: set[str] = set()
        self.log: list[tuple[str, str]] = []

    def factory(self):
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host in self.down or host not in self.apps:
            raise httpx.ConnectError(f"{host} unreachable", request=request)
        self.log.append((host, request.url.path))
        headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
        client = TestClient(self.apps[host], raise_server_exceptions=False)
        r = client.request(request.method, request.url.raw_path.decode(), headers=headers, content=request.content)
        return httpx.Response(r.status_code, headers=[(k, v) for k, v in r.headers.multi_items()
                                                       if k.lower() not in ("content-length", "content-encoding")],
                              content=r.content)


class Fleet:
    """Hubs a(1) b(2) c(3) sharing tokens and a seeded peer list."""

    def __init__(self, tmp: Path, names=("a", "b", "c")):
        self.tmp, self.names = tmp, names
        self.clock, self.net = FakeClock(), Net()
        self.hubs: dict[str, object] = {}
        for n in names:
            self.start(n)

    def start(self, name: str, fresh: bool = True):
        """(Re)start a hub with an empty data dir, as if the machine came back after a rebuild/reboot."""
        d = self.tmp / f"{name}-{len(self.net.log)}-{int(self.clock.t)}"
        d.mkdir(parents=True, exist_ok=True)
        seeds = [{"id": n, "priority": i + 1, "urls": [f"http://{n}:8787"]} for i, n in enumerate(self.names)]
        (d / "config.json").write_text(json.dumps(TOKENS | {"node_id": name, "seed_peers": seeds}), encoding="utf-8")
        app = create_app(d, start_monitor=False, clock=self.clock, client_factory=self.net.factory,
                         releases_dir=d / "releases")
        self.hubs[name] = app
        self.net.apps[name] = app
        self.net.down.discard(name)
        return app

    def kill(self, name: str) -> None:
        self.net.down.add(name)

    def live(self):
        return [n for n in self.names if n not in self.net.down]

    def round(self, secs: float = 3.0) -> None:
        self.clock.advance(secs)
        for n in self.live():
            self.hubs[n].state.tick()

    def converge(self, rounds: int = 12) -> None:
        for _ in range(rounds):
            self.round()

    def leaders(self) -> list[str]:
        return [n for n in self.live() if self.hubs[n].state.cluster.is_leader]

    def client(self, name: str) -> TestClient:
        return TestClient(self.hubs[name], raise_server_exceptions=False)

    ui = {"Authorization": f"Bearer {TOKENS['ui_token']}"}
    agent = {"X-Agent-Token": TOKENS["agent_token"]}
    cluster = {"X-Cluster-Token": TOKENS["cluster_token"]}
