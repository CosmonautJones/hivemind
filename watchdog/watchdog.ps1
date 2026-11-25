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

$WatchdogVersion = '2.1.0'
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

if ($NoRun) { return }

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

function Test-Healthz {
    try { $r = Invoke-WebRequest -Uri "$Local/healthz" -UseBasicParsing -TimeoutSec 4; return ($r.StatusCode -eq 200) } catch { $false }
}

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
        $r = Invoke-Native $py "-m pip install --disable-pip-version-check -q -r `"$(Join-Path $ReleaseDir 'requirements.txt')`""
        if ($r.out) { Write-WdLog "  pip: $($r.out)" }
        if ($r.code -ne 0) { throw "pip exited $($r.code)" }
        [IO.File]::WriteAllText($shaFile, $want, (New-Object Text.UTF8Encoding($false)))
        $script:pipOk = $true
    } | Out-Null
    if ($DryRun) { return $true }
    [bool]$script:pipOk
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
    if ($LocalStatus -and $LocalStatus.version) {
        $out += [pscustomobject]@{ id = "$($Cfg.node_id) (local)"; url = $Local; version = [string]$LocalStatus.version; bundle_sha256 = [string]$LocalStatus.bundle_sha256 }
    }
    foreach ($p in @($peers)) {
        if (-not $p -or $p.id -eq $Cfg.node_id) { continue }
        foreach ($u in @($p.urls)) {
            if (-not $u) { continue }
            $s = Get-ClusterStatus ([string]$u) $Cfg.cluster_token
            if ($s) { $out += [pscustomobject]@{ id = $p.id; url = ([string]$u).TrimEnd('/'); version = [string]$s.version; bundle_sha256 = [string]$s.bundle_sha256 }; break }
        }
    }
    , $out
}

function Invoke-Update($Cand, [string]$CurVer, $Cfg) {
    $ver = [string]$Cand.version
    $dl = Join-Path $DataDir 'tmp'
    $zip = Join-Path $dl "forge-$ver.zip"
    if (Test-LocalRelease -ForgeHome $ForgeHome -Version $ver -ExpectedSha $Cand.bundle_sha256) {
        $ok = $true
        Write-WdLog "release $ver already present locally with matching sha256 (published here); activating"
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
    if (-not $ok) { return $false }
    $relDir = Join-Path $ForgeHome "releases\$ver"
    if (-not (Install-Requirements $relDir)) { Write-WdLog "requirements for $ver failed; not switching"; return $false }
    $flipped = Invoke-Action "switch current -> releases\$ver" { Set-CurrentRelease $ForgeHome $ver }
    if (-not $flipped) { return $false }
    $script:State.probation = @{ from = $CurVer; to = $ver; fails = 0; since = (Get-Date).ToString('o') }
    $script:State.active_version = $ver
    Restart-Hub "updated to $ver"
    $true
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
        $bad = Get-BadVersions $DataDir
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
        $hubOk = Test-Healthz
        if (-not $hubOk -and -not $grace) { Start-Sleep -Seconds 5; $hubOk = Test-Healthz }
        $running = $null; $st = $null
        if ($hubOk) {
            $st = Get-ClusterStatus $Local $cfg.cluster_token
            # status.version = latest AVAILABLE bundle; running_version = what the process runs
            if ($st) { $running = if ($st.running_version) { [string]$st.running_version } else { [string]$st.version } }
        }

        $dec = Get-RollbackDecision -Probation $prob -CurrentVersion $curVer -HealthOk ($hubOk -and (-not $running -or $running -eq $curVer)) -InGrace ($grace -or $justSwitched)
        switch ($dec.action) {
            'confirm' { Write-WdLog "release $curVer confirmed healthy"; $script:State.probation = $null }
            'wait'    { $script:State.probation.fails = $dec.fails; Write-WdLog "release $curVer unhealthy after update ($($dec.fails)/3)" }
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
                        Install-Requirements (Join-Path $ForgeHome "releases\$from") | Out-Null
                    }
                } else { Write-WdLog "cannot roll back: previous release '$from' not present" }
                $script:State.probation = $null
                Restart-Hub 'rollback'
                $grace = $true
            }
        }

        if (-not $grace -and $dec.action -ne 'rollback') {
            if (-not $hubOk) { Restart-Hub 'healthz failed twice' ; $grace = $true }
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
        $script:State.peers_seen = @()
        $probNow = $script:State.probation
        if (-not $probNow -and $dec.action -ne 'rollback') {
            $peers = Get-PeerStatuses $cfg $st
            $script:State.peers_seen = @($peers | ForEach-Object { @{ id = $_.id; version = $_.version } })
            $cand = Select-UpdateCandidate -Peers $peers -Current $curVer -Bad $bad
            $backoff = $false
            if ($cand -and $script:State.update_fail -and $script:State.update_fail.version -eq $cand.version) {
                $backoff = (($nowEpoch - [double]$script:State.update_fail.at) -lt 600)
            }
            if ($cand -and -not $backoff) {
                if (-not (Invoke-Update $cand $curVer $cfg)) {
                    $script:State.update_fail = @{ version = $cand.version; at = $nowEpoch }
                } else { $script:State.update_fail = $null; $curVer = [string]$cand.version }
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

        # ---- 5. status files ----
        $last = $script:State.last_action
        if ($script:Actions.Count) { $last = @{ at = $now.ToString('o'); text = ($script:Actions -join '; ') } }
        $script:State.last_action = $last
        $summary = [ordered]@{
            last_run = $now.ToString('o'); watchdog_version = $WatchdogVersion; node_id = $cfg.node_id
            version = (Get-CurrentVersion $ForgeHome); hub_ok = [bool]$hubOk; hub_running_version = $running
            agent_ok = [bool]$agentOk; system_agent_ok = [bool]$sysAgentOk; actions = @($script:Actions); last_action = $last
            probation = $script:State.probation; bad_versions = @($bad); peers = @($script:State.peers_seen)
            dry_run = [bool]$DryRun
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
