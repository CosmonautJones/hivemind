# SYSTEM-lane smoke test, run as the CURRENT user (never elevated, never SYSTEM): the worker started with
# -Child -Lane system reports a minimal body to a fake hub on 19000-19099, runs an allowlisted action in dry-run
# mode (agent-config action_dry_run), refuses an action whose args the hub would have rejected, runs a harmless
# admin script, refuses a user-lane job, writes data\agent-system.log and sys-<id> job dirs, and never rewrites
# agent-config.json. Everything lives in a scratch dir.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File tests\ps\agent-system-smoke.ps1 [-Port 19053]
param([string]$Scratch = (Join-Path ([IO.Path]::GetTempPath()) "forge-sys-smoke-$([guid]::NewGuid().ToString('n').Substring(0,8))"), [int]$Port = 19053)
$ErrorActionPreference = 'Stop'
if ($Port -lt 19000 -or $Port -gt 19099) { throw 'test servers use ports 19000-19099 only' }
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$data = Join-Path $Scratch 'data'; $wr = Join-Path $Scratch 'work'; $log = Join-Path $Scratch 'posts.jsonl'
New-Item -ItemType Directory -Force $data, $wr | Out-Null
$enc = New-Object Text.UTF8Encoding($false)
[IO.File]::WriteAllText((Join-Path $data 'config.json'), '{"node_id":"smoke-node","agent_token":"smoke-token"}', $enc)
$cfg = @{ node_id = 'smoke-node'; local_hub = "http://127.0.0.1:$Port"; hub_urls = @("http://127.0.0.1:$Port"); work_root_auto = $wr; interval = 1
          logs = @(); action_dry_run = $true; user = "$env:USERDOMAIN\$env:USERNAME" }
$cfgPath = Join-Path $data 'agent-config.json'
[IO.File]::WriteAllText($cfgPath, (ConvertTo-Json -InputObject $cfg), $enc)
$cfgBefore = [IO.File]::ReadAllText($cfgPath)
$py = if ($env:FORGE_TEST_PYTHON) { $env:FORGE_TEST_PYTHON } else { (Get-Command python.exe -ErrorAction Stop).Source }
$srv = Start-Process -FilePath $py -ArgumentList '-I', "`"$(Join-Path $PSScriptRoot 'fake_system_hub.py')`"", $Port, "`"$wr`"", "`"$log`"" -PassThru -WindowStyle Hidden
$agent = $null
function Get-Posts { if (Test-Path $log) { @(Get-Content $log | ConvertFrom-Json) } else { @() } }
function Get-Final([string]$Id) { Get-Posts | Where-Object { $_.path -eq "/api/agent/jobs/$Id/update" -and $_.body.status -in 'done', 'failed', 'cancelled' } | Select-Object -First 1 }
function Get-Text([string]$Id) { (@(Get-Posts | Where-Object { $_.path -eq "/api/agent/jobs/$Id/update" }) | ForEach-Object { $_.body.append }) -join '' }
try {
    Start-Sleep -Seconds 2
    $agent = Start-Process powershell.exe -PassThru -WindowStyle Hidden -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$(Join-Path $Repo 'agent\forge-agent.ps1')`"", '-Child', '-Lane', 'system', '-ConfigPath', "`"$cfgPath`""
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline -and -not ((Get-Final 'sys-act') -and (Get-Final 'sys-bad') -and (Get-Final 'sys-adm') -and (Get-Final 'sys-usr'))) { Start-Sleep -Seconds 2 }
    $ErrorActionPreference = 'Continue'
    $reports = @(Get-Posts | Where-Object { $_.path -eq '/api/agent/report' })
    $r0 = $reports[0].body
    $act = Get-Final 'sys-act'; $bad = Get-Final 'sys-bad'; $adm = Get-Final 'sys-adm'; $usr = Get-Final 'sys-usr'
    $actText = Get-Text 'sys-act'; $admText = Get-Text 'sys-adm'
    $keys = @($r0.PSObject.Properties.Name | Sort-Object)
    $checks = [ordered]@{
        'reports carry lane=system + agent token'           = ($reports.Count -ge 2 -and $r0.lane -eq 'system' -and $reports[0].token -eq 'smoke-token')
        'report body is minimal (no metrics/procs/logs)'    = (($keys -join ',') -eq 'agent_time,agent_version,boot,bundle_version,capacity,lane,node_id,running')
        'capacity never above 1'                            = (@($reports | Where-Object { $_.body.capacity -gt 1 }).Count -eq 0)
        'action dry-run printed the winget argv'            = ($actText -match [regex]::Escape('> winget install --id Git.Git -e --silent --accept-source-agreements --accept-package-agreements') -and $actText -match 'dry run: not executed')
        'action job done, exit 0'                           = ($act -and $act.body.status -eq 'done' -and $act.body.exit_code -eq 0)
        'agent refused hub-invalid action args'             = ($bad -and $bad.body.status -eq 'failed' -and $bad.body.append -match 'rejected by agent')
        'bad action never got a job dir'                    = (-not (Test-Path (Join-Path $wr 'jobs\sys-sys-bad')))
        'admin script ran (as current user) with exit 4'    = ($adm -and $adm.body.status -eq 'failed' -and $adm.body.exit_code -eq 4 -and $admText -match "admin-ok $([regex]::Escape($env:USERNAME))")
        'user-lane job refused in system lane'              = ($usr -and $usr.body.status -eq 'failed' -and $usr.body.append -match 'does not run in the system lane' -and (Get-Text 'sys-usr') -notmatch 'should not run')
        'job dirs are <work_root>\jobs\sys-<id>'            = ((Test-Path (Join-Path $wr 'jobs\sys-sys-act\spec.json')) -and (Test-Path (Join-Path $wr 'jobs\sys-sys-adm\task.ps1')))
        'spec.json holds the re-validated args'             = ((Test-Path (Join-Path $wr 'jobs\sys-sys-act\spec.json')) -and (Get-Content (Join-Path $wr 'jobs\sys-sys-act\spec.json') -Raw | ConvertFrom-Json).args.id -eq 'Git.Git')
        'log is data\agent-system.log (not agent.log)'      = ((Test-Path (Join-Path $data 'agent-system.log')) -and -not (Test-Path (Join-Path $data 'agent.log')))
        'agent-config.json untouched by the system lane'    = ([IO.File]::ReadAllText($cfgPath) -eq $cfgBefore)
    }
    $fail = 0
    foreach ($k in $checks.Keys) { if ($checks[$k]) { Write-Host "  ok   $k" -ForegroundColor Green } else { $fail++; Write-Host "  FAIL $k" -ForegroundColor Red } }
    if ($fail) {
        Write-Host '--- agent-system.log ---'; Get-Content (Join-Path $data 'agent-system.log') -ErrorAction SilentlyContinue | Select-Object -Last 30
        Write-Host '--- posts ---'; Get-Content $log -ErrorAction SilentlyContinue | Select-Object -Last 30
    }
    Write-Host "$($checks.Count - $fail) passed, $fail failed"
    exit $fail
} finally {
    if ($agent) { & taskkill.exe /T /F /PID $agent.Id 2>$null | Out-Null }
    Stop-Process -Id $srv.Id -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    Remove-Item -LiteralPath $Scratch -Recurse -Force -ErrorAction SilentlyContinue
}
