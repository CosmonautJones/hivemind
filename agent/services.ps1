# Forge agent (user lane): keep-alive services (SPEC-phase2 H) - generalizes the 2.1.2 T3 autostart.
# Dot-sourced by forge-agent.ps1. Functions only. The decision logic is pure and unit-tested.
#
# agent-config.json "services" (defaults below when absent; "t3_autostart": false still drops t3):
#   { name, check }                          run a check script (relative = bundle watchdog\ dir). Exit codes:
#                                            0 running, 10 started now, 20 not set up on this machine (not reported)
#   { name, when_tool, start, env, health }  GET health (2xx = ok); unhealthy -> start hidden with env.
#                                            when_tool: only if that tool was found; "<tool.exe>" in start is
#                                            replaced by the resolved tool path.
# Reported as metrics.services: [{name, ok, since}] (since = epoch seconds of the last ok/not-ok change).

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
    @{ name = $name; check = [string](& $get 'check'); when_tool = [string](& $get 'when_tool'); start = [string](& $get 'start')
       health = [string](& $get 'health'); env = $env }
}

function Get-ForgeServices($Config) {
    $raw = $null
    if ($Config -and $Config.PSObject.Properties['services'] -and $null -ne $Config.services) { $raw = @($Config.services) } else { $raw = Get-DefaultForgeServices }
    $out = New-Object System.Collections.Generic.List[object]
    foreach ($s in $raw) {
        $d = ConvertTo-ForgeServiceDef $s
        if (-not $d) { continue }
        if ($d.name -eq 't3' -and $Config -and $Config.PSObject.Properties['t3_autostart'] -and $Config.t3_autostart -eq $false) { continue }
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

function Get-ServiceMetrics([hashtable]$Table) {
    , @($Table.Keys | Sort-Object | ForEach-Object { [ordered]@{ name = $_; ok = [bool]$Table[$_].ok; since = $Table[$_].since } })
}

function Test-ServiceHealth([string]$Url, [int]$TimeoutSec = 3) {
    if (-not $Url) { return $false }
    try { $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec; return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 300) } catch { $false }
}

# Start hidden with extra environment variables (PS 5.1 Start-Process has no -Environment: set, start, restore).
function Start-ForgeServiceProcess([string]$Exe, [string]$Arguments, [hashtable]$Env = @{}) {
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
