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
# Dot-source for tests without side effects:  . .\forge-agent.ps1 -NoRun
param(
    [string]$ConfigPath = (Join-Path $env:ProgramData 'Forge\data\agent-config.json'),
    [ValidateSet('user', 'system')][string]$Lane = 'user',
    [switch]$Child,
    [switch]$NoRun
)

$AgentVersion = '2.1.4'
$LocalHub = 'http://127.0.0.1:8787'

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
function Test-JobLane($Job, [string]$Lane) {
    $sys = ("$($Job.run_as)" -eq 'system') -or (@('action', 'admin') -contains "$($Job.engine)")
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

# Report body for the SYSTEM lane: deliberately minimal (no metrics/procs/logs).
function New-SystemLaneReport {
    param([string]$NodeId, [int]$Capacity, [int]$Running, [bool]$Boot, [string]$AgentVersion, [string]$BundleVersion)
    @{ node_id = $NodeId; lane = 'system'; capacity = $Capacity; running = $Running; boot = $Boot
       agent_version = $AgentVersion; bundle_version = $BundleVersion }
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
if ($IsSystemLane) {
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
        foreach ($p in @("$env:USERPROFILE\.local\bin\$Name.exe", "$env:APPDATA\npm\$Name.cmd", "$env:LOCALAPPDATA\Programs\Ollama\$Name.exe")) {
            if (Test-Path $p) { return $p }
        }
        return $null
    }
    $c.Source
}
$Tools = @{}
if (-not $IsSystemLane) {
    foreach ($t in 'claude', 'codex', 'node', 'python', 'ollama', 'git', 'nvidia-smi') { $p = Resolve-Tool $t; if ($p) { $Tools[$t] = $p } }
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
        hub_url = $script:Hub; bundle_version = $StartBundle
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

function Update-ForgeJob($Id, [hashtable]$Body) {
    try { Send-Hub "/api/agent/jobs/$Id/update" $Body | Out-Null } catch { Write-AgentLog "job $Id update failed: $($_.Exception.Message)" }
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
        $script:Running[$id] = @{ proc = $p; out = $out; offset = [long]0; err = $err; erroffset = [long]0; engine = $Job.engine; cancelled = $false }
        $who = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        Update-ForgeJob $id @{ status = 'running'; pid = $p.Id; append = "[$NodeId] $what started as $who`n" }
        Write-AgentLog "job $id started ($what, pid $($p.Id), dir $dir)"
    } catch {
        Update-ForgeJob $id @{ status = 'failed'; exit_code = -1; append = "[$NodeId] failed to start: $($_.Exception.Message)`n" }
    }
}

function Start-ForgeJob($Job) {
    $id  = [string]$Job.id
    if (-not (Test-JobLane $Job $Lane)) {
        Update-ForgeJob $id @{ status = 'failed'; exit_code = -1; append = "[$NodeId] job refused: engine '$($Job.engine)' (run_as '$($Job.run_as)') does not run in the $Lane lane`n" }
        Write-AgentLog "job $id refused: wrong lane ($($Job.engine))"; return
    }
    if ($IsSystemLane) { Start-SystemJob $Job; return }
    $dir = Join-Path $script:JobsDir $id; New-Item -ItemType Directory -Force $dir | Out-Null
    $out = Join-Path $dir 'out.log'; $promptFile = Join-Path $dir 'prompt.txt'
    $cwd = if ($Job.cwd -and (Test-Path -LiteralPath $Job.cwd)) { $Job.cwd } else { $script:ScratchDir }
    $inner = $null
    switch ($Job.engine) {
        'claude' {
            if ($Tools['claude']) {
                [IO.File]::WriteAllText($promptFile, $Job.prompt, $Utf8)
                $mode = if ($Job.full_access) { 'bypassPermissions' } else { 'default' }
                # Use the claude.ai subscription login unless this node opts into API-key billing.
                $auth = if ($cfg.claude_use_api_key) { '' } else { 'set "ANTHROPIC_API_KEY=" && set "ANTHROPIC_AUTH_TOKEN=" && ' }
                $inner = "$auth`"$($Tools['claude'])`" -p --permission-mode $mode --output-format text < `"$promptFile`""
            }
        }
        'codex' {
            if ($Tools['codex']) {
                [IO.File]::WriteAllText($promptFile, $Job.prompt, $Utf8)
                $sb = if ($Job.full_access) { '--dangerously-bypass-approvals-and-sandbox' } else { '--sandbox read-only' }
                $inner = "`"$($Tools['codex'])`" exec --skip-git-repo-check $sb - < `"$promptFile`""
            }
        }
        'shell' {
            $ps1 = Join-Path $dir 'task.ps1'
            [IO.File]::WriteAllText($ps1, $Job.prompt, (New-Object Text.UTF8Encoding($true)))
            $inner = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$ps1`""
        }
    }
    if (-not $inner) {
        Update-ForgeJob $id @{ status = 'failed'; exit_code = -1; append = "[$NodeId] engine '$($Job.engine)' is not installed on this machine`n" }
        return
    }
    try {
        $p = Start-Process -FilePath cmd.exe -ArgumentList "/s /c `"$inner > `"$out`" 2>&1`"" -WorkingDirectory $cwd -WindowStyle Hidden -PassThru
        $null = $p.Handle   # keep the handle so ExitCode is available later
        $script:Running[$id] = @{ proc = $p; out = $out; offset = [long]0; engine = $Job.engine; cancelled = $false }
        Update-ForgeJob $id @{ status = 'running'; pid = $p.Id; append = "[$NodeId] $($Job.engine) started in $cwd$(if ($Job.full_access) { ' (full access)' })`n" }
        Write-AgentLog "job $id started ($($Job.engine), pid $($p.Id), cwd $cwd)"
    } catch {
        Update-ForgeJob $id @{ status = 'failed'; exit_code = -1; append = "[$NodeId] failed to start: $($_.Exception.Message)`n" }
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

function Step-ForgeJobs {
    foreach ($id in @($script:Running.Keys)) {
        $r = $script:Running[$id]
        $text = (Read-NewOutput $r) + (Read-NewOutput $r 'err' 'erroffset')
        if ($r.proc.HasExited) {
            $text += (Read-NewOutput $r) + (Read-NewOutput $r 'err' 'erroffset')
            $code = $r.proc.ExitCode
            $status = if ($r.cancelled) { 'cancelled' } elseif ($code -eq 0) { 'done' } else { 'failed' }
            Update-ForgeJob $id @{ status = $status; exit_code = $code; append = $text }
            Write-AgentLog "job $id $status (exit $code)"
            $script:Running.Remove($id)
        } elseif ($text) {
            Update-ForgeJob $id @{ append = $text }
        }
    }
}

function Stop-ForgeJob($Id) {
    $r = $script:Running[[string]$Id]
    if ($r -and -not $r.proc.HasExited) {
        $r.cancelled = $true
        & taskkill.exe /T /F /PID $r.proc.Id 2>$null | Out-Null
        Write-AgentLog "job $Id cancelled"
    }
}

function Apply-HubResponse($Resp) {
    if (-not $Resp) { return }
    if ($Resp.hub_urls) { Update-HubUrls @($Resp.hub_urls) }
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

# ---------------- main loop ----------------
Set-WorkRoot
# the SYSTEM lane's ensure_task(run_as=user) needs the node's user; the user lane knows who it is
if (-not $IsSystemLane -and -not $cfg.user -and $env:USERNAME) {
    $me = "$env:USERDOMAIN\$env:USERNAME"
    Save-AgentConfig @{ user = $me }
    $cfg | Add-Member -NotePropertyName user -NotePropertyValue $me -Force
}
Write-AgentLog "agent $AgentVersion starting ($Lane lane as $([Security.Principal.WindowsIdentity]::GetCurrent().Name)): node=$NodeId bundle=$StartBundle hubs=$($script:HubUrls -join ',') work_root=$($script:WorkRoot) tools=$(@($Tools.Keys) -join ',')$(if ($IsSystemLane) { " allow_admin=$(Test-AllowAdmin $cfg) catalog=$([bool]$script:ActionCatalog)" })"
$lastInfo = [datetime]::MinValue; $lastLogs = [datetime]::MinValue; $lastBeat = Get-Date; $lastVer = Get-Date; $boot = $true
while ($true) {
    try {
        $cap = if ($script:Draining) { 0 } else { [Math]::Max(0, $script:MaxJobs - $script:Running.Count) }
        if ($IsSystemLane) {
            $body = New-SystemLaneReport -NodeId $NodeId -Capacity $cap -Running $script:Running.Count -Boot $boot -AgentVersion $AgentVersion -BundleVersion $StartBundle
        } else {
            $body = @{ node_id = $NodeId; metrics = (Get-Metrics); procs = (Get-TopProcs); agents = (Get-Agents)
                       capacity = $cap; boot = $boot; agent_version = $AgentVersion; bundle_version = $StartBundle }
            if (((Get-Date) - $lastInfo).TotalSeconds -ge 600) { $body.info = Get-Info; $lastInfo = Get-Date }
            if (((Get-Date) - $lastLogs).TotalSeconds -ge 20) { $body.logs = Get-Logs; $lastLogs = Get-Date }
        }
        $resp = Send-Hub '/api/agent/report' $body
        $boot = $false
        if ($resp.need_info) { $lastInfo = [datetime]::MinValue }
        Apply-HubResponse $resp
        foreach ($j in @($resp.jobs)) { if ($j) { Start-ForgeJob $j } }
        foreach ($c in @($resp.cancel)) { if ($c) { Stop-ForgeJob $c } }
    } catch {
        Write-AgentLog "report failed: $($_.Exception.Message)"
    }
    try { Step-ForgeJobs } catch { Write-AgentLog "job step failed: $($_.Exception.Message)" }

    # heartbeat (the watchdog restarts us if agent.log goes stale for 2 min)
    if (((Get-Date) - $lastBeat).TotalSeconds -ge 60) {
        Write-AgentLog "alive: hub=$($script:Hub) running=$($script:Running.Count) max_jobs=$($script:MaxJobs)"
        $lastBeat = Get-Date
        # Keep this user's T3 Code server up (its own task only fires at logon; we run at boot as the user).
        # start-t3.ps1 is a no-op when T3 is running or not set up. Opt out with agent-config "t3_autostart": false.
        if (-not $IsSystemLane -and $cfg.t3_autostart -ne $false) {
            $t3s = Join-Path (Split-Path $PSScriptRoot -Parent) 'watchdog\start-t3.ps1'
            if (Test-Path $t3s) { try { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $t3s | Out-Null } catch { Write-AgentLog "t3 autostart failed: $($_.Exception.Message)" } }
        }
    }
    # bundle updated by the watchdog -> stop taking jobs, exit when idle; supervisor relaunches new code
    if ($StartBundle -and ((Get-Date) - $lastVer).TotalSeconds -ge 30) {
        $lastVer = Get-Date
        $now = Get-BundleVersion
        if ($now -and $now -ne $StartBundle) {
            if (-not $script:Draining) { Write-AgentLog "bundle $StartBundle -> $now; draining $($script:Running.Count) job(s) before restart"; $script:Draining = $true }
        }
    }
    if ($script:Draining -and $script:Running.Count -eq 0) { Write-AgentLog "restarting onto new bundle"; exit 0 }
    Start-Sleep -Seconds $Interval
}
