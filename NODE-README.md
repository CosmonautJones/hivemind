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

ACL on `%ProgramData%\Forge`: Administrators, SYSTEM and the installing user get full control. Inheritance is removed,
so other users can't read it.

## Scheduled tasks

| Task | Runs as | When | What |
|---|---|---|---|
| `ForgeHub` | you (S4U, no password stored) | at startup | `current\watchdog\run-hub.cmd` → uvicorn on 0.0.0.0:8787, `FORGE_DATA`/`FORGE_RELEASE_DIR`/`FORGE_RELEASES_DIR` set, log `data\hub.log` (rotated at 5 MB on start) |
| `ForgeAgent` | you (S4U) | at startup | `current\agent\forge-agent.ps1` (supervisor + worker): reports to the first hub in `hub_urls` that answers (localhost first, retried every 60 s) and runs jobs in `<work_root>\scratch` |
| `ForgeSystemAgent` | SYSTEM | at startup (restart on failure) | `current\agent\forge-agent.ps1 -Lane system`: the 2.1 admin lane. Runs only Admin jobs (allowlisted actions from `hub\actions.json` via `agent\actions.ps1`, or custom PowerShell) without UAC prompts. Log `data\agent-system.log`, job dirs `<work_root>\jobs\sys-<id>`. Disable per machine with `"allow_admin": false` in `agent-config.json`. Nodes already on v2 get this task from the watchdog after the bundle update. |
| `ForgeWatchdog` | SYSTEM | every minute + at startup | `current\watchdog\watchdog.ps1`: self-heal, self-update, rollback, prune |

The v1 tasks `ForgeControl-Hub` / `ForgeControl-Agent` are removed. `%USERPROFILE%\forge-agent` is left alone, so you keep the
job history, but its config is renamed to `agent-config.json.v1-migrated`.

**Work root:** the agent uses the fixed drive with the most free space and creates `X:\forge\{jobs,repos,scratch}`. It sticks
with that choice (`work_root_auto` in agent-config.json). You can override it per machine in the UI under Machine → Settings,
which also has `max_jobs`.

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
powershell -NoProfile -ExecutionPolicy Bypass -File tests\ps\run-tests.ps1    # parse + unit + scratch release flips
powershell -NoProfile -ExecutionPolicy Bypass -File tests\ps\dry-run.ps1      # installer + watchdog dry runs (fake hub on 18897)
powershell -NoProfile -ExecutionPolicy Bypass -File tests\ps\agent-smoke.ps1  # agent vs fake hub on 18896, one shell job
powershell -NoProfile -ExecutionPolicy Bypass -File tests\ps\agent-system-smoke.ps1  # SYSTEM lane (runs as you, actions dry-run) vs fake hub on 18851
```
