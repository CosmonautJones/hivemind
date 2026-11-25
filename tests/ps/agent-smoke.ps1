# Agent smoke test against a fake hub on a high port (never 8787): the worker (-Child) reports, gets
# settings {work_root, max_jobs} + hub_urls, runs one shell job in work_root\scratch, streams output and the
# exit code back. Everything lives in a scratch dir (config.local_hub + work_root_auto point there).
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File tests\ps\agent-smoke.ps1 [-Scratch <dir>] [-Port 18896]
param([string]$Scratch = (Join-Path ([IO.Path]::GetTempPath()) "forge-agent-smoke-$([guid]::NewGuid().ToString('n').Substring(0,8))"), [int]$AgentPort = 18896)
$ErrorActionPreference = 'Stop'
if ($AgentPort -eq 8787) { throw 'never use the live hub port' }
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$data = Join-Path $Scratch 'data'; $wr = Join-Path $Scratch 'work'; $log = Join-Path $Scratch 'posts.jsonl'
New-Item -ItemType Directory -Force $data, $wr | Out-Null
$enc = New-Object Text.UTF8Encoding($false)
[IO.File]::WriteAllText((Join-Path $data 'config.json'), '{"node_id":"smoke-node","agent_token":"smoke-token"}', $enc)
$cfg = @{ node_id = 'smoke-node'; local_hub = "http://127.0.0.1:$AgentPort"; hub_urls = @("http://127.0.0.1:$AgentPort"); work_root_auto = $wr; interval = 2; logs = @() }
[IO.File]::WriteAllText((Join-Path $data 'agent-config.json'), (ConvertTo-Json -InputObject $cfg), $enc)
[IO.File]::WriteAllText((Join-Path $data 'watchdog.json'), '{"last_run":"2026-01-01T00:00:00Z","actions":["test"]}', $enc)
$py = (Get-Command python.exe -ErrorAction Stop).Source
$srv = Start-Process -FilePath $py -ArgumentList '-I', "`"$(Join-Path $PSScriptRoot 'fake_agent_hub.py')`"", $AgentPort, "`"$wr`"", "`"$log`"" -PassThru -WindowStyle Hidden
$agent = $null
try {
    Start-Sleep -Seconds 2
    $agent = Start-Process powershell.exe -PassThru -WindowStyle Hidden -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$(Join-Path $Repo 'agent\forge-agent.ps1')`"", '-Child', '-ConfigPath', "`"$(Join-Path $data 'agent-config.json')`""
    $deadline = (Get-Date).AddSeconds(60); $done = $null
    while ((Get-Date) -lt $deadline -and -not $done) {
        Start-Sleep -Seconds 2
        if (Test-Path $log) { $done = Get-Content $log | ConvertFrom-Json | Where-Object { $_.path -eq '/api/agent/jobs/job-0001/update' -and $_.body.status -in 'done', 'failed' } | Select-Object -First 1 }
    }
    $posts = @(Get-Content $log | ConvertFrom-Json)
    $reports = @($posts | Where-Object { $_.path -eq '/api/agent/report' })
    $updates = @($posts | Where-Object { $_.path -like '/api/agent/jobs/job-0001/*' })
    $last = $reports[-1].body
    $out = ($updates | ForEach-Object { $_.body.append }) -join ''
    $checks = [ordered]@{
        'reports sent with agent token'           = ($reports.Count -ge 2 -and $reports[0].token -eq 'smoke-token')
        'metrics.work_root = settings.work_root'  = ($last.metrics.work_root -eq $wr)
        'metrics.work_root_free_gb present'       = ($null -ne $last.metrics.work_root_free_gb)
        'metrics.watchdog = watchdog.json'        = ($last.metrics.watchdog.actions[0] -eq 'test')
        'capacity = max_jobs 3 - 1 running job'  = (@($reports | Where-Object { $_.body.capacity -eq 2 }).Count -ge 1 -and @($reports | Where-Object { $_.body.capacity -lt 2 }).Count -eq 0)
        'job ran in work_root\scratch'            = ($out -match [regex]::Escape("$wr\scratch"))
        'job output streamed'                     = ($out -match 'hello from forge job')
        'job failed with exit 3'                  = ($done -and $done.body.status -eq 'failed' -and $done.body.exit_code -eq 3)
        'hub_urls persisted from response'        = ((Get-Content (Join-Path $data 'agent-config.json') -Raw | ConvertFrom-Json).hub_urls -contains 'http://peer-a.invalid:8787')
        'job dirs under work_root\jobs'           = (Test-Path (Join-Path $wr 'jobs\job-0001\out.log'))
        'agent.log heartbeat file'                = (Test-Path (Join-Path $data 'agent.log'))
    }
    $fail = 0
    foreach ($k in $checks.Keys) { if ($checks[$k]) { Write-Host "  ok   $k" -ForegroundColor Green } else { $fail++; Write-Host "  FAIL $k" -ForegroundColor Red } }
    if ($fail) { Write-Host '--- agent.log ---'; Get-Content (Join-Path $data 'agent.log') -ErrorAction SilentlyContinue | Select-Object -Last 20 }
    Write-Host "$($checks.Count - $fail) passed, $fail failed"
} finally {
    if ($agent) { & taskkill.exe /T /F /PID $agent.Id 2>$null | Out-Null }
    Stop-Process -Id $srv.Id -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    Remove-Item -LiteralPath $Scratch -Recurse -Force -ErrorAction SilentlyContinue
}
