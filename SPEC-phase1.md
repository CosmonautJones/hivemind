# Hivemind — Phase 1 spec: resilient core

Goal: every machine runs the same bundle (hub + agent + watchdog). Exactly one hub is **leader**;
others are **standbys** that replicate state and forward requests. Any machine's
`http://localhost:8787` shows and controls the whole fleet. Failures self-heal; updates self-propagate.
KISS: deterministic priority-based election, pull replication, no consensus library.

Fleet today: forge-01 (always on, priority 1), atlas (2), nova (3), orbit (4).
All Windows. Python 3.10+ on every node. Tailscale MagicDNS names + LAN IPs.

## 1. Shared config (`data/config.json`, identical secrets on all nodes)
```json
{ "node_id": "forge-01",                  // this machine (lowercase COMPUTERNAME)
  "ui_token": "...", "agent_token": "...", "cluster_token": "...",
  "port": 8787 }
```
Peer list is NOT in config: it lives in the replicated `peers` table (below), seeded by the
installer via `POST /cluster/join`.

## 2. Data model changes (hub, SQLite)
- All replicated rows get `updated REAL` (epoch secs, set on every write) and use **TEXT ids**
  that are globally unique and time-sortable: `new_id() = f"{int(time.time()*1000):011x}{secrets.token_hex(3)}"`.
  Jobs and events switch to TEXT ids. (Fresh DB is fine; no migration of v1 data needed.)
- New table `peers(id TEXT PK, priority INTEGER, urls TEXT /*json list, tried in order*/, version TEXT,
  role TEXT, last_ok REAL, updated REAL)`.
- New table `settings(node_id TEXT PK, work_root TEXT NULL, max_jobs INTEGER NULL, updated REAL)` —
  per-node overrides set from UI; returned to agents.
- Replicated tables: `peers, nodes, jobs, events, settings`. NOT replicated: `metrics`, `logs`
  (ephemeral; agents refill within seconds).

## 3. Election (each hub, every 3 s)
- Probe every peer URL list (first URL answering wins) `GET /cluster/status` (header
  `X-Cluster-Token`), 2 s timeout. A peer is **healthy** if its last 2 probes succeeded.
- A hub is **eligible** only after it has done one successful full sync from the current leader
  (or found no healthy peer claiming leadership for 15 s after start).
- **Leader = the eligible, healthy hub with the lowest priority number** (self counts as healthy).
  Every hub computes this independently from the same replicated peer list → same answer.
- Step-down: a leader that sees a lower-priority-number peer healthy AND eligible for ≥ 20 s
  becomes standby (prevents flapping when forge-01 returns; forge-01 must sync first).
- `/cluster/status` → `{node_id, role: "leader"|"standby"|"starting", leader_id, eligible,
  version, bundle_sha256, sync_cursor, now}`.

## 4. Replication (standby pulls from leader every 3 s)
- `GET /cluster/sync?since=<float>` (cluster token) → `{now, tables: {peers:[rows], nodes:[...],
  jobs:[...], events:[...], settings:[...]}}` rows with `updated > since`. Cap 2000 rows/table/call;
  standby loops until caught up. Standby upserts rows where incoming `updated` >= local `updated`.
- `sync_cursor` = leader's `now` from the last successful sync.

## 5. Forwarding (KISS: talk to localhost, it finds the leader)
- On a standby, every request EXCEPT `/cluster/*`, `/healthz`, `/` and static assets is reverse-proxied
  to the leader (httpx, 10 s timeout) preserving method, path, query, body, auth headers/cookies;
  add `X-Forwarded-For` + `X-Forge-Via: <node_id>`. The leader uses X-Forwarded-For for `nodes.addr`.
- If no leader is reachable: return 503 `{detail:"no leader", role, peers:[...]}`.
- UI is served by every hub (so localhost works); its API calls get forwarded.
- `/api/state` response adds `cluster: {leader_id, viewing: <node_id of hub serving page>,
  peers: [{id, priority, role, version, healthy, last_ok, lag_s}]}`.

## 6. Agent ↔ hub (changes from v1)
- Agent `hub_urls`: `["http://127.0.0.1:8787", <peer urls...>]` — tries in order, sticks with the
  first that answers, re-tries localhost every 60 s. (Peer urls refreshed from report response.)
- Report response adds: `settings: {work_root, max_jobs}` (effective values; null = agent default),
  `hub_urls: [...]` (all peer URLs in priority order), `bundle_version`.
- Agent picks **work_root** = `<drive with most free space among fixed drives>:\forge` unless
  settings.work_root set. Subdirs: `jobs\`, `repos\`, `scratch\`. Default job cwd = work_root\scratch.
  Agent reports `metrics.work_root` and `metrics.work_root_free_gb`.
- Agent no longer self-updates its own script; the watchdog updates the whole bundle (§8).
  (v1.x agents in the field still self-update to the transitional 1.2 script, which is the v2 agent.)
- Job `node_id` may be `"auto"` at creation: leader resolves at dispatch time to the online node with
  free capacity that has the engine in `info.tools` (shell = all), lowest `metrics.cpu`; tie → most
  free capacity → priority. Job row stores the resolved node_id and `requested: "auto"`.
- Jobs on a node that goes offline for > 90 s while `starting|running` → `failed` with note
  "[hub] node offline; job lost" (Phase 2 adds retry/requeue).

## 7. Cluster join / bootstrap endpoints (on any hub; forwarded to leader if standby)
- `GET /cluster/bootstrap?t=<agent_token>` → `{ui_token, agent_token, cluster_token, peers:[...],
  bundle_version, bundle_sha256}` (lets a new node become a full member).
- `POST /cluster/join` (cluster token) `{id, urls}` → upsert peer; priority = existing or max+1.
- `PATCH /api/peers/{id}` (UI) `{priority}` — reorder; `DELETE /api/peers/{id}`.
- `PUT /api/nodes/{id}/settings` (UI) `{work_root?, max_jobs?}`.

## 8. Bundle + watchdog (self-update, self-heal)
- Repo root `VERSION` file (semver). Bundle = zip of `hub/ agent/ watchdog/ requirements.txt VERSION`.
- Install layout per node: `%ProgramData%\Forge\` (machine-wide, owned by Administrators+user):
  `releases\<ver>\` (extracted bundles), `current` → junction to active release, `data\` (config, db,
  logs), `venv\`. Hub runs from `current\hub` with `--app-dir`, data dir via `FORGE_DATA`.
- `GET /cluster/bundle` → zip of the serving hub's current release; `bundle_sha256` in status.
- `POST /cluster/publish` (cluster token, body = zip) → hub validates (zip contains VERSION > its own,
  hub/app.py compiles), stores as new release, switches `current`, and its watchdog restarts it.
  Any node can publish to any hub; versions propagate because every watchdog upgrades to the highest
  version seen among healthy peers.
- **watchdog.ps1** — scheduled task as SYSTEM every 1 min (and at startup):
  1. Hub: if `GET localhost:8787/healthz` fails twice in a row → kill whatever listens on 8787,
     restart task `ForgeHub`. Agent: if task `ForgeAgent` not running or `agent.log` not written in
     2 min → restart it.
  2. Update: query peers' `/cluster/status`; if a healthy peer has higher `version`, download its
     bundle, verify sha256, extract to `releases\<ver>`, pip install -r requirements (if changed),
     flip `current`, restart hub+agent. Keep last 3 releases.
  3. Rollback: if after an update local healthz fails for 3 consecutive runs → flip `current` back,
     restart, and mark that version bad in `data\bad-versions.txt` (never auto-installed again).
  4. Writes `data\watchdog.log` (rotated 5 MB) and `data\watchdog.json` {last_run, actions[]}, which
     the agent ships as a log and as `metrics.watchdog`.
- Tasks (all registered by installer): `ForgeHub` (S4U user, at startup, runs `current\run-hub.cmd`),
  `ForgeAgent` (S4U user, at startup), `ForgeWatchdog` (SYSTEM, every minute + at startup).
  Old v1 tasks `ForgeControl-Hub` / `ForgeControl-Agent` are removed by the installer.

## 9. Installer v2 (`agent/install.ps1`, still the one-liner from the UI, elevated)
1. Require admin. Ensure Python ≥3.10 (winget `Python.Python.3.12` machine scope if missing).
2. `GET /cluster/bootstrap` → write `data\config.json` (node_id = this machine), download + verify
   bundle → `releases\<ver>`, junction `current`, create venv, pip install.
3. Firewall rule 8787 inbound from LocalSubnet + 100.64.0.0/10. powercfg no sleep on AC.
4. Tailscale: if not installed → winget install machine scope; if not logged in → print
   "Sign in to Tailscale: <url>" (run `tailscale up --unattended`), do not block.
5. `POST /cluster/join` with urls `[http://<tailscale magicdns name>:8787, http://<ts ip>:8787,
   http://<lan ip>:8787]` (only those that exist).
6. Register the 3 tasks, remove v1 tasks/dirs (`%USERPROFILE%\forge-agent` → keep jobs history
   folder but stop using it), start everything, print the localhost URL and create a desktop
   shortcut "Hivemind" → http://localhost:8787.
Idempotent: re-running upgrades/repairs.

## 10. UI (KISS)
Three tabs: **Fleet · Jobs · Health**. Header: fleet name, `Leader: forge-01` pill, "viewing via
<node>" if forwarded, online count, active jobs, theme toggle, "+ Add machine".
- Fleet: machine cards (as v1, plus role badge leader/standby, work_root drive + free GB, alert
  chips: disk ≥ 90 %, offline, version behind). One "Run task" box: Machine select defaults to
  **Auto (best available)**; engine; prompt; advanced (collapsed): cwd, full access.
- Jobs: table + filters (machine, status, engine), live output modal (as v1), cancel.
- Health: cluster table (node, priority ↑↓ buttons, role, version, healthy, sync lag, watchdog last
  run + last action), alerts list, current bundle version, update rollout status per node.
- Machine drawer gains a **Settings** tab: work root (text, placeholder shows auto choice), max jobs.
- Mobile-first responsive; no build step; vanilla JS; keep a11y basics.

## 11. Tests (pytest, fast, deterministic, AAA)
- Election: 3 in-process hubs (TestClient + fake transport between them): leader = lowest healthy
  eligible; leader killed → next promotes within ≤ 3 probe rounds; returning priority-1 node syncs
  then takes over after hold-down; no two leaders after convergence.
- Replication: rows created on leader appear on standby; conflict uses newer `updated`.
- Forwarding: standby proxies /api/state and POST /api/jobs to leader; 503 when no leader.
- Auto target resolution; offline-node job failure; settings round-trip; bootstrap/join auth.
- Bundle: publish rejects lower/equal version and non-compiling app; status sha matches.
PowerShell: `Parser.ParseFile` clean for all .ps1; Pester tests if Pester 5 is available for the pure
functions (work-root picker, hub URL fallback order, version compare).
