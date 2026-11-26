# Forge Control node agent v2 (Windows PowerShell 5.1+).
# Reports status/metrics/logs to a hub every few seconds and runs jobs the hub hands back
# (Claude Code, Codex, or PowerShell), streaming output to the hub. Pull model: no inbound ports.
#
# v2 changes (SPEC-phase1 ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â§6): hub_urls fallback list (localhost first, sticky, localhost retried
# every 60 s, list refreshed from the report response), work_root auto-pick + hub settings
# override, metrics.work_root / work_root_free_gb / watchdog. No self-update: the SYSTEM watchdog
# updates the whole bundle; when the bundle VERSION next to this script changes, the worker drains
# its running jobs and exits so the supervisor relaunches it on the new code.
#
#
# 2.1 lanes: the same script runs twice per node.
#   -Lane user   (task ForgeAgent, S4U user):  metrics/logs/procs + claude/codex/shell jobs.
#   -Lane system (task ForgeSystemAgent, SYSTEM): minimal heartbeat {node_id, lane, capacity, running} and only
#                 run_as=system jobs (engines 'action' = allowlisted agent\actions.ps1, 'admin' = PowerShell).
#                 Log data\agent-system.log; job dirs <work_root>\jobs\sys-<id>; capacity 1; never writes
#                 agent-config.json. Disabled per node with agent-config.json "allow_admin": false;
#                 "action_dry_run": true prints action commands instead of running them (tests).
#
# 2.2 (SPEC-phase2, user lane only):
#   - session index: every 30 s, metadata of Claude Code / Codex sessions (agent\sessions.ps1) -> POST /api/agent/sessions;
#     every 5 min git status + last commits of known repos -> POST /api/agent/commits.
#   - live transcripts: report response `watch: [{tool, session_id, since}]` -> POST /api/agent/transcript.
#   - resume jobs (job.resume = {tool, session_id}), engine `ollama` (agent\ollama-job.ps1, REST to 127.0.0.1:11434),
#     job.model for claude/codex/ollama.
#   - keep-alive services (agent\services.ps1; replaces the 2.1.2 T3 autostart) -> metrics.services.
#   Hub endpoints that answer 404 (older hubs) are retried every 10 min.
#   Jobs: default cwd <work_root>\scratch\<jobid>; claude --permission-mode acceptEdits / codex --sandbox workspace-write
#   unless full_access; job.timeout_s (default 7200) kills the process tree; running jobs are persisted to
#   data\running.json (system lane: running-system.json) and re-attached by a restarted worker; every report carries
#   running (job ids) + agent_time (epoch s). Agent status lines of brief jobs start with '[forge] ' (hub brief filter).
#
# 2.3 (CONTRACT-phase3 R5/R7): the tasks run this file with -Child under <FH>\bin\agent-loop.cmd (the loop restarts it;
#   the supervisor below is kept only for 2.2 task definitions). A draining worker (new bundle VERSION) exits after
#   drain_timeout_s (agent-config, else the hub's report response, else 1800) even with jobs still running: the next
#   worker re-attaches them (running.json). While draining, metrics.draining_to = the new version (watchdog health gate).
#
# Dot-source for tests without side effects:  . .\forge-agent.ps1 -NoRun
param(
    [string]$ConfigPath = (Join-Path $env:ProgramData 'Forge\data\agent-config.json'),
    [ValidateSet('user', 'system')][string]$Lane = 'user',
    [switch]$Child,
    [switch]$NoRun
)

$AgentVersion = '2.4.0'
$LocalHub = 'http://127.0.0.1:8787'

# 2.2 helpers (functions only). Missing when this file is run on its own (GET /agent/v2/forge-agent.ps1):
# then sessions/transcripts/services are simply off.
$Has22Helpers = $true
foreach ($h in 'sessions.ps1', 'services.ps1') { $hp = Join-Path $PSScriptRoot $h; if (Test-Path -LiteralPath $hp) { . $hp } else { $Has22Helpers = $false } }
# 2.3 helpers (CONTRACT-phase3.md section 6): jobtoken.ps1 + secrets.ps1 (workstream S), upkeep.ps1 (workstream F).
# Missing (agent run on its own) -> 2.2 behaviour: jobs start with Start-Process, no token rotation, no upkeep fields.
$HasP3Helpers = $true
foreach ($h in 'jobtoken.ps1', 'secrets.ps1', 'upkeep.ps1') { $hp = Join-Path $PSScriptRoot $h; if (Test-Path -LiteralPath $hp) { . $hp } else { $HasP3Helpers = $false } }

# ======================= pure helpers (unit-tested) =======================

function ConvertTo-HubUrl([string]$Url) {
    if (-not $Url) { return $null }
    $u = $Url.Trim().TrimEnd('/')
    if (-not $u) { return $null }
    $u
}

function Test-IsLocalHubUrl([string]$Url) {
    $u = ConvertTo-HubUrl $Url
    if (-not $u) { return $false }
    $u -match '^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$'
}

# Localhost first, then the hub-provided peer URLs in priority order, de-duplicated.
function Merge-HubUrls {
    param([string]$Local = 'http://127.0.0.1:8787', [object[]]$Urls = @())
    $seen = @{}; $out = New-Object System.Collections.Generic.List[string]
    foreach ($u in @($Local) + @($Urls)) {
        $n = ConvertTo-HubUrl ([string]$u)
        if (-not $n) { continue }
        $k = $n.ToLowerInvariant()
        if ($n -ne $Local -and (Test-IsLocalHubUrl $n)) { continue }   # alias of localhost
        if ($seen.ContainsKey($k)) { continue }
        $seen[$k] = $true; $out.Add($n)
    }
    , $out.ToArray()
}

# Order in which to try hubs this round: the sticky (last good) hub first, then the rest in list
# order. When RetryLocal is set and we're stuck on a remote hub, localhost goes in front.
function Get-HubCandidates {
    param([object[]]$Urls = @(), [string]$Sticky, [bool]$RetryLocal = $false)
    $list = @($Urls | ForEach-Object { ConvertTo-HubUrl ([string]$_) } | Where-Object { $_ })
    $out = New-Object System.Collections.Generic.List[string]
    $s = ConvertTo-HubUrl $Sticky
    if ($RetryLocal -and $list.Count -gt 0 -and $s -ne $list[0]) { $out.Add($list[0]) }
    if ($s -and -not $out.Contains($s)) { $out.Add($s) }
    foreach ($u in $list) { if (-not $out.Contains($u)) { $out.Add($u) } }
    , $out.ToArray()
}

# Fixed drive with the most free space -> "<X>:\forge", unless the hub setting overrides it.
# Disks: objects with name ('C:') and free_gb. Previous: earlier auto choice (kept while its drive exists).
function Select-WorkRoot {
    param([object[]]$Disks = @(), [string]$Override, [string]$Previous)
    if ($Override -and $Override.Trim()) { return $Override.Trim() }
    $names = @($Disks | ForEach-Object { "$($_.name)".ToUpperInvariant() })
    if ($Previous -and $Previous.Length -ge 2 -and $names -contains $Previous.Substring(0, 2).ToUpperInvariant()) { return $Previous }
    $best = $Disks | Where-Object { $_.name } | Sort-Object -Property @{ Expression = { [double]$_.free_gb }; Descending = $true }, @{ Expression = { "$($_.name)" } } | Select-Object -First 1
    if (-not $best) { return (Join-Path $env:SystemDrive 'forge') }
    "$("$($best.name)".TrimEnd('\'))\forge"
}

function Get-EffectiveMaxJobs {
    param($Setting, $Config, [int]$Default = 2)
    if ($null -ne $Setting -and "$Setting" -ne '' -and [int]$Setting -ge 0) { return [int]$Setting }
    if ($null -ne $Config -and "$Config" -ne '' -and [int]$Config -ge 0) { return [int]$Config }
    $Default
}

function Remove-Ansi([string]$Text) {
    (($Text -replace "`0", '') -replace "\x1b\[[0-9;?]*[A-Za-z]", '') -replace "\x1b\][^\x07]*\x07", ''
}

function Read-JsonFile([string]$Path) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $null }
    try { Get-Content -LiteralPath $Path -Raw -ErrorAction Stop | ConvertFrom-Json } catch { $null }
}

function Write-JsonFile([string]$Path, $Obj) {
    $tmp = "$Path.tmp"
    [IO.File]::WriteAllText($tmp, (ConvertTo-Json -InputObject $Obj -Depth 8), (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $tmp -Destination $Path -Force
}

# Does this job belong to this lane? System jobs = run_as 'system' or engine action/admin; everything else is user.
# 2.3 (G-F1): engine 'action' with run_as 'user' is a user-lane action (tool updates, git_sync, ollama_pull).
function Test-JobLane($Job, [string]$Lane) {
    $sys = ("$($Job.run_as)" -eq 'system') -or ("$($Job.engine)" -eq 'admin') -or ("$($Job.engine)" -eq 'action' -and "$($Job.run_as)" -ne 'user')
    if ($Lane -eq 'system') { return $sys }
    -not $sys
}

# Per-node kill switch for the SYSTEM lane (agent-config.json allow_admin, default true).
function Test-AllowAdmin($Config) {
    if ($null -eq $Config -or $null -eq $Config.PSObject.Properties['allow_admin']) { return $true }
    $v = $Config.allow_admin
    -not ($v -eq $false -or "$v" -match '^(false|0|no)$')
}

function Test-SafeJobId([string]$Id) { $Id -match '^[A-Za-z0-9_-]{1,64}$' }

# Values that end up on a cmd.exe command line must be boring.
function Test-SafeSessionId([string]$Id) { "$Id" -match '^[A-Za-z0-9][A-Za-z0-9-]{7,63}$' }
function Test-SafeModelName([string]$Name) { "$Name" -match '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$' }
function Test-SafeLocalUrl([string]$Url) { "$Url" -match '^https?://[A-Za-z0-9.\-]+(:\d{1,5})?/?$' }
function Test-SafeUploadName([string]$Name) { "$Name" -match '^[A-Za-z0-9][A-Za-z0-9._-]{0,79}\.(png|jpg|webp|gif)$' }

# 2.4 chat: the text put in front of the prompt when the user attached photos (claude/codex read them from disk).
function Get-AttachmentPreamble([string[]]$Paths) {
    $Paths = @($Paths | Where-Object { $_ })
    if (-not $Paths.Count) { return '' }
    $list = ($Paths | ForEach-Object { "- $_" }) -join "`n"
    "The user attached $($Paths.Count) image(s) to this message. Look at them (open each file) before you answer:`n$list`n`n"
}

# The cmd.exe inner command for a user-lane job (the caller appends the output redirect).
# Returns @{inner; write ('prompt'|'ps1'); redirect ('out' = stdout+stderr -> out.log | 'err' = the runner
# writes out.log itself, cmd output -> err.log); resume_id} or @{error}.
#   claude:        [clear API key env] "claude" -p [--resume <id> | --session-id <hub id>] [--add-dir <photos>] --permission-mode <mode> --output-format text [--model m] < prompt
#                  mode = acceptEdits (full_access: bypassPermissions)
#   codex:         "codex" exec [--image=<photo> ...] --skip-git-repo-check <sandbox> [-m m] [resume <id>] - < prompt
#                  sandbox = --sandbox workspace-write (full_access: --dangerously-bypass-approvals-and-sandbox)
#                  (exec options must precede `resume`: `codex exec resume` rejects --sandbox)
#   shell:         powershell.exe -File task.ps1
#   ollama:        powershell.exe -File agent\ollama-job.ps1 -PromptFile .. -Model .. -BaseUrl .. -OutFile ..
#                  text jobs only (job.kind missing = text, for hubs that do not send kind)
function Get-JobCommand {
    param($Job, [hashtable]$Tools = @{}, $Config, [string]$PromptFile, [string]$JobDir, [string]$AgentDir, [string]$OutFile,
          [string[]]$Images = @())
    $engine = [string]$Job.engine
    $Images = @($Images | Where-Object { $_ })
    $rid = $null
    $resume = $Job.resume
    if ($resume) {
        $rtool = [string]$resume.tool; $rid = [string]$resume.session_id
        if (-not $engine) { $engine = $rtool }
        if ($rtool -and $rtool -ne $engine) { return @{ error = "resume tool '$rtool' does not match engine '$engine'" } }
        if (@('claude', 'codex') -notcontains $engine) { return @{ error = "engine '$engine' cannot resume sessions" } }
        if (-not (Test-SafeSessionId $rid)) { return @{ error = 'bad resume session id' } }
        $rid = $rid.ToLowerInvariant()
    }
    $model = [string]$Job.model
    if ($model -and -not (Test-SafeModelName $model)) { return @{ error = "bad model name '$model'" } }
    $missing = @{ error = "engine '$engine' is not installed on this machine" }
    switch ($engine) {
        'claude' {
            if (-not $Tools['claude']) { return $missing }
            $mode = if ($Job.full_access) { 'bypassPermissions' } else { 'acceptEdits' }
            # Use the claude.ai subscription login unless this node opts into API-key billing.
            $auth = if ($Config -and $Config.claude_use_api_key) { '' } else { 'set "ANTHROPIC_API_KEY=" && set "ANTHROPIC_AUTH_TOKEN=" && ' }
            $res = if ($rid) { " --resume $rid" } else { '' }
            # 2.4 chat: the hub names a new conversation's session, so follow-ups can --resume it
            $nsid = [string]$Job.session_id
            if (-not $rid -and $nsid) {
                if (-not (Test-SafeSessionId $nsid)) { return @{ error = 'bad session id' } }
                $res = " --session-id $($nsid.ToLowerInvariant())"
            }
            if ($Images.Count) { $res += " --add-dir `"$(Split-Path -Parent $Images[0])`"" }
            $m = if ($model) { " --model $model" } else { '' }
            return @{ inner = "$auth`"$($Tools['claude'])`" -p$res --permission-mode $mode --output-format text$m < `"$PromptFile`""; write = 'prompt'; redirect = 'out'; resume_id = $rid }
        }
        'codex' {
            if (-not $Tools['codex']) { return $missing }
            $sb = if ($Job.full_access) { '--dangerously-bypass-approvals-and-sandbox' } else { '--sandbox workspace-write' }
            $m = if ($model) { " -m $model" } else { '' }
            $sub = if ($rid) { " resume $rid" } else { '' }
            $img = ($Images | ForEach-Object { " --image=`"$_`"" }) -join ''
            return @{ inner = "`"$($Tools['codex'])`" exec$img --skip-git-repo-check $sb$m$sub - < `"$PromptFile`""; write = 'prompt'; redirect = 'out'; resume_id = $rid }
        }
        'shell' {
            return @{ inner = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $JobDir 'task.ps1')`""; write = 'ps1'; redirect = 'out' }
        }
        'ollama' {
            $kind = "$($Job.kind)"
            if ($kind -and $kind -ne 'text') { return @{ error = "engine 'ollama' runs text jobs only (job kind '$kind')" } }
            $url =if ($Config -and $Config.ollama_url) { [string]$Config.ollama_url } else { 'http://127.0.0.1:11434' }
            if (-not $Tools['ollama'] -and -not ($Config -and $Config.ollama_url)) { return $missing }
            if (-not (Test-SafeLocalUrl $url)) { return @{ error = "bad ollama_url '$url'" } }
            $mdl = if ($model) { $model } elseif ($Config -and $Config.ollama_model -and (Test-SafeModelName ([string]$Config.ollama_model))) { [string]$Config.ollama_model } else { 'qwen2.5:14b' }
            $to = if ($Config -and $Config.ollama_timeout_s) { " -TimeoutSec $([int]$Config.ollama_timeout_s)" } else { '' }
            return @{ inner = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $AgentDir 'ollama-job.ps1')`" -PromptFile `"$PromptFile`" -Model $mdl -BaseUrl $($url.TrimEnd('/')) -OutFile `"$OutFile`"$to"
                      write = 'prompt'; redirect = 'err' }
        }
    }
    $missing
}

# Report body for the SYSTEM lane: deliberately minimal (no metrics/procs/logs).
# running = ids of the jobs this lane is running (re-attached ones included); agent_time = epoch seconds (float).
function New-SystemLaneReport {
    param([string]$NodeId, [int]$Capacity, [string[]]$Running = @(), [bool]$Boot, [string]$AgentVersion, [string]$BundleVersion, [double]$AgentTime = 0)
    @{ node_id = $NodeId; lane = 'system'; capacity = $Capacity; running = @($Running | Where-Object { $_ }); boot = $Boot
       agent_version = $AgentVersion; bundle_version = $BundleVersion; agent_time = $AgentTime }
}

function Get-AgentEpoch { [Math]::Round([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0, 3) }

# Agent status lines in a job's output. Brief jobs (source 'brief') get the '[forge] ' prefix the hub's brief
# noise filter drops, so the stored brief is the model's text only (no node name / scratch path).
function Format-AgentJobLine($Job, [string]$NodeId, [string]$Text) {
    $tag = if ($Job -and "$($Job.source)" -eq 'brief') { '[forge] ' } else { '' }
    "$tag[$NodeId] $Text`n"
}

# job.timeout_s (seconds, > 0) or the default (2 h).
function Get-JobTimeoutSec($Job, [int]$Default = 7200) {
    $t = 0.0
    if ($Job -and $null -ne $Job.timeout_s -and [double]::TryParse("$($Job.timeout_s)", [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$t) -and $t -ge 1) { return [int][Math]::Min($t, [int]::MaxValue) }
    $Default
}

function Test-JobTimedOut([double]$Started, [double]$Now, [int]$TimeoutSec) { ($TimeoutSec -gt 0) -and ($Started -gt 0) -and (($Now - $Started) -ge $TimeoutSec) }

# Job dir under <work_root>\jobs: <id> (user lane) or sys-<id> (system lane).
function Get-JobDirName([string]$Id, [string]$Lane = 'user') { if ($Lane -eq 'system') { "sys-$Id" } else { $Id } }

# data\running.json (user lane) / data\running-system.json: what a restarted worker needs to re-attach to its
# still-running jobs. No paths are stored: they are derived from the job id, so an edited file cannot point the
# agent at other files.
function ConvertTo-RunningRecords([hashtable]$Running) {
    $out = New-Object System.Collections.Generic.List[object]
    foreach ($id in @($Running.Keys | Sort-Object)) {
        $r = $Running[$id]
        $out.Add([ordered]@{ id = [string]$id; pid = [int]$r.pid; engine = [string]$r.engine; offset = [long]$r.offset; erroffset = [long]$r.erroffset
                             has_err = [bool]$r.err; started = [double]$r.started; timeout_s = [int]$r.timeout_s; source = [string]$r.source
                             resume_tool = [string]$r.resume_tool; resume_id = [string]$r.resume_id; cancelled = [bool]$r.cancelled })
    }
    , $out.ToArray()
}

# Records + live processes (@{pid -> @{pid; cmd}}) -> @{attach = records whose process still runs THIS job; gone = the rest}.
# "Still runs this job" = same pid AND its command line names the job's own dir (guards pid reuse and edited files).
function Select-ReattachJobs {
    param([object[]]$Records = @(), [hashtable]$Procs = @{}, [string]$JobsDir, [string]$Lane = 'user')
    $attach = New-Object System.Collections.Generic.List[object]; $gone = New-Object System.Collections.Generic.List[object]
    $seen = @{}
    foreach ($r in @($Records)) {
        if (-not $r -or -not (Test-SafeJobId ([string]$r.id)) -or $seen.ContainsKey([string]$r.id)) { continue }
        $seen[[string]$r.id] = $true
        $p = $null; if ($r.pid) { $p = $Procs[[int]$r.pid] }
        $dir = (Join-Path $JobsDir (Get-JobDirName ([string]$r.id) $Lane)) + '\'
        if ($p -and "$($p.cmd)".IndexOf($dir, [StringComparison]::OrdinalIgnoreCase) -ge 0) { $attach.Add($r) } else { $gone.Add($r) }
    }
    @{ attach = $attach.ToArray(); gone = $gone.ToArray() }
}

# ---- live transcripts (SPEC C), factored out of the main loop so it can be tested with a stubbed sender ----
function New-TranscriptState { @{ names = @{}; missing = @{}; last = @{}; rot = 0 } }

# Up to Take watches per cycle, rotating through the list so a 5th+ watched session is served too.
function Select-RotatingWatches([object[]]$Watches = @(), [int]$Offset = 0, [int]$Take = 4) {
    $ws = @($Watches | Where-Object { $_ }); $n = $ws.Count
    if ($n -eq 0) { return @{ items = @(); next = 0 } }
    $o = $Offset % $n; if ($o -lt 0) { $o += $n }
    $k = [Math]::Min($Take, $n)
    $items = @(for ($i = 0; $i -lt $k; $i++) { $ws[($o + $i) % $n] })
    @{ items = $items; next = (($o + $k) % $n) }
}

# The since to read from. The hub echoes the cursor of the last batch it stored; if a watch still carries the
# since of a batch we already POSTed (stale watch list, hub switch), continue after that batch: never resend it.
function Resolve-TranscriptSince($Watch, [hashtable]$Last) {
    $s = $Watch.since
    if ($null -eq $s -or "$s" -eq '') { return $null }
    $l = $Last[([string]$Watch.session_id).ToLowerInvariant()]
    if ($l -and "$($l.since)" -eq "$s" -and "$($l.cursor)" -ne "$s") { return $l.cursor }
    $s
}

# One transcript cycle: <= 4 watched sessions, <= 200 events each, one first-call tail per cycle, nothing posted
# when nothing changed, 'session not found' at most once per 60 s per session. Send: {param($Path, $Body)} -> $true on success.
function Invoke-TranscriptStep {
    param([object[]]$Watches = @(), [hashtable]$TxState, [hashtable]$SessState, [string]$NodeId, [double]$Now, [scriptblock]$Send)
    $sel = Select-RotatingWatches -Watches $Watches -Offset ([int]$TxState.rot)
    $TxState.rot = $sel.next
    if ($TxState.last.Count -gt 200) { $TxState.last.Clear() }
    if ($TxState.missing.Count -gt 200) { $TxState.missing.Clear() }
    $tailDone = $false
    foreach ($w in @($sel.items)) {
        $sid = ([string]$w.session_id).ToLowerInvariant(); $tool = [string]$w.tool
        if (-not $sid) { continue }
        $since = Resolve-TranscriptSince $w $TxState.last
        if ($null -eq $since) { if ($tailDone) { continue }; $tailDone = $true }   # first-call tails are the expensive part
        $m = Find-SessionMeta $SessState $tool $sid
        if (-not $m) {
            if (-not $TxState.missing[$sid] -or ($Now - $TxState.missing[$sid]) -ge 60) {
                $TxState.missing[$sid] = $Now
                $null = & $Send '/api/agent/transcript' ([ordered]@{ node_id = $NodeId; tool = $tool; session_id = $sid; cursor = $null; events = @(); done = $true; error = 'session not found on this node' })
            }
            continue
        }
        if (-not $TxState.names.ContainsKey($sid)) { $TxState.names[$sid] = @{} }
        $b = Get-TranscriptBatch -Tool $m.tool -Path $m.path -Since $since -ToolNames $TxState.names[$sid]
        Limit-ToolNames $TxState.names[$sid]
        if ($null -ne $since -and "$since" -eq "$($b.cursor)" -and @($b.events).Count -eq 0) { continue }
        $st = Get-SessionState -LastWrite $m.mtime -Now $Now -ProcAlive ($null -ne $m.pid)   # pid from the last index cycle
        $ok = & $Send '/api/agent/transcript' ([ordered]@{ node_id = $NodeId; tool = $m.tool; session_id = $sid; cursor = $b.cursor
                                                         events = @($b.events); done = [bool]$b.at_end; state = $st })
        if ($ok) { $TxState.last[$sid] = @{ since = "$since"; cursor = $b.cursor } }
    }
}

# 2.3 R5: drain timeout in seconds: agent-config drain_timeout_s > the hub's report response value > 1800.
function Get-DrainTimeoutSec($ConfigValue, $HubValue, [int]$Default = 1800) {
    foreach ($v in @($ConfigValue, $HubValue)) {
        $t = 0.0
        if ($null -ne $v -and "$v" -ne '' -and [double]::TryParse("$v", [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$t) -and $t -ge 1) { return [int]$t }
    }
    $Default
}

# Should a draining worker give up waiting for its jobs? (it exits; the next worker re-attaches them)
function Test-DrainTimedOut([double]$Since, [double]$Now, [int]$TimeoutSec, [int]$Running) {
    ($Running -gt 0) -and ($Since -gt 0) -and (($Now - $Since) -ge $TimeoutSec)
}

if ($NoRun) { return }

# ======================= supervisor =======================
# The scheduled task runs this file without -Child; we relaunch the worker forever, so a crash or
# a bundle update (worker exits after the `current` junction flips) just restarts it on new code.
if (-not $Child) {
    while ($true) {
        $p = Start-Process powershell.exe -WindowStyle Hidden -PassThru -Wait -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
            '-File', "`"$PSCommandPath`"", '-Child', '-ConfigPath', "`"$ConfigPath`"", '-Lane', $Lane)
        Start-Sleep -Seconds 3
    }
}

# ======================= worker =======================
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Utf8    = New-Object Text.UTF8Encoding($false)
$DataDir = Split-Path -Parent $ConfigPath
$cfg     = Read-JsonFile $ConfigPath
if (-not $cfg) { $cfg = [pscustomobject]@{} }
$shared  = Read-JsonFile (Join-Path $DataDir 'config.json')
$Token   = if ($shared -and $shared.agent_token) { $shared.agent_token } elseif ($cfg.agent_token) { $cfg.agent_token } else { '' }
$Headers = @{ 'X-Agent-Token' = $Token }
$NodeId  = if ($cfg.node_id) { $cfg.node_id } elseif ($shared -and $shared.node_id) { $shared.node_id } else { $env:COMPUTERNAME.ToLower() }
$IsSystemLane = ($Lane -eq 'system')
$AgentLog = Join-Path $DataDir $(if ($IsSystemLane) { 'agent-system.log' } else { 'agent.log' })
$Interval = if ($cfg.interval) { [int]$cfg.interval } else { 5 }
$Cores    = [int](Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors
# system lane: allowlisted actions (agent\actions.ps1) validated against the bundle's hub\actions.json
$AgentActionsScript = Join-Path $PSScriptRoot 'actions.ps1'
$AgentCatalogPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'hub\actions.json'
$script:ActionCatalog = $null
# 2.3 (G-F1): the user lane loads the catalog too (lane "user" actions)
if ($IsSystemLane -or ($HasP3Helpers -and (Test-Path -LiteralPath $AgentActionsScript))) {
    . $AgentActionsScript
    try { $script:ActionCatalog = Read-ForgeActionCatalog $AgentCatalogPath } catch { $script:ActionCatalog = $null }
}

# hub_urls (v1 configs only have hub_url)
$initial = @()
if ($cfg.hub_urls) { $initial = @($cfg.hub_urls) } elseif ($cfg.hub_url) { $initial = @($cfg.hub_url) }
if ($cfg.local_hub) { $LocalHub = ConvertTo-HubUrl ([string]$cfg.local_hub) }   # tests / non-default port
$script:HubUrls = Merge-HubUrls -Local $LocalHub -Urls $initial
$script:Hub = $script:HubUrls[0]
$script:LastLocalTry = Get-Date

# bundle version next to this script (current\VERSION); change => drain + restart
$VersionFile = Join-Path (Split-Path -Parent $PSScriptRoot) 'VERSION'
function Get-BundleVersion { try { if (Test-Path -LiteralPath $VersionFile) { return (Get-Content -LiteralPath $VersionFile -Raw).Trim() } } catch {}; $null }
$StartBundle = Get-BundleVersion
$script:Draining = $false
$script:DrainSince = 0.0; $script:DrainTarget = $null; $script:HubDrainTimeout = $null

# effective settings (from hub)
$script:Settings = @{ work_root = $null; max_jobs = $null }
$script:MaxJobs = if ($IsSystemLane) { Get-EffectiveMaxJobs -Setting $null -Config $cfg.system_max_jobs -Default 1 } else { Get-EffectiveMaxJobs -Setting $null -Config $cfg.max_jobs }

function Write-AgentLog([string]$m) {
    try {
        Add-Content -Path $AgentLog -Value "[$(Get-Date -Format s)] $m" -Encoding UTF8
        if ((Get-Item $AgentLog).Length -gt 5MB) { Move-Item $AgentLog "$AgentLog.old" -Force }
    } catch {}
}

function Save-AgentConfig([hashtable]$Changes) {
    if ($IsSystemLane) { return }   # only the user lane writes agent-config.json (no two writers)
    try {
        $cur = Read-JsonFile $ConfigPath
        $h = [ordered]@{}
        if ($cur) { foreach ($p in $cur.PSObject.Properties) { $h[$p.Name] = $p.Value } }
        foreach ($k in $Changes.Keys) { $h[$k] = $Changes[$k] }
        Write-JsonFile $ConfigPath $h
    } catch { Write-AgentLog "could not save agent config: $($_.Exception.Message)" }
}

# ---------- work root ----------
function Get-FixedDisks {
    @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' -ErrorAction SilentlyContinue | ForEach-Object {
        @{ name = $_.DeviceID; total_gb = [math]::Round($_.Size / 1GB, 1); free_gb = [math]::Round($_.FreeSpace / 1GB, 1) } })
}

function Set-WorkRoot {
    $disks = @(Get-FixedDisks)
    $root = Select-WorkRoot -Disks $disks -Override $script:Settings.work_root -Previous $cfg.work_root_auto
    if ($root -ne $script:WorkRoot) {
        try {
            foreach ($d in 'jobs', 'repos', 'scratch') { New-Item -ItemType Directory -Force (Join-Path $root $d) -ErrorAction Stop | Out-Null }
        } catch {
            Write-AgentLog "work_root '$root' unusable ($($_.Exception.Message)); falling back to auto"
            $root = Select-WorkRoot -Disks $disks -Previous $cfg.work_root_auto
            foreach ($d in 'jobs', 'repos', 'scratch') { New-Item -ItemType Directory -Force (Join-Path $root $d) | Out-Null }
        }
        if (-not $script:Settings.work_root -and $root -ne $cfg.work_root_auto) {
            Save-AgentConfig @{ work_root_auto = $root }
            $cfg | Add-Member -NotePropertyName work_root_auto -NotePropertyValue $root -Force
        }
        # the SYSTEM watchdog prunes <work_root>\jobs dirs older than 14 days; it reads the effective root from here
        if ("$($cfg.work_root_current)" -ne $root) {
            Save-AgentConfig @{ work_root_current = $root }
            if (-not $IsSystemLane) { $cfg | Add-Member -NotePropertyName work_root_current -NotePropertyValue $root -Force }
        }
        $script:WorkRoot = $root
        $script:JobsDir = Join-Path $root 'jobs'
        $script:ScratchDir = Join-Path $root 'scratch'
        Write-AgentLog "work_root = $root"
    }
}

function Get-WorkRootFreeGb {
    try {
        $q = [IO.Path]::GetPathRoot($script:WorkRoot)
        [math]::Round((New-Object IO.DriveInfo($q)).AvailableFreeSpace / 1GB, 1)
    } catch { $null }
}

# ---------- hub transport with fallback ----------
function Invoke-HubPost([string]$Base, [string]$Path, $Obj, [int]$Timeout = 15) {
    $json  = ConvertTo-Json -InputObject $Obj -Depth 8 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    Invoke-RestMethod -Uri "$Base$Path" -Method Post -Headers $Headers -ContentType 'application/json; charset=utf-8' -Body $bytes -TimeoutSec $Timeout -UseBasicParsing
}

function Send-Hub([string]$Path, $Obj) {
    $retryLocal = ($script:Hub -ne $script:HubUrls[0]) -and (((Get-Date) - $script:LastLocalTry).TotalSeconds -ge 60)
    if ($retryLocal) { $script:LastLocalTry = Get-Date }
    $cands = Get-HubCandidates -Urls $script:HubUrls -Sticky $script:Hub -RetryLocal $retryLocal
    $lastErr = $null
    foreach ($base in $cands) {
        try {
            $r = Invoke-HubPost $base $Path $Obj
            if ($base -ne $script:Hub) { Write-AgentLog "hub switched: $($script:Hub) -> $base"; $script:Hub = $base }
            return $r
        } catch {
            $lastErr = $_
            if ($base -eq $script:HubUrls[0]) { $script:LastLocalTry = Get-Date }
        }
    }
    if ($lastErr) { throw $lastErr }
    throw "no hub urls configured"
}

function Update-HubUrls($FromHub) {
    if (-not $FromHub) { return }
    $new = Merge-HubUrls -Local $LocalHub -Urls @($FromHub)
    if (($new -join '|') -ne ($script:HubUrls -join '|')) {
        $script:HubUrls = $new
        if ($new -notcontains $script:Hub) { $script:Hub = $new[0] }
        Save-AgentConfig @{ hub_urls = $new }
        Write-AgentLog "hub_urls = $($new -join ', ')"
    }
}

# ---------- tools / info / metrics ----------
function Resolve-Tool([string]$Name) {
    if ($cfg.tools -and $cfg.tools.$Name) { return $cfg.tools.$Name }
    $c = Get-Command -Name "$Name.exe", "$Name.cmd" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $c) {
        foreach ($p in @("$env:USERPROFILE\.local\bin\$Name.exe", "$env:USERPROFILE\.local\bin\$Name.cmd", "$env:APPDATA\npm\$Name.cmd", "$env:LOCALAPPDATA\Programs\Ollama\$Name.exe")) {
            if (Test-Path $p) { return $p }
        }
        return $null
    }
    $c.Source
}
$Tools = @{}
if (-not $IsSystemLane) {
    foreach ($t in 'claude', 'codex', 'node', 'python', 'ollama', 'git', 'nvidia-smi', 't3') { $p = Resolve-Tool $t; if ($p) { $Tools[$t] = $p } }
}

function Get-Info {
    $os  = Get-CimInstance Win32_OperatingSystem
    $cs  = Get-CimInstance Win32_ComputerSystem
    $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
    $versions = @{}
    foreach ($t in 'claude', 'codex', 'node', 'python', 'ollama', 'git') {
        if ($Tools[$t]) { try { $versions[$t] = ("$(& $Tools[$t] --version 2>$null | Select-Object -First 1)").Trim() } catch {} }
    }
    $wsl = @()
    try { $raw = @(wsl.exe -l -q 2>$null); if ($LASTEXITCODE -eq 0) { $wsl = @($raw | ForEach-Object { ($_ -replace "`0", '').Trim() } | Where-Object { $_ }) } } catch {}
    @{
        hostname = $env:COMPUTERNAME; user = "$env:USERDOMAIN\$env:USERNAME"
        os = "$($os.Caption) $($os.Version)"; model = "$($cs.Manufacturer) $($cs.Model)".Trim()
        cpu = "$($cpu.Name)".Trim(); cores = $cs.NumberOfLogicalProcessors
        ram_gb = [math]::Round($cs.TotalPhysicalMemory / 1GB, 1)
        gpus = @(Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name })
        versions = $versions; wsl = $wsl; tools = @($Tools.Keys)
        boot = $os.LastBootUpTime.ToString('o'); agent_version = $AgentVersion; max_jobs = $script:MaxJobs
        bundle_version = $StartBundle; work_root = $script:WorkRoot
    }
}

function Get-Metrics {
    $os  = Get-CimInstance Win32_OperatingSystem
    $cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
    $tot = [double]$os.TotalVisibleMemorySize; $free = [double]$os.FreePhysicalMemory
    $disks = @(Get-FixedDisks)
    $gpu = @()
    if ($Tools['nvidia-smi']) {
        try {
            $gpu = @(& $Tools['nvidia-smi'] --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu --format=csv,noheader,nounits 2>$null |
                ForEach-Object { $p = $_ -split ',\s*'; @{ name = $p[0]; util = [int]$p[1]; mem_used_mb = [int]$p[2]; mem_total_mb = [int]$p[3]; temp_c = [int]$p[4] } })
        } catch {}
    }
    $bat = Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue | Select-Object -First 1
    $ips = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
        ForEach-Object { @{ iface = $_.InterfaceAlias; ip = $_.IPAddress } })
    @{
        cpu = if ($cpu -ne $null) { [math]::Round($cpu, 0) } else { $null }
        mem_pct = [math]::Round(100 * ($tot - $free) / $tot, 1)
        mem_used_gb = [math]::Round(($tot - $free) / 1MB, 1); mem_total_gb = [math]::Round($tot / 1MB, 1)
        disks = $disks; gpu = $gpu; ips = $ips
        uptime_s = [int]((Get-Date) - $os.LastBootUpTime).TotalSeconds
        battery = if ($bat) { @{ pct = $bat.EstimatedChargeRemaining; ac = ($bat.BatteryStatus -eq 2) } } else { $null }
        procs_total = (Get-Process).Count
        work_root = $script:WorkRoot; work_root_free_gb = (Get-WorkRootFreeGb)
        watchdog = (Read-JsonFile (Join-Path $DataDir 'watchdog.json'))
        hub_url = $script:Hub; bundle_version = $StartBundle; draining_to = $script:DrainTarget
        services = $(if ($Has22Helpers) { Get-ServiceMetrics $script:ServiceStatus } else { @() })
    }
}

$script:PrevCpu = @{}; $script:PrevT = Get-Date
function Get-TopProcs {
    $now = Get-Date; $dt = [math]::Max(0.5, ($now - $script:PrevT).TotalSeconds)
    $cur = @{}; $list = New-Object System.Collections.Generic.List[object]
    foreach ($p in Get-Process) {
        $c = $p.CPU; if ($c -eq $null) { continue }
        $cur[$p.Id] = $c
        $prev = $script:PrevCpu[$p.Id]
        $pct = if ($prev -ne $null) { [math]::Round(($c - $prev) / $dt / $Cores * 100, 1) } else { 0 }
        $list.Add([pscustomobject]@{ name = $p.ProcessName; pid = $p.Id; cpu = $pct; mem_mb = [math]::Round($p.WorkingSet64 / 1MB) })
    }
    $script:PrevCpu = $cur; $script:PrevT = $now
    , @($list | Sort-Object cpu, mem_mb -Descending | Select-Object -First 15 |
        ForEach-Object { @{ name = $_.name; pid = $_.pid; cpu = $_.cpu; mem_mb = $_.mem_mb } })
}

function Get-Agents {
    $out = New-Object System.Collections.Generic.List[object]
    $filter = "Name='claude.exe' OR Name='codex.exe' OR Name='node.exe' OR Name='ollama.exe' OR Name='hermes.exe' OR Name='python.exe'"
    foreach ($p in Get-CimInstance Win32_Process -Filter $filter -ErrorAction SilentlyContinue) {
        $cmd = "$($p.CommandLine)"; $kind = $null
        switch -regex ($p.Name) {
            '^claude'  { $kind = 'claude' }
            '^codex'   { $kind = 'codex' }
            '^ollama'  { $kind = 'ollama' }
            '^hermes'  { $kind = 'hermes' }
            '^node'    { if ($cmd -match 'desktop-commander') { $kind = 'desktop-commander' } elseif ($cmd -match 'claude-code') { $kind = 'claude' } elseif ($cmd -match '@openai[\\/]codex') { $kind = 'codex' } }
            '^python'  { if ($cmd -match 'hermes') { $kind = 'hermes' } elseif ($cmd -match 'uvicorn.*app:app|hub\\serve\.py') { $kind = 'forge-hub' } }
        }
        if ($kind) {
            $out.Add(@{ kind = $kind; pid = [int]$p.ProcessId
                started = if ($p.CreationDate) { $p.CreationDate.ToString('o') } else { $null }
                cmd = $cmd.Substring(0, [Math]::Min(160, $cmd.Length)) })
        }
    }
    foreach ($id in @($script:Running.Keys)) { $out.Add(@{ kind = 'forge-job'; pid = $script:Running[$id].proc.Id; job = [string]$id; started = $null; cmd = $script:Running[$id].engine }) }
    , $out.ToArray()
}

function Get-Logs {
    $h = @{}
    $logs = @($cfg.logs) + @(
        @{ name = 'forge-agent'; path = $AgentLog },
        @{ name = 'agent-system'; path = (Join-Path $DataDir 'agent-system.log') },
        @{ name = 'watchdog'; path = (Join-Path $DataDir 'watchdog.log') },
        @{ name = 'hub'; path = (Join-Path $DataDir 'hub.log') })
    foreach ($l in $logs) {
        if ($l -and $l.path -and $l.name -and (Test-Path -LiteralPath $l.path)) {
            try {
                $fs = [IO.File]::Open($l.path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
                try {
                    $take = [long][Math]::Min($fs.Length, 64KB)
                    $null = $fs.Seek(-$take, [IO.SeekOrigin]::End)
                    $buf = New-Object byte[] $take
                    $n = $fs.Read($buf, 0, [int]$take)
                } finally { $fs.Close() }
                $lines = (Remove-Ansi ([Text.Encoding]::UTF8.GetString($buf, 0, $n))) -split "`r?`n"
                if ($lines.Count -gt 150) { $lines = $lines[($lines.Count - 150)..($lines.Count - 1)] }
                $h[$l.name] = $lines -join "`n"
            } catch {}
        }
    }
    $h
}

# ---------------- jobs ----------------
$script:Running = @{}

$script:JobAttempt = @{}
function Update-ForgeJob($Id, [hashtable]$Body) {
    # Fencing (hub 2.2.1+): say who we are and which dispatch attempt we hold, so a requeued job's stale copy
    # (e.g. a laptop that slept) cannot overwrite the new holder's result.
    $Id = [string]$Id
    $Body.node_id = $NodeId
    if ($script:JobAttempt.ContainsKey($Id)) { $Body.attempt = $script:JobAttempt[$Id] }
    try { Send-Hub "/api/agent/jobs/$Id/update" $Body | Out-Null }
    catch {
        $code = $null; try { $code = [int]$_.Exception.Response.StatusCode } catch {}
        if ($code -eq 409) {
            # job reassigned / already final on the hub: our copy is stale -> stop it and forget it
            Write-AgentLog "job $Id rejected by hub (409, reassigned or final); stopping local copy"
            $r = $script:Running[$Id]
            if ($r -and $r.proc -and -not $r.proc.HasExited) { $r.cancelled = $true; & taskkill.exe /T /F /PID $r.proc.Id 2>$null | Out-Null }
            $script:Running.Remove($Id); $script:JobAttempt.Remove($Id)
        } else { Write-AgentLog "job $Id update failed: $($_.Exception.Message)" }
    }
}

function Protect-SystemJobDir([string]$Dir) {
    # As SYSTEM: only SYSTEM + Administrators may touch the job dir, so the (limited-token) user lane cannot swap
    # task.ps1 / spec.json before SYSTEM runs it. Skipped when not SYSTEM (tests run as the current user).
    if (-not [Security.Principal.WindowsIdentity]::GetCurrent().IsSystem) { return }
    $acl = New-Object System.Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in 'S-1-5-18', 'S-1-5-32-544') {
        $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
            (New-Object Security.Principal.SecurityIdentifier $sid), 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
    }
    Set-Acl -LiteralPath $Dir -AclObject $acl
}

# SYSTEM lane: engine 'action' (allowlisted, re-validated here and again in the job process) or 'admin'
# (free-form PowerShell). No cmd.exe and nothing untrusted on a command line: the payload lives in files.
function Start-SystemJob($Job) {
    $id = [string]$Job.id
    if (-not (Test-AllowAdmin $cfg)) {
        Update-ForgeJob $id @{ status = 'failed'; exit_code = -1; append = "[$NodeId] admin lane is disabled on this machine (agent-config.json allow_admin=false)`n" }
        Write-AgentLog "job $id refused: allow_admin=false"; return
    }
    if (-not (Test-SafeJobId $id)) { Update-ForgeJob $id @{ status = 'failed'; exit_code = -1; append = "[$NodeId] bad job id`n" }; return }
    $norm = $null
    if ($Job.engine -eq 'action') {
        # defense in depth: re-validate against the bundle's catalog before anything touches disk
        try {
            if (-not $script:ActionCatalog) { throw "action catalog missing ($AgentCatalogPath)" }
            $norm = Test-ForgeActionArgs -Catalog $script:ActionCatalog -Name ([string]$Job.action) -ActionArgs $Job.args
            $null = Get-ForgeActionSteps -Name ([string]$Job.action) -ActionArgs $norm -User ([string]$cfg.user)
        } catch {
            Update-ForgeJob $id @{ status = 'failed'; exit_code = -1; append = "[$NodeId] action rejected by agent: $($_.Exception.Message)`n" }
            Write-AgentLog "job $id action rejected: $($_.Exception.Message)"; return
        }
    }
    $dir = Join-Path $script:JobsDir "sys-$id"
    try { New-Item -ItemType Directory -Force $dir -ErrorAction Stop | Out-Null; Protect-SystemJobDir $dir }
    catch { Update-ForgeJob $id @{ status = 'failed'; exit_code = -1; append = "[$NodeId] cannot prepare $dir : $($_.Exception.Message)`n" }; return }
    $out = Join-Path $dir 'out.log'; $err = Join-Path $dir 'err.log'
    $what = ''
    if ($Job.engine -eq 'action') {
        $specPath = Join-Path $dir 'spec.json'
        Write-JsonFile $specPath ([ordered]@{ action = [string]$Job.action; args = $norm; dry_run = [bool]$cfg.action_dry_run; user = [string]$cfg.user })
        $argLine = "-NoProfile -ExecutionPolicy Bypass -File `"$AgentActionsScript`" -ActionRunSpec `"$specPath`" -ActionCatalogPath `"$AgentCatalogPath`""
        $what = "ACTION $($Job.action)$(if ($cfg.action_dry_run) { ' (dry run)' })"
    } else {
        $ps1 = Join-Path $dir 'task.ps1'
        [IO.File]::WriteAllText($ps1, [string]$Job.prompt, (New-Object Text.UTF8Encoding($true)))
        $argLine = "-NoProfile -ExecutionPolicy Bypass -File `"$ps1`""
        $what = 'ADMIN PowerShell'
    }
    try {
        $p = Start-Process -FilePath powershell.exe -ArgumentList $argLine -WorkingDirectory $dir -NoNewWindow -PassThru `
            -RedirectStandardOutput $out -RedirectStandardError $err
        $null = $p.Handle
        $script:Running[$id] = @{ proc = $p; pid = $p.Id; out = $out; offset = [long]0; err = $err; erroffset = [long]0; engine = [string]$Job.engine; cancelled = $false
                                  started = (Get-AgentEpoch); timeout_s = (Get-JobTimeoutSec $Job); source = [string]$Job.source; timed_out = $false }
        Save-RunningJobs
        $who = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        Update-ForgeJob $id @{ status = 'running'; pid = $p.Id; append = "[$NodeId] $what started as $who`n" }
        Write-AgentLog "job $id started ($what, pid $($p.Id), dir $dir)"
    } catch {
        Update-ForgeJob $id @{ status = 'failed'; exit_code = -1; append = "[$NodeId] failed to start: $($_.Exception.Message)`n" }
    }
}

# 2.4 chat: download a job's photos into <jobdir>ttachments (hub GET /api/agent/uploads/<id>) -> local paths.
function Save-JobAttachments($Job, [string]$Dir) {
    $att = Join-Path $Dir 'attachments'; New-Item -ItemType Directory -Force $att | Out-Null
    $paths = New-Object System.Collections.Generic.List[string]
    foreach ($a in @($Job.attachments | Select-Object -First 8)) {
        $aid = [string]$a.id; $name = [string]$a.name
        if (-not (Test-SafeJobId $aid) -or -not (Test-SafeUploadName $name)) { throw "bad attachment '$aid'" }
        $dest = Join-Path $att $name
        if ($paths -contains $dest) { $dest = Join-Path $att "$aid-$name" }
        $ok = $false; $lastErr = $null
        foreach ($base in (Get-HubCandidates -Urls $script:HubUrls -Sticky $script:Hub -RetryLocal $false)) {
            try { Invoke-WebRequest -Uri "$base/api/agent/uploads/$aid" -Headers $Headers -OutFile $dest -TimeoutSec 60 -UseBasicParsing | Out-Null; $ok = $true; break }
            catch { $lastErr = $_ }
        }
        if (-not $ok) { throw "download of $name failed: $($lastErr.Exception.Message)" }
        $paths.Add($dest)
    }
    , $paths.ToArray()
}

function Start-ForgeJob($Job) {
    $id  = [string]$Job.id
    if ($null -ne $Job.attempt) { $script:JobAttempt[$id] = $Job.attempt }
    if (-not (Test-JobLane $Job $Lane)) {
        Update-ForgeJob $id @{ status = 'failed'; exit_code = -1; append = "[$NodeId] job refused: engine '$($Job.engine)' (run_as '$($Job.run_as)') does not run in the $Lane lane`n" }
        Write-AgentLog "job $id refused: wrong lane ($($Job.engine))"; return
    }
    if ($IsSystemLane) { Start-SystemJob $Job; return }
    if (-not (Test-SafeJobId $id)) { Update-ForgeJob $id @{ status = 'failed'; exit_code = -1; append = "[$NodeId] bad job id`n" }; return }
    $dir = Join-Path $script:JobsDir $id; New-Item -ItemType Directory -Force $dir | Out-Null
    $out = Join-Path $dir 'out.log'; $err = Join-Path $dir 'err.log'; $promptFile = Join-Path $dir 'prompt.txt'
    $cwd = if ($Job.cwd -and (Test-Path -LiteralPath $Job.cwd)) { $Job.cwd } else { $null }
    if (-not $cwd -and $Job.scratch_of -and (Test-SafeJobId ([string]$Job.scratch_of))) {
        # 2.4 chat: a follow-up runs in the folder of the conversation's first turn (where its CLI session lives)
        $cwd = Join-Path $script:ScratchDir ([string]$Job.scratch_of)
        try { New-Item -ItemType Directory -Force $cwd -ErrorAction Stop | Out-Null } catch { $cwd = $null }
    }
    if (-not $cwd -and $Has22Helpers -and $Job.resume -and $Job.resume.session_id) {
        # resume needs the session's own cwd (claude looks sessions up per project dir)
        $m = Find-SessionMeta $script:SessState ([string]$Job.resume.tool) ([string]$Job.resume.session_id)
        if ($m -and $m.cwd -and (Test-Path -LiteralPath $m.cwd)) { $cwd = $m.cwd }
    }
    if (-not $cwd) {
        # each job gets its own folder <work_root>\scratch\<jobid> (jobs never share a scratch dir)
        $cwd = Join-Path $script:ScratchDir $id
        try { New-Item -ItemType Directory -Force $cwd -ErrorAction Stop | Out-Null }
        catch { Update-ForgeJob $id @{ status = 'failed'; exit_code = -1; append = (Format-AgentJobLine $Job $NodeId "cannot create $cwd : $($_.Exception.Message)") }; return }
    }
    if ("$($Job.engine)" -eq 'action') {      # 2.3 (G-F1): user-lane action -> agent\actions.ps1 via spec.json
        if ($HasP3Helpers) { $cmd = Get-UserActionCommand -Job $Job -Catalog $script:ActionCatalog -JobDir $dir -AgentDir $PSScriptRoot -CatalogPath $AgentCatalogPath -Tools $Tools -Config $cfg -User ([string]$cfg.user) }
        else { $cmd = @{ error = 'user-lane actions need the 2.3 helpers (agent\upkeep.ps1)' } }
    } else {
        $images = @()
        if ($Job.attachments) {
            try { $images = @(Save-JobAttachments $Job $dir) }
            catch { Update-ForgeJob $id @{ status = 'failed'; exit_code = -1; append = (Format-AgentJobLine $Job $NodeId "could not fetch the attached photos: $($_.Exception.Message)") }; return }
        }
        $cmd = Get-JobCommand -Job $Job -Tools $Tools -Config $cfg -PromptFile $promptFile -JobDir $dir -AgentDir $PSScriptRoot -OutFile $out -Images $images
    }
    if ($cmd.error) {
        Update-ForgeJob $id @{ status = 'failed'; exit_code = -1; append = (Format-AgentJobLine $Job $NodeId $cmd.error) }
        return
    }
    if ("$($Job.engine)" -eq 'action') { }   # spec.json already written
    elseif ($cmd.write -eq 'ps1') { [IO.File]::WriteAllText((Join-Path $dir 'task.ps1'), [string]$Job.prompt, (New-Object Text.UTF8Encoding($true))) }
    else { [IO.File]::WriteAllText($promptFile, (Get-AttachmentPreamble $images) + [string]$Job.prompt, $Utf8) }
    $redir = if ($cmd.redirect -eq 'err') { $err } else { $out }
    $timeout = Get-JobTimeoutSec $Job
    try {
        # TODO(Phase 3): jobs currently run with the agent's FULL admin token: the ForgeAgent task is S4U, and S4U
        # tasks are not UAC-filtered even with RunLevel Limited. Phase 3 launches jobs with a SAFER normal-user token.
        $cmdArgs = "/s /c `"$($cmd.inner) > `"$redir`" 2>&1`""
        # 2.3 S1: Start-ForgeJobProcess (agent\jobtoken.ps1, workstream S) launches with the SAFER normal-user token
        if ($HasP3Helpers) { $p = Start-ForgeJobProcess -Arguments $cmdArgs -WorkingDirectory $cwd -JobDir $dir -Job $Job }
        else { $p = Start-Process -FilePath cmd.exe -ArgumentList $cmdArgs -WorkingDirectory $cwd -WindowStyle Hidden -PassThru }
        $null = $p.Handle   # keep the handle so ExitCode is available later
        $script:Running[$id] = @{ proc = $p; pid = $p.Id; out = $out; offset = [long]0; err = $(if ($cmd.redirect -eq 'err') { $err } else { $null }); erroffset = [long]0
                                  engine = [string]$Job.engine; cancelled = $false; resume_tool = $(if ($cmd.resume_id) { [string]$Job.resume.tool } else { $null }); resume_id = $cmd.resume_id
                                  started = (Get-AgentEpoch); timeout_s = $timeout; source = [string]$Job.source; timed_out = $false }
        Save-RunningJobs
        $what = if ($cmd.resume_id) { "$($Job.engine) resume $($cmd.resume_id)" } else { "$($Job.engine)" }
        if ($Job.model) { $what += " (model $($Job.model))" }
        $note = if ($HasP3Helpers) { Get-JobLaunchNote $p } else { '' }   # G-S2: " as <user> (filtered token, Medium)"
        Update-ForgeJob $id @{ status = 'running'; pid = $p.Id; append = (Format-AgentJobLine $Job $NodeId "$what started in $cwd$(if ($Job.full_access) { ' (full access)' })$note") }
        Write-AgentLog "job $id started ($($Job.engine), pid $($p.Id), cwd $cwd, timeout $timeout s)"
    } catch {
        Update-ForgeJob $id @{ status = 'failed'; exit_code = -1; append = (Format-AgentJobLine $Job $NodeId "failed to start: $($_.Exception.Message)") }
    }
}

function Read-NewOutput($R, [string]$PathKey = 'out', [string]$OffsetKey = 'offset') {
    $path = $R[$PathKey]
    if (-not $path -or -not (Test-Path -LiteralPath $path)) { return '' }
    $fs = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try {
        if ($fs.Length -le $R[$OffsetKey]) { return '' }
        $null = $fs.Seek($R[$OffsetKey], [IO.SeekOrigin]::Begin)
        $len = [int][Math]::Min($fs.Length - $R[$OffsetKey], 200000)
        $buf = New-Object byte[] $len
        $n = $fs.Read($buf, 0, $len)
        $R[$OffsetKey] += $n
        [Text.Encoding]::UTF8.GetString($buf, 0, $n)
    } finally { $fs.Close() }
}

function Get-ProcExitCode($Proc) { try { $c = $Proc.ExitCode; if ($null -eq $c) { -1 } else { [int]$c } } catch { -1 } }

function Step-ForgeJobs {
    $dirty = $false
    $now = Get-AgentEpoch
    foreach ($id in @($script:Running.Keys)) {
        $r = $script:Running[$id]
        $o1 = $r.offset; $o2 = $r.erroffset
        $text = (Read-NewOutput $r) + (Read-NewOutput $r 'err' 'erroffset')
        if (-not $r.proc.HasExited -and -not $r.timed_out -and -not $r.cancelled -and (Test-JobTimedOut $r.started $now $r.timeout_s)) {
            # job.timeout_s (default 2 h): kill the whole process tree; reported as failed once it is gone
            $r.timed_out = $true; $dirty = $true
            & taskkill.exe /T /F /PID $r.pid 2>$null | Out-Null
            Write-AgentLog "job $id timed out after $($r.timeout_s) s; killed process tree (pid $($r.pid))"
            try { $null = $r.proc.WaitForExit(5000) } catch {}
        }
        if ($r.proc.HasExited) {
            $text += (Read-NewOutput $r) + (Read-NewOutput $r 'err' 'erroffset')
            $code = Get-ProcExitCode $r.proc
            $status = if ($r.cancelled) { 'cancelled' } elseif ($r.timed_out) { 'failed' } elseif ($code -eq 0) { 'done' } else { 'failed' }
            if ($r.timed_out) { $text += "`n" + (Format-AgentJobLine @{ source = $r.source } $NodeId "job timed out after $($r.timeout_s) s (job.timeout_s); process tree killed") }
            Update-ForgeJob $id @{ status = $status; exit_code = $code; append = $text }
            Write-AgentLog "job $id $status (exit $code)"
            $script:Running.Remove($id); $dirty = $true
            if ($r.resume_id -and $script:SessState.force) {
                # SPEC D: rescan the resumed session right away (and resend it even if unchanged)
                $script:SessState.force[$r.resume_id] = $true
                $script:NextSessScan = [datetime]::MinValue
            }
        } elseif ($text) {
            Update-ForgeJob $id @{ append = $text }
        }
        if ($r.offset -ne $o1 -or $r.erroffset -ne $o2) { $dirty = $true }
    }
    if ($dirty) { Save-RunningJobs }
}

function Stop-ForgeJob($Id) {
    $r = $script:Running[[string]$Id]
    if ($r -and -not $r.proc.HasExited) {
        $r.cancelled = $true
        & taskkill.exe /T /F /PID $r.pid 2>$null | Out-Null
        Write-AgentLog "job $Id cancelled"
        Save-RunningJobs
    }
}

# ---------- orphan safety: running jobs survive a worker restart ----------
$RunningPath = Join-Path $DataDir $(if ($IsSystemLane) { 'running-system.json' } else { 'running.json' })

function Save-RunningJobs {
    try { Write-JsonFile $RunningPath ([ordered]@{ lane = $Lane; saved = (Get-AgentEpoch); jobs = (ConvertTo-RunningRecords $script:Running) }) }
    catch { Write-AgentLog "could not save $RunningPath : $($_.Exception.Message)" }
}

# At worker start: re-attach to jobs whose process is still alive (output continues from the saved offset); jobs
# whose process is gone are reported failed with whatever output is left. Both happen before the first (boot) report,
# whose `running` list tells the hub which jobs survived.
function Restore-RunningJobs {
    $saved = Read-JsonFile $RunningPath
    $recs = @(); if ($saved -and $saved.jobs) { $recs = @($saved.jobs | Where-Object { $_ }) }
    if ($saved -and "$($saved.lane)" -and "$($saved.lane)" -ne $Lane) { $recs = @() }
    if (-not $recs.Count) { return }
    $procs = @{}
    foreach ($rec in $recs) {
        if (-not $rec.pid) { continue }
        $cp = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$rec.pid)" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($cp) { $procs[[int]$cp.ProcessId] = @{ pid = [int]$cp.ProcessId; cmd = "$($cp.CommandLine)" } }
    }
    $plan = Select-ReattachJobs -Records $recs -Procs $procs -JobsDir $script:JobsDir -Lane $Lane
    $attachIds = @{}; foreach ($a in @($plan.attach)) { if ($a) { $attachIds[[string]$a.id] = $true } }
    foreach ($rec in @($plan.attach) + @($plan.gone)) {
        if (-not $rec) { continue }
        $id = [string]$rec.id
        $dir = Join-Path $script:JobsDir (Get-JobDirName $id $Lane)
        $errName = if ($IsSystemLane -or $rec.has_err) { 'err.log' } else { $null }
        $r = @{ proc = $null; pid = [int]$rec.pid; out = (Join-Path $dir 'out.log'); offset = [long]$rec.offset; err = $(if ($errName) { Join-Path $dir $errName } else { $null })
                erroffset = [long]$rec.erroffset; engine = [string]$rec.engine; cancelled = [bool]$rec.cancelled; started = [double]$rec.started
                timeout_s = $(if ([int]$rec.timeout_s -gt 0) { [int]$rec.timeout_s } else { 7200 }); source = [string]$rec.source; timed_out = $false
                resume_tool = $(if ($rec.resume_tool) { [string]$rec.resume_tool } else { $null }); resume_id = $(if ($rec.resume_id) { [string]$rec.resume_id } else { $null }) }
        $attached = $false
        if ($attachIds.ContainsKey($id)) {
            try { $r.proc = [Diagnostics.Process]::GetProcessById([int]$rec.pid); $null = $r.proc.Handle; $attached = $true } catch { $attached = $false }
        }
        if ($attached) {
            $script:Running[$id] = $r
            Write-AgentLog "job $id re-attached after agent restart (pid $($r.pid), output offset $($r.offset))"
        } else {
            $text = ''; try { $text = (Read-NewOutput $r) + (Read-NewOutput $r 'err' 'erroffset') } catch {}
            $st = if ($r.cancelled) { 'cancelled' } else { 'failed' }
            Update-ForgeJob $id @{ status = $st; exit_code = -1; append = $text + "`n" + (Format-AgentJobLine @{ source = $r.source } $NodeId 'agent restarted while this job ran; its process is gone (exit code unknown)') }
            Write-AgentLog "job $id ended while the agent was down (pid $($r.pid)); reported $st"
        }
    }
    Save-RunningJobs
}

function Apply-HubResponse($Resp) {
    if (-not $Resp) { return }
    if ($Resp.hub_urls) { Update-HubUrls @($Resp.hub_urls) }
    if ($null -ne $Resp.drain_timeout_s) { $script:HubDrainTimeout = $Resp.drain_timeout_s }   # 2.3 R5
    if ($HasP3Helpers) {
        # 2.3 S5: rotated agent token (both lanes); F: wol etc. (user lane)
        $nt = Update-AgentTokenFromResponse $Resp
        if ($nt) { $script:Token = $nt; $Headers['X-Agent-Token'] = $nt; Write-AgentLog 'agent token rotated by the hub' }
        if (-not $IsSystemLane) { try { Invoke-UpkeepResponse $Resp } catch { Write-AgentLog "upkeep response failed: $($_.Exception.Message)" } }
    }
    if ($null -ne $Resp.settings) {
        $wr = if ($Resp.settings.work_root) { [string]$Resp.settings.work_root } else { $null }
        $mj = $Resp.settings.max_jobs
        if ($wr -ne $script:Settings.work_root) { $script:Settings.work_root = $wr; Set-WorkRoot }
        $script:Settings.max_jobs = $mj
        if ($IsSystemLane) { return }   # hub max_jobs is for the user lane; system lane stays at system_max_jobs (1)
        $m = Get-EffectiveMaxJobs -Setting $mj -Config $cfg.max_jobs
        if ($m -ne $script:MaxJobs) { Write-AgentLog "max_jobs = $m"; $script:MaxJobs = $m }
    }
}

# ---------------- 2.2: sessions, transcripts, commits, services (user lane) ----------------
$script:SessState = if ($Has22Helpers) { New-SessionIndexState } else { @{ files = @{}; sent = @{}; git = @{}; force = @{}; last_full = 0.0 } }
$script:Watches = @()
$script:TxState = New-TranscriptState
$script:ServiceStatus = @{}; $script:SvcRuntime = @{}
$script:EndpointDown = @{}
$script:T3Support = $null; $script:T3Checked = $false
$script:CommitsCursor = 0
$SessEnabled = $Has22Helpers -and (-not $IsSystemLane) -and ($cfg.sessions_enabled -ne $false)
$ClaudeHome = if ($cfg.claude_home) { [string]$cfg.claude_home } else { Join-Path $env:USERPROFILE '.claude' }
$ClaudeRoot = Join-Path $ClaudeHome 'projects'
$ClaudePidDir = Join-Path $ClaudeHome 'sessions'
$CodexRoot = if ($cfg.codex_sessions_dir) { [string]$cfg.codex_sessions_dir } else { Join-Path $env:USERPROFILE '.codex\sessions' }
$SessIntervalS = if ($cfg.sessions_interval_s) { [int]$cfg.sessions_interval_s } else { 30 }
$CommitsIntervalS = if ($cfg.commits_interval_s) { [int]$cfg.commits_interval_s } else { 300 }
$ServicesIntervalS = if ($cfg.services_interval_s) { [int]$cfg.services_interval_s } else { 60 }
$WatchdogDir = Join-Path (Split-Path $PSScriptRoot -Parent) 'watchdog'
$script:NextSessScan = [datetime]::MinValue; $script:NextCommits = (Get-Date).AddSeconds(20); $script:NextServices = [datetime]::MinValue

# POST to a 2.2 endpoint; an older hub answers 404/405 -> stop trying that path for 10 min.
# Returns @{resp} on success, $null on failure.
function Send-HubOptional([string]$Path, $Body) {
    $until = $script:EndpointDown[$Path]
    if ($until -and (Get-Date) -lt $until) { return $null }
    try { return @{ resp = (Send-Hub $Path $Body) } } catch {
        $code = $null; try { $code = [int]$_.Exception.Response.StatusCode } catch {}
        if ($code -eq 404 -or $code -eq 405) { $script:EndpointDown[$Path] = (Get-Date).AddMinutes(10); Write-AgentLog "hub has no $Path (HTTP $code); retrying in 10 min" }
        else { Write-AgentLog "$Path failed: $($_.Exception.Message)" }
        $null
    }
}

# t3 sessions are not collected yet, so t3 is reported 'unsupported' whenever the CLI is installed (never
# 'available' just because a newer `t3 project --help` lists a `list` subcommand).
# TODO: implement `t3 project list --json` + `t3 thread list --project <id> --json` -> tool='t3' sessions, then report
# 'available' when Test-T3ListSupport (sessions.ps1) passes.
function Get-T3Support {
    if ($script:T3Checked) { return $script:T3Support }
    $script:T3Checked = $true
    $script:T3Support = if ($Tools['t3']) { 'unsupported' } else { $null }
    if ($script:T3Support) { Write-AgentLog "t3 session listing: $($script:T3Support) (thread collection not implemented)" }
    $script:T3Support
}

function Step-SessionIndex {
    $now = ConvertTo-EpochFromDate (Get-Date)
    $procs = Get-AiCliProcesses
    $pidMap = Read-ClaudePidMap $ClaudePidDir
    $cyc = Invoke-SessionIndexCycle -State $script:SessState -NodeId $NodeId -ClaudeRoot $ClaudeRoot -CodexRoot $CodexRoot `
        -GitExe $Tools['git'] -Procs $procs -PidMap $pidMap -Now $now
    if (@($cyc.sessions).Count -or @($cyc.removed).Count -or $cyc.full) {
        $body = [ordered]@{ node_id = $NodeId; sessions = @($cyc.sessions); removed = @($cyc.removed) }
        $t3 = Get-T3Support; if ($t3) { $body.t3 = $t3 }
        if (Send-HubOptional '/api/agent/sessions' $body) { Confirm-SessionsSent $script:SessState $cyc $now }
    }
}

function Step-Commits {
    if (-not $Tools['git']) { return }
    $cwds = Get-KnownRepoCwds $script:SessState
    if (-not @($cwds).Count) { return }
    $sw = [Diagnostics.Stopwatch]::StartNew(); $repos = New-Object System.Collections.Generic.List[object]
    $n = @($cwds).Count
    for ($i = 0; $i -lt $n -and $repos.Count -lt 20 -and $sw.ElapsedMilliseconds -lt 3000; $i++) {
        $cwd = $cwds[($script:CommitsCursor + $i) % $n]
        $rep = Get-RepoReport $Tools['git'] $cwd $script:SessState.git[$cwd].remote
        if ($rep) { $repos.Add($rep) }
    }
    $script:CommitsCursor = ($script:CommitsCursor + $i) % [Math]::Max(1, $n)
    if ($repos.Count) { $null = Send-HubOptional '/api/agent/commits' ([ordered]@{ node_id = $NodeId; repos = $repos.ToArray() }) }
}

# SPEC C (Invoke-TranscriptStep above): up to 4 watched sessions per cycle (rotating), <= 200 events each.
function Step-Transcripts {
    Invoke-TranscriptStep -Watches @($script:Watches) -TxState $script:TxState -SessState $script:SessState -NodeId $NodeId `
        -Now (ConvertTo-EpochFromDate (Get-Date)) -Send { param($P, $B) [bool](Send-HubOptional $P $B) }
}

function Step-Services {
    $now = ConvertTo-EpochFromDate (Get-Date)
    $defs = Get-ForgeServices $cfg $script:WorkRoot      # 2.3 (G-F2): work_root -> OLLAMA_MODELS for the ollama service
    foreach ($k in @($script:ServiceStatus.Keys)) { if (-not (@($defs) | Where-Object { $_.name -eq $k })) { $script:ServiceStatus.Remove($k) } }
    foreach ($s in @($defs)) {
        $rt = $script:SvcRuntime[$s.name]
        if (-not $rt) { $rt = @{ last_start = 0.0; fails = 0 }; $script:SvcRuntime[$s.name] = $rt }
        if ($s.starting_grace_s) { $rt.grace = [int]$s.starting_grace_s }      # 2.3 F6c "starting" state
        $ok = $false
        if ($s.check) {
            $path = if ([IO.Path]::IsPathRooted($s.check)) { $s.check } else { Join-Path $WatchdogDir $s.check }
            if (-not (Test-Path -LiteralPath $path)) { $script:ServiceStatus.Remove($s.name); continue }
            try { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $path | Out-Null; $code = $LASTEXITCODE } catch { $code = -1; Write-AgentLog "service $($s.name) check failed: $($_.Exception.Message)" }
            if ($code -eq 20) { $script:ServiceStatus.Remove($s.name); continue }   # not set up here
            if ($code -eq 10) { $rt.last_start = $now; Write-AgentLog "service $($s.name): started by $($s.check)" }
            $ok = ($code -eq 0)
        } else {
            if (-not $s.health) { continue }
            if ($s.when_tool -and -not $Tools[$s.when_tool]) { $script:ServiceStatus.Remove($s.name); continue }
            $ok = Test-ServiceHealth $s.health
            $cmd = Split-ServiceCommand $s.start $Tools
            $dec = Get-ServiceDecision -ToolPresent $true -Healthy $ok -CanStart ([bool]$cmd) -Now $now -LastStart $rt.last_start -Fails $rt.fails
            if ($dec -eq 'ok') { $rt.fails = 0 }
            elseif ($dec -eq 'start') {
                $rt.last_start = $now; $rt.fails++
                try { $null = Start-ForgeServiceProcess $cmd.exe $cmd.args $s.env; Write-AgentLog "service $($s.name): unhealthy, started $($cmd.exe) $($cmd.args) (attempt $($rt.fails))" }
                catch { Write-AgentLog "service $($s.name): start failed: $($_.Exception.Message)" }
            }
        }
        if (Update-ServiceStatus $script:ServiceStatus $s.name $ok $now) { Write-AgentLog "service $($s.name): $(if ($ok) { 'ok' } else { 'not ok' })" }
    }
}

# ---------------- main loop ----------------
Set-WorkRoot
# the SYSTEM lane's ensure_task(run_as=user) needs the node's user; the user lane knows who it is
if (-not $IsSystemLane -and -not $cfg.user -and $env:USERNAME) {
    $me = "$env:USERDOMAIN\$env:USERNAME"
    Save-AgentConfig @{ user = $me }
    $cfg | Add-Member -NotePropertyName user -NotePropertyValue $me -Force
}
try { Restore-RunningJobs } catch { Write-AgentLog "re-attach of running jobs failed: $($_.Exception.Message)" }
# G-S1 (2.3 S1): user-lane jobs run with a SAFER normal-user token (agent\jobtoken.ps1); prepared once per worker
$jobTok = ''
if ($HasP3Helpers -and -not $IsSystemLane) {
    # job_token_mode comes from data\config.json (admin-written), never agent-config.json (S-R6)
    $jtMode = Get-JobTokenMode (Split-Path -Parent $ConfigPath)
    $jt = Initialize-JobToken -Config @{ job_token_mode = $jtMode }
    $jobTok = " job_token=$(Get-JobTokenStatus)$(if ($jt.error) { " ($($jt.error))" })"
    if ("$($cfg.job_token_mode)" -eq 'full' -and $jtMode -ne 'full') { $jobTok += ' (job_token_mode in agent-config.json ignored: it is only read from data\config.json)' }
}
Write-AgentLog "agent $AgentVersion starting ($Lane lane as $([Security.Principal.WindowsIdentity]::GetCurrent().Name))$($jobTok): node=$NodeId bundle=$StartBundle hubs=$($script:HubUrls -join ',') work_root=$($script:WorkRoot) tools=$(@($Tools.Keys) -join ',')$(if ($IsSystemLane) { " allow_admin=$(Test-AllowAdmin $cfg) catalog=$([bool]$script:ActionCatalog)" })"
$lastInfo = [datetime]::MinValue; $lastLogs = [datetime]::MinValue; $lastBeat = Get-Date; $lastVer = Get-Date; $boot = $true
while ($true) {
    try {
        $cap = if ($script:Draining) { 0 } else { [Math]::Max(0, $script:MaxJobs - $script:Running.Count) }
        # running = ids still running here (re-attached jobs included): on boot the hub fails only the others
        $runIds = [string[]]@($script:Running.Keys)
        if ($IsSystemLane) {
            $body = New-SystemLaneReport -NodeId $NodeId -Capacity $cap -Running $runIds -Boot $boot -AgentVersion $AgentVersion -BundleVersion $StartBundle -AgentTime (Get-AgentEpoch)
        } else {
            $body = @{ node_id = $NodeId; metrics = (Get-Metrics); procs = (Get-TopProcs); agents = (Get-Agents)
                       capacity = $cap; boot = $boot; agent_version = $AgentVersion; bundle_version = $StartBundle
                       running = @($runIds); agent_time = (Get-AgentEpoch) }
            if (((Get-Date) - $lastInfo).TotalSeconds -ge 600) { $body.info = Get-Info; $lastInfo = Get-Date }
            if (((Get-Date) - $lastLogs).TotalSeconds -ge 20) { $body.logs = Get-Logs; $lastLogs = Get-Date }
            if ($HasP3Helpers) { Add-JobTokenReport $body; Add-UpkeepReport $body }   # 2.3: S (job_token), F (macs, ...)
        }
        $resp = Send-Hub '/api/agent/report' $body
        $boot = $false
        if ($resp.need_info) { $lastInfo = [datetime]::MinValue }
        Apply-HubResponse $resp
        foreach ($j in @($resp.jobs)) { if ($j) { Start-ForgeJob $j } }
        foreach ($c in @($resp.cancel)) { if ($c) { Stop-ForgeJob $c } }
        if (-not $IsSystemLane) { $script:Watches = @($resp.watch | Where-Object { $_ }) }
    } catch {
        Write-AgentLog "report failed: $($_.Exception.Message)"
        # the watch list (and its cursors) came with the last successful report; reusing it would resend batches
        $script:Watches = @()
    }
    try { Step-ForgeJobs } catch { Write-AgentLog "job step failed: $($_.Exception.Message)" }

    if ($SessEnabled) {
        if ((Get-Date) -ge $script:NextSessScan) {
            $script:NextSessScan = (Get-Date).AddSeconds($SessIntervalS)
            try { Step-SessionIndex } catch { Write-AgentLog "session index failed: $($_.Exception.Message) @ $($_.InvocationInfo.ScriptLineNumber)" }
        }
        if (@($script:Watches).Count) { try { Step-Transcripts } catch { Write-AgentLog "transcript step failed: $($_.Exception.Message) @ $($_.InvocationInfo.ScriptLineNumber)" } }
        if ((Get-Date) -ge $script:NextCommits) {
            $script:NextCommits = (Get-Date).AddSeconds($CommitsIntervalS)
            try { Step-Commits } catch { Write-AgentLog "commits step failed: $($_.Exception.Message)" }
        }
    }
    # Keep-alive services (T3 via start-t3.ps1, ollama serve); SPEC H. Opt out of T3 with "t3_autostart": false.
    if ($Has22Helpers -and -not $IsSystemLane -and (Get-Date) -ge $script:NextServices) {
        $script:NextServices = (Get-Date).AddSeconds($ServicesIntervalS)
        try { Step-Services } catch { Write-AgentLog "services step failed: $($_.Exception.Message)" }
    }

    # heartbeat (the watchdog restarts us if agent.log goes stale for 2 min)
    if (((Get-Date) - $lastBeat).TotalSeconds -ge 60) {
        Write-AgentLog "alive: hub=$($script:Hub) running=$($script:Running.Count) max_jobs=$($script:MaxJobs) sessions=$($script:SessState.files.Count) watches=$(@($script:Watches).Count)"
        $lastBeat = Get-Date
    }
    # bundle updated by the watchdog -> stop taking jobs, exit when idle; supervisor relaunches new code
    if ($StartBundle -and ((Get-Date) - $lastVer).TotalSeconds -ge 30) {
        $lastVer = Get-Date
        $now = Get-BundleVersion
        if ($now -and $now -ne $StartBundle) {
            if (-not $script:Draining) {
                Write-AgentLog "bundle $StartBundle -> $now; draining $($script:Running.Count) job(s) before restart"
                $script:Draining = $true; $script:DrainSince = Get-AgentEpoch; $script:DrainTarget = $now
            }
        }
    }
    if ($script:Draining -and $script:Running.Count -eq 0) { Write-AgentLog "restarting onto new bundle"; exit 0 }
    if ($script:Draining -and (Test-DrainTimedOut $script:DrainSince (Get-AgentEpoch) (Get-DrainTimeoutSec $cfg.drain_timeout_s $script:HubDrainTimeout) $script:Running.Count)) {
        # 2.3 R5: jobs keep running; running.json lets the next worker (new code, started by agent-loop.cmd) re-attach them
        Write-AgentLog "drain timeout: restarting with $($script:Running.Count) job(s) still running"
        Save-RunningJobs
        exit 0
    }
    Start-Sleep -Seconds $Interval
}
