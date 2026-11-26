# Hivemind node (v2)

Every machine runs the same bundle (hub + agent + watchdog) from `%ProgramData%\Forge`.

## Install / repair / upgrade

Open an **elevated** PowerShell and paste the one-liner from the dashboard's "+ Add machine" dialog:

```powershell
irm 'http://<any-hub>:8787/agent/install.ps1?t=<agent_token>' | iex
```

You can re-run it whenever you like: it repairs or upgrades the install, and it never downgrades `current`. Here is what it does:
Python 3.10+ (installs it with winget if missing), then `GET /cluster/bootstrap` to fetch the tokens and the peer list into
`data\config.json` (the peer list goes in `seed_peers`), then it downloads the bundle and checks its sha256 →
`releases\<ver>`, creates the `current` junction, the venv and runs pip. After that it adds the firewall rule for 8787
(LocalSubnet + 100.64.0.0/10), turns off sleep on AC, and sets up Tailscale (if Tailscale needs a login it prints the URL and
carries on). Then it runs `POST /cluster/join`, swaps the v1 tasks for the three v2 tasks, starts everything and creates the
desktop shortcut **Hivemind** → http://localhost:8787.

To see what it would do without changing anything:
`.\agent\install.ps1 -DryRun -Hub http://forge-01:8787 -Token <t>`. The test version is `tests\ps\dry-run.ps1`, which uses a fixture.

## Layout

```
%ProgramData%\Forge\
  releases\<ver>\     extracted bundles (hub\ agent\ watchdog\ requirements.txt VERSION bundle.zip bundle.sha256)
  current  ->         junction to the active release
  venv\               python venv for the hub
  data\               config.json (tokens, seed_peers; readable only by Admins/SYSTEM/you), agent-config.json,
                      forge.db, hub.log, agent.log, watchdog.log, watchdog.json, watchdog-state.json, bad-versions.txt
```

ACL on `%ProgramData%\Forge` (2.2): Administrators and SYSTEM get full control, the installing user gets read + execute
only, and inheritance is removed (root only, then `icacls <root>\* /reset /T`), so other users can't read it and the user
account cannot change the code SYSTEM runs (`releases\`, `current`, `venv\`). `data\` adds Modify for the user (agent
config, logs, `running.json`, hub state). `data\config.json` (tokens) is Administrators + SYSTEM full, user read only.
Consequence: a hub running as the user can no longer write `releases\` itself, so `POST /cluster/publish` on a node needs
the hub side to stage releases elsewhere (or publish from an elevated shell). The installer also runs `w32tm /resync` once.

## Scheduled tasks

| Task | Runs as | When | What |
|---|---|---|---|
| `ForgeHub` | you (S4U, no password stored) | at startup | `current\watchdog\run-hub.cmd` → uvicorn on 0.0.0.0:8787, `FORGE_DATA`/`FORGE_RELEASE_DIR`/`FORGE_RELEASES_DIR` set, log `data\hub.log` (rotated at 5 MB on start) |
| `ForgeAgent` | you (S4U) | at startup | `current\agent\forge-agent.ps1` (supervisor + worker): reports to the first hub in `hub_urls` that answers (localhost first, retried every 60 s) and runs jobs in `<work_root>\scratch\<job id>` |
| `ForgeSystemAgent` | SYSTEM | at startup (restart on failure) | `current\agent\forge-agent.ps1 -Lane system`: the 2.1 admin lane. Runs only Admin jobs (allowlisted actions from `hub\actions.json` via `agent\actions.ps1`, or custom PowerShell) without UAC prompts. Log `data\agent-system.log`, job dirs `<work_root>\jobs\sys-<id>`. Disable per machine with `"allow_admin": false` in `agent-config.json`. Nodes already on v2 get this task from the watchdog after the bundle update. |
| `ForgeWatchdog` | SYSTEM | every minute + at startup | `current\watchdog\watchdog.ps1`: self-heal, self-update, rollback, prune |

The v1 tasks `ForgeControl-Hub` / `ForgeControl-Agent` are removed. `%USERPROFILE%\forge-agent` is left alone, so you keep the
job history, but its config is renamed to `agent-config.json.v1-migrated`.

**Work root:** the agent uses the fixed drive with the most free space and creates `X:\forge\{jobs,repos,scratch}`. It sticks
with that choice (`work_root_auto` in agent-config.json; the effective root, override included, is `work_root_current`).
You can override it per machine in the UI under Machine → Settings, which also has `max_jobs`. The watchdog deletes
`<work_root>\jobs\<id>` dirs untouched for 14 days (at most once an hour; jobs in `running.json` are kept; `rmdir /s`, which
never follows junctions a job left behind). `scratch\<id>` dirs are not pruned.

**Jobs (2.2):**
- Each job without a usable `cwd` runs in its own folder `<work_root>\scratch\<job id>` (resume jobs: the session's cwd).
- Defaults: claude `--permission-mode acceptEdits`, codex `--sandbox workspace-write`. `full_access` switches to
  `bypassPermissions` / `--dangerously-bypass-approvals-and-sandbox`.
- `job.timeout_s` (default 7200): when it passes, the agent kills the process tree (`taskkill /T /F`) and reports the job
  `failed` with a note.
- Engine `ollama` runs only jobs of `kind` `text` (a job without `kind` counts as text).
- Orphan safety: running jobs are kept in `data\running.json` (system lane: `running-system.json`; id, pid, engine, output
  offsets, start time, timeout; no paths: they come from the job id). A restarted worker re-attaches to every job whose
  process is still alive and whose command line names the job's own dir, and continues its output from the saved offset.
  Jobs whose process is gone are reported `failed` (exit code unknown) with their remaining output. Every report (both
  lanes) carries `running: [job ids]` and `agent_time` (epoch seconds, float); on `boot: true` the hub should fail only
  jobs that are NOT in `running` (hub side).
- Agent status lines in a brief job's output (`source: "brief"`) start with `[forge] `, which the hub's brief noise
  filter drops, so stored briefs hold only the model's text.
- Jobs still run with the agent's full admin token (S4U tasks are not UAC-filtered); Phase 3 moves job launch to a
  SAFER normal-user token.

## Logs

- `data\hub.log` (and `hub.log.old`)
- `data\agent.log`: prints a heartbeat line every 60 s. If it goes stale for 2 min, the watchdog restarts the agent.
- `data\watchdog.log`: one `ACTION ...` line per thing it did. `data\watchdog.json` holds the last run summary and is shown in the UI.
- Job output: `<work_root>\jobs\<job id>\out.log`

The agent ships the tails of agent.log, watchdog.log and hub.log to the hub, so you can read them in the UI too.

## Updates

- **Publish:** run `.\tools\publish.ps1` from a repo checkout. It bumps nothing, so bump `VERSION` first. It builds
  `dist\forge-<ver>.zip` and POSTs it to `http://localhost:8787/cluster/publish`. Add `-Hub` to target another hub, and
  `-DryRun` to only see what it would send.
  Use `.\tools\build-bundle.ps1` if you only want the zip and its sha256.
- **Propagation:** each watchdog upgrades to the highest `version` it sees among healthy peers, including its own hub's
  published-but-not-active release. It downloads `/cluster/bundle?version=X`, checks the sha256 against the peer's status,
  and makes sure the scripts parse. Then it runs pip only if `requirements.txt` changed, flips `current` and restarts the
  hub. The agent finishes its running jobs, then restarts itself on the new code.
- **Automatic rollback:** if `/healthz` fails on 3 runs in a row after an update, the watchdog flips `current` back,
  restarts the hub and appends the version to `data\bad-versions.txt`. That version is never auto-installed again.
- It keeps the last 3 releases (always including the active one and the rollback target).
- **Staggered rollout (2.2):** the download and extract can happen at any time, but the watchdog only activates a newer
  release (pip, flip, hub restart) while it holds the leader's update lease (`POST /cluster/update-lease`). The hub hands
  the lease out canary first and the leader last. While it waits, `watchdog.log` says `waiting for update lease (holder X)`
  and `watchdog.json` shows `lease_wait`. If the leader is a pre-2.2 hub (404), the watchdog behaves as before. If no
  leader answers for more than 5 minutes, it goes ahead anyway (lone survivor). Once the new release is confirmed healthy
  it gives the lease back (`DELETE /cluster/update-lease/<node>`). A leader that answers with an error (401/422/500/502)
  is not "no leader": the watchdog keeps waiting and the 5-minute lone-survivor clock restarts (only connection failures
  and 503 "no leader" count). After a rollback it gives it back and
  reports the version with `POST /cluster/bad-version` (sent to the local hub, which proxies it to the leader, when no lease was held); the version also shows up in `watchdog.json` `bad_versions`. Versions listed in the leader's
  `/cluster/status` `bad_versions` (fleet-bad) are never installed (`watchdog.json` `fleet_bad_versions`).

## Sessions, transcripts, services (2.2, user lane)

The user-lane agent (`agent\sessions.ps1`, `agent\services.ps1`) only reads metadata. Full transcripts are sent only
while someone has a session open in the UI.

- **Session index**, every 30 s: Claude Code `%USERPROFILE%\.claude\projects\<slug>\<uuid>.jsonl` (top level only, so
  `subagents\` is skipped) and Codex `%USERPROFILE%\.codex\sessions\**\*.jsonl` (current and 2025 formats), for files
  written in the last 14 days. Parsing is incremental: it keeps a byte offset per file and reads only appended bytes. Each
  cycle has a budget of 1 s / 16 MB, and a big file carries on into the next cycle. A session is sent once its file is fully
  parsed, then again whenever it changes (state transitions included), and everything is resent every 10 min. The
  payload goes to `POST /api/agent/sessions` as `{node_id, sessions, removed:[session_id...], t3}`. Titles are the first
  real prompt (not injected `<environment_context>` or slash-command wrappers), whitespace-collapsed, redacted and cut to
  140 chars. Extra fields: `ai_title` (Claude's own title) and `entrypoint`. `started` and `last_activity` (file mtime) are
  epoch seconds. `tokens` for Claude is input + cache creation + output, counted once per API message (cache reads
  excluded). For Codex it is the last `total_token_usage`.
- **Live process → session (`pid`)**: first Claude's own `%USERPROFILE%\.claude\sessions\<pid>.json` (sessionId +
  procStart, checked against the process start time so a reused pid isn't trusted), then `--resume/--session-id/-r <id>` or
  `codex ... resume <id>` in the command line. Otherwise it takes the newest session file of that tool written since the
  process started (process cwd isn't readable without PEB tricks), and only if both are batch (`codex exec`, `claude -p`
  / entrypoint `*exec*` / `sdk*`) or both interactive, so a terminal session never claims a finished Forge job's session.
  Sessions that started after the process win over ones that were only written later. The Claude/Codex desktop apps (WindowsApps,
  Electron `--type=`, `app-server`) are ignored.
- **Git**: remote and branch per cwd are cached for 10 min (at most 6 lookups per cycle; credentials are stripped from
  URLs). Every 5 min, for each known repo cwd, it runs `git --no-optional-locks status --porcelain=v1 -b` and
  `git log -5` and sends `POST /api/agent/commits` (at most 20 repos / 3 s per run, rotating).
- **t3**: thread collection is not implemented, so when the `t3` CLI is installed the agent sends `t3: "unsupported"`
  (no `t3` CLI: the field is left out).
- **Transcripts**: report response `watch: [{tool, session_id, since}]` → `POST /api/agent/transcript`
  `{node_id, tool, session_id, cursor, events, done, state}`. **cursor = byte offset in the session file** just past the last
  line included. `since: null` sends the last 200 events (read backwards, at most 8 MB) with cursor = end of file.
  `since: N` sends at most 200 events after N, and the cursor stops at the last line fully included, so nothing is lost or
  repeated. `done` = true when the cursor reached the end of the file (caught up). A cursor beyond the file (rewritten) is
  treated as `null`. Nothing is posted when nothing changed. Per cycle it handles up to 4 watches, rotating through the
  list so a 5th+ watched session is served too, and only one of them can be a first-call tail. If a report fails, the
  watch list is dropped until the next good report; if a watch still carries the `since` of a batch already posted, the
  agent continues after that batch instead of resending it. An unknown session gets
  `{cursor: null, events: [], done: true, error: "session not found on this node"}` (at most once a minute); normal
  batches carry `state` (live/idle/done). The hub should store `error` and `state` and return them from
  `GET /api/sessions/{tool}/{id}/transcript` so the UI can tell "not on this node" from "no events yet". Events:
  `{ts, role, kind, text (<= 4000 chars, redacted), tool_name?, collapsed}`. Tool calls/results and thinking come
  collapsed. Bookkeeping records, sidechains, `isMeta` and malformed lines are skipped.
- **Redaction** (titles, transcript text, commit subjects), in order: PEM private key blocks → `[redacted private key]`;
  URL credentials `scheme://user:pass@` → `scheme://[redacted]@`; `Bearer` / `Basic` tokens; values of `KEY=value`,
  `key: value`, `"key": "value"` and connection-string pairs whose name contains PASSWORD, PASSWD, SECRET, TOKEN, API_KEY,
  ACCESS_KEY, ACCOUNTKEY or PRIVATE_KEY (numeric values such as token counts are kept); then
  `/(sk-|ghp_|xox|AKIA|eyJ)[A-Za-z0-9_\-]{10,}/` (covers `sk-ant-`) plus `github_pat_`, `glpat-` and `AIza` keys →
  `[redacted]`. Text is redacted before it is cut. The hub's `SECRET_RE` should mirror this.
- **Resume jobs** (`job.resume = {tool, session_id}`): claude runs
  `claude -p --resume <id> --permission-mode <mode> --output-format text < prompt` (`ANTHROPIC_API_KEY` /
  `ANTHROPIC_AUTH_TOKEN` are still cleared unless `claude_use_api_key`). Codex runs
  `codex exec --skip-git-repo-check <sandbox flags> resume <id> - < prompt`: the exec options have to come *before*
  `resume`, because `codex exec resume` rejects `--sandbox`. If the job has no usable `cwd`, the agent uses the session's
  own cwd. When the job ends, the session is rescanned and resent at once. Optional `job.model` becomes `--model`
  (claude) or `-m` (codex). Ids and model names are validated before they touch a command line.
- **Engine `ollama`**: `agent\ollama-job.ps1`, REST only. `GET /api/tags`; if the model is missing, `POST /api/pull`
  (streamed, with one progress line per status / 10 %); then `POST /api/generate {model, prompt, stream:false}`. The
  model is `job.model`, else agent-config `ollama_model`, else `qwen2.5:14b`. The URL is agent-config `ollama_url`
  (default `http://127.0.0.1:11434`), and the engine is available if the `ollama` tool exists or `ollama_url` is set.
  `ollama_timeout_s` defaults to 3600. Text jobs only (see Jobs above).
- **Keep-alive services** (replaces the 2.1.2 T3 autostart): agent-config `services`. The default is T3 via
  `watchdog\start-t3.ps1` (exit 0 = running, 10 = started, 20 = not set up → not reported) plus `ollama`. For `ollama`,
  when the tool exists it checks `http://127.0.0.1:11434/api/version` every 60 s and, if that fails, starts
  `<ollama.exe> serve` hidden with `OLLAMA_HOST=127.0.0.1:11434`. A restart gets 120 s of grace, and after 3 failed
  starts it backs off for 10 min. Results are reported as `metrics.services: [{name, ok, since (epoch)}]`.
  `"t3_autostart": false` still turns T3 off, and `"services": []` turns everything off.
- Hub endpoints that answer 404/405 (hubs older than 2.2) are retried every 10 min. Other agent-config knobs:
  `sessions_enabled` (default true), `claude_home`, `codex_sessions_dir`, `sessions_interval_s`, `commits_interval_s`,
  `services_interval_s`, `tools.<name>` (path override).

### By hand (elevated PowerShell)

```powershell
$F = "$env:ProgramData\Forge"
Get-Content "$F\current\VERSION"; Get-ChildItem "$F\releases"            # what's active / available
# switch to another release (rollback or forward):
cmd /c rmdir "$F\current"; cmd /c mklink /J "$F\current" "$F\releases\2.0.0"
Add-Content "$F\data\bad-versions.txt" '2.0.1'                           # stop the watchdog re-installing it
Stop-ScheduledTask ForgeHub; Start-ScheduledTask ForgeHub                # agent restarts itself when VERSION changes
# watchdog once, printing what it would do:
powershell -NoProfile -ExecutionPolicy Bypass -File "$F\current\watchdog\watchdog.ps1" -DryRun
```

Never delete the `current` junction with `Remove-Item -Recurse`. On PS 5.1 that can delete the release behind it. Use
`cmd /c rmdir`.

## Tests

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tests\ps\run-tests.ps1    # parse + unit (incl. 2.2 parsers on tests\ps\fixtures\sessions) + scratch release flips
powershell -NoProfile -ExecutionPolicy Bypass -File tests\ps\dry-run.ps1      # installer + watchdog dry runs + update-lease client + job dir pruning (fake hub on 19052)
powershell -NoProfile -ExecutionPolicy Bypass -File tests\ps\agent-smoke.ps1  # agent vs fake hub on 19050 + fake ollama on 19051: shell, resume (fake claude), ollama brief, timeout, worker-restart re-attach, sessions, watch, commits, services
powershell -NoProfile -ExecutionPolicy Bypass -File tests\ps\agent-system-smoke.ps1  # SYSTEM lane (runs as you, actions dry-run) vs fake hub on 19053
```
The session fixtures are synthetic but follow the real Claude Code / Codex record shapes, and every secret in them is fake.
The smoke test never reads the real `~/.claude` or `~/.codex`: it points `claude_home` and `codex_sessions_dir` at a
scratch dir.
