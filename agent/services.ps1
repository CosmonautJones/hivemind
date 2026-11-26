# Forge agent (user lane): keep-alive services (SPEC-phase2 H) - generalizes the 2.1.2 T3 autostart.
# Dot-sourced by forge-agent.ps1. Functions only. The decision logic is pure and unit-tested.
#
# agent-config.json "services" (defaults below when absent; "t3_autostart": false still drops t3):
#   { name, check }                          run a check script (relative = bundle watchdog\ dir). Exit codes:
#                                            0 running, 10 started now, 20 not set up on this machine (not reported)
#   { name, when_tool, start, env, health }  GET health (2xx = ok); unhealthy -> start hidden with env.
#                                            when_tool: only if that tool was found; "<tool.exe>" in start is
#                                            replaced by the resolved tool path.
# Reported as metrics.services: [{name, ok, since, state}] (since = epoch seconds of the last ok/not-ok change).
# 2.3 F6c: state = ok | starting (within starting_grace_s, default 60, of a (re)start) | down; ok = (state -eq 'ok').
# 2.3 F6b: the ollama service gets OLLAMA_MODELS = <work_root>\ollama\models (agent-config ollama_models_dir wins)
#          when work_root is not on the system drive and OLLAMA_MODELS is not already set for the machine or user.

function Get-DefaultForgeServices {
    @(
        @{ name = 't3'; check = 'start-t3.ps1' },
        @{ name = 'ollama'; when_tool = 'ollama'; start = '<ollama.exe> serve'; env = @{ OLLAMA_HOST = '127.0.0.1:11434' }; health = 'http://127.0.0.1:11434/api/version' }
    )
}

function ConvertTo-ForgeServiceDef($S) {
    if ($null -eq $S) { return $null }
    $get = { param($k) if ($S -is [System.Collections.IDictionary]) { $S[$k] } else { $p = $S.PSObject.Properties[$k]; if ($p) { $p.Value } } }
    $name = [string](& $get 'name'); if (-not $name) { return $null }
    $env = @{}
    $e = & $get 'env'
    if ($e -is [System.Collections.IDictionary]) { foreach ($k in $e.Keys) { $env[[string]$k] = [string]$e[$k] } }
    elseif ($e) { foreach ($p in $e.PSObject.Properties) { $env[$p.Name] = [string]$p.Value } }
    $grace = 60; $g = & $get 'starting_grace_s'; if ($null -ne $g -and "$g" -match '^\d+$') { $grace = [int]$g }
    @{ name = $name; check = [string](& $get 'check'); when_tool = [string](& $get 'when_tool'); start = [string](& $get 'start')
       health = [string](& $get 'health'); env = $env; starting_grace_s = $grace }
}

# F6b: models folder for the ollama service, or $null (leave ollama's default). -Existing = an OLLAMA_MODELS value
# already set machine-wide / for the user (then we never override it).
function Get-OllamaModelsDir {
    param($Config, [string]$WorkRoot, [string]$Existing = $null, [string]$SystemDrive = $env:SystemDrive)
    if ($PSBoundParameters.ContainsKey('Existing') -eq $false) {
        $Existing = [Environment]::GetEnvironmentVariable('OLLAMA_MODELS', 'Machine')
        if (-not $Existing) { $Existing = [Environment]::GetEnvironmentVariable('OLLAMA_MODELS', 'User') }
    }
    if ($Existing) { return $null }
    if ($Config -and $Config.PSObject.Properties['ollama_models_dir'] -and $Config.ollama_models_dir) { return [string]$Config.ollama_models_dir }
    if (-not $WorkRoot) { return $null }
    $drive = [IO.Path]::GetPathRoot($WorkRoot).TrimEnd('\')
    if (-not $drive -or $drive -ieq "$SystemDrive".TrimEnd('\')) { return $null }
    $WorkRoot.TrimEnd('\') + '\ollama\models'      # (Join-Path fails on drives that do not exist here)
}

function Get-ForgeServices($Config, [string]$WorkRoot = $null) {
    $raw = $null
    if ($Config -and $Config.PSObject.Properties['services'] -and $null -ne $Config.services) { $raw = @($Config.services) } else { $raw = Get-DefaultForgeServices }
    $out = New-Object System.Collections.Generic.List[object]
    foreach ($s in $raw) {
        $d = ConvertTo-ForgeServiceDef $s
        if (-not $d) { continue }
        if ($d.name -eq 't3' -and $Config -and $Config.PSObject.Properties['t3_autostart'] -and $Config.t3_autostart -eq $false) { continue }
        if ($d.name -eq 'ollama' -and $d.start -and -not $d.env.ContainsKey('OLLAMA_MODELS')) {
            $md = Get-OllamaModelsDir -Config $Config -WorkRoot $WorkRoot
            if ($md) { $d.env['OLLAMA_MODELS'] = $md }
        }
        $out.Add($d)
    }
    , $out.ToArray()
}

# ok    healthy
# skip  its when_tool is not installed (not reported)
# start unhealthy and allowed to (re)start now
# wait  unhealthy but started < Grace s ago, or backing off after 3 failed starts (10 min)
# down  unhealthy and there is nothing to start
function Get-ServiceDecision {
    param([bool]$ToolPresent = $true, [bool]$Healthy = $false, [bool]$CanStart = $true, [double]$Now = 0,
          [double]$LastStart = 0, [int]$Fails = 0, [int]$Grace = 120, [int]$Backoff = 600)
    if (-not $ToolPresent) { return 'skip' }
    if ($Healthy) { return 'ok' }
    if (-not $CanStart) { return 'down' }
    if ($LastStart -gt 0 -and ($Now - $LastStart) -lt $Grace) { return 'wait' }
    if ($Fails -ge 3 -and $LastStart -gt 0 -and ($Now - $LastStart) -lt $Backoff) { return 'wait' }
    'start'
}

# "<ollama.exe> serve" -> @{exe = <resolved path>; args = 'serve'}; $null when a placeholder tool is missing.
function Split-ServiceCommand([string]$Command, [hashtable]$Tools = @{}) {
    $c = "$Command".Trim()
    if (-not $c) { return $null }
    if ($c -match '^<([A-Za-z0-9_.-]+?)(\.exe)?>\s*(.*)$') {
        $t = $Tools[$Matches[1]]
        if (-not $t) { return $null }
        return @{ exe = [string]$t; args = $Matches[3] }
    }
    if ($c -match '^"([^"]+)"\s*(.*)$') { return @{ exe = $Matches[1]; args = $Matches[2] } }
    $i = $c.IndexOf(' ')
    if ($i -lt 0) { return @{ exe = $c; args = '' } }
    @{ exe = $c.Substring(0, $i); args = $c.Substring($i + 1).Trim() }
}

# Status table: name -> @{ok; since}. Returns $true when the ok state changed.
function Update-ServiceStatus([hashtable]$Table, [string]$Name, [bool]$Ok, [double]$Now) {
    $cur = $Table[$Name]
    if ($cur -and [bool]$cur.ok -eq $Ok) { return $false }
    $Table[$Name] = @{ ok = $Ok; since = $Now }
    $true
}

# F6c: 'ok' | 'starting' (not ok yet, within its grace after a start) | 'down'
function Get-ServiceState([bool]$Ok, [double]$LastStart = 0, [double]$Now = 0, [int]$Grace = 60) {
    if ($Ok) { return 'ok' }
    if ($LastStart -gt 0 -and ($Now - $LastStart) -lt $Grace) { return 'starting' }
    'down'
}

# -Runtime: name -> @{ last_start; grace } (the agent's $script:SvcRuntime); -Now: epoch seconds.
function Get-ServiceMetrics([hashtable]$Table, [hashtable]$Runtime = $null, [double]$Now = 0) {
    if ($null -eq $Runtime) { $Runtime = $script:SvcRuntime }
    if ($null -eq $Runtime) { $Runtime = @{} }
    if ($Now -le 0) { $Now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0 }
    , @($Table.Keys | Sort-Object | ForEach-Object {
        $rt = $Runtime[$_]; $ls = 0.0; $gr = 60
        if ($rt) { if ($rt.last_start) { $ls = [double]$rt.last_start }; if ($rt.grace) { $gr = [int]$rt.grace } }
        $st = Get-ServiceState -Ok ([bool]$Table[$_].ok) -LastStart $ls -Now $Now -Grace $gr
        [ordered]@{ name = $_; ok = ($st -eq 'ok'); since = $Table[$_].since; state = $st } })
}

function Test-ServiceHealth([string]$Url, [int]$TimeoutSec = 3) {
    if (-not $Url) { return $false }
    try { $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec; return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 300) } catch { $false }
}

# Start hidden with extra environment variables (PS 5.1 Start-Process has no -Environment: set, start, restore).
function Start-ForgeServiceProcess([string]$Exe, [string]$Arguments, [hashtable]$Env = @{}) {
    if ($Env['OLLAMA_MODELS'] -and -not (Test-Path -LiteralPath $Env['OLLAMA_MODELS'])) {
        try { New-Item -ItemType Directory -Force $Env['OLLAMA_MODELS'] | Out-Null } catch {}
    }
    $saved = @{}
    foreach ($k in $Env.Keys) { $saved[$k] = [Environment]::GetEnvironmentVariable($k, 'Process'); [Environment]::SetEnvironmentVariable($k, $Env[$k], 'Process') }
    try {
        $sp = @{ FilePath = $Exe; WindowStyle = 'Hidden'; PassThru = $true }
        if ($Arguments) { $sp.ArgumentList = $Arguments }
        Start-Process @sp
    } finally {
        foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k], 'Process') }
    }
}
