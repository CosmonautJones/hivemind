# Forge Control watchdog (SPEC-phase1 §8). Runs as SYSTEM every minute + at startup (task ForgeWatchdog).
#   1. self-heal: hub /healthz fails twice -> kill the 8787 listener, restart task ForgeHub;
#      task ForgeAgent not running or agent.log stale > 2 min -> restart it.
#      task ForgeSystemAgent (2.1 SYSTEM lane) missing -> register + start it; not running or
#      agent-system.log stale > 2 min -> restart it (only the -Lane system process is killed).
#   2. self-update: highest-version healthy peer (not in bad-versions.txt) > ours -> download its bundle,
#      verify sha256, extract to releases\<ver>, pip install if requirements changed, flip `current`,
#      restart hub (the agent drains its jobs and restarts itself when it sees the new VERSION).
#   3. rollback: local healthz fails for 3 consecutive runs after an update -> flip back, restart,
#      record the version in data\bad-versions.txt.
#   4. keep the last 3 releases; data\watchdog.log (rotated at 5 MB); data\watchdog.json.
#      Job dirs under <work_root>\jobs untouched for 14 days are deleted (at most once per hour; running jobs kept).
#   2.3 (CONTRACT-phase3 R): the ForgeWatchdog task runs the frozen <FH>\bin\wd-launcher.ps1 (copied + task migrated
#      here once); the agents run under <FH>\bin\agent-loop.cmd (no in-script supervisor). Release health =
#      /healthz ok (DB write probe, free space) + eligible + the local agent on the new version (release_health_s);
#      a 503 for low disk only is not restarted, a failed DB write probe is (max 3/h). Requirements: unchanged -> no pip; changed -> releases\<ver>\venv
#      (promoted into <FH>\venv once the release is confirmed).
#   2.2 (SPEC-phase2 A): download/extract any time, but ACTIVATE (pip + flip) only while holding the leader's
#      update lease (POST /cluster/update-lease); "waiting for update lease (holder X)" otherwise. Pre-2.2 leader
#      (404) -> old behaviour; no leader reachable for > 5 min -> proceed (lone survivor). The lease is released
#      (DELETE) once the new release is confirmed healthy, or after a rollback (+ POST /cluster/bad-version). Versions in the
#      leader's /cluster/status bad_versions (fleet-bad) are never installed.
# Safe to run every minute: a lock file prevents overlap; the healthy no-op path is a few HTTP calls.
#
#   -DryRun : log every action instead of doing it (no state written).
#   -NoRun  : dot-source for tests:  . .\watchdog.ps1 -NoRun
param(
    [string]$ForgeHome = (Join-Path $env:ProgramData 'Forge'),
    [int]$Port = 8787,
    [switch]$DryRun,
    [switch]$NoRun
)

$WatchdogVersion = '2.3.0'
$RequiredBundleFiles = @('hub/app.py', 'agent/forge-agent.ps1', 'watchdog/watchdog.ps1', 'watchdog/run-hub.cmd')

# ======================= pure helpers (unit-tested) =======================

# Semver-ish parse: "2.10.1", "v2.1", "2.1.0-rc.1". Returns @{nums=[int[]](3); pre=string}
function ConvertTo-SemVer([string]$Version) {
    $v = "$Version".Trim()
    if ($v -match '^[vV]') { $v = $v.Substring(1) }
    $pre = ''
    $plus = $v.IndexOf('+'); if ($plus -ge 0) { $v = $v.Substring(0, $plus) }
    $dash = $v.IndexOf('-'); if ($dash -ge 0) { $pre = $v.Substring($dash + 1); $v = $v.Substring(0, $dash) }
    $parts = $v.Split('.')
    $nums = New-Object int[] 3
    for ($i = 0; $i -lt 3; $i++) {
        $n = 0
        if ($i -lt $parts.Count) { [void][int]::TryParse($parts[$i], [ref]$n) }
        $nums[$i] = $n
    }
    @{ nums = $nums; pre = $pre }
}

# -1 if A < B, 0 if equal, 1 if A > B. A release sorts above its prereleases. Null/empty is lowest.
function Compare-SemVer([string]$A, [string]$B) {
    if (-not "$A".Trim() -and -not "$B".Trim()) { return 0 }
    if (-not "$A".Trim()) { return -1 }
    if (-not "$B".Trim()) { return 1 }
    $x = ConvertTo-SemVer $A; $y = ConvertTo-SemVer $B
    for ($i = 0; $i -lt 3; $i++) {
        if ($x.nums[$i] -lt $y.nums[$i]) { return -1 }
        if ($x.nums[$i] -gt $y.nums[$i]) { return 1 }
    }
    if ($x.pre -eq $y.pre) { return 0 }
    if (-not $x.pre) { return 1 }
    if (-not $y.pre) { return -1 }
    $pa = $x.pre.Split('.'); $pb = $y.pre.Split('.')
    for ($i = 0; $i -lt [Math]::Max($pa.Count, $pb.Count); $i++) {
        if ($i -ge $pa.Count) { return -1 }
        if ($i -ge $pb.Count) { return 1 }
        $na = 0; $nb = 0
        $ia = [int]::TryParse($pa[$i], [ref]$na); $ib = [int]::TryParse($pb[$i], [ref]$nb)
        if ($ia -and $ib) { if ($na -ne $nb) { if ($na -lt $nb) { return -1 } else { return 1 } } ; continue }
        if ($ia) { return -1 }; if ($ib) { return 1 }
        $c = [string]::CompareOrdinal($pa[$i], $pb[$i])
        if ($c -ne 0) { if ($c -lt 0) { return -1 } else { return 1 } }
    }
    0
}

function Test-VersionString([string]$V) { "$V" -match '^[vV]?\d+(\.\d+){0,2}(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$' }

# Peers: objects with id, version, bundle_sha256, url (healthy = answered status). Returns the peer
# with the highest version strictly above Current, skipping bad versions and peers without a sha.
function Select-UpdateCandidate {
    param([object[]]$Peers = @(), [string]$Current, [string[]]$Bad = @())
    $best = $null
    foreach ($p in $Peers) {
        if (-not $p -or -not $p.version -or -not (Test-VersionString $p.version)) { continue }
        if (-not $p.bundle_sha256) { continue }
        if ($Bad -contains [string]$p.version) { continue }
        if ((Compare-SemVer $p.version $Current) -le 0) { continue }
        if (-not $best -or (Compare-SemVer $p.version $best.version) -gt 0) { $best = $p }
    }
    $best
}

# Versions present under releases\. Keep `Current`, anything in Protect, then the newest others until
# Keep total are kept. Returns the versions to delete (oldest first).
function Select-ReleasesToPrune {
    param([string[]]$Versions = @(), [string]$Current, [int]$Keep = 3, [string[]]$Protect = @())
    $keepSet = New-Object System.Collections.Generic.List[string]
    if ($Current -and $Versions -contains $Current) { $keepSet.Add($Current) }
    foreach ($p in $Protect) { if ($p -and $Versions -contains $p -and -not $keepSet.Contains($p)) { $keepSet.Add($p) } }
    $sorted = Sort-VersionsDescending $Versions
    foreach ($v in $sorted) {
        if ($keepSet.Count -ge $Keep) { break }
        if (-not $keepSet.Contains($v)) { $keepSet.Add($v) }
    }
    $drop = @($sorted | Where-Object { -not $keepSet.Contains($_) })
    [array]::Reverse($drop)
    , $drop
}

function Sort-VersionsDescending([string[]]$Versions) {
    $list = New-Object System.Collections.Generic.List[string]
    foreach ($v in $Versions) { if ($v) { $list.Add($v) } }
    $list.Sort([Comparison[string]] { param($a, $b) Compare-SemVer $b $a })
    , $list.ToArray()
}

# Post-update probation. Probation: @{from; to; fails} or $null. Returns @{action; fails}:
#   none     - no probation for the running version
#   confirm  - healthy: update is good, end probation
#   wait     - unhealthy but < Threshold consecutive runs (fails = new count)
#   rollback - unhealthy for Threshold consecutive runs
#   skip     - in restart grace period, don't count this run
function Get-RollbackDecision {
    param($Probation, [string]$CurrentVersion, [bool]$HealthOk, [bool]$InGrace = $false, [int]$Threshold = 3)
    if (-not $Probation -or -not $Probation.to -or $Probation.to -ne $CurrentVersion) { return @{ action = 'none'; fails = 0 } }
    $f = 0; if ($Probation.fails) { $f = [int]$Probation.fails }
    if ($HealthOk) { return @{ action = 'confirm'; fails = 0 } }
    if ($InGrace) { return @{ action = 'skip'; fails = $f } }
    $f++
    if ($f -ge $Threshold) { return @{ action = 'rollback'; fails = $f } }
    @{ action = 'wait'; fails = $f }
}

function Get-FileSha256([string]$Path) {
    $sha = [Security.Cryptography.SHA256]::Create()
    $fs = [IO.File]::OpenRead($Path)
    try { ([BitConverter]::ToString($sha.ComputeHash($fs)) -replace '-', '').ToLowerInvariant() } finally { $fs.Close(); $sha.Dispose() }
}

function Test-BundleSha([string]$Path, [string]$Expected) {
    if (-not $Expected -or -not (Test-Path -LiteralPath $Path)) { return $false }
    $e = $Expected.Trim().ToLowerInvariant()
    if ($e -match '^sha256:') { $e = $e.Substring(7) }
    (Get-FileSha256 $Path) -eq $e
}

function Test-StaleFile([string]$Path, [datetime]$Now, [int]$MaxAgeSec = 120) {
    if (-not (Test-Path -LiteralPath $Path)) { return $true }
    (($Now - (Get-Item -LiteralPath $Path).LastWriteTime).TotalSeconds -gt $MaxAgeSec)
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

function ConvertTo-Hashtable($Obj) {
    $h = @{}
    if ($Obj) { foreach ($p in $Obj.PSObject.Properties) { $h[$p.Name] = $p.Value } }
    $h
}

function Test-PsFilesParse([string]$Dir) {
    $bad = @()
    foreach ($f in Get-ChildItem -LiteralPath $Dir -Recurse -Filter *.ps1 -File -ErrorAction SilentlyContinue) {
        $errs = $null; $tok = $null
        [void][System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$tok, [ref]$errs)
        if ($errs -and $errs.Count) { $bad += $f.FullName }
    }
    , $bad
}

# ======================= release management (file ops, testable on a scratch ForgeHome) =======================

function Get-ReleaseVersions([string]$ForgeHome) {
    $rel = Join-Path $ForgeHome 'releases'
    if (-not (Test-Path -LiteralPath $rel)) { return , @() }
    , @(Get-ChildItem -LiteralPath $rel -Directory | Where-Object { (Test-VersionString $_.Name) -and (Test-Path -LiteralPath (Join-Path $_.FullName 'VERSION')) } | ForEach-Object { $_.Name })
}

function Get-CurrentTarget([string]$ForgeHome) {
    $cur = Join-Path $ForgeHome 'current'
    if (-not (Test-Path -LiteralPath $cur)) { return $null }
    $it = Get-Item -LiteralPath $cur -Force
    $t = @($it.Target) | Where-Object { $_ } | Select-Object -First 1
    if ($t) { return [string]$t }
    $it.FullName
}

function Get-CurrentVersion([string]$ForgeHome) {
    $f = Join-Path $ForgeHome 'current\VERSION'
    if (Test-Path -LiteralPath $f) { return (Get-Content -LiteralPath $f -Raw).Trim() }
    $null
}

function Remove-Junction([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $it = Get-Item -LiteralPath $Path -Force
    if (($it.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        # rmdir on a junction removes the link only, never the target's contents
        & cmd.exe /d /c "rmdir `"$Path`"" | Out-Null
    } else {
        Move-Item -LiteralPath $Path -Destination "$Path.bak-$(Get-Date -Format yyyyMMddHHmmss)" -Force
    }
}

# Point <ForgeHome>\current at <ForgeHome>\releases\<Version> (build current.new, swap).
function Set-CurrentRelease([string]$ForgeHome, [string]$Version) {
    $target = Join-Path $ForgeHome "releases\$Version"
    if (-not (Test-Path -LiteralPath (Join-Path $target 'VERSION'))) { throw "release $Version not found at $target" }
    $cur = Join-Path $ForgeHome 'current'; $new = "$cur.new"
    Remove-Junction $new
    & cmd.exe /d /c "mklink /J `"$new`" `"$target`"" | Out-Null
    if (-not (Test-Path -LiteralPath (Join-Path $new 'VERSION'))) { throw "mklink /J failed for $new" }
    Remove-Junction $cur
    [IO.Directory]::Move($new, $cur)
}

# Extract + validate a bundle zip into releases\<ver>. Returns the version installed.
function Install-ReleaseZip {
    param([string]$ForgeHome, [string]$ZipPath, [string]$ExpectedSha, [string]$ExpectedVersion)
    if (-not (Test-BundleSha $ZipPath $ExpectedSha)) { throw "sha256 mismatch for $ZipPath (expected $ExpectedSha, got $(Get-FileSha256 $ZipPath))" }
    $rel = Join-Path $ForgeHome 'releases'
    New-Item -ItemType Directory -Force $rel | Out-Null
    $stage = Join-Path $rel ("_staging-" + [guid]::NewGuid().ToString('n').Substring(0, 8))
    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($ZipPath, $stage)
        $vf = Join-Path $stage 'VERSION'
        if (-not (Test-Path -LiteralPath $vf)) { throw 'bundle has no VERSION' }
        $ver = (Get-Content -LiteralPath $vf -Raw).Trim()
        if (-not (Test-VersionString $ver)) { throw "bundle VERSION '$ver' is not a version" }
        if ($ExpectedVersion -and $ver -ne $ExpectedVersion) { throw "bundle VERSION $ver != advertised $ExpectedVersion" }
        foreach ($req in $RequiredBundleFiles) {
            if (-not (Test-Path -LiteralPath (Join-Path $stage $req))) { throw "bundle is missing $req" }
        }
        $bad = Test-PsFilesParse $stage
        if ($bad.Count) { throw "bundle scripts do not parse: $($bad -join ', ')" }
        $dest = Join-Path $rel $ver
        $curT = Get-CurrentTarget $ForgeHome
        if (Test-Path -LiteralPath $dest) {
            if ($curT -and ((Resolve-Path -LiteralPath $curT).Path -eq (Resolve-Path -LiteralPath $dest).Path)) { throw "release $ver is the active release; not overwriting" }
            Remove-Item -LiteralPath $dest -Recurse -Force
        }
        # same layout /cluster/publish uses: extracted files + bundle.zip + bundle.sha256
        Copy-Item -LiteralPath $ZipPath -Destination (Join-Path $stage 'bundle.zip') -Force
        [IO.File]::WriteAllText((Join-Path $stage 'bundle.sha256'), (Get-FileSha256 $ZipPath), (New-Object Text.UTF8Encoding($false)))
        [IO.Directory]::Move($stage, $dest)
        $ver
    } finally {
        if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

# A release already extracted under releases\<ver> (by POST /cluster/publish on this node, or an
# earlier watchdog run) can be activated without downloading if its bundle.zip has the expected sha.
function Test-LocalRelease {
    param([string]$ForgeHome, [string]$Version, [string]$ExpectedSha)
    $d = Join-Path $ForgeHome "releases\$Version"
    $vf = Join-Path $d 'VERSION'
    if (-not (Test-Path -LiteralPath $vf)) { return $false }
    if ((Get-Content -LiteralPath $vf -Raw).Trim() -ne $Version) { return $false }
    if (-not (Test-BundleSha (Join-Path $d 'bundle.zip') $ExpectedSha)) { return $false }
    foreach ($req in $RequiredBundleFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $d $req))) { return $false }
    }
    (Test-PsFilesParse $d).Count -eq 0
}

# ForgeSystemAgent (2.1 SYSTEM lane): the action line the installer and the watchdog both register.
function Get-SystemAgentTaskArgs([string]$ForgeHome) {
    $cur = Join-Path $ForgeHome 'current'
    $cfgPath = Join-Path $ForgeHome 'data\agent-config.json'
    "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$cur\agent\forge-agent.ps1`" -ConfigPath `"$cfgPath`" -Lane system"
}

# Which lane does a forge-agent.ps1 command line belong to? $null if it is not an agent.
function Get-AgentLaneFromCmd([string]$CommandLine) {
    if ("$CommandLine" -notmatch 'forge-agent\.ps1') { return $null }
    if ("$CommandLine" -match '-Lane\s+"?system\b') { return 'system' }
    'user'
}

function Get-BadVersions([string]$DataDir) {
    $f = Join-Path $DataDir 'bad-versions.txt'
    if (-not (Test-Path -LiteralPath $f)) { return , @() }
    , @(Get-Content -LiteralPath $f | ForEach-Object { $_.Trim() } | Where-Object { $_ -and -not $_.StartsWith('#') })
}

# ---- 2.2 staggered rollout (SPEC-phase2 A) ----
# Fleet-bad versions: the leader's /cluster/status `bad_versions` (unioned over every status we saw, so a
# standby that replicated it counts too). Never installed, like data\bad-versions.txt.
function Get-FleetBadVersions([object[]]$Statuses = @()) {
    $set = New-Object System.Collections.Generic.List[string]
    foreach ($s in @($Statuses)) {
        if (-not $s) { continue }
        foreach ($v in @($s.bad_versions)) { if ($v -and -not $set.Contains([string]$v)) { $set.Add([string]$v) } }
    }
    , $set.ToArray()
}

# Statuses: @{url; role}. The first one whose hub says it is the leader.
function Find-LeaderUrl([object[]]$Statuses = @()) {
    foreach ($s in @($Statuses)) { if ($s -and "$($s.role)" -eq 'leader' -and $s.url) { return ([string]$s.url).TrimEnd('/') } }
    $null
}

# Result of asking the leader for the update lease: @{kind = granted|denied|unsupported|error|unreachable; holder}.
# Returns @{action = proceed|wait; reason; holder; no_leader_since}:
#   granted      -> proceed (we hold the lease)
#   unsupported  -> proceed (leader is a pre-2.2 hub without the endpoint: old behaviour)
#   denied       -> wait    "waiting for update lease (holder X)"
#   error        -> wait    a leader ANSWERED (401/422/500/502...): never the lone-survivor path, and it resets that
#                           clock (otherwise every node would activate at the same moment after 5 min)
#   unreachable  -> wait, unless no leader answered for >= LoneSurvivorSec (lone survivor) -> proceed
#                   (connection failure, or 503 "no leader" from a standby)
function Get-LeaseDecision {
    param($Result, $NoLeaderSince = $null, [double]$Now = 0, [int]$LoneSurvivorSec = 300)
    $kind = if ($Result -and $Result.kind) { [string]$Result.kind } else { 'unreachable' }
    if ($kind -eq 'granted') { return @{ action = 'proceed'; reason = 'update lease granted'; holder = $null; no_leader_since = $null } }
    if ($kind -eq 'unsupported') { return @{ action = 'proceed'; reason = 'leader has no update lease endpoint (pre-2.2 hub); proceeding'; holder = $null; no_leader_since = $null } }
    if ($kind -eq 'denied') {
        $h = if ($Result.holder) { [string]$Result.holder } else { 'unknown' }
        $why = if ($Result.why) { ": $($Result.why)$(if ($Result.waiting_for) { " $($Result.waiting_for)" })" } else { '' }
        return @{ action = 'wait'; reason = "waiting for update lease (holder $h)$why"; holder = $h; no_leader_since = $null }
    }
    if ($kind -eq 'error') {
        return @{ action = 'wait'; reason = "waiting for update lease: request failed (HTTP $($Result.code)): $($Result.msg)"; holder = $null; no_leader_since = $null }
    }
    $since =if ($null -ne $NoLeaderSince -and "$NoLeaderSince" -ne '') { [double]$NoLeaderSince } else { $Now }
    $age = [int]($Now - $since)
    if ($age -ge $LoneSurvivorSec) { return @{ action = 'proceed'; reason = "no leader reachable for $age s (lone survivor); proceeding without lease"; holder = $null; no_leader_since = $since } }
    @{ action = 'wait'; reason = "waiting for update lease (holder unknown: no leader reachable for $age s)"; holder = $null; no_leader_since = $since }
}

function Request-UpdateLease([string]$LeaderUrl, [string]$Token, [string]$NodeId, [string]$Version, [int]$TimeoutSec = 5) {
    $body = [Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -InputObject @{ node_id = $NodeId; version = $Version } -Compress))
    try {
        $r = Invoke-RestMethod -Uri "$($LeaderUrl.TrimEnd('/'))/cluster/update-lease" -Method Post -Headers @{ 'X-Cluster-Token' = $Token } `
            -ContentType 'application/json' -Body $body -TimeoutSec $TimeoutSec -UseBasicParsing
        if ($r.granted -eq $true) { return @{ kind = 'granted'; holder = [string]$r.holder } }
        return @{ kind = 'denied'; holder = [string]$r.holder; retry_after_s = $r.retry_after_s; why = [string]$r.reason; waiting_for = [string]$r.waiting_for }
    } catch {
        $code = $null; try { $code = [int]$_.Exception.Response.StatusCode } catch {}
        if ($code -eq 404 -or $code -eq 405 -or $code -eq 501) { return @{ kind = 'unsupported'; code = $code } }
        if ($code -eq 503) { return @{ kind = 'unreachable'; code = $code; msg = 'no leader' } }
        if ($code -eq 409 -or $code -eq 423 -or $code -eq 429) {
            $holder = $null; try { $holder = [string]($_.ErrorDetails.Message | ConvertFrom-Json).holder } catch {}
            return @{ kind = 'denied'; holder = $holder; code = $code }
        }
        if ($code) { return @{ kind = 'error'; code = $code; msg = $_.Exception.Message } }
        @{ kind = 'unreachable'; msg = $_.Exception.Message }
    }
}

# 2.2 hub: explicit fleet-bad report after a rollback (the leader also detects it on its own).
function Send-BadVersion([string]$HubUrl, [string]$Token, [string]$NodeId, [string]$Version, [int]$TimeoutSec = 5) {
    if (-not $HubUrl -or -not $Version) { return $false }
    $body = [Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -InputObject @{ version = $Version; node_id = $NodeId; reason = 'watchdog rollback' } -Compress))
    try {
        Invoke-RestMethod -Uri "$($HubUrl.TrimEnd('/'))/cluster/bad-version" -Method Post -Headers @{ 'X-Cluster-Token' = $Token } `
            -ContentType 'application/json' -Body $body -TimeoutSec $TimeoutSec -UseBasicParsing | Out-Null
        $true
    } catch { $false }
}

function Remove-UpdateLease([string]$LeaderUrl, [string]$Token, [string]$NodeId, [string]$BadVersion, [int]$TimeoutSec = 5) {
    if (-not $LeaderUrl) { return $false }
    $q = if ($BadVersion) { "?bad_version=$([uri]::EscapeDataString($BadVersion))" } else { '' }
    try {
        Invoke-RestMethod -Uri "$($LeaderUrl.TrimEnd('/'))/cluster/update-lease/$([uri]::EscapeDataString($NodeId))$q" -Method Delete `
            -Headers @{ 'X-Cluster-Token' = $Token } -TimeoutSec $TimeoutSec -UseBasicParsing | Out-Null
        $true
    } catch { $false }
}

# The lease step of Invoke-Update as a state machine. State (watchdog-state.json hashtable) keys used:
# lease_no_leader_since, lease_wait, lease. Record = $false (dry run) never stores a held lease.
# Returns Get-LeaseDecision's result; State is updated in place.
function Step-LeaseState {
    param($Result, [hashtable]$State, [string]$Version, [string]$Leader, [double]$Now, [int]$LoneSurvivorSec = 300, [bool]$Record = $true)
    $ld = Get-LeaseDecision -Result $Result -NoLeaderSince $State.lease_no_leader_since -Now $Now -LoneSurvivorSec $LoneSurvivorSec
    $State.lease_no_leader_since = $ld.no_leader_since
    if ($ld.action -eq 'wait') {
        $State.lease_wait = @{ version = $Version; holder = $ld.holder; at = $Now; reason = $ld.reason }
        return $ld
    }
    $State.lease_wait = $null
    $State.lease = if ($Record -and $Result -and $Result.kind -eq 'granted' -and $Leader) { @{ version = $Version; url = $Leader; at = $Now } } else { $null }
    $ld
}

function Get-WdNodeId($Cfg) { if ($Cfg -and $Cfg.node_id) { [string]$Cfg.node_id } else { $env:COMPUTERNAME.ToLowerInvariant() } }

# Give the update lease back (after the new release proved healthy, or after a failed/rolled-back activation).
# Uses $script:State, $DryRun, $Local and Write-WdLog from the runtime (tests stub them).
function Clear-UpdateLease($Cfg, [string]$BadVersion) {
    $l = $script:State.lease
    if ($BadVersion -and -not $DryRun) {
        # tell the leader (any 2.2 hub proxies it) even when we held no lease (pre-2.2 leader / lone survivor)
        $u = if ($l -and $l.url) { [string]$l.url } else { $Local }
        if (Send-BadVersion $u ([string]$Cfg.cluster_token) (Get-WdNodeId $Cfg) $BadVersion) { Write-WdLog "reported $BadVersion as fleet-bad to $u" }
    }
    if (-not $l) { return }
    if ($DryRun) { Write-WdLog "[dry-run] release update lease ($($l.version))"; return }
    $ok = Remove-UpdateLease ([string]$l.url) ([string]$Cfg.cluster_token) (Get-WdNodeId $Cfg) $BadVersion
    Write-WdLog "released update lease for $($l.version)$(if ($BadVersion) { " (rolled back: $BadVersion is bad)" })$(if (-not $ok) { ' (leader did not answer; it expires on its own)' })"
    $script:State.lease = $null
}

# ---- job dir cleanup (<work_root>\jobs, 14 days, at most once per hour) ----
# Work roots from data\agent-config.json (work_root_current = the agent's effective root, work_root_auto = its auto
# pick). The file is user-writable, so only absolute local-drive paths are accepted and only <root>\jobs is touched.
function Get-JobCleanupRoots($AgentCfg) {
    $out = New-Object System.Collections.Generic.List[string]
    if (-not $AgentCfg) { return , $out.ToArray() }
    foreach ($k in 'work_root_current', 'work_root_auto') {
        $v = "$($AgentCfg.$k)".Trim().TrimEnd('\')
        if ($v -notmatch '^[A-Za-z]:\\[^<>|"?*]+$' -or $v -match '\\\.\.?(\\|$)') { continue }
        if (-not ($out | Where-Object { $_ -eq $v })) { $out.Add($v) }
    }
    , $out.ToArray()
}

# Dirs: @{name; path; mtime (newest of the dir and its top-level files, epoch); reparse; marker (has a job file)}.
# Stale = older than MaxAgeDays, a real dir (never a junction/symlink), named like a job dir, and holding a job
# file (out.log / err.log / prompt.txt / task.ps1 / spec.json). Skip = ids of jobs still running.
function Select-StaleJobDirs {
    param([object[]]$Dirs = @(), [double]$Now, [int]$MaxAgeDays = 14, [string[]]$Skip = @())
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($d in @($Dirs)) {
        if (-not $d -or $d.reparse -or -not $d.marker) { continue }
        $n = [string]$d.name
        if ($n -notmatch '^(sys-)?[A-Za-z0-9_-]{1,64}$') { continue }
        if ($Skip -contains $n -or $Skip -contains ($n -replace '^sys-', '')) { continue }
        if (($Now - [double]$d.mtime) -lt ($MaxAgeDays * 86400)) { continue }
        $out.Add([string]$d.path)
    }
    , $out.ToArray()
}

# rmdir /s removes junctions/symlinks inside the tree as links: it never deletes what they point to (a job can
# leave a junction to C:\Windows in its dir; Remove-Item -Recurse on PS 5.1 would follow it). Returns $true when gone.
function Remove-JobDirTree([string]$Path) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $true }
    & cmd.exe /d /c "rmdir /s /q `"$Path`"" 2>$null | Out-Null
    -not (Test-Path -LiteralPath $Path)
}

function Test-CleanupDue($LastRun, [double]$Now, [int]$EverySec = 3600) {
    if ($null -eq $LastRun -or "$LastRun" -eq '') { return $true }
    ($Now - [double]$LastRun) -ge $EverySec
}


# ---- 2.3 R1: release health gate (CONTRACT-phase3.md section 5 R1) ----
# A release on probation is healthy when /healthz answers ok, /cluster/status says eligible, and the local agent reports
# the new bundle_version within HealthSec (release_health_s, 180). Returns @{ok; pending; reason}:
#   pending = not judged yet (does not count as a strike): the hub is still syncing, the agent is restarting or still
#             draining its jobs toward this version (agent draining_to), or the disk is low (not the release's fault).
# Status $null (status call failed) keeps the 2.2 rule: healthz alone decides.
function Get-ReleaseHealth {
    param([bool]$HubUp, [bool]$HubOk, $Body = $null, $Status = $null, [string]$Version, [double]$ElapsedSec = 0,
          [int]$HealthSec = 180, [bool]$AgentRequired = $true, [int]$DrainSec = 1800)
    if (-not $HubUp) { return @{ ok = $false; pending = $false; reason = 'hub does not answer /healthz' } }
    if (-not $HubOk) {
        $chk = if ($Body) { $Body.checks } else { $null }
        if ($chk -and $chk.db_write -ne $false -and $chk.disk -eq $false) {
            return @{ ok = $false; pending = $true; reason = "low disk ($($Body.free_gb) GB free); not counted against the release" }
        }
        $why = if ($chk -and $chk.db_write -eq $false) { 'database write probe failed' } else { 'healthz unhealthy' }
        return @{ ok = $false; pending = $false; reason = $why }
    }
    if (-not $Status) { return @{ ok = $true; pending = $false; reason = 'healthz ok (no cluster status)' } }
    $run = [string]$Status.running_version
    if ($run -and $Version -and $run -ne $Version) { return @{ ok = $false; pending = $false; reason = "hub runs $run, not $Version" } }
    if ($Status.eligible -eq $false) {
        if ($ElapsedSec -lt $HealthSec) { return @{ ok = $false; pending = $true; reason = 'hub is not eligible yet (syncing)' } }
        return @{ ok = $false; pending = $false; reason = "hub not eligible after $HealthSec s" }
    }
    $la = $Status.local_agent
    if ($AgentRequired -and $la) {
        $bv = [string]$la.bundle_version
        if (-not ($bv -and (Compare-SemVer $bv $Version) -ge 0)) {
            if ([string]$la.draining_to -eq $Version -and $ElapsedSec -lt ($DrainSec + $HealthSec)) {
                return @{ ok = $false; pending = $true; reason = "agent is draining its jobs before it restarts on $Version" }
            }
            if ($ElapsedSec -lt $HealthSec) { return @{ ok = $false; pending = $true; reason = "waiting for the agent to report $Version" } }
            return @{ ok = $false; pending = $false; reason = "agent did not report $Version within $HealthSec s (reports '$bv')" }
        }
    }
    @{ ok = $true; pending = $false; reason = 'healthz ok, eligible, agent on the new version' }
}

# Seconds since an ISO-8601 time ('o' format), 0 when missing / unparsable.
function Get-ElapsedSec($Since, [double]$NowEpoch) {
    if (-not $Since) { return 0 }
    try { [Math]::Max(0, $NowEpoch - [double]([DateTimeOffset]::Parse([string]$Since, [Globalization.CultureInfo]::InvariantCulture)).ToUnixTimeSeconds()) } catch { 0 }
}

# ---- 2.3 R5: per-release venv ----
# ReleaseSha = sha256 of the release's requirements.txt ('' = none); BaseSha = what <FH>\venv has (data\requirements.sha256);
# OwnSha = releases\<ver>\venv\forge-requirements.sha256 when the release already has its own venv.
#   none  -> the base venv fits (no pip, no venv);  keep -> its own venv already fits;  build -> python -m venv + pip.
function Get-VenvPlan([string]$ReleaseSha, [string]$BaseSha, [string]$OwnSha) {
    if (-not $ReleaseSha -or $ReleaseSha -eq $BaseSha) { return 'none' }
    if ($OwnSha -and $OwnSha -eq $ReleaseSha) { return 'keep' }
    'build'
}

# python.exe the base venv was made from (pyvenv.cfg home = ...), else the venv's own python.
function Get-BasePython([string]$ForgeHome) {
    $venvPy = Join-Path $ForgeHome 'venv\Scripts\python.exe'
    $cfgFile = Join-Path $ForgeHome 'venv\pyvenv.cfg'
    if (Test-Path -LiteralPath $cfgFile) {
        foreach ($l in Get-Content -LiteralPath $cfgFile) {
            if ($l -match '^\s*home\s*=\s*(.+?)\s*$') { $p = Join-Path $Matches[1] 'python.exe'; if (Test-Path -LiteralPath $p) { return $p } }
        }
    }
    $venvPy
}

# ---- 2.3 R2 / R7: frozen launcher + agent loop in <FH>\bin, task specs ----
# Version stamped in a launcher file: $LauncherVersion = N (wd-launcher.ps1) or "rem LoopVersion=N" (agent-loop.cmd).
function Get-LauncherFileVersion([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return 0 }
    $t = [IO.File]::ReadAllText($Path)
    if ($t -match '\$LauncherVersion\s*=\s*(\d+)') { return [int]$Matches[1] }
    if ($t -match 'LoopVersion=(\d+)') { return [int]$Matches[1] }
    0
}

# Copies current\watchdog\wd-launcher.ps1 + agent-loop.cmd to <FH>\bin when missing or older. -> , @(file names copied)
# <FH>\bin inherits the <FH> root ACL (Administrators + SYSTEM full, the user read + execute): never under data\.
function Install-WdLauncher {
    param([string]$ForgeHome, [switch]$DryRun)
    $src = Join-Path $ForgeHome 'current\watchdog'
    $bin = Join-Path $ForgeHome 'bin'
    $done = New-Object System.Collections.Generic.List[string]
    foreach ($f in 'wd-launcher.ps1', 'agent-loop.cmd') {
        $s = Join-Path $src $f; $d = Join-Path $bin $f
        if (-not (Test-Path -LiteralPath $s)) { continue }
        if ((Test-Path -LiteralPath $d) -and (Get-LauncherFileVersion $s) -le (Get-LauncherFileVersion $d)) { continue }
        $done.Add($f)
        if ($DryRun) { continue }
        New-Item -ItemType Directory -Force $bin | Out-Null
        Copy-Item -LiteralPath $s -Destination "$d.tmp" -Force
        Move-Item -LiteralPath "$d.tmp" -Destination $d -Force
    }
    , $done.ToArray()
}

# The four Forge tasks as 2.3 registers them. user = 'user' (S4U, the node user) | 'system';
# trigger = 'startup' | 'minute'; restart = @{count; interval_min} (long-running tasks) | $null.
function Get-ForgeTaskSpecs {
    param([string]$ForgeHome)
    $cur = Join-Path $ForgeHome 'current'; $bin = Join-Path $ForgeHome 'bin'; $data = Join-Path $ForgeHome 'data'
    $restart = @{ count = 3; interval_min = 1 }
    @(
        @{ name = 'ForgeHub'; user = 'user'; command = 'cmd.exe'; arguments = "/d /c `"$cur\watchdog\run-hub.cmd`""; dir = $cur; trigger = 'startup'; restart = $restart },
        @{ name = 'ForgeAgent'; user = 'user'; command = 'cmd.exe'; arguments = "/d /s /c `"`"$bin\agent-loop.cmd`" user`""; dir = $data; trigger = 'startup'; restart = $restart },
        @{ name = 'ForgeSystemAgent'; user = 'system'; command = 'cmd.exe'; arguments = "/d /s /c `"`"$bin\agent-loop.cmd`" system`""; dir = $data; trigger = 'startup'; restart = $restart },
        @{ name = 'ForgeWatchdog'; user = 'system'; command = 'powershell.exe'; arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$bin\wd-launcher.ps1`" -ForgeHome `"$ForgeHome`""; dir = $data; trigger = 'minute'; restart = $null }
    )
}

# Should an existing task's action be moved to its 2.3 spec? 'migrate' | 'current' (already there) |
# 'foreign' (the task runs another ForgeHome: e.g. a scratch dry run on a machine with a live fleet; never touch it).
function Test-TaskMigration($Spec, [string]$Execute, [string]$Arguments, [string]$ForgeHome) {
    $line = "$Execute $Arguments"
    if ($line.IndexOf($ForgeHome.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) -lt 0) { return 'foreign' }
    if ($Execute -eq $Spec.command -and $Arguments -eq $Spec.arguments) { return 'current' }
    $marker = if ($Spec.name -eq 'ForgeWatchdog') { 'bin\wd-launcher.ps1' } elseif ($Spec.name -like 'Forge*Agent') { 'bin\agent-loop.cmd' } else { $null }
    if (-not $marker -or $Arguments.IndexOf($marker, [StringComparison]::OrdinalIgnoreCase) -ge 0) { return 'current' }
    'migrate'
}

# ---- 2.3 R1: a hub that answers 503 ----
# Low disk alone (db_write ok) -> leave it running: a restart frees nothing. A failed DB write probe (SQLite I/O error,
# disk full, a wedged connection or store lock) -> restart, at most MaxPerHour times per rolling hour.
# Restarts = epochs of earlier DB-probe restarts (State.db_restarts). Returns @{action = restart|limit|skip; restarts; reason}.
function Get-UnhealthyHubAction {
    param($Body = $null, [object[]]$Restarts = @(), [double]$Now = 0, [int]$MaxPerHour = 3)
    $recent = @(@($Restarts) | Where-Object { $null -ne $_ -and "$_" -ne '' -and ($Now - [double]$_) -lt 3600 } | ForEach-Object { [double]$_ })
    $chk = if ($Body) { $Body.checks } else { $null }
    if (-not ($chk -and $chk.db_write -eq $false)) {
        return @{ action = 'skip'; restarts = $recent; reason = "hub reports unhealthy (db_write=$(if ($chk) { $chk.db_write }), disk=$(if ($chk) { $chk.disk }), $(if ($Body) { $Body.free_gb }) GB free); not restarting it" }
    }
    if ($recent.Count -ge $MaxPerHour) {
        return @{ action = 'limit'; restarts = $recent; reason = "database write probe failing; already restarted $($recent.Count) times in the last hour, not again" }
    }
    @{ action = 'restart'; restarts = @($recent + @($Now)); reason = 'database write probe failed twice' }
}

# ---- 2.3 R1: keep the update lease while the release is still on probation ----
# The leader's lease TTL is 10 min, but a probation can stay pending (agent draining) for drain_timeout_s + 180 s.
# Renew (re-POST for the same node + version; the leader refreshes `expires`) while pending, every EverySec.
function Test-LeaseRenewDue {
    param($Lease, [string]$Version, [bool]$Pending, [double]$Now, [int]$EverySec = 120)
    if (-not $Pending -or -not $Lease -or -not $Lease.url -or [string]$Lease.version -ne $Version) { return $false }
    $last = if ($Lease.renewed) { [double]$Lease.renewed } elseif ($Lease.at) { [double]$Lease.at } else { 0 }
    ($Now - $last) -ge $EverySec
}

# ---- 2.3 R1: /healthz result. up = the hub answered (200 or a 503 health body); ok = 200 ----
function ConvertFrom-HealthzReply([int]$Code, [string]$Text) {
    $body = $null; if ($Text) { try { $body = $Text | ConvertFrom-Json } catch { $body = $null } }
    $up = ($Code -eq 200) -or ($Code -eq 503 -and $body -and $null -ne $body.checks)
    @{ up = [bool]$up; ok = ($Code -eq 200); code = $Code; body = $body }
}

if ($NoRun) { return }

# 2.3 (CONTRACT-phase3.md section 6.4): signed bundles + secrets.json (workstream S). Missing -> 2.2 behaviour.
$VerifyBundleScript = Join-Path $PSScriptRoot 'verify-bundle.ps1'
$HasVerifyBundle = Test-Path -LiteralPath $VerifyBundleScript
if ($HasVerifyBundle) { . $VerifyBundleScript }

# ======================= runtime =======================
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$DataDir   = Join-Path $ForgeHome 'data'
$LogPath   = Join-Path $DataDir 'watchdog.log'
$StatePath = Join-Path $DataDir 'watchdog-state.json'
$Local     = "http://127.0.0.1:$Port"
$script:Actions = New-Object System.Collections.Generic.List[string]

function Write-WdLog([string]$m) {
    $line = "[$(Get-Date -Format s)] $m"
    if ($DryRun) { Write-Host $line; return }
    try {
        if ((Test-Path -LiteralPath $LogPath) -and (Get-Item -LiteralPath $LogPath).Length -gt 5MB) { Move-Item -LiteralPath $LogPath "$LogPath.old" -Force }
        [IO.File]::AppendAllText($LogPath, "$line`r`n", (New-Object Text.UTF8Encoding($false)))
    } catch {}
}

# Every side effect goes through here so -DryRun can print it instead.
function Invoke-Action([string]$What, [scriptblock]$Do) {
    $script:Actions.Add($What)
    if ($DryRun) { Write-WdLog "[dry-run] $What"; return $true }
    Write-WdLog "ACTION $What"
    try { & $Do; $true } catch { Write-WdLog "  failed: $($_.Exception.Message)"; $false }
}

function Get-Healthz {
    try {
        $r = Invoke-WebRequest -Uri "$Local/healthz" -UseBasicParsing -TimeoutSec 4
        return (ConvertFrom-HealthzReply ([int]$r.StatusCode) ([string]$r.Content))
    } catch {
        $code = 0; try { $code = [int]$_.Exception.Response.StatusCode } catch {}
        $txt = ''; try { $txt = [string]$_.ErrorDetails.Message } catch {}
        return (ConvertFrom-HealthzReply $code $txt)
    }
}

function Test-Healthz { (Get-Healthz).ok }

function Get-ClusterStatus([string]$Base, [string]$Token, [int]$Timeout = 3) {
    try { Invoke-RestMethod -Uri "$($Base.TrimEnd('/'))/cluster/status" -Headers @{ 'X-Cluster-Token' = $Token } -TimeoutSec $Timeout -UseBasicParsing }
    catch { if ($DryRun) { Write-WdLog "status $Base failed: $($_.Exception.Message)" }; $null }
}

function Stop-PortListeners([int]$P) {
    $pids = @(Get-NetTCPConnection -LocalPort $P -State Listen -ErrorAction SilentlyContinue | ForEach-Object { $_.OwningProcess } | Sort-Object -Unique | Where-Object { $_ -gt 4 })
    foreach ($id in $pids) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
    # Also kill hub processes that are NOT listening: a hub whose accept loop died keeps running and keeps
    # hub.log locked, so every new run-hub.cmd exits instantly (seen on atlas, Python 3.14 WinError 64).
    $zombies = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $c = "$($_.CommandLine)"; ($c -match 'uvicorn\s+app:app' -or $c -match 'hub\\serve\.py') -and $c -match '\\Forge\\(current|releases)\\' })
    foreach ($z in $zombies) {
        if ($pids -notcontains [int]$z.ProcessId) { Write-WdLog "killing stale hub process $($z.ProcessId)"; Stop-Process -Id $z.ProcessId -Force -ErrorAction SilentlyContinue }
    }
}

function Restart-Hub([string]$Why) {
    Invoke-Action "restart hub ($Why)" {
        Stop-ScheduledTask -TaskName 'ForgeHub' -ErrorAction SilentlyContinue
        Stop-PortListeners $Port
        Start-Sleep -Seconds 1
        Start-ScheduledTask -TaskName 'ForgeHub' -ErrorAction Stop
    } | Out-Null
    $script:State.last_hub_restart = [double][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
}

function Restart-Agent([string]$Why) {
    Invoke-Action "restart agent ($Why)" {
        Stop-ScheduledTask -TaskName 'ForgeAgent' -ErrorAction SilentlyContinue
        Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
            Where-Object { (Get-AgentLaneFromCmd $_.CommandLine) -eq 'user' } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 1
        Start-ScheduledTask -TaskName 'ForgeAgent' -ErrorAction Stop
    } | Out-Null
    $script:State.last_agent_restart = [double][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
}

function Restart-SystemAgent([string]$Why) {
    Invoke-Action "restart system agent ($Why)" {
        Stop-ScheduledTask -TaskName 'ForgeSystemAgent' -ErrorAction SilentlyContinue
        Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
            Where-Object { (Get-AgentLaneFromCmd $_.CommandLine) -eq 'system' } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 1
        Start-ScheduledTask -TaskName 'ForgeSystemAgent' -ErrorAction Stop
    } | Out-Null
    $script:State.last_system_agent_restart = [double][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
}

# Same settings pattern as the installer's long-running tasks (ForgeAgent), but as SYSTEM.
function Register-SystemAgentTask {
    $a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (Get-SystemAgentTaskArgs $ForgeHome) -WorkingDirectory $DataDir
    $boot = New-ScheduledTaskTrigger -AtStartup; $boot.Delay = 'PT15S'
    $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -StartWhenAvailable
    $p = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName 'ForgeSystemAgent' -Action $a -Trigger @($boot) -Settings $s -Principal $p -Force -ErrorAction Stop | Out-Null
}

# Rollout without prompts: machines already on v2 get the SYSTEM lane from a bundle update alone.
function Invoke-SystemAgentCheck([datetime]$Now, [double]$NowEpoch) {
    $task = Get-ScheduledTask -TaskName 'ForgeSystemAgent' -ErrorAction SilentlyContinue
    if (-not $task) {
        $ok = Invoke-Action 'register ForgeSystemAgent (SYSTEM, at startup, restart on failure)' { Register-SystemAgentTask }
        if ($ok) { Invoke-Action 'start ForgeSystemAgent' { Start-ScheduledTask -TaskName 'ForgeSystemAgent' -ErrorAction Stop } | Out-Null
                   $script:State.last_system_agent_restart = $NowEpoch }
        return $false
    }
    $grace = ($script:State.last_system_agent_restart -and (($NowEpoch - [double]$script:State.last_system_agent_restart) -lt 150))
    if ("$($task.State)" -ne 'Running') { Restart-SystemAgent "task state $($task.State)"; return $false }
    if (-not $grace -and (Test-StaleFile (Join-Path $DataDir 'agent-system.log') $Now 120)) {
        $info = Get-ScheduledTaskInfo -TaskName 'ForgeSystemAgent' -ErrorAction SilentlyContinue
        if (-not $info -or ($Now - $info.LastRunTime).TotalSeconds -gt 120) { Restart-SystemAgent 'agent-system.log stale > 2 min'; return $false }
    }
    $true
}

# Delete <work_root>\jobs\<id> dirs untouched for 14 days (at most once per hour). Jobs listed in the agents'
# running.json / running-system.json are kept. rmdir /s never follows junctions/symlinks a job left inside its dir.
function Invoke-JobDirCleanup([double]$NowEpoch) {
    if (-not (Test-CleanupDue $script:State.last_job_cleanup $NowEpoch)) { return }
    $script:State.last_job_cleanup = $NowEpoch
    $skip = @()
    foreach ($f in 'running.json', 'running-system.json') {
        $r = Read-JsonFile (Join-Path $DataDir $f)
        if ($r -and $r.jobs) { $skip += @($r.jobs | Where-Object { $_ -and $_.id } | ForEach-Object { [string]$_.id }) }
    }
    $markers = @('out.log', 'err.log', 'prompt.txt', 'task.ps1', 'spec.json')
    foreach ($root in (Get-JobCleanupRoots (Read-JsonFile (Join-Path $DataDir 'agent-config.json')))) {
        $jobs = Join-Path $root 'jobs'
        if (-not (Test-Path -LiteralPath $jobs -PathType Container)) { continue }
        if (((Get-Item -LiteralPath $jobs -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { Write-WdLog "job cleanup: $jobs is a junction/symlink; skipped"; continue }
        $dirs = @(foreach ($d in @(Get-ChildItem -LiteralPath $jobs -Directory -Force -ErrorAction SilentlyContinue)) {
            $reparse = (($d.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
            $newest = $d.LastWriteTimeUtc; $marker = $false
            if (-not $reparse) {
                foreach ($f in @(Get-ChildItem -LiteralPath $d.FullName -File -Force -ErrorAction SilentlyContinue)) {
                    if ($f.LastWriteTimeUtc -gt $newest) { $newest = $f.LastWriteTimeUtc }
                    if ($markers -contains $f.Name.ToLowerInvariant()) { $marker = $true }
                }
            }
            @{ name = $d.Name; path = $d.FullName; reparse = $reparse; marker = $marker
               mtime = [double]([DateTimeOffset][DateTime]::SpecifyKind($newest, [DateTimeKind]::Utc)).ToUnixTimeSeconds() }
        })
        $stale = Select-StaleJobDirs -Dirs $dirs -Now $NowEpoch -MaxAgeDays 14 -Skip $skip
        if ($stale.Count -gt 500) { $stale = $stale[0..499] }
        if (-not $stale.Count) { continue }
        Invoke-Action "remove $($stale.Count) job dir(s) older than 14 days under $jobs" {
            foreach ($p in $stale) { if (-not (Remove-JobDirTree $p)) { Write-WdLog "  could not remove $p" } }
        } | Out-Null
    }
}

function Invoke-Native([string]$Exe, [string]$Arguments, [int]$TimeoutSec = 900) {
    $o = [IO.Path]::GetTempFileName(); $e = [IO.Path]::GetTempFileName()
    try {
        $p = Start-Process -FilePath $Exe -ArgumentList $Arguments -NoNewWindow -PassThru -RedirectStandardOutput $o -RedirectStandardError $e
        if (-not $p.WaitForExit($TimeoutSec * 1000)) { try { $p.Kill() } catch {}; throw "$Exe timed out" }
        $txt = ((Get-Content $o -Raw) + (Get-Content $e -Raw))
        @{ code = $p.ExitCode; out = "$txt".Trim() }
    } finally { Remove-Item $o, $e -Force -ErrorAction SilentlyContinue }
}

function Get-RequirementsSha([string]$ReleaseDir) {
    $f = Join-Path $ReleaseDir 'requirements.txt'
    if (Test-Path -LiteralPath $f) { Get-FileSha256 $f } else { '' }
}

function Install-Requirements([string]$ReleaseDir) {
    $want = Get-RequirementsSha $ReleaseDir
    $shaFile = Join-Path $DataDir 'requirements.sha256'
    $have = if (Test-Path -LiteralPath $shaFile) { (Get-Content -LiteralPath $shaFile -Raw).Trim() } else { '' }
    if (-not $want -or $want -eq $have) { return $true }
    $py = Join-Path $ForgeHome 'venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $py)) { Write-WdLog "venv missing at $py; cannot pip install"; return $false }
    $script:pipOk = $false
    Invoke-Action "pip install -r $ReleaseDir\requirements.txt" {
        Write-WdHeartbeat "pip install into the base venv"
        $r = Invoke-Native $py (Get-PipArgs $ReleaseDir) 420
        if ($r.out) { Write-WdLog "  pip: $($r.out)" }
        if ($r.code -ne 0) { throw "pip exited $($r.code)" }
        [IO.File]::WriteAllText($shaFile, $want, (New-Object Text.UTF8Encoding($false)))
        $script:pipOk = $true
    } | Out-Null
    if ($DryRun) { return $true }
    [bool]$script:pipOk
}

# 2.3 R5: a long venv build must not look like a hung watchdog to wd-launcher.ps1 (it runs the previous release's
# watchdog once watchdog.json last_run is > 10 min old): refresh last_run (and say what is running) before each step.
function Write-WdHeartbeat([string]$What) {
    if ($DryRun) { return }
    try {
        $f = Join-Path $DataDir 'watchdog.json'
        $j = ConvertTo-Hashtable (Read-JsonFile $f)
        $j.last_run = (Get-Date).ToString('o'); $j.busy = $What; $j.busy_since = (Get-Date).ToString('o')
        Write-JsonFile $f $j
    } catch {}
}

function Get-PipArgs([string]$ReleaseDir) {
    $cache = Join-Path $DataDir 'tmp\pip-cache'          # resumable: a retry reuses what was already downloaded
    "-m pip install --disable-pip-version-check -q --timeout 60 --retries 3 --cache-dir `"$cache`" -r `"$(Join-Path $ReleaseDir 'requirements.txt')`""
}

# 2.3 R5: make <ReleaseDir> runnable. Its requirements equal the base venv's -> nothing (no pip). Otherwise build
# <ReleaseDir>\venv (python -m venv from the base interpreter + pip install) before the flip; run-hub.cmd prefers it.
function Install-ReleaseRequirements([string]$ReleaseDir) {
    $want = Get-RequirementsSha $ReleaseDir
    $shaFile = Join-Path $DataDir 'requirements.sha256'
    $base = if (Test-Path -LiteralPath $shaFile) { (Get-Content -LiteralPath $shaFile -Raw).Trim() } else { '' }
    $venv = Join-Path $ReleaseDir 'venv'; $ownFile = Join-Path $venv 'forge-requirements.sha256'
    $own = if (Test-Path -LiteralPath $ownFile) { (Get-Content -LiteralPath $ownFile -Raw).Trim() } else { '' }
    $plan = Get-VenvPlan $want $base $own
    if ($plan -eq 'none') { Write-WdLog "requirements of $(Split-Path -Leaf $ReleaseDir) unchanged; no pip"; return $true }
    if ($plan -eq 'keep') { Write-WdLog "release venv $venv already matches its requirements"; return $true }
    $py = Get-BasePython $ForgeHome
    if (-not (Test-Path -LiteralPath $py)) { Write-WdLog "no python at $py; cannot build $venv"; return $false }
    $script:venvOk = $false
    Invoke-Action "create $venv (requirements changed) + pip install" {
        if (Test-Path -LiteralPath $venv) { Remove-Item -LiteralPath $venv -Recurse -Force }
        Write-WdHeartbeat "python -m venv $venv"
        $r = Invoke-Native $py "-m venv `"$venv`"" 180
        if ($r.code -ne 0) { throw "venv exited $($r.code): $($r.out)" }
        Write-WdHeartbeat "pip install for $(Split-Path -Leaf $ReleaseDir)"
        # bounded so the whole run stays inside the task's 15 min limit and the launcher's 10 min staleness rule
        $r = Invoke-Native (Join-Path $venv 'Scripts\python.exe') (Get-PipArgs $ReleaseDir) 420
        if ($r.out) { Write-WdLog "  pip: $($r.out)" }
        if ($r.code -ne 0) { throw "pip exited $($r.code)" }
        [IO.File]::WriteAllText($ownFile, $want, (New-Object Text.UTF8Encoding($false)))
        $script:venvOk = $true
    } | Out-Null
    if ($DryRun) { return $true }
    if (-not $script:venvOk -and (Test-Path -LiteralPath $venv)) { Remove-Item -LiteralPath $venv -Recurse -Force -ErrorAction SilentlyContinue }
    [bool]$script:venvOk
}

# After a release with its own venv is confirmed: bring the base venv up to it, so the next release with the same
# requirements needs no venv of its own.
function Invoke-VenvPromotion([string]$ReleaseDir) {
    if (-not (Test-Path -LiteralPath (Join-Path $ReleaseDir 'venv\forge-requirements.sha256'))) { return }
    if (-not (Install-Requirements $ReleaseDir)) { Write-WdLog "could not update the base venv to $(Split-Path -Leaf $ReleaseDir)'s requirements (the release keeps its own venv)" }
}

# 2.3 R2/R7, once per node: <FH>\bin launcher files, then the task actions (only tasks that run THIS ForgeHome).
function Invoke-LauncherMigration {
    $copied = Install-WdLauncher -ForgeHome $ForgeHome -DryRun:$DryRun
    foreach ($f in $copied) { $script:Actions.Add("install bin\$f"); Write-WdLog "$(if ($DryRun) { '[dry-run] ' } else { 'ACTION ' })install bin\$f" }
    $bin = Join-Path $ForgeHome 'bin'
    if (-not $DryRun -and -not ((Test-Path -LiteralPath (Join-Path $bin 'wd-launcher.ps1')) -and (Test-Path -LiteralPath (Join-Path $bin 'agent-loop.cmd')))) { return }
    foreach ($spec in (Get-ForgeTaskSpecs -ForgeHome $ForgeHome)) {
        if ($spec.name -eq 'ForgeHub') { continue }
        $task = Get-ScheduledTask -TaskName $spec.name -ErrorAction SilentlyContinue
        if (-not $task -or -not $task.Actions) { continue }
        $act = @($task.Actions)[0]
        $m = Test-TaskMigration $spec ([string]$act.Execute) ([string]$act.Arguments) $ForgeHome
        if ($m -ne 'migrate') { continue }
        $target = if ($spec.name -eq 'ForgeWatchdog') { 'bin\wd-launcher.ps1' } else { 'bin\agent-loop.cmd' }
        $user = [string]$task.Principal.UserId
        Invoke-Action "migrate $($spec.name) -> $target" { Register-ForgeTaskSpec $spec $user } | Out-Null
    }
}

function Register-ForgeTaskSpec($Spec, [string]$User) {
    $a = New-ScheduledTaskAction -Execute $Spec.command -Argument $Spec.arguments -WorkingDirectory $Spec.dir
    $boot = New-ScheduledTaskTrigger -AtStartup; $boot.Delay = 'PT15S'
    if ($Spec.trigger -eq 'minute') {
        $rep = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
        $triggers = @($boot, $rep)
        $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -MultipleInstances IgnoreNew -StartWhenAvailable
    } else {
        $triggers = @($boot)
        $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -RestartCount $Spec.restart.count -RestartInterval (New-TimeSpan -Minutes $Spec.restart.interval_min) -MultipleInstances IgnoreNew -StartWhenAvailable
    }
    if ($Spec.user -eq 'system') { $p = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\SYSTEM' -LogonType ServiceAccount -RunLevel Highest }
    else { $p = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Limited }
    Register-ScheduledTask -TaskName $Spec.name -Action $a -Trigger $triggers -Settings $s -Principal $p -Force -ErrorAction Stop | Out-Null
}

function Get-PeerStatuses($Cfg, $LocalStatus) {
    # peer list from the local hub (bootstrap is agent-token authed and forwarded to the leader);
    # cached so updates still work while the local hub is down.
    $peers = $null
    try {
        $b = Invoke-RestMethod -Uri "$Local/cluster/bootstrap?t=$([uri]::EscapeDataString([string]$Cfg.agent_token))" -TimeoutSec 5 -UseBasicParsing
        if ($b -and $b.peers) { $peers = @($b.peers); $script:State.peers_cache = $peers }
    } catch {}
    if (-not $peers -and $script:State.peers_cache) { $peers = @($script:State.peers_cache) }
    $out = @()
    # the local hub's status.version is the latest AVAILABLE bundle (e.g. published here, not yet active)
    # role + bad_versions are used by the 2.2 update lease / fleet-bad logic
    if ($LocalStatus -and $LocalStatus.version) {
        $out += [pscustomobject]@{ id = "$($Cfg.node_id) (local)"; url = $Local; version = [string]$LocalStatus.version; bundle_sha256 = [string]$LocalStatus.bundle_sha256
                                   role = [string]$LocalStatus.role; bad_versions = @($LocalStatus.bad_versions | Where-Object { $_ }) }
    }
    foreach ($p in @($peers)) {
        if (-not $p -or $p.id -eq $Cfg.node_id) { continue }
        foreach ($u in @($p.urls)) {
            if (-not $u) { continue }
            $s = Get-ClusterStatus ([string]$u) $Cfg.cluster_token
            if ($s) { $out += [pscustomobject]@{ id = $p.id; url = ([string]$u).TrimEnd('/'); version = [string]$s.version; bundle_sha256 = [string]$s.bundle_sha256
                                                 role = [string]$s.role; bad_versions = @($s.bad_versions | Where-Object { $_ }) }; break }
        }
    }
    , $out
}

# Returns 'ok' (switched), 'fail' (backoff applies) or 'wait' (release staged, waiting for the update lease).
function Invoke-Update($Cand, [string]$CurVer, $Cfg, [object[]]$Peers = @(), [double]$NowEpoch = 0, $LocalStatus = $null) {
    $ver = [string]$Cand.version
    $dl = Join-Path $DataDir 'tmp'
    $zip = Join-Path $dl "forge-$ver.zip"
    if (Test-LocalRelease -ForgeHome $ForgeHome -Version $ver -ExpectedSha $Cand.bundle_sha256) {
        $ok = $true
        Write-WdLog "release $ver present locally with matching sha256 (published here or staged earlier)"
        $script:Actions.Add("activate local release $ver")
    } else {
      $ok = Invoke-Action "update $CurVer -> $ver from $($Cand.id) ($($Cand.url))" {
        New-Item -ItemType Directory -Force $dl | Out-Null
        Invoke-WebRequest -Uri "$($Cand.url)/cluster/bundle?version=$([uri]::EscapeDataString($ver))" -Headers @{ 'X-Cluster-Token' = [string]$Cfg.cluster_token } -OutFile $zip -UseBasicParsing -TimeoutSec 300
        $installed = Install-ReleaseZip -ForgeHome $ForgeHome -ZipPath $zip -ExpectedSha $Cand.bundle_sha256 -ExpectedVersion $ver
        Write-WdLog "  extracted release $installed (sha256 verified)"
      }
      Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
    }
    if (-not $ok) { return 'fail' }
    # ---- 2.3 S6: never activate a release whose signature does not verify against the pinned key ----
    if ($HasVerifyBundle) {
        $sig = Test-BundleSignature -ReleaseDir (Join-Path $ForgeHome "releases\$ver") -PubKeyXml (Get-SigningPubKey $DataDir)
        if (-not $sig.ok) { Write-WdLog "release $ver refused: $($sig.reason)"; $script:Actions.Add("refused unsigned/bad release $ver"); $script:State.signature_refused = $ver; return 'fail' }
    }

    # ---- 2.2: activation needs the fleet update lease (canary first, leader last; SPEC-phase2 A) ----
    # the leader seen in the statuses, else the local hub (a 2.2 standby proxies /cluster/update-lease to the leader; 503 = no leader)
    $leader = Find-LeaderUrl $Peers; if (-not $leader) { $leader = $Local }
    if ($DryRun) { Write-WdLog "[dry-run] request update lease for $ver from $(if ($leader) { $leader } else { 'no reachable leader' })"; $lr = @{ kind = 'granted' } }
    elseif ($leader) { $lr = Request-UpdateLease $leader ([string]$Cfg.cluster_token) (Get-WdNodeId $Cfg) $ver }
    else { $lr = @{ kind = 'unreachable' } }
    $ld = Step-LeaseState -Result $lr -State $script:State -Version $ver -Leader $leader -Now $NowEpoch -Record (-not $DryRun)
    if ($ld.action -eq 'wait') { Write-WdLog "$($ld.reason); release $ver is staged"; return 'wait' }
    Write-WdLog "update to ${ver}: $($ld.reason)"

    $relDir = Join-Path $ForgeHome "releases\$ver"
    if (-not (Install-ReleaseRequirements $relDir)) { Write-WdLog "requirements for $ver failed; not switching"; Clear-UpdateLease $Cfg $null; return 'fail' }
    $flipped = Invoke-Action "switch current -> releases\$ver" { Set-CurrentRelease $ForgeHome $ver }
    if (-not $flipped) { Clear-UpdateLease $Cfg $null; return 'fail' }
    # 2.3 R1: the release must also bring the local agent onto it, if that agent was reporting before the flip
    $agentReq = [bool]($LocalStatus -and $LocalStatus.local_agent -and $LocalStatus.local_agent.online)
    $script:State.probation = @{ from = $CurVer; to = $ver; fails = 0; since = (Get-Date).ToString('o'); agent_required = $agentReq }
    $script:State.active_version = $ver
    Restart-Hub "updated to $ver"
    'ok'
}

function Invoke-Watchdog {
    New-Item -ItemType Directory -Force $DataDir | Out-Null
    $lockPath = Join-Path $DataDir 'watchdog.lock'
    $lock = $null
    try { $lock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch { return }   # another run is in progress
    try {
        $now = Get-Date
        $nowEpoch = [double][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        $script:State = ConvertTo-Hashtable (Read-JsonFile $StatePath)
        $cfg = Read-JsonFile (Join-Path $DataDir 'config.json')
        if (-not $cfg) { Write-WdLog "no data\config.json yet; nothing to do"; return }
        if ($HasVerifyBundle) { $cfg = Merge-WdSecrets $cfg $DataDir }   # 2.3 S2: cluster_token etc. from data\private\secrets.json
        $bad = Get-BadVersions $DataDir
        try { Invoke-LauncherMigration } catch { Write-WdLog "launcher migration failed: $($_.Exception.Message)" }
        $curVer = Get-CurrentVersion $ForgeHome
        if (-not $curVer) { Write-WdLog "no current release under $ForgeHome; nothing to do"; return }
        $prob = $script:State.probation
        $justSwitched = $false

        # current was switched by someone else (POST /cluster/publish, installer) -> probation + hub restart
        if (-not $script:State.active_version) { $script:State.active_version = $curVer }
        if ($script:State.active_version -ne $curVer -and (-not $prob -or $prob.to -ne $curVer)) {
            Write-WdLog "current switched externally: $($script:State.active_version) -> $curVer"
            $script:State.probation = @{ from = $script:State.active_version; to = $curVer; fails = 0; since = $now.ToString('o') }
            $script:State.active_version = $curVer
            $prob = $script:State.probation
            $justSwitched = $true
        }

        # ---- 1. hub health ----
        $grace = $false
        if ($script:State.last_hub_restart) { $grace = (($nowEpoch - [double]$script:State.last_hub_restart) -lt 45) }
        $hz = Get-Healthz
        if (-not $hz.up -and -not $grace) { Start-Sleep -Seconds 5; $hz = Get-Healthz }
        elseif ($hz.up -and -not $hz.ok -and $hz.body -and $hz.body.checks -and $hz.body.checks.db_write -eq $false -and -not $grace) {
            Start-Sleep -Seconds 5; $hz = Get-Healthz      # a failed DB write probe must fail twice before a restart
        }
        $hubUp = [bool]$hz.up; $hubOk = [bool]$hz.ok
        $running = $null; $st = $null
        if ($hubUp) {
            $st = Get-ClusterStatus $Local $cfg.cluster_token
            # status.version = latest AVAILABLE bundle; running_version = what the process runs
            if ($st) { $running = if ($st.running_version) { [string]$st.running_version } else { [string]$st.version } }
        }

        # 2.3 R1 health gate: healthz (DB write + disk) + eligible + local agent on the new version within release_health_s
        $health = @{ ok = ($hubOk -and (-not $running -or $running -eq $curVer)); pending = $false; reason = '' }
        if ($prob -and $prob.to -eq $curVer) {
            if ($null -eq $prob.agent_required) {      # switched externally: required when the agent reports right now
                $req = [bool]($st -and $st.local_agent -and $st.local_agent.online)
                if ($prob -is [hashtable]) { $prob.agent_required = $req } else { $prob | Add-Member -NotePropertyName agent_required -NotePropertyValue $req -Force }
            }
            $hs = if ($cfg.release_health_s) { [int]$cfg.release_health_s } else { 180 }
            $ds = if ($cfg.drain_timeout_s) { [int]$cfg.drain_timeout_s } else { 1800 }
            $health = Get-ReleaseHealth -HubUp $hubUp -HubOk $hubOk -Body $hz.body -Status $st -Version $curVer -ElapsedSec (Get-ElapsedSec $prob.since $nowEpoch) `
                -HealthSec $hs -AgentRequired ([bool]$prob.agent_required) -DrainSec $ds
            if (-not $health.ok) { Write-WdLog "release $curVer on probation: $($health.reason)" }
            $l = $script:State.lease
            if (-not $DryRun -and (Test-LeaseRenewDue -Lease $l -Version $curVer -Pending ([bool]$health.pending) -Now $nowEpoch)) {
                $rr = Request-UpdateLease ([string]$l.url) ([string]$cfg.cluster_token) (Get-WdNodeId $cfg) $curVer
                $script:State.lease = @{ version = [string]$l.version; url = [string]$l.url; at = $l.at; renewed = $nowEpoch }
                if ($rr.kind -ne 'granted') { Write-WdLog "could not renew the update lease for ${curVer}: $($rr.kind) $($rr.holder)$($rr.msg)" }
            }
        }
        $dec = Get-RollbackDecision -Probation $prob -CurrentVersion $curVer -HealthOk $health.ok -InGrace ($grace -or $justSwitched -or $health.pending)
        switch ($dec.action) {
            'confirm' { Write-WdLog "release $curVer confirmed healthy"; $script:State.probation = $null; try { Clear-UpdateLease $cfg $null } catch {}
                        try { Invoke-VenvPromotion (Join-Path $ForgeHome "releases\$curVer") } catch { Write-WdLog "venv promotion failed: $($_.Exception.Message)" } }
            'wait'    { $script:State.probation.fails = $dec.fails; Write-WdLog "release $curVer unhealthy after update ($($dec.fails)/3): $($health.reason)" }
            'rollback' {
                $from = [string]$prob.from
                Invoke-Action "mark $curVer bad" {
                    [IO.File]::AppendAllText((Join-Path $DataDir 'bad-versions.txt'), "$curVer`r`n", (New-Object Text.UTF8Encoding($false)))
                } | Out-Null
                $bad += $curVer
                if ($from -and (Test-Path -LiteralPath (Join-Path $ForgeHome "releases\$from\VERSION"))) {
                    if (Invoke-Action "rollback current $curVer -> $from" { Set-CurrentRelease $ForgeHome $from }) {
                        $script:State.active_version = $from
                        $curVer = $from
                        # 2.3 S-R7: a pre-2.3 hub reads its tokens from config.json and its DB from data\forge.db
                        if ($HasVerifyBundle -and (Compare-SemVer $from '2.3.0') -lt 0) {
                            Invoke-Action "restore the pre-2.3 data layout for $from (stop hub, tokens + forge.db back)" {
                                Stop-ScheduledTask -TaskName 'ForgeHub' -ErrorAction SilentlyContinue
                                Stop-PortListeners $Port
                                foreach ($a in (Restore-Pre23Layout $DataDir)) { Write-WdLog "  $a" }
                            } | Out-Null
                        }
                        Install-ReleaseRequirements (Join-Path $ForgeHome "releases\$from") | Out-Null
                    }
                } else { Write-WdLog "cannot roll back: previous release '$from' not present" }
                # 2.2: hand the lease back and tell the leader this version is bad (it marks it fleet-bad)
                try { Clear-UpdateLease $cfg ([string]$prob.to) } catch {}
                $script:State.probation = $null
                Restart-Hub 'rollback'
                $grace = $true
            }
        }

        if (-not $grace -and $dec.action -ne 'rollback') {
            if (-not $hubUp) { Restart-Hub 'healthz failed twice' ; $grace = $true }
            elseif (-not $hubOk) {
                $ua = Get-UnhealthyHubAction -Body $hz.body -Restarts @($script:State.db_restarts) -Now $nowEpoch
                $script:State.db_restarts = @($ua.restarts)
                if ($ua.action -eq 'restart') { Restart-Hub $ua.reason; $grace = $true } else { Write-WdLog $ua.reason }
            }
            elseif ($running -and $running -ne $curVer) { Restart-Hub "running $running, current is $curVer"; $grace = $true }
        }

        # ---- 1b. agent health ----
        $agentOk = $true
        $task = Get-ScheduledTask -TaskName 'ForgeAgent' -ErrorAction SilentlyContinue
        $agentGrace = ($script:State.last_agent_restart -and (($nowEpoch - [double]$script:State.last_agent_restart) -lt 150))
        if (-not $task) { Write-WdLog 'task ForgeAgent is not registered'; $agentOk = $false }
        elseif ("$($task.State)" -ne 'Running') { $agentOk = $false; Restart-Agent "task state $($task.State)" }
        elseif (-not $agentGrace -and (Test-StaleFile (Join-Path $DataDir 'agent.log') $now 120)) {
            $info = Get-ScheduledTaskInfo -TaskName 'ForgeAgent' -ErrorAction SilentlyContinue
            if (-not $info -or ($now - $info.LastRunTime).TotalSeconds -gt 120) { $agentOk = $false; Restart-Agent 'agent.log stale > 2 min' }
        }

        # ---- 1c. SYSTEM-lane agent (ForgeSystemAgent): register if missing, keep it running ----
        $sysAgentOk = $false
        try { $sysAgentOk = Invoke-SystemAgentCheck $now $nowEpoch } catch { Write-WdLog "system agent check failed: $($_.Exception.Message)" }

        # ---- 2. update from peers ----
        $script:State.peers_seen = @(); $fleetBad = @()
        $probNow = $script:State.probation
        if (-not $probNow -and $dec.action -ne 'rollback') {
            $peers = Get-PeerStatuses $cfg $st
            $script:State.peers_seen = @($peers | ForEach-Object { @{ id = $_.id; version = $_.version; role = $_.role } })
            # 2.2: fleet-bad versions from the leader's status are never installed (like bad-versions.txt)
            $fleetBad = Get-FleetBadVersions $peers
            $cand = Select-UpdateCandidate -Peers $peers -Current $curVer -Bad (@($bad) + @($fleetBad))
            $backoff = $false
            if ($cand -and $script:State.update_fail -and $script:State.update_fail.version -eq $cand.version) {
                $backoff = (($nowEpoch - [double]$script:State.update_fail.at) -lt 600)
            }
            if (-not $cand) { $script:State.lease_wait = $null; $script:State.lease_no_leader_since = $null }
            if ($cand -and -not $backoff) {
                $res = Invoke-Update $cand $curVer $cfg $peers $nowEpoch $st
                if ($res -eq 'fail') { $script:State.update_fail = @{ version = $cand.version; at = $nowEpoch } }
                elseif ($res -eq 'ok') { $script:State.update_fail = $null; $curVer = [string]$cand.version }
            }
        }

        # ---- 4. prune releases ----
        $protect = @(); if ($script:State.probation) { $protect = @([string]$script:State.probation.from) }
        $drop = Select-ReleasesToPrune -Versions (Get-ReleaseVersions $ForgeHome) -Current (Get-CurrentVersion $ForgeHome) -Keep 3 -Protect $protect
        foreach ($v in $drop) {
            $d = Join-Path $ForgeHome "releases\$v"
            Invoke-Action "prune release $v" { Remove-Item -LiteralPath $d -Recurse -Force } | Out-Null
        }
        foreach ($s in @(Get-ChildItem -LiteralPath (Join-Path $ForgeHome 'releases') -Directory -Filter '_staging-*' -ErrorAction SilentlyContinue)) {
            if (($now - $s.LastWriteTime).TotalMinutes -gt 30) { Invoke-Action "remove stale $($s.Name)" { Remove-Item -LiteralPath $s.FullName -Recurse -Force } | Out-Null }
        }
        # ---- 4b. old job dirs under <work_root>\jobs (14 days, hourly) ----
        try { Invoke-JobDirCleanup $nowEpoch } catch { Write-WdLog "job dir cleanup failed: $($_.Exception.Message)" }

        # ---- 5. status files ----
        $last = $script:State.last_action
        if ($script:Actions.Count) { $last = @{ at = $now.ToString('o'); text = ($script:Actions -join '; ') } }
        $script:State.last_action = $last
        $summary = [ordered]@{
            last_run = $now.ToString('o'); watchdog_version = $WatchdogVersion; node_id = $cfg.node_id
            version = (Get-CurrentVersion $ForgeHome); hub_ok = [bool]$hubOk; hub_up = [bool]$hubUp; hub_running_version = $running
            release_health = $(if ($health.reason) { $health.reason } else { $null })
            agent_ok = [bool]$agentOk; system_agent_ok = [bool]$sysAgentOk; actions = @($script:Actions); last_action = $last
            probation = $script:State.probation; bad_versions = @($bad); peers = @($script:State.peers_seen)
            fleet_bad_versions = @($fleetBad); update_lease = $script:State.lease; lease_wait = $script:State.lease_wait
            dry_run = [bool]$DryRun
            # 2.3 S6: newest release refused for a missing/bad signature (cleared once current reaches it)
            signature_refused = $(if ($script:State.signature_refused -and (Compare-SemVer ([string]$script:State.signature_refused) ([string](Get-CurrentVersion $ForgeHome))) -gt 0) { [string]$script:State.signature_refused } else { $null })
        }
        if ($DryRun) { Write-Host (ConvertTo-Json -InputObject $summary -Depth 6); return }
        Write-JsonFile (Join-Path $DataDir 'watchdog.json') $summary
        Write-JsonFile $StatePath $script:State
    } catch {
        Write-WdLog "watchdog error: $($_.Exception.Message) @ $($_.InvocationInfo.ScriptLineNumber)"
    } finally {
        if ($lock) { $lock.Close() }
    }
}

Invoke-Watchdog
