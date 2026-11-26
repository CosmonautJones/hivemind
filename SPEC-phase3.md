# Hivemind — Phase 3 spec: complete, safe, intuitive (release 2.3.0)

Inputs: systems-advisor audit, UX-advisor walkthrough (2026-11-25), live finding that **every user-lane
job runs with a full admin token** (S4U tasks are not UAC-filtered; verified on all 4 nodes:
High integrity, Administrators enabled). Phase 2 (2.2.0) already absorbs: cooldown false positives,
`any`→ollama only for text jobs, brief window + GPU gate + digest, canary flag, merge-on-election,
clock-skew alerts, retention, job defaults that can edit, per-job scratch, timeouts, orphan reattach,
code-dir ACL lockdown in the installer, Hivemind branding + UX P0 copy fixes, PWA basics.

Principles: **safe by default, quiet by default, one obvious way.** The user should never need to know
the words leader, lane, lease, S4U or watchdog.

---

## Workstream S — Security (P0)
S1. **Least-privilege jobs.** User-lane jobs launch with a SAFER `NORMALUSER` token derived from the
agent's own token (`SaferCreateLevel(SAFER_SCOPEID_USER, SAFER_LEVELID_NORMALUSER)` →
`SaferComputeTokenFromLevel` → `CreateProcessAsUser`, stdin/stdout via the same file redirection as today,
P/Invoke from PowerShell via Add-Type C#). Admin group becomes deny-only, Medium integrity. Verify claude,
codex, git, node, python, ollama all work from the filtered token (user profile + creds are the same user).
Agent reports `metrics.job_token: "filtered"|"full"`; UI alert if "full". Fallback: if Safer APIs fail,
refuse full_access jobs and run normal jobs anyway but raise an error alert (never silently elevate).
S2. **Secret split.** `data\config.json` (user-readable) keeps only `node_id`, `agent_token`, `port`.
`data\secrets.json` (Administrators+SYSTEM only — readable by the hub because the hub's S4U token is admin;
NOT readable by filtered jobs) holds `ui_token_hash`, `cluster_token`, `signing_pubkey`.
S3. **Enrollment codes.** `POST /api/enroll` mints a single-use code (10 min TTL, replicated). The one-liner
carries the code, not a token; `/cluster/bootstrap?code=` redeems it once. Command uses the leader's
Tailscale MagicDNS URL first, never `localhost`.
S4. **Per-device sessions.** Login (or a pairing claim) creates a random session id (hashed in a replicated
`ui_sessions` table, 90-day sliding expiry, device label, last seen); cookie holds the session id, never the
token. Health → Devices lists sessions with Revoke.
S5. **Token rotation.** `POST /api/tokens/rotate` (UI, confirm) → new agent/cluster tokens replicated via
`secrets` rows; old accepted for 10 min; agents pick up the new token from the report response.
S6. **Signed bundles.** `tools/new-signing-key.ps1` creates an RSA-3072 key on the publishing machine
(private key DPAPI-protected, never leaves it). `build-bundle.ps1 -Sign` writes `bundle.sig`. Hub + watchdog
verify against the pinned `signing_pubkey` (RSACryptoServiceProvider works on PS 5.1/.NET 4). Unsigned or
bad signature → refuse to activate, alert.
S7. **Admin step-up.** Admin PowerShell and destructive actions require re-entering a 6-digit admin PIN
(set at first run, hashed) unless done within the last 10 min on that device.

## Workstream R — Reliability (P1)
R1. **Real health gate** for rollback + lease release: `/healthz` does a DB write probe and checks free
space; release healthy = healthz ok + `/cluster/status` eligible + local agent reports new version within
3 min. `/healthz` returns 503 when the data drive has < 1 GB free.
R2. **Frozen watchdog launcher** `data\wd-launcher.ps1` (installed once, not in the bundle) runs
`current\watchdog\watchdog.ps1`; if `watchdog.json` is stale > 10 min it runs the previous release's watchdog.
R3. **DB safety.** Leader: nightly `VACUUM INTO data\backup\forge-YYYYMMDD.db` (keep 7). On start
`PRAGMA quick_check`; failure → move aside, start ineligible, reseed from peers.
R4. **Data model.** `job_output(job_id, seq, text)` chunk table (append-only, replicated by seq) replaces
rewriting `jobs.output`; `presence(node_id, last_seen, metrics, procs, agents)` is NOT replicated.
R5. **Upgrades.** Skip pip when the requirements hash is unchanged; otherwise build `releases\<ver>\venv`
and point run-hub at it. Agent drain timeout 30 min, then restart and reattach (2.2 orphan reattach).
R6. **Machines lifecycle.** Hubs re-announce their URLs to the leader when they change (rename, new
Tailscale IP). `POST /api/machines/{id}/retire`: drain, run SYSTEM uninstall action, delete node/peer/
settings, offer token rotation.
R7. **Process model simplification.** Remove agent supervisors; Task Scheduler restart-on-failure + a
`cmd /c` loop wrapper. Keep one hub, two lanes, one watchdog per node.

## Workstream N — Notifications & attention (P1)
N1. **Alert engine** in the leader tick: alerts are rows (`alerts(id, kind, node_id, level, opened, resolved,
self_healing, last_notified)`), deduplicated, auto-resolved; self-healing alerts (rollout in progress,
service restarting) never notify unless they last > 15 min.
N2. **Notifiers** (hub config, UI settings page): ntfy.sh topic (default suggestion: random topic,
QR to subscribe on phone), Discord webhook, and Web Push for the installed PWA (VAPID keys generated at
first run; iOS needs the PWA installed). Notify only on: task failed, task needs input/finished (opt-in per
task "notify me"), machine down and not recovering (> 10 min), auto-repair failed/rollback.
N3. **Dead-man switch** (optional): leader pings a user-supplied healthchecks.io URL every 5 min.
N4. **Morning brief** includes overnight alerts and finished tasks.

## Workstream U — UX: "Today", Ask, flows (P1)
U1. **Information architecture.** Bottom tab bar on phone / left rail on desktop: **Today · Projects ·
Machines · Activity**. Health moves into Today ("Needs you") + Machines → Advanced.
U2. **Today** (home): one status sentence; "Needs you" (≤ 3 cards, each reason + one button: resume
session, retry task, wake machine, fix); "Happening now" (live sessions + running tasks, one line each);
"Recent" (Today / Yesterday / Earlier sessions with search over title, project, machine; transcript search
later); morning brief card.
U3. **Ask Hivemind** — persistent input (Ctrl/Cmd+K on desktop, above the tab bar on phone). v1: fuzzy
match over projects, sessions, machines and named actions + "Run '<text>' on any free machine". v2: free text
→ small model proposes a previewed action ([Run] [Edit]); never executes side effects without the preview.
U4. **Run a task** = one field "What should it do?" + chips Where (Any free machine) / With (Best fit) /
Project. After submit: "Started on forge-03 · Watch".
U5. **Maintenance** flow (Machines → Maintenance): named actions ("Update an app", "Update Claude Code &
Codex", "Restart Hivemind", "Free disk space", "Restart machine") → target "All machines (N online)" or a
pick → preview "one at a time: forge-03, nova, atlas, forge-01" → confirm (+ PIN). Runs rolling via the
hub, stops on first failure.
U6. **Add machine wizard**: Windows PC / Phone → command or QR with an enrollment code, "Waiting for it to
appear…" live state → "✓ joined · Run a test task".
U7. **Machines**: compact rows (status dot, name, "Idle / Running 2 tasks", one warning); bars, sparkline,
services, logs in the detail view; Advanced: hub order, versions, auto-repair log, devices, tokens.
U8. **First run** (zero machines / zero projects) onboarding cards; after first machine: "Add to Home
Screen for alerts".
U9. **PWA**: hub serves `/manifest.webmanifest`, icons, `/sw.js` (offline shell + last-known state banner
"Offline · data from 14:02"), Web Push.
U10. Hash routing for every view (session, project, machine, task) so back/refresh/share work; 44 px tap
targets; Auto/Light/Dark theme setting.

## Workstream F — First run & upkeep (P1)
F1. **`install.ps1 -NewFleet`**: generates tokens, signing key (on this machine), admin PIN prompt, seeds
the fleet, then opens `http://localhost:8787/?claim=<one-time code>` (sets a device session). No hand-written
bootstrap.json.
F2. **Tool upkeep**: maintenance actions `update_claude` (claude update / npm), `update_codex`,
`update_t3` (`t3 update`), `update_ollama`, `winget_upgrade` — scheduled rolling weekly (canary first) +
on demand; results in Activity.
F3. **Repo sync**: per project "Keep in sync on: [machines]" → user-lane job `git fetch` + fast-forward only
when clean, every 30 min; never pushes; conflicts surface as "Needs you".
F4. **Windows Update**: maintenance window per node (default 03:00–05:00, staggered 30 min per node by
priority, leader last), `PSWindowsUpdate`-free approach via `UsoClient`/COM `Microsoft.Update.Session` as
SYSTEM, reboot only if required and only if the cluster has another healthy hub.
F5. **Wake-on-LAN** from any peer on the same LAN ("Wake" button on offline machines; MAC learned from
agent reports).

F6. **Live-run fixes from 2.2.0:** (a) session cwds under Claude Desktop scratch workspaces
(`*\claude-data\*\scratch-workspaces\*`) and `%TEMP%` map to `misc:<node>` instead of one project each;
(b) the agent starts `ollama serve` with `OLLAMA_MODELS=<work_root>\ollama\models` (never the system drive when
a bigger drive exists) and the brief node pre-pulls `brief_model` once, off-hours; (c) service health reports
"starting" for the first 60 s instead of "not ok".

## Workstream P — Polish (P2)
Embedded Python in the bundle; user-visible names to Hivemind (firewall rule display name, shortcut, tray)
without renaming tasks/paths; Ask v2 (LLM); transcript full-text search; Mac/Linux agents.

## User decisions (2026-11-25)
- Notifications: **ntfy** (QR subscribe, private random topic) **+ Web Push** from the installed PWA. No Discord/email.
- Windows Update window: **03:00–05:00**, staggered 30 min per machine, leader last.
- Admin PIN: **yes**, re-prompt after 10 min per device.
- Repo sync: **opt-in per project** in the UI ("Keep in sync on").

## Acceptance (must all pass before 2.3.0 ships)
- pytest + PS suites green; chaos sim incl. partition/skew/rollback; N-1 agent vs N hub compatibility test.
- Live: a user-lane job reports Medium integrity and cannot write `C:\ProgramData\Forge\current`.
- Live: enrollment by code works from a phone-copied link; old agent token rotated; revoked device logged out.
- Live: kill leader → Today shows "All good" again within 60 s; a forced failed release auto-rolls back,
  marks "rolled back", sends one notification.
- UX: the 7 jobs-to-be-done from the UX review each take ≤ 3 taps on a 390 px phone.
