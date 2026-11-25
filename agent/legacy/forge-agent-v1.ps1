# Forge Control node agent (Windows PowerShell 5.1+).
# Reports status/metrics/logs to the hub every few seconds and runs jobs the hub hands back
# (Claude Code, Codex, or PowerShell), streaming output to the hub. Pull model: no inbound ports.
param([string]$ConfigPath = (Join-Path $PSScriptRoot 'agent-config.json'), [switch]$Child)

$AgentVersion = '1.1.1'

# Supervisor: the scheduled task runs this file without -Child; we relaunch the worker forever,
# so a self-update (worker replaces this file and exits) or a crash just restarts it.
if (-not $Child) {
    while ($true) {
        $p = Start-Process powershell.exe -WindowStyle Hidden -PassThru -Wait -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
            '-File', "`"$PSCommandPath`"", '-Child', '-ConfigPath', "`"$ConfigPath`"")
        Start-Sleep -Seconds 3
    }
}
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$cfg      = Get-Content $ConfigPath -Raw | ConvertFrom-Json
$Hub      = $cfg.hub_url.TrimEnd('/')
$Headers  = @{ 'X-Agent-Token' = $cfg.agent_token }
$NodeId   = if ($cfg.node_id) { $cfg.node_id } else { $env:COMPUTERNAME.ToLower() }
$Root     = if ($cfg.work_dir) { $cfg.work_dir } else { $PSScriptRoot }
$JobsDir  = Join-Path $Root 'jobs'
$AgentLog = Join-Path $Root 'agent.log'
$Interval = if ($cfg.interval) { [int]$cfg.interval } else { 5 }
$MaxJobs  = if ($cfg.max_jobs) { [int]$cfg.max_jobs } else { 2 }
$Cores    = [int](Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors
$Utf8     = New-Object Text.UTF8Encoding($false)
New-Item -ItemType Directory -Force $JobsDir | Out-Null

function Write-AgentLog([string]$m) {
    try {
        Add-Content -Path $AgentLog -Value "[$(Get-Date -Format s)] $m" -Encoding UTF8
        if ((Get-Item $AgentLog).Length -gt 5MB) { Move-Item $AgentLog "$AgentLog.old" -Force }
    } catch {}
}

function Send-Hub([string]$Path, $Obj) {
    $json  = ConvertTo-Json -InputObject $Obj -Depth 8 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    Invoke-RestMethod -Uri "$Hub$Path" -Method Post -Headers $Headers -ContentType 'application/json; charset=utf-8' -Body $bytes -TimeoutSec 20
}

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
foreach ($t in 'claude', 'codex', 'node', 'python', 'ollama', 'git', 'nvidia-smi') { $p = Resolve-Tool $t; if ($p) { $Tools[$t] = $p } }

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
        boot = $os.LastBootUpTime.ToString('o'); agent_version = $AgentVersion; max_jobs = $MaxJobs
    }
}

function Get-Metrics {
    $os  = Get-CimInstance Win32_OperatingSystem
    $cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
    $tot = [double]$os.TotalVisibleMemorySize; $free = [double]$os.FreePhysicalMemory
    $disks = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object {
        @{ name = $_.DeviceID; total_gb = [math]::Round($_.Size / 1GB, 1); free_gb = [math]::Round($_.FreeSpace / 1GB, 1) } })
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
            '^python'  { if ($cmd -match 'hermes') { $kind = 'hermes' } elseif ($cmd -match 'uvicorn.*app:app') { $kind = 'forge-hub' } }
        }
        if ($kind) {
            $out.Add(@{ kind = $kind; pid = [int]$p.ProcessId
                started = if ($p.CreationDate) { $p.CreationDate.ToString('o') } else { $null }
                cmd = $cmd.Substring(0, [Math]::Min(160, $cmd.Length)) })
        }
    }
    foreach ($id in @($script:Running.Keys)) { $out.Add(@{ kind = 'forge-job'; pid = $script:Running[$id].proc.Id; job = [int]$id; started = $null; cmd = $script:Running[$id].engine }) }
    , $out.ToArray()
}

function Get-Logs {
    $h = @{}
    $logs = @($cfg.logs) + @(@{ name = 'forge-agent'; path = $AgentLog })
    foreach ($l in $logs) {
        if ($l -and $l.path -and (Test-Path $l.path)) {
            try {
                $txt = (Get-Content $l.path -Tail 150 -ErrorAction Stop) -join "`n"
                $h[$l.name] = ($txt -replace "`0", '') -replace "\x1b\[[0-9;?]*[A-Za-z]", ''
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

function Start-ForgeJob($Job) {
    $id  = [string]$Job.id
    $dir = Join-Path $JobsDir $id; New-Item -ItemType Directory -Force $dir | Out-Null
    $out = Join-Path $dir 'out.log'; $promptFile = Join-Path $dir 'prompt.txt'
    $cwd = if ($Job.cwd -and (Test-Path $Job.cwd)) { $Job.cwd } else { $env:USERPROFILE }
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
        Write-AgentLog "job $id started ($($Job.engine), pid $($p.Id))"
    } catch {
        Update-ForgeJob $id @{ status = 'failed'; exit_code = -1; append = "[$NodeId] failed to start: $($_.Exception.Message)`n" }
    }
}

function Read-NewOutput($R) {
    if (-not (Test-Path $R.out)) { return '' }
    $fs = [IO.File]::Open($R.out, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try {
        if ($fs.Length -le $R.offset) { return '' }
        $null = $fs.Seek($R.offset, [IO.SeekOrigin]::Begin)
        $len = [int][Math]::Min($fs.Length - $R.offset, 200000)
        $buf = New-Object byte[] $len
        $n = $fs.Read($buf, 0, $len)
        $R.offset += $n
        [Text.Encoding]::UTF8.GetString($buf, 0, $n)
    } finally { $fs.Close() }
}

function Step-ForgeJobs {
    foreach ($id in @($script:Running.Keys)) {
        $r = $script:Running[$id]
        $text = Read-NewOutput $r
        if ($r.proc.HasExited) {
            $text += Read-NewOutput $r
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

function Update-Self([string]$Version) {
    try {
        $tmp = "$PSCommandPath.new"
        Invoke-WebRequest "$Hub/agent/forge-agent.ps1" -OutFile $tmp -UseBasicParsing -TimeoutSec 30
        $null = [scriptblock]::Create((Get-Content $tmp -Raw))   # refuse to install a script that doesn't parse
        Move-Item $tmp $PSCommandPath -Force
        Write-AgentLog "self-update $AgentVersion -> $Version; restarting"
        exit 0
    } catch { Write-AgentLog "self-update failed: $($_.Exception.Message)" }
}

# ---------------- main loop ----------------
Write-AgentLog "agent $AgentVersion starting: node=$NodeId hub=$Hub tools=$(@($Tools.Keys) -join ',')"
$lastInfo = [datetime]::MinValue; $lastLogs = [datetime]::MinValue; $boot = $true
while ($true) {
    try {
        $body = @{ node_id = $NodeId; metrics = (Get-Metrics); procs = (Get-TopProcs); agents = (Get-Agents)
                   capacity = [Math]::Max(0, $MaxJobs - $script:Running.Count); boot = $boot }
        if (((Get-Date) - $lastInfo).TotalSeconds -ge 600) { $body.info = Get-Info; $lastInfo = Get-Date }
        if (((Get-Date) - $lastLogs).TotalSeconds -ge 20) { $body.logs = Get-Logs; $lastLogs = Get-Date }
        $resp = Send-Hub '/api/agent/report' $body
        $boot = $false
        if ($resp.need_info) { $lastInfo = [datetime]::MinValue }
        foreach ($j in @($resp.jobs)) { if ($j) { Start-ForgeJob $j } }
        foreach ($c in @($resp.cancel)) { if ($c) { Stop-ForgeJob $c } }
        if ($resp.agent_version -and $resp.agent_version -ne $AgentVersion -and $script:Running.Count -eq 0) { Update-Self $resp.agent_version }
    } catch {
        Write-AgentLog "report failed: $($_.Exception.Message)"
    }
    try { Step-ForgeJobs } catch { Write-AgentLog "job step failed: $($_.Exception.Message)" }
    Start-Sleep -Seconds $Interval
}
