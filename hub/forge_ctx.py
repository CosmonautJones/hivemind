"""Phase 3 (2.3.0) plug-in context shared by the workstream modules (see CONTRACT-phase3.md §2).

`hub/app.py` builds ONE HubContext per hub and passes it to every workstream module's `register(app, ctx)`:
forge_security (S), forge_reliability (R), forge_alerts (N), forge_upkeep (F), forge_ui (U).

Rules (CONTRACT-phase3.md §2.2):
- A module adds routes with `@app.get/post/...` inside `register`. app.py moves module routes IN FRONT of the core
  routes, so a module may replace a core route by registering the same method + path; only the overrides listed in the
  contract are allowed (e.g. S: /api/login, /api/logout, /api/enroll, /cluster/bootstrap, /agent/install.ps1).
- Behaviour that is not a route is changed through the hook slots below. app.py calls them; the owner workstream sets
  them. Slots are plain attributes / lists so tests can swap them.
- Hook callables in `on_*` / `alert_sources` lists must never raise into the caller: app.py wraps each call
  (`ctx.safe_call`), logs the exception to stderr and carries on.
- This file is owned by the tech lead. A workstream that needs a new slot adds it here in a small, separate hunk and
  documents it in CONTRACT-phase3.md (amendment section) in the same commit.
"""
from __future__ import annotations

import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from forge_store import Clock, Store


def _noop(*_a: Any, **_k: Any) -> None:
    return None


@dataclass
class HubContext:
    # ---- core objects (read-only for modules; never replace them) ----
    app: Any
    store: Store
    cluster: Any                     # forge_cluster.Cluster
    clock: Clock
    cfg: dict                        # merged hub config: data/config.json + data/private/secrets.json (S2)
    node_id: str                     # this hub's node id
    data_dir: Path                   # FORGE_DATA
    release_dir: Path                # FORGE_RELEASE_DIR (running release)
    releases_dir: Path               # FORGE_RELEASES_DIR
    releases: Any                    # forge_bundle.Releases
    lease: Any                       # forge_lease.UpdateLease
    p2: Any                          # forge_phase2.Phase2 (sched_lock, insert_job, place_jobs, ...)
    actions_catalog: dict            # hub/actions.json

    # ---- core helpers (set by app.py; call, do not replace) ----
    require_ui: Callable = _noop             # FastAPI dependency: UI auth (session cookie / legacy token)
    require_agent: Callable = _noop          # FastAPI dependency: X-Agent-Token
    require_cluster: Callable = _noop        # FastAPI dependency: X-Cluster-Token
    leader_json: Callable = _noop            # async (request) -> (forwarded Response | None, body dict)
    leader_only_or_forward: Callable = _noop # (request, raw bytes) -> Response | None (None = we are leader)
    node_settings: Callable = _noop          # (node_id) -> {work_root, max_jobs, canary}
    effective_settings: Callable = _noop     # (node_id) -> what agents receive in `settings`
    job_row: Callable = _noop                # (sqlite row, with_output: bool) -> dict (UI job shape)

    # ---- standby-local paths (the forwarding middleware answers these locally instead of proxying) ----
    local_paths: set = field(default_factory=set)
    local_prefixes: list = field(default_factory=list)

    # ---- S: security slots (owner S; defaults keep 2.2 behaviour) ----
    # ui_auth(request) -> principal dict ({"session_id", "label", ...}) or None (401). None slot = legacy ui_token check.
    ui_auth: Optional[Callable] = None
    # check_agent_token(token) -> bool / check_cluster_token(token) -> bool. None slot = compare with cfg values.
    check_agent_token: Optional[Callable] = None
    check_cluster_token: Optional[Callable] = None
    # require_step_up(request) -> None; raises HTTPException(403, {"detail", "step_up": "required"|"setup"}).
    # Default: no-op (2.2 behaviour). Callers: create_job (admin / destructive), R retire, F maintenance runs, S rotate.
    require_step_up: Callable = _noop
    # verify_bundle(release_files_dir: Path) -> None; raises forge_bundle.BundleError(msg, 400/403) on a bad signature.
    verify_bundle: Optional[Callable] = None

    # ---- R: reliability slots (owner R) ----
    # healthz() -> (status_code, body dict). None = 2.2 body {ok, node_id, role} with 200.
    healthz: Optional[Callable] = None
    # job_output(job_id) -> str and append_output(job_id, text, now) -> None (R4 chunk table). None = jobs.output column.
    job_output: Optional[Callable] = None
    append_output: Optional[Callable] = None
    # status_extras() -> dict merged into GET /cluster/status (R1 local_agent, R3 db).
    status_extras: list = field(default_factory=list)

    # ---- N: alert / notification slots (owner N; stubs are no-ops until N lands) ----
    # raise_alert(kind, *, key=None, node_id=None, level="warn", msg="", self_healing=False, data=None, action=None) -> id|None
    raise_alert: Callable = _noop
    # resolve_alert(key, *, by="auto") -> bool
    resolve_alert: Callable = _noop
    # notify(kind, title, body, *, node_id=None, job_id=None, url=None) -> None (rule-filtered, deduplicated by N)
    notify: Callable = _noop

    # ---- hook lists (any workstream appends; app.py calls each via safe_call) ----
    on_tick: list = field(default_factory=list)            # fn(now: float, settled: bool) on every hub, every tick
    on_report: list = field(default_factory=list)          # fn(node_id, body, resp, now): user-lane report; mutate resp
    on_system_report: list = field(default_factory=list)   # fn(node_id, body, resp, now): system-lane report
    on_job_finished: list = field(default_factory=list)    # fn(job: dict, status, output: str, now): done|failed|cancelled
    alert_sources: list = field(default_factory=list)      # fn(now) -> [ {kind, node_id, level, msg, ...} ] (GET /api/health)
    state_extras: list = field(default_factory=list)       # fn(state: dict, now) mutates GET /api/state response

    def safe_call(self, fn: Callable, *args: Any, **kw: Any) -> Any:
        try:
            return fn(*args, **kw)
        except Exception:  # a plug-in must never take down a report, a tick or a UI call
            print(f"[forge_ctx] hook {getattr(fn, '__module__', '?')}.{getattr(fn, '__name__', fn)} failed:\n"
                  + traceback.format_exc(), file=sys.stderr)
            return None

    def run_hooks(self, hooks: list, *args: Any) -> list:
        return [self.safe_call(fn, *args) for fn in list(hooks)]

    @property
    def is_leader(self) -> bool:
        return bool(self.cluster.is_leader)

    def settled_leader(self) -> bool:
        """Leader that is not contested (the only hub that may make fleet decisions)."""
        return bool(self.cluster.is_leader and not self.cluster.contested())

    def event(self, node_id: Optional[str], level: str, msg: str) -> None:
        self.store.event(node_id, level, msg)
