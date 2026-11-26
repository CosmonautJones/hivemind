# Hivemind 2.3.0 — Phase 3 contract (S · R · N · U · F)

Status: **binding** for the five Phase 3 workstreams. Requirements: `SPEC-phase3.md`. Base: 2.2.x
(`HUB-CONTRACT.md`, `NODE-README.md`). Anything not listed here keeps its 2.2 behaviour.
The contract commit (branch `p3-contract`) already contains: `VERSION` = 2.3.0, `$AgentVersion` = 2.3.0,
`$WatchdogVersion` = 2.3.0, `hub/forge_ctx.py`, five empty hub modules wired into `hub/app.py`, the **whole 2.3
schema** in `hub/forge_store.py`, and PowerShell stub files wired into the agent and watchdog (the stubs keep 2.2 behaviour).
`tests/test_lease.py` now pins the fleet's starting version to 2.2.0 (its rollout target is 2.3.0).

Changing the contract: add a short entry to §10 "Amendments" in the same commit as the code, and tell the other teams.
Never change another team's endpoint shape without that team's agreement.

---------------------------------------------------------------------------------------------------------------------

## 0. Ground rules (all teams)

| Rule | Detail |
|---|---|
| Live fleet | Never touch `C:\ProgramData\Forge`, `C:\Users\you\forge-hub` (main checkout), port 8787, scheduled tasks, firewall rules, `~/.claude`, `~/.codex`, `~/.t3`, Ollama or other machines. Never elevate. The installer and watchdog run only with `-DryRun` or against a scratch `-ForgeHome`. |
| Test ports | Only your range (§9). The existing suites keep their old ports (19050-19053, 19080, and the 19000/19099 fixtures). |
| Python | 3.10 compatible (no `typing.Self`, no `datetime.UTC`, no 3.11+ stdlib). Venv: `C:\Users\you\forge-hub\.venv\Scripts\python.exe`. Pin any new dependency exactly (`==`) in `requirements.txt` and install it into that venv. Only N is expected to add one (Web Push: `cryptography`, optionally `pywebpush`). S verifies RSA signatures in pure Python (`pow(sig, e, n)` + PKCS#1 v1.5 padding check). |
| PowerShell | Must run on 5.1: return one-element or empty arrays as `, @()`; use `ConvertTo-Json -InputObject`; no ternary, `??` or `&&`. `Set-Content -Encoding UTF8` writes a BOM, so write JSON that other tools read with `[IO.File]::WriteAllText(p, s, (New-Object Text.UTF8Encoding($false)))`. |
| Errors | Hub errors are `{"detail": "<message>"}`, plus the extra fields listed per endpoint. 400 bad input, 401 auth, 403 step-up / forbidden, 404 unknown id, 409 conflict / wrong state, 422 malformed JSON or version, 429 rate limit, 503 no leader / leader contested / unhealthy. |
| Leader rule | Fleet decisions (alerts, notifications, maintenance runs, repo sync, Windows Update slots, backups, retire) run only where `ctx.settled_leader()` is true. UI and agent writes reach the leader anyway, because standbys forward them. |
| Mixed fleet | A 2.2 agent must work against a 2.3 hub. A 2.3 standby must work against a 2.2 leader during rollout (the leader updates last). Every new report or response field is optional, and every new table may be missing on the other side. |
| Tests | pytest (Arrange/Act/Assert, deterministic, fake clock via `create_app(clock=...)`) plus the PS suites under `tests\ps`. Each team adds `tests/test_p3_<team>.py` and/or `tests\ps\p3-<team>.ps1` and keeps every existing suite green. |
| Commits | Use the commit identity given in the workstream brief. Every message ends with a blank line + `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. |

## 1. File ownership map

**own** = only this team edits the file. **hook** = other teams may make only the small edits listed in §1.2.

### 1.1 Owned files

| Team | Owns (create / edit freely) |
|---|---|
| **S** security | `hub/forge_security.py`; `agent/jobtoken.ps1`; `agent/secrets.ps1`; `watchdog/verify-bundle.ps1`; `tools/new-signing-key.ps1`; `tools/build-bundle.ps1` and `tools/publish.ps1` (`-Sign`); `hub/forge_bundle.py` (signature handling, `bundle.sig` in zips); `tests/test_p3_security.py`, `tests/ps/p3-security.ps1` |
| **R** reliability | `hub/forge_reliability.py`; `hub/forge_cluster.py` (re-announce, quick_check eligibility); `watchdog/wd-launcher.ps1`; `watchdog/agent-loop.cmd` (new); `watchdog/run-hub.cmd`; **primary owner** of `agent/forge-agent.ps1`, `watchdog/watchdog.ps1` and `hub/app.py`; job output + presence in `hub/forge_phase2.py`; `tests/test_p3_reliability.py`, `tests/ps/p3-reliability.ps1`; chaos-sim extensions in `tests/test_chaos.py` |
| **N** notifications | `hub/forge_alerts.py`; the alert computation moved out of `health_report` (hook A-N1); `tests/test_p3_alerts.py` |
| **U** UX | `hub/static/*` (incl. new `hub/static/icons/*`, `hub/static/sw.js`, optional split JS/CSS), `tools/mock_ui_server.py`, `hub/forge_ui.py`; `tests/test_ui_contract.py`, `tests/test_ui_logic_js.py`, `tests/js/*`, `tests/test_mock_ui_server.py` |
| **F** first run + upkeep | `hub/forge_upkeep.py`; `agent/install.ps1`; `agent/upkeep.ps1`; additions to `hub/actions.json` and `agent/actions.ps1`; `agent/sessions.ps1`; `agent/services.ps1`; `agent/ollama-job.ps1`; the project-key mapping in `hub/forge_projects.py` (F6a); installer parts of `tests/ps/dry-run.ps1`; `tests/test_p3_upkeep.py`, `tests/ps/p3-upkeep.ps1` |
| **Lead** | `CONTRACT-phase3.md`, `hub/forge_ctx.py`, `hub/forge_store.py` (schema blocks: each team edits only its own block, §3), `VERSION`. Final merge of `HUB-CONTRACT.md` / `NODE-README.md`: each team writes its doc section in its PR description and the lead merges the docs. |

### 1.2 Shared files: primary owner + allowed hook points

**`hub/app.py`** (primary owner R; the lead already wired the ctx). Other teams may make ONLY these edits:

| Id | Team | Edit |
|---|---|---|
| A-S1 | S | `load_config`: also read `data/private/secrets.json` and merge it (S2); migrate the tokens out of `config.json`. Choose the DB path (`data/private/forge.db` when `data/private` exists, §4.1). |
| A-S2 | S | `LOCAL_PATHS`: remove `/api/enroll` (it must reach the leader), add `/api/claim`. |
| A-R1 | R | `lose_job`, `_job_update`, `_job_row`, `get_job`: output through `ctx.append_output` / `ctx.job_output` (R4) |
| A-R2 | R | `agent_report`, `state`, `node_detail`, `check_presence`: read and write `presence` (R4) |
| A-R3 | R | `_dispatch`: skip nodes whose `settings.drain = 1` (R6) |
| A-N1 | N | `health_report`: replace the inline alert list with `forge_alerts.current_conditions(ctx, now)` (same output shape plus the new fields) |
| A-N2 | N | `JobIn`: add `notify: bool = False`; `create_job`: pass `notify` through to `insert_job` |
| A-F1 | F | `create_job` action branch: actions with `"lane": "user"` run in the user lane (`run_as` user; `node_id` may be `auto`) |
| A-F2 | F | `state`, `list_jobs`, `node_detail`: hide `source: "sync"` jobs unless `include_sync=1` (same pattern as briefs) |
| A-F3 | F | `insert_job` call: accept an internal `run_id` (set only by maintenance runs, never accepted from the UI) |

U makes no edits to `app.py`. Its routes live in `forge_ui.py`. Everything else goes into your module through
`register(app, ctx)` and the ctx hooks (§2). Step-up for admin/destructive jobs is already wired: `create_job` calls
`ctx.require_step_up(request)`.

**`hub/forge_store.py`** (primary owner: lead). Each team edits only its block in `SCHEMA_P3`, its lines in `MIGRATIONS`
and its entries in `REPLICATED` (§3). R may add helper methods (`append_chunk`, `delete_job_cascade`) at the end of `Store`.

**`hub/forge_phase2.py`** (primary owner R for job output and presence). N: add `self.digest_extras: list = []` in
`__init__`, and `_rollup_prompt` appends the text of each `digest_extras` callable (N4). F and N: `insert_job` gains the
`run_id` and `notify` keywords (one keyword each; land them in one small commit).

**`agent/forge-agent.ps1`** (primary owner R). The lead already wired these lines; do not move them:
dot-source of `jobtoken.ps1` / `secrets.ps1` / `upkeep.ps1` (`$HasP3Helpers`); the `Start-ForgeJobProcess` call in `Start-ForgeJob`;
`Add-JobTokenReport $body; Add-UpkeepReport $body` in the user-lane report; `Update-AgentTokenFromResponse` and
`Invoke-UpkeepResponse` in `Apply-HubResponse`. Other teams may add only:

| Id | Team | Edit |
|---|---|---|
| G-S1 | S | call `Initialize-JobToken` once at worker start (user lane) and log its result in the start line |
| G-F1 | F | the user lane loads `actions.ps1` and the catalog too; `Test-JobLane` accepts engine `action` with `run_as: "user"` (F2/F3) |
| G-F2 | F | services: pass `work_root` to `Get-ForgeServices` so the ollama service gets `OLLAMA_MODELS` (F6b) |

**`watchdog/watchdog.ps1`** (primary owner R). Already wired: dot-source of `verify-bundle.ps1`, `Merge-WdSecrets` on the config,
`Test-BundleSignature` before activation. S may also add the `watchdog.json` field `signature_refused`. F makes no edits (Windows
Update is a SYSTEM-lane action, not watchdog code).

**`hub/actions.json` / `agent/actions.ps1`** (primary owner F). R needs the action `forge_uninstall`, which F writes (§7.6).

**`hub/static/index.html`** (U only). Other teams never edit it; they give U the endpoint shapes below. U keeps
`tools/mock_ui_server.py` in sync with this contract (mock data for every new endpoint).

## 2. Hub plug-in context (`hub/forge_ctx.py`)

`create_app` builds one `HubContext` (`app.state.ctx`). At the end it calls, in this order:
`forge_security.register(app, ctx)`, `forge_reliability…`, `forge_alerts…`, `forge_upkeep…`, `forge_ui…`.
Module routes are then moved **in front of** the core routes, so a module can replace a core route by registering the
same method + path. **Allowed replacements:** S may replace `POST /api/login`, `POST /api/logout`, `GET /api/enroll`,
`GET /cluster/bootstrap` and `GET /agent/install.ps1`. R uses `ctx.healthz` rather than replacing `/healthz`. No other route
may be replaced.

### 2.1 Fields
| Field | Type | Notes |
|---|---|---|
| `app, store, cluster, clock, cfg, node_id` | core | `cfg` = config.json merged with secrets.json (S2). Read-only. |
| `data_dir, release_dir, releases_dir, releases, lease, p2, actions_catalog` | core | `p2.sched_lock` serializes job state changes; `p2.insert_job(**kw) -> id`; `p2.place_jobs(now)` |
| `require_ui, require_agent, require_cluster` | FastAPI deps | use as `dependencies=[Depends(ctx.require_ui)]`. After S4, `request.state.principal` = `{session_id, label}` |
| `leader_json(request)` | async → `(Response or None, dict)` | for `/cluster/*` writes: forwards to the leader when this hub is a standby |
| `leader_only_or_forward(request, raw)` | → `Response or None` | sync variant |
| `node_settings(nid)`, `effective_settings(nid)`, `job_row(row, with_output)` | helpers | |
| `local_paths: set`, `local_prefixes: list` | mutable | paths a standby answers itself instead of forwarding (U adds `/manifest.webmanifest`, `/sw.js` and the `/icons/` prefix) |
| `event(node_id, level, msg)` | helper | `level` ∈ ok / info / warn / error |
| `is_leader`, `settled_leader()` | helpers | |
| `safe_call(fn, *a)`, `run_hooks(list, *a)` | helpers | call plug-in code; exceptions are logged to stderr and swallowed |

### 2.2 Slots (one owner sets each; the default keeps 2.2 behaviour)
| Slot | Owner | Signature | Callers |
|---|---|---|---|
| `ui_auth` | S | `(request) -> dict or None` | `require_ui` |
| `check_agent_token` / `check_cluster_token` | S | `(token: str) -> bool` | agent and cluster deps, bundle auth |
| `require_step_up` | S | `(request) -> None`; raises `HTTPException(403, {"detail", "step_up": "required"\|"setup"})` | `create_job` (admin / destructive), R retire, F maintenance runs, S rotate / signing key / PIN change |
| `verify_bundle` | S | `(release_dir: Path) -> None`; raises `BundleError(msg, 400)` | `Releases.publish` (S wires it inside forge_bundle) |
| `healthz` | R | `() -> (status_code: int, body: dict)` | `GET /healthz` |
| `job_output` / `append_output` | R | `(job_id) -> str` / `(job_id, text, now) -> None` | app.py, N (failure excerpts), F (parsing action results) |
| `raise_alert` | N | `(kind, *, key=None, node_id=None, level="warn", msg="", self_healing=False, data=None, action=None) -> id or None` | S, R, F |
| `resolve_alert` | N | `(key, *, by="auto") -> bool` | S, R, F |
| `notify` | N | `(kind, title, body, *, node_id=None, job_id=None, url=None) -> None` | R (rollback), F (upkeep failures) |

Until N lands, `raise_alert`, `resolve_alert` and `notify` are no-ops. Callers must not depend on their return value.

### 2.3 Hook lists (append to them; app.py calls each through `ctx.safe_call`)
| List | Signature | Called |
|---|---|---|
| `on_tick` | `fn(now, settled: bool)` | on every hub, every 3 s tick, after the core duties. `settled` = leader past its grace period and not contested. |
| `on_report` | `fn(node_id, body, resp, now)` | user-lane `POST /api/agent/report`; mutate `resp` to add response fields |
| `on_system_report` | same | system-lane report |
| `on_job_finished` | `fn(job: dict (row before the update), status, output: str, now)` | a job reaches done / failed / cancelled through an agent update, or `[hub] job lost` |
| `alert_sources` | `fn(now) -> [ {level, kind, node_id, msg, ...} ]` | `GET /api/health` (and N's engine) |
| `state_extras` | `fn(state: dict, now)` | mutates the `GET /api/state` response |
| `status_extras` | `fn() -> dict` (merged) | `GET /cluster/status` |

Report-response key ownership: S `rotate`; R `drain_timeout_s`; F `wol`. N adds none. Never reuse another team's key.

## 3. Data model (all in `hub/forge_store.py`; already created)

### 3.1 New tables (`SCHEMA_P3`)
| Table | Owner | Replicated | Columns |
|---|---|---|---|
| `ui_sessions` | S | yes (`id`) | `id` (sha256 hex of the session secret), `label`, `created`, `last_seen`, `expires`, `revoked` 0/1, `stepup_until`, `user_agent`, `ip`, `updated` |
| `enroll_codes` | S | yes (`id`) | `id` (sha256 hex of the code), `kind` `machine`\|`device`, `label`, `created`, `expires`, `used`, `used_by` (node id or session id), `created_by`, `updated` |
| `secrets` | S | yes (`name`) | `name`, `value`, `prev_value`, `rotated`, `updated`. Names: `agent_token`, `cluster_token`, `ui_token_hash`, `admin_pin_hash`, `signing_pubkey`, `vapid_private_key`, `vapid_public_key` (N writes the two VAPID rows) |
| `job_output` | R | yes (`id`) | `id` = `<job_id>:<seq, 8 digits>`, `job_id`, `seq`, `ts`, `text`, `updated` |
| `presence` | R | **no** | `node_id`, `last_seen`, `addr`, `metrics`, `procs`, `agents`, `capacity` |
| `healthz_probe` | R | **no** | `id`, `ts` (DB write probe) |
| `retirements` | R | yes (`node_id`) | `node_id`, `state`, `started`, `finished`, `job_id`, `rotate`, `error`, `requested_by`, `updated` |
| `alerts` | N | yes (`id`) | `id`, `key` (dedupe), `kind`, `node_id`, `level`, `msg`, `opened`, `resolved`, `resolved_by` `auto`\|`user`, `self_healing`, `last_notified`, `notify_count`, `data` (JSON; may hold `action`), `updated` |
| `push_subs` | N | yes (`id`) | `id` (sha256(endpoint)[:32]), `endpoint`, `p256dh`, `auth`, `session_id`, `label`, `created`, `last_ok`, `fails`, `updated` |
| `prefs` | lead (shared) | yes (`key`) | `key`, `value` (JSON), `updated`. Key namespaces: `notify.*` N, `upkeep.*` F, `security.*` S, `ui.*` U |
| `maintenance_runs` | F | yes (`id`) | `id`, `action`, `args` JSON, `targets` JSON list (in run order), `idx`, `status`, `current_job`, `results` JSON list, `source` `ui`\|`schedule`, `created`, `started`, `finished`, `stop_on_failure`, `created_by`, `updated` |

### 3.2 New columns (`MIGRATIONS`)
| Column | Owner | Meaning |
|---|---|---|
| `jobs.output_seq`, `jobs.output_len`, `jobs.output_min_seq` | R | last chunk seq, total chars, first kept seq (older chunks are trimmed past 256 KB) |
| `settings.drain` | R | 1 = dispatch no new jobs to this node (retire / manual drain) |
| `jobs.notify` | N | 1 = "notify me" when this task finishes |
| `jobs.run_id` | F | the maintenance run that created the job |
| `projects.sync_nodes` | F | JSON list of node ids ("Keep in sync on") |
| `settings.wu_window`, `settings.wu_last` | F | per-node Windows Update window override (`"HH:MM-HH:MM"` / `"off"`), last result as JSON |

Replication notes: R makes job deletion (retention) also delete that job's `job_output` rows on every hub. Standbys do it
when they apply the `jobs` tombstone, so there are no per-chunk tombstones. Trimmed chunks (seq < `output_min_seq`) are
deleted locally by each hub. Reader rule for the mixed fleet: **use the chunks if any exist for the job, else `jobs.output`**.

## 4. Config keys and files

### 4.1 Node file layout (2.3)
`<FH>` = `%ProgramData%\Forge`.

| Path | ACL (S2/S1) | Content / owner |
|---|---|---|
| `<FH>\data\config.json` | Admins+SYSTEM full, user **read** | `node_id`, `agent_token`, `port`, `seed_peers`, the non-secret hub keys of §4.2. S migrates `ui_token` and `cluster_token` out of it. |
| `<FH>\data\private\` | Admins+SYSTEM only, inheritance off | S |
| `<FH>\data\private\secrets.json` | inherits from private | §4.3. The hub reads it at start and rewrites it on rotation (S). |
| `<FH>\data\private\forge.db` (+`-wal`, `-shm`) | inherits from private | S moves `data\forge.db*` here at hub start when `data\private` exists. `create_app` uses env `FORGE_DB` if set, else `data_dir/private/forge.db` when that dir exists, else `data_dir/forge.db` (tests). |
| `<FH>\data\private\backup\forge-YYYYMMDD.db` | inherits | R3. Deviation from the spec path (`data\backup`): backups contain secrets. |
| `<FH>\data\private\firstrun-claim.json` | inherits | F writes it; S's hub imports it once and deletes it: `{"code_sha256": "...", "expires": <epoch>, "label": "first device"}` |
| `<FH>\data\` (everything else) | Admins+SYSTEM full, user **read** | logs, `agent-config.json`, `running*.json`, `watchdog*.json`, written by admin-token processes only. 2.2 gave the user Modify here; 2.3 removes it, because a filtered job must not be able to rewrite `agent-config.json` (tools, services) that the full-token agent trusts. |
| `<FH>\bin\` | Admins+SYSTEM full, user read + execute | R: `wd-launcher.ps1` (R2), `agent-loop.cmd` (R7). Deviation from the spec (`data\wd-launcher.ps1`): `data\` must never hold code that SYSTEM runs. |
| `<FH>\releases\<ver>\venv\` | as releases | R5: only when that release's `requirements.txt` sha differs from the previous release |
| `<FH>\releases\<ver>\bundle.sig` | as releases | S6 (also at the zip root) |
| `<work_root>\jobs\<id>`, `<work_root>\scratch\<id>` | + user SID Modify (S `Grant-JobDirAccess`) | filtered jobs write here |
| `<work_root>\ollama\models` | user Modify | F6b `OLLAMA_MODELS` |
| `%LOCALAPPDATA%\Hivemind\signing-key.dpapi`, `signing-pubkey.xml` | user profile | S6, on the publishing machine only |

### 4.2 Hub config (`config.json`, read by the hub; all optional)
| Key | Default | Owner |
|---|---|---|
| `session_days` | 90 | S |
| `stepup_minutes` | 10 | S |
| `enroll_ttl_s` | 600 | S |
| `token_grace_s` | 600 | S |
| `legacy_bootstrap_secrets` | false; true = `bootstrap?t=` also returns `cluster_token` (2.2 installers; S-10) | S |
| `job_token_mode` | `"filtered"`; `"full"` = explicit opt-out, read by the agent from here (S-10), reported, error alert | S |
| `public_url` | none (then the leader's first MagicDNS peer URL) | S (enroll links), N (notification links) |
| `healthz_min_free_gb` | 1.0 | R |
| `backup_keep` | 7 | R |
| `backup_time` | `"02:30"` (local) | R |
| `release_health_s` | 180 | R |
| `drain_timeout_s` | 1800 (sent to agents in the report response) | R |
| `announce_every_s` | 300 | R |
| `alert_selfheal_grace_s` | 900 | N |
| `machine_down_notify_s` | 600 | N |
| `deadman_every_s` | 300 | N |

Signed bundles are required as soon as `signing_pubkey` is pinned (no config switch). Notifier and upkeep settings are
**not** in config.json, because they must follow the leader: they live in `prefs` (§5 N, §5 F).

### 4.3 `data\private\secrets.json` (S2)
```json
{"ui_token_hash": "sha256:<hex>|null", "cluster_token": "<str>", "cluster_token_prev": "<str>|null", "rotated": 1.7e9,
 "signing_pubkey": "<RSAKeyValue>…</RSAKeyValue>|null", "admin_pin_hash": "pbkdf2_sha256$200000$<salt b64>$<hash b64>|null"}
```
`agent_token` stays in `config.json`, because the user-lane agent reads it from there. Accepted residual risk: a filtered job
can read the agent token.

### 4.4 `agent-config.json` (agent; all optional)
| Key | Default | Owner |
|---|---|---|
| ~~`job_token_mode`~~ | moved to `config.json` (amendment S-10); ignored here | S |
| `drain_timeout_s` | the hub's value, else 1800 | R |
| `wol_enabled` | true | F |
| `ollama_models_dir` | `<work_root>\ollama\models` when work_root is not on the system drive | F |
| `services[].starting_grace_s` | 60 | F |

## 5. Endpoints per item

Auth: **UI** = `require_ui`; **UI+PIN** = UI + `ctx.require_step_up`; **A** = agent token; **C** = cluster token;
**none** = public. All bodies are JSON. Times are epoch seconds (float).

### Workstream S — Security

**S1 least-privilege jobs** (agent side in §6.1). The report carries `metrics.job_token: "filtered"|"full"|"unavailable"` and
`metrics.job_token_error: str|null`. First job output line (agent):
`[<node>] <engine> started in <cwd> as <user> (filtered token, Medium)`.
Rules: with token `full` or `unavailable`, `full_access` jobs fail with
`[<node>] refused: full access needs the filtered job token` (exit -1); normal jobs still run. Hub (S, via `alert_sources`):
alert `job_token_full` (level `error`) when a 2.3+ agent reports anything other than `filtered`. A missing field means a
pre-2.3 agent: alert `job_token_unknown` (warn). Node rows in `/api/state` carry the field inside `metrics`.

**S2 secret split.** No endpoint. `load_config` (hook A-S1): if `config.json` holds `ui_token` / `cluster_token` and
`data/private` is writable, write `secrets.json` (`ui_token_hash = "sha256:" + sha256(ui_token)`), remove those keys from
`config.json`, and keep a one-time `data\private\config.json.pre23.bak`. The hub keeps accepting the raw legacy ui token
(hash compare). `/cluster/bootstrap` never returns `ui_token` again; it returns `ui_token_hash`.

**S3 enrollment codes.**
- `POST /api/enroll` (UI) `{kind: "machine"|"device", label?: str (<= 64)}` →
  `200 {id, code: "ABCD-EFGH", kind, expires, hub, command?, url?}`.
  - `command` (machine): `irm '<hub>/agent/install.ps1?code=<code>' | iex`. `url` (device): `<hub>/?claim=<code>`.
  - `hub` = config `public_url`, else the leader's first MagicDNS (`*.ts.net`) peer URL, else its first non-loopback URL; never `localhost`.
  - Code: 8 chars of Crockford base32 (no I/L/O/U), shown as `XXXX-XXXX`, compared case-insensitively without the dash, stored
    hashed, TTL `enroll_ttl_s`.
- `GET /api/enroll` (UI, legacy; S replaces it) → same as POST with `kind: "machine"`. The raw agent token is no longer in the command.
- `GET /api/enroll/{id}` (UI) → `{id, kind, state: "pending"|"redeemed"|"joined"|"expired", node_id?: str, expires}`.
  `joined` = a node redeemed the code and its agent has reported. This drives U6 "Waiting for it to appear…".
- `DELETE /api/enroll/{id}` (UI) → `{ok}` (cancel the code).
- `GET /agent/install.ps1?code=<code>` (none) → installer text with `__HUB__` and `__CODE__` substituted. It checks that the code
  exists and has not expired, and does NOT consume it. 401 for a bad or expired code. `?t=<agent_token>` keeps working (2.2 installers, re-runs).
- `GET /cluster/bootstrap?code=<code>` (none) → redeems the code once, on the leader (a standby forwards the call) →
  `{agent_token, cluster_token, signing_pubkey, ui_token_hash, peers: [{id, priority, urls}], bundle_version, bundle_sha256, node_hint}`.
  401 for an unknown, used or expired code; 429 after 10 failures per minute per client IP. `?t=<agent_token>` still works
  (watchdog, re-runs) and returns the same shape (no `ui_token`).

**S4 per-device sessions.** Cookie `forge_sid` (HttpOnly, SameSite=Lax, Path=/, Max-Age 90 days) holds a 32-byte urlsafe secret;
the table stores `sha256(secret)`. Sliding expiry: `last_seen` / `expires` are refreshed at most every 10 min (keeps replication
quiet). The legacy `forge_token` cookie and `Authorization: Bearer <ui_token>` still authenticate (hash compare), and a browser
with the legacy cookie is upgraded to a session.
- `POST /api/login` (none) `{token, label?}` → `{ok, session: {id, label}}` + cookie; 401 `wrong token`.
- `POST /api/claim` (none; standby-local path that S forwards to the leader) `{code, label?}` → `{ok, session: {id, label}}` + cookie;
  401 for a bad, used or expired code. Consumes a `device` code or the first-run code.
- `POST /api/logout` (UI) → `{ok}`; revokes the current session and clears both cookies.
- `GET /api/devices` (UI) → `{devices: [{id: <first 12 hex>, label, created, last_seen, expires, current: bool, ip, user_agent}]}`.
- `PATCH /api/devices/{id}` (UI) `{label}` → the device row. `DELETE /api/devices/{id}` (UI) → `{ok}` (revoke; revoking the
  current device is allowed and equals logout).

**S5 token rotation.**
- `POST /api/tokens/rotate` (UI+PIN) `{which?: ["agent","cluster"] (default both), confirm: true, force?: bool}` →
  `{ok, rotated: ["agent","cluster"], grace_until}`. 400 `confirmation required`; 409 (§7.5) when agents older than 2.3 would be locked out.
- The leader writes the `secrets` rows (`value` = new, `prev_value` = old, `rotated` = now). Every hub, when it sees a newer row
  (S `on_tick`), rewrites `config.json.agent_token` / `secrets.json.cluster_token` and updates `cfg`. Both values are accepted
  until `rotated + token_grace_s`. While an agent still authenticates with the previous token, its report response (both lanes)
  carries `rotate: {agent_token, grace_until}`. The watchdog re-reads the secrets on every run (`Merge-WdSecrets`).
- `GET /api/tokens` (UI) → `{agent: {rotated, grace_until}, cluster: {rotated, grace_until}}` (never the values).
- S exports `forge_security.rotate(ctx, which: list[str]) -> dict` for R6.

**S6 signed bundles.**
- Manifest: §6.4 `Get-BundleManifest`; Python twin `forge_security.bundle_manifest(dir: Path) -> bytes`. Signature: RSA-3072,
  PKCS#1 v1.5, over the SHA-256 of the manifest bytes. `bundle.sig` (zip root, JSON, UTF-8):
  `{"v": 1, "version": "2.3.0", "manifest_sha256": "<hex>", "sig": "<base64>", "key_id": "<first 16 hex of sha256(pubkey xml)>"}`.
  Signing the manifest, not the zip, keeps the hub's deterministic re-zip of the running release verifiable.
- `signing_pubkey` = `<RSAKeyValue><Modulus>b64</Modulus><Exponent>b64</Exponent></RSAKeyValue>` (.NET `ToXmlString($false)`).
- `POST /cluster/publish`: when a key is pinned, the extracted bundle must verify (`ctx.verify_bundle`), else 400
  `bundle not signed` / `bundle signature invalid`. `GET /cluster/status` adds `signing: {pinned: bool, key_id}`.
- Watchdog: `Test-BundleSignature` runs before activation (already wired). A refusal means no activation, plus
  `watchdog.json.signature_refused: "<ver>"`.
- `GET /api/security/signing-key` (UI) → `{pinned, key_id}`. `PUT /api/security/signing-key` (UI+PIN) `{pubkey_xml, replace?: bool}`
  → `{ok, key_id}`; 400 when it is not an RSA key of >= 3072 bits; 409 when a different key is already pinned and `replace` is not true.

**S7 admin step-up.** The PIN is exactly 6 digits. Hash format `pbkdf2_sha256$200000$<salt b64>$<hash b64>` (Python
`hashlib.pbkdf2_hmac`, PowerShell `Rfc2898DeriveBytes` with SHA256), stored in `secrets.admin_pin_hash` and in `secrets.json`.
- `GET /api/stepup` (UI) → `{pin_set: bool, valid_until: float|null}`.
- `POST /api/stepup` (UI) `{pin}` → `{ok, valid_until}` (sets `ui_sessions.stepup_until = now + stepup_minutes`). 403 `wrong PIN`;
  429 after 5 wrong PINs in 10 min per session.
- `PUT /api/stepup/pin` (UI) `{pin, old_pin?}` → `{ok}`. When a PIN exists, this needs `old_pin` or a valid step-up. 400 if not 6 digits.
- Protected operations: `POST /api/jobs` with engine `admin` or a destructive action; `POST /api/maintenance/runs` (destructive or
  system lane); `POST /api/machines/{id}/retire`; `POST /api/tokens/rotate`; `PUT /api/security/signing-key`; `DELETE /api/peers/{id}`.
  They answer 403 `{"detail": "admin PIN required", "step_up": "required"}`. When no PIN exists yet they answer 403
  `{"detail": "admin PIN not set", "step_up": "setup"}` (safe by default).
  UI flow: on a 403 carrying `step_up`, prompt for the PIN (or ask the user to set one), `POST /api/stepup`, retry once.

### Workstream R — Reliability

**R1 health gate.** `GET /healthz` (none, standby-local) →
`200 {ok: true, node_id, role, version, running_version, db_ok, free_gb, checks: {db_write: bool, disk: bool}}`.
It answers **503** with the same body and `ok: false` when the DB write probe (`healthz_probe` upsert) fails or the data drive has
less than `healthz_min_free_gb`. `/cluster/status` adds `local_agent: {online, agent_version, bundle_version, last_seen}` for this
hub's own node. Release healthy (watchdog, R) = healthz ok AND status `eligible` AND `local_agent.bundle_version >= release`
within `release_health_s`; otherwise the existing 3-strike rollback applies. The lease auto-release stays as in 2.2.1.

**R2 frozen launcher.** `<FH>\bin\wd-launcher.ps1` (§6.5). The ForgeWatchdog task action becomes
`powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<FH>\bin\wd-launcher.ps1"`. The 2.3 watchdog migrates existing nodes:
it copies the launcher when it is missing or `$LauncherVersion` is higher, re-registers its own task once, and logs
`ACTION migrate ForgeWatchdog -> bin\wd-launcher.ps1`. F's installer does the same for new nodes by calling R's `Install-WdLauncher`.

**R3 DB safety.** Leader: once a day after `backup_time`, `VACUUM INTO data\private\backup\forge-YYYYMMDD.db`, keeping `backup_keep` files.
Every hub at start runs `PRAGMA quick_check`. If it is not `ok`: close the DB, rename `forge.db` → `forge.db.corrupt-<stamp>`
(+ wal/shm), start with an empty DB and stay `eligible=false` until one full sync from a leader succeeds (no solo self-election
while peers are configured), plus an event and alert `db_reseeded`. `/cluster/status` adds
`db: {quick_check: "ok"|"reseeded", last_backup: float|null}`. `GET /api/backups` (UI) → `{backups: [{file, size, ts}], last_backup}`.

**R4 data model.** Job output appends become `job_output` chunk rows (<= 64 KB each; larger appends are split); `jobs.output`
is no longer rewritten (it is still read as a fallback). `GET /api/jobs/{id}` keeps `output` (concatenated) and adds `output_seq`.
New: `GET /api/jobs/{id}/output?since_seq=<int>` (UI) → `{chunks: [{seq, ts, text}], next_seq, status, truncated: bool}`; U uses it
for live output instead of re-polling the whole job. Report metrics / procs / agents go to `presence` (not replicated).
`nodes.last_seen` is replicated only every 60 s or on an online/offline change. `/api/state` and `/api/nodes/{id}` read presence first.

**R5 upgrades.** Watchdog: when the requirements sha is unchanged against the running release, no pip and no venv. When it changed,
create `releases\<ver>\venv` (`python -m venv`, then pip install) before the flip. `run-hub.cmd` uses
`%FORGE_RELEASE_DIR%\venv\Scripts\python.exe` when it exists, else `%FORGE_HOME%\venv`. Agent: the report response carries
`drain_timeout_s`; a draining worker that still has jobs after that time exits anyway (the 2.2 orphan reattach picks the jobs up)
and logs `drain timeout: restarting with N job(s) still running`.

**R6 machines lifecycle.**
- Every hub, every `announce_every_s` and whenever its URL set changes, sends `POST /cluster/join {id, urls}` to the leader (existing
  endpoint; the leader **replaces** the peer's urls). URL set = MagicDNS name + Tailscale IP + LAN IPv4s, as `http://<x>:<port>`.
- `POST /api/machines/{id}/retire` (UI+PIN) `{confirm: true, uninstall: bool = true, rotate_tokens: bool = false}` →
  `202 {ok, state: "draining"}`. 404 unknown node; 409 already retiring, or it is the only hub; 400 `confirmation required`.
- `GET /api/machines/{id}/retire` (UI) → `{node_id, state: "draining"|"uninstalling"|"removing"|"done"|"failed", started, finished,
  job_id, error}`. `DELETE /api/machines/{id}/retire` (UI) → cancel (only while draining).
- Flow (leader tick): set `settings.drain=1` → running jobs finish (at most 30 min, then they are cancelled) → if `uninstall`, a
  system job with action `forge_uninstall` (F) → wait until it is done or the node has been offline for 5 min → delete the
  `nodes`, `peers`, `settings`, sessions and repos rows (tombstones) → if `rotate_tokens`, `forge_security.rotate(ctx, ["agent", "cluster"])`
  → `done`.

**R7 process model.** Tasks ForgeAgent / ForgeSystemAgent run `cmd.exe /c "<FH>\bin\agent-loop.cmd" user|system`. The loop runs
`powershell -NoProfile -ExecutionPolicy Bypass -File <FH>\current\agent\forge-agent.ps1 -Child -Lane <lane>` forever with a 5 s pause,
and the tasks set restart-on-failure (3 × 1 min). The in-script supervisor (no `-Child`) stays for compatibility but is no longer
used. The watchdog migrates the task actions once (R). The agent liveness check is unchanged (agent.log heartbeat).

### Workstream N — Notifications

**N1 alert engine** (leader, every tick, in `forge_alerts`). Conditions = the core alerts (node_offline, disk_full, version_behind,
watchdog_stale, clock_skew, hub_down, fleet_bad_version) + `ctx.alert_sources` + raised alerts. Each condition has a `key`
(`<kind>:<node_id or ->[:<subject>]`), and an open alert per key is one row. A condition that is gone for 2 ticks is `resolved`
with `resolved_by: "auto"`. Self-healing kinds (`rollout_in_progress`, `service_restarting`, `version_behind` while a lease is
held) never notify unless they stay open longer than `alert_selfheal_grace_s`.
- `GET /api/alerts?open=1&limit=100&since=<float>` (UI) → `{alerts: [{id, key, kind, node_id, level, msg, opened, resolved,
  resolved_by, self_healing, last_notified, data, action}]}`. `action` = `{label, kind: "resume_session"|"retry_job"|"wake_machine"|
  "open_machine"|"open_project"|"open_job"|"fix", target: {...}}` or null.
- The alert's `action` is stored in `alerts.data` as `data.action` (a dict, or absent). N also exports
  `forge_alerts.list_open(ctx, now) -> [alert]`, the open alerts in exactly the `GET /api/alerts?open=1` item shape; U's
  `GET /api/today` calls it when present and only reads the table (with `data.action`) as a fallback.
- `POST /api/alerts/{id}/ack` (UI) → `{ok}`. Resolves the alert with `resolved_by: "user"`; a condition that is still true reopens after 1 h.
- `GET /api/health` keeps its 2.2 shape; `alerts[]` entries gain `id`, `opened`, `self_healing`, `action`.
- Alert kinds owned by other teams: S `job_token_full`, `job_token_unknown`, `bundle_unsigned`; R `db_reseeded`, `low_disk_hub`,
  `rolled_back`, `retire_failed`; F `repo_conflict`, `upkeep_failed`, `windows_update_failed`, `reboot_pending`. N's own:
  `task_failed`, `task_finished`, `machine_down`.

**N2 notifiers.** Prefs: `notify.ntfy` = `{enabled, server: "https://ntfy.sh", topic}`, `notify.webpush` = `{enabled}`,
`notify.rules` = `{task_failed: true, task_finished: true (only jobs with notify=1), machine_down: true, autorepair: true}`.
Notify only on: a failed job (user lane, `source` not `brief`/`sync`); a done job with `notify=1`; `machine_down` open longer than
`machine_down_notify_s` and not self-healing; `rolled_back`, `upkeep_failed`, `fleet_bad_version`. At most one notification per
alert key per 6 h.
- `GET /api/notify/settings` (UI) → `{ntfy: {enabled, server, topic, subscribe_url}, webpush: {enabled, vapid_public_key, devices: int},
  deadman: {enabled, url, send_fail, last_ping, last_ok}, rules: {...}}`.
- `PUT /api/notify/settings` (UI) takes a partial of the same shape and returns the full shape. 400 for a bad URL or topic
  (`^[A-Za-z0-9_-]{8,64}$`).
- `POST /api/notify/ntfy/topic` (UI) → `{topic: "hm-<20 base32>", subscribe_url: "<server>/<topic>"}` (does not save it).
- `POST /api/notify/test` (UI) `{channel?: "ntfy"|"webpush"|"all"}` → `{ok, results: {ntfy?: "ok"|"<error>", webpush?: {sent, failed}}}`.
- `POST /api/push/subscribe` (UI) `{endpoint, keys: {p256dh, auth}, label?}` → `{ok, id}`. `DELETE /api/push/subscribe` (UI)
  `{endpoint}` → `{ok}`. The leader generates the VAPID P-256 keys on first need (`secrets` rows `vapid_*`; the public key is
  base64url, uncompressed). Push payload JSON: `{title, body, url: "#/…", tag: <alert key>}`; U's `sw.js` shows it and opens `url` on click.
- Job field `notify: bool` on `POST /api/jobs` (hook A-N2); job rows carry `notify`.

**N3 dead-man switch.** Prefs `notify.deadman` = `{enabled, url, send_fail: false}`. The leader GETs `url` every `deadman_every_s`
(timeout 10 s). With `send_fail` and any open `error` alert, it GETs `url + "/fail"` instead. Its state is in the settings response.

**N4 morning brief.** `p2.digest_extras` text is appended to the roll-up prompt: overnight alerts and finished tasks (since 18:00
local the previous day). `GET /api/brief/all` adds `overnight: {alerts: [{kind, node_id, msg, opened, resolved}],
finished: [{id, prompt (<= 80 chars), status, node_id, finished}]}`.

### Workstream U — UX (hub side in `forge_ui.py`; everything else in `hub/static`)

- **U9 PWA.** `GET /manifest.webmanifest` (none, standby-local) → `application/manifest+json`
  `{name: "Hivemind", short_name: "Hivemind", start_url: "/#/today", display: "standalone", background_color, theme_color,
  icons: [{src: "/icons/icon-192.png", sizes: "192x192"}, {src: "/icons/icon-512.png", sizes: "512x512"},
  {src: "/icons/maskable-512.png", sizes: "512x512", purpose: "maskable"}]}`.
  `GET /sw.js` (none; headers `Service-Worker-Allowed: /`, `Cache-Control: no-cache`) serves `hub/static/sw.js`. `GET /icons/<file>.png` (none).
  The offline shell caches `/`, the manifest and the icons. The last `/api/state` and `/api/today` responses are cached for the
  "Offline · data from HH:MM" banner. `sw.js` handles `push` and `notificationclick` (N2 payload).
- **U2 Today.** `GET /api/today?q=&all=0|1` (UI; `all=1` returns every needs item, up to 100, not the first 3) → `{now, status: {level: "ok"|"warn"|"error", sentence}, needs_you: {items: [alert (N1 shape)], total},
  happening: [{kind: "session"|"job", id, tool?, title, node_id, project_id?, since, state}], recent: {today: [], yesterday: [], earlier: []}
  (session rows, <= 20 each, newest first), brief: {text, ts, model}|null, first_run: {machines: int, projects: int}}`.
  Built from `alerts` (before N lands: from the `/api/health` alerts), jobs, sessions and briefs. `?q=` filters `recent` by title,
  project name or machine (the U2 search).
- **U3 Ask.** `GET /api/ask?q=<text>&limit=8` (UI) → `{results: [{type: "project"|"session"|"machine"|"action"|"maintenance"|"run",
  id, title, subtitle, score, target: {...}}]}`. The last entry is always `{type: "run", title: "Run '<text>' on any free machine",
  target: {prompt, engine: "any", node_id: "auto"}}`. v1 is fuzzy matching only; nothing runs until the user presses the button.
- **U4** uses `POST /api/jobs` `{prompt, engine: "any"|<engine>, node_id: "auto"|<id>, project_id?, notify?}` (unchanged + N's `notify`).
- **U5** uses F's `/api/maintenance/*`; **U6** S3's `/api/enroll*`; **U7** `/api/state`, `/api/nodes/{id}`, `/api/alerts`, R's
  `/api/machines/{id}/retire`, S's `/api/devices` and `/api/tokens`, F's `/api/machines/{id}/wake`; **U8** `today.first_run`.
- **U10 hash routes:** `#/today`, `#/projects`, `#/project/<id>`, `#/session/<tool>/<id>`, `#/machines`, `#/machine/<id>`,
  `#/machines/maintenance`, `#/machines/advanced`, `#/activity`, `#/task/<id>`, `#/settings`, `#/add-machine`. Legacy hashes map:
  `#fleet` → `#/machines`, `#jobs` → `#/activity`, `#health` → `#/today`, `#projects` → `#/projects`. Query `?claim=<code>` on `/` →
  `POST /api/claim`, then `history.replaceState` to `#/today`. Theme: `localStorage["hm.theme"]` = `auto|light|dark`.
  Notification links use these routes.

### Workstream F — First run & upkeep

**F1 `install.ps1 -NewFleet`** (signatures in §6.3). Steps:
1. Generate `agent_token` and `cluster_token` (24 random bytes, urlsafe base64) and the signing key (S's `New-ForgeSigningKey`).
2. Prompt for the admin PIN twice (`Read-Host -AsSecureString`; `-AdminPin` param for tests) and hash it (`New-ForgePinHash`).
3. Write `config.json` + `secrets.json` (S's `Write-ForgeSecrets`) and apply `Set-ForgeDataAcl`.
4. Install the local bundle (repo or `-BundleFile`) and register the tasks with R's launcher (`Install-WdLauncher`, `Get-ForgeTaskSpecs`).
5. Write `firstrun-claim.json`, wait for `/healthz`, open `http://localhost:8787/?claim=<code>`.
No hand-written bootstrap.json. `-NewFleet -DryRun` prints every step. Join mode: `-Code <code>` (replaces `-Token` in the one-liner;
placeholder `__CODE__`); `-Token` still works.

**F2 tool upkeep.** `hub/actions.json` entries gain `"lane": "system"|"user"` (default `system`). New actions:

| name | lane | destructive | args | does |
|---|---|---|---|---|
| `update_claude` | user | false | none | `claude update` (native install), else `npm i -g @anthropic-ai/claude-code@latest` |
| `update_codex` | user | false | none | `npm i -g @openai/codex@latest` (or `codex update` when available) |
| `update_t3` | user | false | none | `t3 update` |
| `update_ollama` | system | false | none | `winget upgrade --id Ollama.Ollama --silent` |
| `winget_upgrade` | system | false | existing | existing |
| `git_sync` | user | false | `cwd` (path, <= 260) | F3 |
| `ollama_pull` | user | false | `model` (MODEL_RE) | F6b pre-pull |
| `windows_update` | system | false | `reboot: "never"` (enum) | F4: search / download / install via the `Microsoft.Update.Session` COM API |
| `free_disk` | system | true (F-2) | none | temp items untouched 7 days, `<work_root>\scratch` dirs with no file changed in 14 days, every user's recycle bin; links removed, never followed |
| `forge_uninstall` | system | true | `keep_data: bool` | for R6: stop and unregister the Forge tasks, remove the firewall rule, remove `<FH>` (except `data\` when `keep_data`) |

Every action ends its output with one line `##RESULT <json>` (e.g. `{"result":"ff","branch":"main"}`); F parses it in `on_job_finished`.
Maintenance catalog (friendly names, `forge_upkeep.MAINTENANCE`): `update_app` (winget_upgrade with an id), `update_ai_tools`
(update_claude + update_codex), `restart_hivemind` (restart_task ForgeHub + ForgeAgent), `free_disk`, `restart_machine` (reboot).
- `GET /api/maintenance/actions` (UI) → `{actions: [{name, title, description, steps: [action names], lane, destructive, args: {...schema}}]}`.
- `POST /api/maintenance/preview` (UI) `{action, args?, targets: "all"|[node ids]}` →
  `{order: [ids], skipped: [{node_id, reason}], destructive, needs_step_up, online: int}`. Order = rollout order
  (canary first, descending priority number, leader last; reuse `forge_lease.build_order`).
- `POST /api/maintenance/runs` (UI; UI+PIN when destructive or system lane) `{action, args?, targets, confirm: true}` → `{id, order}`.
  400 unknown action / bad args / `confirmation required`; 409 another run is active on an overlapping target.
- `GET /api/maintenance/runs?limit=20` (UI) → `{runs: [run]}`; `GET /api/maintenance/runs/{id}` → run =
  `{id, action, title, args, targets, idx, status: "pending"|"running"|"done"|"failed"|"cancelled", current: {node_id, job_id}|null,
  results: [{node_id, job_id, status, summary, finished}], source, created, started, finished}`;
  `POST /api/maintenance/runs/{id}/cancel` → `{ok}`. One node at a time, stop on the first failure (`stop_on_failure`);
  offline targets are `skipped`.
- Schedule: prefs `upkeep.schedule` = `{enabled: true, day: "sun", time: "04:00", actions: ["update_ai_tools", "update_t3",
  "update_ollama", "winget_upgrade"]}`. `GET/PUT /api/upkeep/settings` (UI) → `{schedule, windows_update, repo_sync_every_s}`.
  Results are maintenance runs + jobs (shown in Activity).

**F3 repo sync.** `PUT /api/projects/{id}/sync` (UI) `{nodes: [ids]}` → `{ok, sync_nodes}`. `GET /api/projects` and the project
detail gain `sync_nodes`. Leader, every 30 min (`repo_sync_every_s`): for each project and each listed node that has a `repos`
row for it, queue a user-lane `git_sync {cwd}` (`source: "sync"`; skipped when one is already pending). Agent: `git fetch --prune`,
then fast-forward only when the tree is clean, the branch has an upstream and is not ahead; it never pushes.
`##RESULT {"result": "ff"|"uptodate"|"dirty"|"diverged"|"no_upstream"|"error"}`. `diverged` / `error` raise the alert
`repo_conflict` (action `open_project`); the next `ff` / `uptodate` resolves it.

**F4 Windows Update.** Prefs `upkeep.windows_update` = `{enabled: true, window: "03:00-05:00", stagger_min: 30}`; per-node override
`settings.wu_window` (`PUT /api/nodes/{id}/settings {wu_window}`; F extends the existing validator: `"HH:MM-HH:MM"`, `"off"` or null).
Slot = window start + stagger × rank (rollout order, leader last). At its slot (once per local day) the leader queues `windows_update`.
`##RESULT {"installed": n, "failed": n, "reboot_required": bool, "kb": [...]}` is stored in `settings.wu_last`. Reboot (a queued
`reboot` with `delay_s: 60`) only if `reboot_required` AND another hub is healthy and eligible AND it is still inside the window;
otherwise alert `reboot_pending`.

**F5 Wake-on-LAN.** The agent reports `metrics.macs: [{iface, mac, ip, prefix}]` (F, `Add-UpkeepReport`).
`POST /api/machines/{id}/wake` (UI) → `{ok, via: [node ids], mac}`. 404 unknown node; 409 `already online`; 422 `no MAC known`;
503 `no online machine on the same network`. The leader picks up to 2 online nodes that share an IPv4 subnet with the target's last
known IPs and adds `wol: [{mac, broadcast, port: 9}]` to their next user-lane report response (held in memory, 60 s TTL).

**F6 live-run fixes.**
- (a) Project key `misc:<node>` also for cwds matching `\claude-data\<x>\scratch-workspaces\` (any depth below) and the user's
  `%TEMP%` (`\AppData\Local\Temp\`). Hub side in `forge_projects`; agent title logic unchanged.
- (b) The ollama service env gets `OLLAMA_MODELS = <ollama_models_dir>`, only when `OLLAMA_MODELS` is not already set machine-wide.
  The brief node gets one `ollama_pull {model: brief_model}` per day inside `brief_window` when its report shows the model missing
  (`metrics.ollama_models: [names]`).
- (c) `metrics.services[]` gains `state: "ok"|"starting"|"down"`; `starting` lasts `starting_grace_s` after a (re)start. `ok` stays a
  bool (`ok = state == "ok"`). The UI shows `starting` as neutral.

## 6. PowerShell functions that cross workstream boundaries

All of these exist now as stubs that keep 2.2 behaviour. The owner replaces the bodies, never the signatures.

### 6.1 `agent/jobtoken.ps1` (S) — called by `forge-agent.ps1`
```powershell
Initialize-JobToken [-Config <hashtable>]            # -> @{ ok; mode = 'filtered'|'full'; error }   never throws
Get-JobTokenStatus                                    # -> 'filtered'|'full'|'unavailable'
Add-JobTokenReport [hashtable]$Body                   # sets $Body.metrics.job_token (+ job_token_error)
Grant-JobDirAccess [string]$Path                      # -> [bool]; user SID Modify, idempotent
Start-ForgeJobProcess -Arguments <string> -WorkingDirectory <string> -JobDir <string> -Job <obj>
                                                      # -> [System.Diagnostics.Process]; throws to refuse a job
Get-ProcessIntegrityLevel [int]$ProcessId             # -> 'Low'|'Medium'|'High'|'System'|$null
```
### 6.2 `agent/secrets.ps1` (S) — called by `forge-agent.ps1`, `install.ps1` (F) and the watchdog migration (R)
```powershell
Get-ForgePrivateDir [string]$DataDir                  # -> <data>\private
Read-ForgeSecrets [string]$DataDir                    # -> hashtable (config.json + secrets.json merged) | $null
Write-ForgeSecrets [string]$DataDir [hashtable]$Secrets [-DryRun]   # -> path
Set-ForgeDataAcl [string]$ForgeHome [string]$User [-DryRun]          # -> , @(@{path; grant; inherit}) plan (applied unless -DryRun)
New-ForgePinHash [string]$Pin                         # -> 'pbkdf2_sha256$200000$<salt>$<hash>'
Update-AgentTokenFromResponse $Resp                   # -> new token string | $null
```
### 6.3 `agent/upkeep.ps1` (F) — called by `forge-agent.ps1`
```powershell
Get-NetworkMacs                                       # -> , @(@{iface; mac; ip; prefix})
Add-UpkeepReport [hashtable]$Body                     # metrics.macs, metrics.ollama_models, metrics.ollama_models_dir
Invoke-UpkeepResponse $Resp                           # handles resp.wol; never throws
Send-WakeOnLan [string]$Mac [string]$Broadcast='255.255.255.255' [int]$Port=9   # -> [bool]
```
`agent/install.ps1` (F) new params: `-NewFleet`, `-Code <string>`, `-AdminPin <string>` (tests only), `-NoBrowser`. The existing
`-Hub -Token -ForgeHome -DryRun -BootstrapFile -BundleFile -NoRun` are unchanged. Exposed for tests: `New-FleetSecrets` →
`@{agent_token; cluster_token}`, `New-FirstRunClaim [string]$DataDir [int]$TtlSec = 600` → code string.

### 6.4 `watchdog/verify-bundle.ps1` (S) — called by `watchdog.ps1` (R)
```powershell
Get-SigningPubKey [string]$DataDir                    # -> RSAKeyValue xml | $null
Get-BundleManifest [string]$ReleaseDir                # -> manifest string
Test-BundleSignature -ReleaseDir <string> -PubKeyXml <string>   # -> @{ ok; signed; reason }
Merge-WdSecrets $Cfg [string]$DataDir                 # -> cfg object with cluster_token / signing_pubkey from secrets.json
```
Manifest: one line per file of the extracted release, except `bundle.sig`, `bundle.zip`, `bundle.sha256` and `venv\`:
`<sha256 lowercase hex> <relative/path/with/forward/slashes>\n`, ordinal-sorted by path, UTF-8 without BOM. No pinned key → ok
(unsigned allowed); key pinned → ok only when `<ReleaseDir>\bundle.sig` verifies.

### 6.5 `watchdog/wd-launcher.ps1` (R) — installed by `install.ps1` (F) and the watchdog migration (R)
```powershell
# script params: -ForgeHome -Port -DryRun -NoRun ; $LauncherVersion = <int>
Select-WatchdogScript -ForgeHome <string> -NowEpoch <double> [-StaleSec 600]   # -> path of the watchdog.ps1 to run
# in watchdog.ps1 (R), used by install.ps1 (F):
Install-WdLauncher -ForgeHome <string> [-DryRun]      # copies current\watchdog\wd-launcher.ps1 + agent-loop.cmd to <FH>\bin
Get-ForgeTaskSpecs -ForgeHome <string>                # -> @(@{name; user; command; arguments; restart}) for the 4 tasks
```
### 6.6 `tools/new-signing-key.ps1` (S) — called by `install.ps1 -NewFleet` (F) and `build-bundle.ps1 -Sign` (S)
```powershell
New-ForgeSigningKey [string]$KeyPath [string]$PubKeyPath [-Force]   # -> public key xml
Invoke-ForgeSign [byte[]]$Data [string]$KeyPath                   # -> base64 signature
```

## 7. Agent ↔ hub summary (2.3 additions only)

### 7.1 Report body (user lane): new optional fields
| Field | Owner |
|---|---|
| `metrics.job_token`, `metrics.job_token_error` | S |
| `metrics.macs`, `metrics.ollama_models`, `metrics.ollama_models_dir` | F |
| `metrics.services[].state` | F |
### 7.2 Report response: new optional fields
| Field | Lane | Owner |
|---|---|---|
| `rotate: {agent_token, grace_until}` | both | S |
| `drain_timeout_s: int` | user | R |
| `wol: [{mac, broadcast, port}]` | user | F |
### 7.3 Dispatch items (job fields sent to agents)
| Field | Owner | Notes |
|---|---|---|
| `run_as: "user"` with `engine: "action"`, `action`, `args` | F | user-lane actions (F2 / F3 / F6b) |
### 7.4 Job row fields (UI)
`notify` (N), `run_id` (F), `output_seq` (R). `source` gains `"sync"` (hidden by default) and `"upkeep"` (shown).
### 7.5 Version gates
User-lane `action` jobs are dispatched only to agents >= 2.3.0 (otherwise `wait_reason: "needs agent 2.3"`). `rotate` is sent to any
agent, but 2.2 agents ignore it and would be locked out when the grace ends. So the rotate endpoint answers 409
`agents older than 2.3 would be locked out: <ids>` unless `force: true`.
### 7.6 `forge_uninstall` (F writes it, R calls it)
`{"engine": "action", "action": "forge_uninstall", "args": {"keep_data": false}, "confirm": true}` on the retiring node (system lane).

## 8. Integration order (merge train)

1. **S** and **R** first (both touch auth and data paths). R rebases on S for `data\private`.
2. **N** (consumes the S/R alert sources, but works with the stubs).
3. **F** (needs S's secrets functions for `-NewFleet`, R's launcher functions, N's `raise_alert`).
4. **U** last (consumes everything; meanwhile it develops against `tools/mock_ui_server.py`).

Every merge must keep the full pytest suite and `tests\ps\run-tests.ps1`, `dry-run.ps1`, `agent-smoke.ps1`,
`agent-system-smoke.ps1` green, plus the N-1 compatibility test (2.2 agent fixture against a 2.3 hub), which R owns.

## 9. Test port ranges (within 19100-19299)

| Range | Team | Suggested use |
|---|---|---|
| 19100-19139 | S | fake hubs for enrollment / rotation, bundle signing |
| 19140-19179 | R | multi-hub runs on real sockets, launcher / watchdog fake hubs, N-1 compatibility |
| 19180-19209 | N | fake ntfy server, fake push service, fake healthchecks.io |
| 19210-19249 | U | `tools/mock_ui_server.py` (change its assert to `19210 <= PORT <= 19249`, default 19210), browser runs |
| 19250-19279 | F | installer `-NewFleet` dry-run fake hub, agent upkeep smoke (fake hub + WoL UDP sink) |
| 19280-19299 | Lead | integration / acceptance runs after merges |

## 10. Amendments
(R, 2.3.0, branch p3-rel)
- **R-A1 presence columns + view.** `presence` also has `sys_last_seen`, `sys_capacity`, `sys_version` (the system-lane
  heartbeat is local too). The view `nodes_live` (created by `Store.ensure_views()`, called after the migrations) lays
  presence over `nodes`. Anyone reading `last_seen` / `metrics` / `procs` / `agents` / `capacity` / `addr` / `sys_*` must
  read `nodes_live`, not `nodes` (N: `forge_alerts.current_conditions`; F: WoL MACs and `ollama_models` in `metrics`).
- **R-A2 `/cluster/status` extras.** Besides `local_agent` and `db`: `disk: {free_gb, ok}`, `db.reseed_pending`, and
  `local_agent.draining_to` (online = reported within 90 s, because standbys see their own node through the 60 s nodes row).
- **R-A3 report field.** User-lane `metrics.draining_to: "<ver>"|null` (R): the bundle the agent is draining toward.
- **R-A4 retire.** 409 also when the target is the hub answering the request and `uninstall` is false, and when
  `uninstall` is requested but `forge_uninstall` is not in the catalog (until F lands, use `uninstall: false`).
  `DELETE /api/machines/{id}/retire` also clears a `failed` retirement. While the uninstall job is not queued yet,
  `retirements.job_id` holds the marker `"pending"` (GET shows `null`).
- **R-A5 health gate details.** Low disk (`checks.disk` false, DB write ok) never counts as a rollback strike, and a 503
  hub is not restarted by the watchdog. The agent part of the gate applies only when the local agent was online before the
  flip.
- **R-A6 venv.** "Unchanged" is measured against the base venv (`data\requirements.sha256`). After a release with its own
  venv is confirmed, the base venv is pip-upgraded to it, so in steady state this equals "unchanged against the running
  release".
- **R-A7 test ports.** The PS smoke/dry-run scripts and the `tests/ps/fake_*.py` servers accept 19000-19299 (default ports
  unchanged), so each team can run them in its own range.
- **R-A8 helpers.** `Store` gains `ensure_views`, `append_chunk`, `job_chunks`, `job_output_text`, `job_output_tail`,
  `delete_job_cascade`, `cleanup_chunks` and `db_path`. `app._job_row(row, with_output, store)` reads chunks.
  UI job rows include `output_seq` and do not include `output_len` / `output_min_seq`.

(S, 2.3.0, branch p3-sec)
- **S-1 (S, manifest):** `Get-BundleManifest` / `bundle_manifest` also skip `__pycache__/` dirs and `*.pyc` files at any depth,
  so a release dir that Python has already run from still verifies. The sort is ordinal by UTF-16 code units on both sides.
- **S-2 (S, step-up for API clients):** `ctx.require_step_up` also accepts header `X-Admin-Pin: <6 digits>` per request
  (Bearer / script clients have no session to store a step-up). Wrong PINs count toward the same 5-per-10-min limit.
- **S-3 (S, hook A-S3 in `hub/app.py`):** `delete_peer` takes `request` and calls `ctx.require_step_up(request)` first (it is a
  protected operation in §5 S7 but had no wiring).
- **S-4 (S, hook G-S2 in `agent/forge-agent.ps1`):** the user-lane "started in" line appends `Get-JobLaunchNote $p`
  (` as <user> (filtered token, Medium)`), which produces the first-line format of §5 S1. A refused full-access job reads
  `[<node>] failed to start: refused: full access needs the filtered job token` (the existing catch line adds the prefix).
- **S-5 (S, bootstrap):** `GET /cluster/bootstrap?code=` takes an optional `&node=<id>`, stored as `enroll_codes.used_by`
  (drives `joined`). F's installer should pass it. Without it, `joined` = the first node created after redemption.
- **S-6 (S, login without a leader):** `/api/login`, `/api/claim`, `/api/logout` are forwarded to the leader by S; when no leader
  is reachable the standby answers itself (the session then exists only on that hub). Keeps the 2.2 "local paths still work"
  behaviour.
- **S-7 (S, rotation hint):** during the grace window every report response carries `rotate` (the hub cannot tell which
  token a report used from inside `on_report`); 2.3 agents ignore it when they already use the new token.
- **S-8 (S, tests):** the PS smoke/dry-run scripts and their fake servers accept ports 19000-19299 (was 19000-19099) so each team
  can run them in its own §9 range in parallel. Defaults are unchanged.
- **S-9 (S, `Write-ForgeSecrets`):** extra optional switch `-SkipAcl` (tests only); without it a newly created `data\private`
  is protected (Admins + SYSTEM, inheritance off) before secrets.json is written. Extra helper `Test-ForgePinHash`.
- **S-10 (S, review fixes S-R1..S-R9):**
  - `GET /cluster/bootstrap?t=<agent_token>` returns `{agent_token, peers, bundle_version, bundle_sha256, node_hint}` only;
    `cluster_token`, `signing_pubkey`, `ui_token_hash` need `?code=` (or hub config `legacy_bootstrap_secrets: true`, for
    2.2 installers joining with `?t=`). F's installer: join with `-Code`; a `-Token` re-run takes `cluster_token` from the
    node's own `data\private\secrets.json` (`Read-ForgeSecrets`). The watchdog only needs `peers` from it.
  - `Cluster.forward` sends `X-Forge-Proxy: <cluster token>`; S trusts `X-Forwarded-For` (last entry) only with it.
    Client-sent `X-Forge-Via` / `X-Forge-Proxy` are dropped by `forward`. A pre-2.3 standby does not send it: clients
    behind it share the standby's address for rate limits (stricter, never looser).
  - Signed-bundle verification (hub `verify_release`, watchdog `Test-BundleSignature`) refuses any `__pycache__`,
    `*.pyc`, `*.pyo` (and on the hub a top-level `venv/`; the watchdog checks `venv/` in `bundle.zip`, since R5 may
    create `releases\<ver>\venv` after verification). The manifest still skips them (re-zips of a running release).
  - `job_token_mode` is read from `data\config.json` (`Get-JobTokenMode`), never from `agent-config.json`. Extra
    jobtoken.ps1 helpers: `Get-JobTokenMode`, `Test-JobTokenHandle`. A launch failure with a valid token fails that job
    only; the token is re-created when it is invalid, and retried every 60 s while `unavailable`.
  - Watchdog rollback to a version < 2.3.0 stops the hub and calls `Restore-Pre23Layout $DataDir` (verify-bundle.ps1)
    before restarting it (R's rollback branch, hook added by S).
  - Admin PIN reset: `tools\reset-admin-pin.ps1` writes `data\private\pin-reset.json`; the leader clears the PIN row
    (`value = NULL`), every hub drops it from cfg and its files. A NULL `secrets` row is authoritative (`value()`).
  - Not S: F's `install.ps1` and R's watchdog migration must apply `Set-ForgeDataAcl` (data\ user read-only) and create
    `data\private` (§4.1, §5 F1 step 3); until they do, a node stays in the 2.2 layout. R's `Get-JobCleanupRoots` reads
    `agent-config.json`, which is only trustworthy once that ACL is applied.

(N, 2.3.0, branch p3-notify)
- **N-1 (N):** `forge_alerts` replaces the core `GET /api/brief/all` route (allowed replacement added). It returns the same
  body plus `overnight` (N4); there was no hook for that response. No `app.py` edit beyond A-N1/A-N2.
- **N-2 (N):** new hub file `hub/forge_webpush.py` (owner N): RFC 8291/8292 Web Push implemented directly on
  `cryptography==44.0.0`. `pywebpush` is not used, because it pulls in `requests`, `aiohttp`, `http-ece` and `py-vapid`.
  New optional config key `vapid_subject` (default `mailto:hivemind@example.com`).
- **N-3 (N):** additive response fields: `/api/notify/settings` `webpush.available` and `deadman.last_error`; health `alerts[]`
  entries also carry `key`; `DELETE /api/push/subscribe` returns `removed`. Alert `data.notify_state` records the notify decision.
- **N-4 (N, review fixes):** `GET /api/notify/settings` returns `deadman.url` masked (`scheme://host/…<last 4>`); a PUT that
  sends the masked value back leaves the URL unchanged. With `send_fail`, `/fail` (appended to the URL path, query kept) is
  sent only while an error alert of an infrastructure kind is open (`node_offline`, `machine_down`, `hub_down`,
  `fleet_bad_version`, `rolled_back`), not for a failed user task. `ntfy.server` / `deadman.url` reject `user:pass@` and
  loopback / link-local hosts. `POST /api/push/subscribe` accepts only endpoints under a known push service
  (`fcm.googleapis.com`, `push.services.mozilla.com`, `push.apple.com`, `notify.windows.com`; config `push_host_allow`
  adds suffixes) and answers 503 while the leader has no VAPID keys (the leader now creates them on its tick). A push
  subscription is deleted on 404/410, or after 20 consecutive 400/401/403/413 answers with no success for 24 h; transport
  errors, 429 and 5xx are not counted. Push subscriptions whose S session is revoked / expired (or missing for 1 h) are
  dropped by the hourly housekeeping. `notify_state: "suppressed:dedup"` is not final: the alert notifies once the 6 h
  window passes while it is still open. `POST /api/notify/test` with `all` reports only configured channels.
  `overnight` keeps open / failed and then the newest items (max 30 each, shown oldest first), skips system-lane jobs,
  and adds `truncated: {alerts, finished}`.

**F-1 (workstream F, branch p3-upkeep).** Additive unless marked.
- *Extra shared-file edits beyond A-F1..A-F3 / G-F1..G-F2:*
  - `hub/app.py put_settings`: accepts `wu_window` (validator `forge_upkeep.normalize_wu_window`, as F4 says) and now updates
    the settings row **in place**. The old `INSERT OR REPLACE` wiped the 2.3 columns (R `drain`, F `wu_*`) on every PUT.
  - `hub/forge_phase2.py`: besides the `run_id` keyword on `insert_job`, the 7.5 gate for user-lane `action` jobs lives in
    `has_engine` / `dispatchable` / `_place_jobs` (`USER_ACTION_AGENT = (2, 3, 0)`, wait reason `needs agent 2.3`), and the project
    summary carries `sync_nodes`.
  - `hub/forge_actions.py`: arg type `bool` and the `action_lane(spec)` helper.
  - A-F3 needs no `create_job` change: maintenance runs call `p2.insert_job(run_id=...)` directly.
  - The user-lane dispatch item gets `run_as: "user"`, `action` and `args` from F's `on_report` hook, so `_dispatch` is unchanged.
  - `agent/forge-agent.ps1 Step-Services` (G-F2 region): sets `$rt.grace` and sets `last_start` on a check exit code 10, for
    the `starting` state.
  - Shared tests: `tests/test_admin.py` catalog-shape test, `tests/ps/run-tests.ps1` builder sample map,
    `tests/ps/fixtures/action-vectors.json` (new vectors). The port guards of `agent-smoke.ps1`, `agent-system-smoke.ps1`,
    `dry-run.ps1` and the four fake servers now also accept 19100-19299, so teams can run them in their own range.
- *S (enrollment):* `firstrun-claim.json.code_sha256` = sha256 hex of the code **upper-cased without the dash** (same
  normalisation as S3's case-insensitive compare). The installer writes the claim with a 30 min TTL
  (`New-FirstRunClaim -TtlSec 1800`), before ForgeHub starts. The installer keeps the tokens in `config.json` unless S's
  `Write-ForgeSecrets` really wrote `cluster_token` into `secrets.json`; the hub's A-S1 migration then moves them.
- *R (retire):* `forge_uninstall` registers a one-shot SYSTEM task `ForgeUninstall` that runs 90 s later, and the job
  ends `done` with `##RESULT {"result":"scheduled"}`, because the job runs inside ForgeSystemAgent, which the uninstall stops.
  The agents can report for about 90 s after `done`, so the retire flow should wait for "offline" (or done + 2 min) before
  it deletes the node rows; otherwise the node re-appears as "joined the fleet".
- *R (tasks):* the installer reads `Get-ForgeTaskSpecs` entries as `user` = `SYSTEM` (or `NT AUTHORITY\SYSTEM`) -> SYSTEM
  service account, anything else (`user`, empty, or an account name) -> S4U for the installing user. `ForgeWatchdog` gets the
  1-minute repetition trigger, `restart: true` gets restart-on-failure 3 x 1 min. When the functions are missing it falls back
  to the 2.2 task set.
- *U (extra fields):* run `results[].step`; `GET /api/upkeep/settings` adds `wu_slots`; `/api/state` nodes add
  `settings.wu_window` and `upkeep: {wu_last, wake}` (`wake` = offline with a known MAC). Maintenance runs and previews
  also accept a raw catalog action name (except `forge_uninstall`), so the schedule can list `update_t3`, `update_ollama` and
  `winget_upgrade`. A scheduled action may not need args or be destructive (PUT returns 400).
- *Defaults to note:* Windows Update windows and the weekly schedule are **on** by default (`enabled: true`, as the spec
  decided), so a 2.2 fleet upgraded to 2.3 starts both automatically. On its first tick the leader merges the 2.2 per-folder
  scratch/temp projects into `misc:<node>`. `reboot_pending` is raised with level `info`.
**F-2 (workstream F, review fixes on p3-upkeep).**
- *Marked change:* `free_disk` is now `destructive: true` (it deletes files as SYSTEM). It removes links (junctions,
  symlinks) itself and never follows them, skips a root that is or sits under a link, ages a `<work_root>\scratch` folder by
  the newest file in its tree, and empties every user's `$Recycle.Bin\<SID>` (not just SYSTEM's).
- *Maintenance runs:* a finished `reboot` or `restart_task ForgeHub|ForgeAgent` step puts the run in a wait state
  (`results[-1].wait = {kind: reboot|hub|agent, node_id, since, boot, back?}`). The run advances only when the machine is back
  (new `info.boot`, or a boot report), the hub is healthy again (at least 60 s later), or the agent sent its boot report;
  after 30 min (reboot) / 10 min (hub, agent) the step fails with `upkeep_failed`. A run also waits before queueing work on
  a machine whose post-update reboot is imminent. One run whose stored args no longer validate is failed and never blocks
  the tick.
- *Mixed fleet:* the 2.3-only system actions (`windows_update`, `free_disk`, `update_ollama`, `forge_uninstall`) are skipped
  with `needs agent 2.3` (not failed) on a node whose `nodes.sys_version` < 2.3.0; the nightly Windows Update slot does the same.
- *Windows Update:* the stagger shrinks so `rank x stagger` never wraps past the window (leader stays last). The post-update
  reboot waits (`wu_last.reboot = "waiting"`) while the node has queued/running jobs or a maintenance step, and becomes
  `pending` (+ `reboot_pending`) when the window closes. `wu_last` stores `reboot_job` / `reboot_ts`; a reboot job that failed
  (e.g. admin lane disabled), was cancelled, or did not reboot the machine within delay + 15 min turns into `pending` + alert.
  "Another healthy hub" excludes a hub that is itself running/queued for `windows_update`/`reboot`. A node with a maintenance
  step in progress is not given its Windows Update slot until the step ends.
- *Repo sync:* `repo_conflict` only for an agent-reported `##RESULT` `diverged`/`error`; cancelled, lost and timed-out sync
  jobs (no result) never raise it.
- *Settings:* `PUT /api/upkeep/settings` validates every section before writing any (400 leaves all prefs unchanged).
- *Uninstall:* the Forge home must be the real layout (`<FH>\current\agent` or `<FH>\releases\<ver>\agent`, with
  `data\config.json` and `releases\`) and the ForgeSystemAgent task must point into it; the generated script is written to a
  fresh `%SystemRoot%\Temp\forge-uninstall-<guid>` folder created with a SYSTEM + Administrators-only ACL.
- *Installer:* re-running `-NewFleet` keeps an existing `admin_pin_hash` unless `-ResetPin`; `-AdminPin` is refused outside
  `-DryRun` (`FORGE_ADMIN_PIN` is the unattended alternative and is cleared after reading); `data\private` is restricted to
  Administrators + SYSTEM before `secrets.json` / `firstrun-claim.json` are written, and again after the 2.2-style ACL fallback.

(U, 2.3.0, branch p3-ux)
- **U-1 (U, 2.3.0):** `GET /api/ask` with an empty `q` answers `{results: []}` (no run entry: there is nothing to run). `limit`
  counts the run entry. `GET /api/today` items in `needs_you` always carry `action` (hub default when the alert has none, see
  HUB-CONTRACT.md "Phase 3 ... workstream U"). `hub/static/icons/badge-72.png` is an extra icon (notification badge).
- **U-2 (U → R, request):** `GET /api/machines/{id}/retire` for a known node that was never retired should answer 200 with
  `state: null` (the UI asks when the machine's Settings tab opens; a 404 there is tolerated but shows up in the browser console).
- **U-3 (U → S, note):** the UI accepts the step-up 403 body both as the contract's flat `{"detail", "step_up"}` and as FastAPI's
  `{"detail": {"detail", "step_up"}}` (what `HTTPException(403, {...})` produces).

(Lead, 2.3.0 integration, branch p3-integrate)
- **L-1 (presence reads):** per R-A1, `forge_alerts` (conditions), `forge_security` (old agents/hubs, `joined`, job-token
  alerts), `forge_ui` (machine list) and `forge_upkeep` (WoL MACs, `sys_version` gate) read `nodes_live`, not `nodes`.
- **L-2 (tests):** R/F tests call step-up routes with `p2util.admin(c)` (sets the PIN, sends `X-Admin-Pin`); N tests report
  as 2.3 agents (`metrics.job_token`); the U fallback tests force the pre-N path; R's "uninstall not in catalog" 409 case
  is dropped (F ships `forge_uninstall`); F's dry-run ACL check accepts S's `Set-ForgeDataAcl` plan; F's PS scripts also
  accept the lead range 19280-19299.
