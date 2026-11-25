# Hivemind — Phase 2 spec: Projects ("Jarvis"), live sessions, briefings, smart scheduling

Builds on v2.1.4 (SPEC-phase1.md, HUB-CONTRACT.md, NODE-README.md). Same rules: pull model (agents
report every 5 s, hub answers with work), PS 5.1 + Python 3.10 compatible, KISS UI, mobile-first.
Target release: **2.2.0**. All new hub data tables replicate to standbys like Phase 1 tables
(`updated` column + TEXT ids) unless marked *ephemeral*.

## A. Staggered rollout (fixes leaderless window when every hub updates at once)
- Leader grants an **update lease**: `POST /cluster/update-lease` (cluster token) `{node_id, version}` →
  `{granted: bool, holder, retry_after_s}`. One holder at a time, lease TTL 10 min, released by
  `DELETE /cluster/update-lease/{node_id}` or automatically when that node's hub reports
  `running_version == version` and healthy for 60 s.
- Order: standbys by **descending** priority number (lowest-priority node first = canary), **leader last**;
  the leader only takes the lease when every other healthy peer runs the target version (unreachable
  peers for > 10 min are skipped).
- Watchdog: before activating a newer version (download/extract may happen anytime), it must hold the
  lease. If no leader is reachable for > 5 min, it may proceed (lone-survivor case). Log "waiting for
  update lease (holder X)".
- If a canary rolls back (bad version), leader marks the version **fleet-bad**: `/cluster/status` includes
  `bad_versions: [...]`; watchdogs never install a fleet-bad version. UI Health shows it.

## B. Session index (agent user lane → hub)
Every 30 s the user-lane agent scans and sends **metadata only** (never full transcripts) for:
| tool | source | id |
|---|---|---|
| `claude` | `%USERPROFILE%\.claude\projects\<slug>\<uuid>.jsonl` | uuid |
| `codex` | `%USERPROFILE%\.codex\sessions\**\*.jsonl` | session id from the file's first `session_meta` line (or filename uuid) |
| `t3` | `t3 thread list --json` / `t3 project list --json` if the CLI supports it (check `--help`; else skip t3 and set `t3: unsupported`) | thread id |
| `forge` | the hub already has jobs | job id |
Only sessions with activity in the last 14 days. Parse incrementally (remember file size/offset; never
re-read whole large files every cycle).
Per session send: `{tool, session_id, node_id, cwd, git_remote, git_branch, title (first real user prompt,
<=140 chars, secrets redacted: strings matching /(sk-|ghp_|xox|AKIA|eyJ)[A-Za-z0-9_\-]{10,}/ → "[redacted]"),
started, last_activity, messages (count), tokens? (if present), state: "live"|"idle"|"done",
remote_control: bool (if detectable), pid (if a live claude/codex process has this session open — match by
`--resume <id>`/`--session-id <id>` in its command line or by cwd + most recent file), model}`.
`state`: live = file written < 90 s ago OR owning process alive; idle = otherwise within 24 h; done = older.
`git_remote` normalized by the HUB: `https://github.com/Owner/Repo.git`, `git@github.com:Owner/Repo` →
`github.com/owner/repo`. Agent caches `git -C <cwd> remote get-url origin` + branch per cwd for 10 min.
Endpoint: `POST /api/agent/sessions` (agent token) `{node_id, sessions:[...], removed:[ids]}` — upsert.
Also every 5 min per known repo cwd: `git log -5 --format=%H%x1f%an%x1f%at%x1f%s` → `POST /api/agent/commits`
`{node_id, repos:[{cwd, git_remote, branch, dirty: bool, ahead, behind, commits:[{sha, author, ts, subject}]}]}`.

## C. Live transcript (on demand, ephemeral)
- UI opens a session → `POST /api/sessions/{tool}/{id}/watch` (UI) → hub remembers watchers for 30 s
  (refreshed by the UI every 10 s while open).
- Report response gains `watch: [{tool, session_id, since: <cursor or null>}]` for that node.
- Agent then sends `POST /api/agent/transcript` `{node_id, tool, session_id, cursor, events:[...], done}`
  with up to 200 new parsed events per cycle (first call: last 200). Event shape:
  `{ts, role: "user"|"assistant"|"tool"|"system", kind: "text"|"tool_call"|"tool_result"|"thinking",
  text (<= 4000 chars, redacted), tool_name?, collapsed: bool}`. Hub keeps the last 1000 events per
  session in memory/ephemeral table; `GET /api/sessions/{tool}/{id}/transcript?since=` serves them.
- Parsers: Claude Code jsonl (`type: user|assistant|system`, message.content blocks text/tool_use/
  tool_result/thinking) and Codex jsonl (`response_item` / `event_msg` records). Be tolerant: unknown
  records → skip; malformed line → skip.

## D. Continue / take over
New job fields: `resume: {tool, session_id}` (+ `node_id` must be the session's node, `cwd` = its cwd).
- claude: `claude -p --resume <id> --permission-mode <mode> --output-format text < prompt`
- codex: `codex exec resume <id> - < prompt` (sandbox flags as today)
Hub refuses (409 "session is live in a terminal") when the session `state == "live"` and it has a `pid`,
unless `force: true`. After the job the agent rescans that session immediately.
"New task here": a normal job with the project's node + cwd prefilled.

## E. Projects (hub)
- Table `projects(id, key, name, group_name, pinned, created, updated)`; `key` = normalized git remote, or
  `local:<node_id>:<cwd-lower>` for non-git folders. Sessions/jobs/commits link by key (sessions table
  `sessions(tool, session_id, node_id, project_key, ... , updated)`). Auto-create on first sight;
  name = repo name (or folder name). Ignore cwd = user profile root, system32, temp, Forge scratch
  (`\forge\scratch`) → project key `misc:<node_id>` named "Scratch / misc".
- `GET /api/projects` → `[{id, key, name, group_name, pinned, machines:[node_ids], live_sessions,
  sessions_24h, last_activity, brief: {text, ts, model} | null, dirty_any, branches:[...]}]` sorted pinned,
  then last_activity desc.
- `GET /api/projects/{id}` → project + `sessions:[...]` (newest first, all tools incl. forge jobs),
  `commits:[...]` (merged across machines, dedup by sha), `repos:[{node_id, cwd, branch, dirty, ahead,
  behind}]`, `briefs:[last 10]`.
- `PATCH /api/projects/{id}` `{name?, group_name?, pinned?}`; `POST /api/projects/merge` `{ids}` (manual
  "these are related": merges into first; keeps an alias table so future sessions map there).

## F. Briefings
- Hub loop every 30 min: for each project with new sessions/commits/jobs since its last brief, enqueue a
  **brief job**: engine `ollama` on the node that has `ollama` tool AND the `briefs` capability (config
  `brief_node`, default: the node with the most GPU memory that has ollama), model from hub config
  `brief_model` (default `qwen2.5:14b`). If that node is offline the brief waits (do NOT fall back to
  Claude automatically). Max 6 brief jobs per loop.
- Prompt built by the hub from metadata only: project name, sessions (tool, title, state, last activity,
  message count), last commits, dirty/ahead/behind, recent forge job titles + status. Ask for: 3-6 bullets
  "what's happening / what changed / what's blocked / suggested next step", <= 120 words.
- `POST /api/projects/{id}/brief` (UI "Brief me") → engine `claude` with `--model haiku` on Auto node,
  marked `source: "brief"`; also `POST /api/brief/all` → one Claude roll-up across all active projects.
- Brief jobs are normal jobs (`source: "brief"`, `project_id`) hidden from the main Jobs table by default;
  result text stored into `briefs(project_id, text, ts, model, job_id)`.
- New agent engine `ollama`: `POST http://127.0.0.1:11434/api/generate {model, prompt, stream:false}`
  (Invoke-RestMethod; no CLI parsing). Agent keeps `ollama serve` alive (see H) and, on first use of a
  model that `GET /api/tags` lacks, runs `ollama pull <model>` once (streaming progress to job output).

## G. Smart scheduler + rate limits
- Job `engine` may be `"any"` (Claude → Codex → ollama preference, configurable) and `node_id` `"auto"`.
- Score for auto placement (higher wins), among online nodes whose agent has the engine tool and capacity:
  `100 - cpu*0.5 - mem_pct*0.3 - active_jobs*15 - (is_leader ? 10 : 0) + (gpu && engine == ollama ? 40 : 0)
  - (work_root_free_gb < 20 ? 50 : 0)`; ties → most free capacity → priority.
- **Account cooldowns** (accounts are shared across machines!): when a claude/codex job's output matches
  usage-limit patterns (`/usage limit|rate limit|limit reached|try again (at|in)|5-hour limit|quota/i`)
  the hub sets `cooldowns[engine] = until` (parse a reset time if present, else +30 min), emits a warn
  event, and: queued jobs with that explicit engine stay queued with `wait_reason: "claude cooling down
  until HH:MM"`; `any` jobs pick the next engine. `GET /api/scheduler` → `{cooldowns, queue_depth,
  placement_preview}`; `DELETE /api/scheduler/cooldowns/{engine}` to clear manually.
- **Requeue on node loss**: a job lost because its node went offline is requeued once (to `auto` placement
  if it was `requested: auto`, else same node) ONLY if it produced < 1 KB output and ran < 60 s;
  otherwise failed with note. `attempts` column.

## H. Agent: keep-alive services (generalizes T3 autostart)
agent-config `services` (defaults if absent): `[{name: "t3", check: start-t3.ps1}, {name: "ollama",
when_tool: "ollama", start: "<ollama.exe> serve", env: {OLLAMA_HOST: "127.0.0.1:11434"}, health:
"http://127.0.0.1:11434/api/version"}]`. Every 60 s: if health fails → start hidden. Report
`metrics.services: [{name, ok, since}]`. UI shows service chips (T3 ✓, Ollama ✓).

## I. UI
- New first tab **Projects**: search box; cards (name, group, machine chips, live-session count with pulsing
  dot, last activity, brief excerpt 2 lines, dirty/ahead badges); "Brief me" (all) button.
- Project page (drawer or full view on mobile): header (name editable, pin, group, machines), latest brief
  + "Brief me" + brief history, **Sessions** list (tool icon/label, title, machine, state dot, last activity,
  messages) → click → **Session view**: live transcript as chat (user right/assistant left, tool calls as
  collapsed one-line chips expandable, thinking hidden by default), auto-scroll when at bottom, a prompt box
  at the bottom = Continue (disabled with explanation when live in a terminal; "Take over anyway" with
  confirm), buttons **Open in T3** (`http://<node addr>:3773` using the node's Tailscale/LAN address from
  peer urls) and **Open in claude.ai** (`https://claude.ai/code` when remote_control). **Commits** list,
  **Repos** (per machine branch/dirty/ahead/behind), **New task here**.
- Machine cards: **Open T3** and **Pair phone** (runs `t3 pair --ttl 10m` as a user-lane shell job, shows
  the pairing URL as a big link + copy button) and service chips.
- Jobs tab: engine "Any" option; wait_reason shown for queued jobs; scheduler strip (cooldowns with clear
  button). Health: fleet-bad versions, update lease holder.

## J. Tests (must stay fast; pytest + tests/ps)
- Rollout lease: ordering canary→…→leader, single holder, TTL expiry, lone-survivor, fleet-bad.
- Sessions/projects: remote normalization table, misc mapping, merge+alias, live/idle/done, 409 on live
  continue, transcript watch/serve/cap, redaction.
- Scheduler: scoring, cooldown detection + reset parse, `any` fallback, requeue rules.
- **Chaos simulation** (pytest, in-process, fake clock): 4 hubs + 30 fake agents, 500 jobs, random leader
  kills / agent drops / partitions for 30 simulated minutes. Invariants: no job lost without a terminal
  status, no job dispatched to two agents, leader re-elected < 60 s simulated, all hubs converge.
- PS: Claude + Codex jsonl parsers on fixture files (write realistic fixtures), title redaction, session
  state calc, services keep-alive decision, lease-wait logic in watchdog.
