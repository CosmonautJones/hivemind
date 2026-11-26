# Workstream F agent smoke test: a real user-lane worker (-Child) against a fake hub in F's port range.
#   - user-lane action job (git_sync on a scratch repo) runs through agent\actions.ps1 and ends with ##RESULT uptodate,
#   - a user-lane action with bad args and a SYSTEM action are refused by the user lane,
#   - the report carries metrics.macs and metrics.ollama_models (fake model list) and services[].state,
#   - the report response's wol item makes the agent send a magic packet (UDP sink on 127.0.0.1).
# Nothing real is touched: sessions off, tools are fakes (except git on the scratch repo), ollama_url = the fake hub,
# keep-alive services point at the fake hub / a closed port.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File tests\ps\p3-upkeep-smoke.ps1 [-Port 19265]
param([string]$Scratch = (Join-Path ([IO.Path]::GetTempPath()) "forge-p3f-smoke-$([guid]::NewGuid().ToString('n').Substring(0,8))"), [int]$Port = 19265)
$ErrorActionPreference = 'Stop'
if (($Port -lt 19250 -or $Port -gt 19277) -and ($Port -lt 19280 -or $Port -gt 19297)) { throw 'workstream F test ports are 19250-19279 (or the lead range 19280-19299) only' }
$WolPort = $Port + 1; $DeadPort = $Port + 2
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$data = Join-Path $Scratch 'data'; $wr = Join-Path $Scratch 'work'; $log = Join-Path $Scratch 'posts.jsonl'; $bin = Join-Path $Scratch 'bin'
New-Item -ItemType Directory -Force $data, $wr, $bin | Out-Null
$enc = New-Object Text.UTF8Encoding($false)
foreach ($t in 'claude', 'codex', 't3', 'ollama') { [IO.File]::WriteAllText((Join-Path $bin "fake-$t.cmd"), "@echo fake-$t 1.0`r`n@exit /b 0`r`n", $enc) }

# scratch repo with an upstream (bare remote) -> git_sync says "uptodate"
$git = (Get-Command git.exe -ErrorAction Stop).Source
$ErrorActionPreference = 'Continue'
$bare = Join-Path $Scratch 'remote.git'; $repoDir = Join-Path $Scratch 'repo'
& $git init --bare -q $bare 2>&1 | Out-Null
& $git clone -q $bare $repoDir 2>&1 | Out-Null
[IO.File]::WriteAllText((Join-Path $repoDir 'a.txt'), "1`n")
& $git -C $repoDir add a.txt 2>&1 | Out-Null
& $git -C $repoDir -c user.name=t -c user.email=t@example.invalid -c commit.gpgsign=false commit -q -m one 2>&1 | Out-Null
& $git -C $repoDir branch -M main 2>&1 | Out-Null
& $git -C $repoDir push -q -u origin main 2>&1 | Out-Null
$ErrorActionPreference = 'Stop'

[IO.File]::WriteAllText((Join-Path $data 'config.json'), '{"node_id":"f-smoke","agent_token":"smoke-token"}', $enc)
$cfg = @{ node_id = 'f-smoke'; local_hub = "http://127.0.0.1:$Port"; hub_urls = @("http://127.0.0.1:$Port"); work_root_auto = $wr; interval = 1; logs = @()
          sessions_enabled = $false; claude_home = (Join-Path $Scratch 'claude-home'); codex_sessions_dir = (Join-Path $Scratch 'codex')
          tools = @{ claude = (Join-Path $bin 'fake-claude.cmd'); codex = (Join-Path $bin 'fake-codex.cmd'); t3 = (Join-Path $bin 'fake-t3.cmd'); ollama = (Join-Path $bin 'fake-ollama.cmd'); git = $git }
          ollama_url = "http://127.0.0.1:$Port"; services_interval_s = 2; t3_autostart = $false
          services = @(@{ name = 'oksvc'; health = "http://127.0.0.1:$Port/health" },
                       @{ name = 'deadsvc'; health = "http://127.0.0.1:$DeadPort/"; start = 'cmd.exe /c exit 0' }) }
[IO.File]::WriteAllText((Join-Path $data 'agent-config.json'), (ConvertTo-Json -InputObject $cfg -Depth 5), $enc)
$py = if ($env:FORGE_TEST_PYTHON) { $env:FORGE_TEST_PYTHON } else { 'C:\Users\you\forge-hub\.venv\Scripts\python.exe' }
$srv = Start-Process -FilePath $py -ArgumentList '-I', "`"$(Join-Path $PSScriptRoot 'fake_upkeep_hub.py')`"", $Port, "`"$repoDir`"", "`"$log`"", $WolPort -PassThru -WindowStyle Hidden
$sink = New-Object Net.Sockets.UdpClient((New-Object Net.IPEndPoint([Net.IPAddress]::Loopback, $WolPort)))
$agent = $null; $packet = $null
function Get-Posts { if (Test-Path $log) { @(Get-Content $log -Encoding UTF8 | ForEach-Object { $_ | ConvertFrom-Json }) } else { @() } }
function Get-Final([string]$Id) { Get-Posts | Where-Object { $_.path -eq "/api/agent/jobs/$Id/update" -and $_.body.status -in 'done', 'failed', 'cancelled' } | Select-Object -First 1 }
function Get-Text([string]$Id) { (@(Get-Posts | Where-Object { $_.path -eq "/api/agent/jobs/$Id/update" }) | ForEach-Object { $_.body.append }) -join '' }
function Get-Reports { @(Get-Posts | Where-Object { $_.path -eq '/api/agent/report' -and -not $_.body.lane }) }
$fail = 0
try {
    Start-Sleep -Seconds 2
    $agentArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$(Join-Path $Repo 'agent\forge-agent.ps1')`"", '-Child', '-ConfigPath', "`"$(Join-Path $data 'agent-config.json')`"")
    $agent = Start-Process powershell.exe -PassThru -WindowStyle Hidden -ArgumentList $agentArgs
    $deadline = (Get-Date).AddSeconds(120)
    $ready = { $packet -and (Get-Final 'u-git') -and (Get-Final 'u-bad') -and (Get-Final 'u-sys') -and
               @(Get-Reports | Where-Object { @($_.body.metrics.services | Where-Object { $_.name -eq 'deadsvc' -and $_.state -eq 'starting' }).Count }).Count }
    while ((Get-Date) -lt $deadline -and -not (& $ready)) {
        if (-not $packet -and $sink.Available -gt 0) { $from = New-Object Net.IPEndPoint([Net.IPAddress]::Any, 0); $packet = $sink.Receive([ref]$from) }
        Start-Sleep -Milliseconds 500
    }
    $reports = Get-Reports
    $m = ($reports | Where-Object { $_.body.metrics.ollama_models } | Select-Object -Last 1).body.metrics
    $gitText = Get-Text 'u-git'
    $specPath = Join-Path $wr 'jobs\u-git\spec.json'
    $checks = [ordered]@{
        'wol: magic packet sent to the requested broadcast/port' = ($packet -and $packet.Length -eq 102 -and $packet[6] -eq 0xAA -and $packet[11] -eq 0x01)
        'report: metrics.macs from the physical adapters'      = [bool](@($reports | Where-Object { @($_.body.metrics.macs | Where-Object { "$($_.mac)" -match '^([0-9A-F]{2}-){5}[0-9A-F]{2}$' }).Count }).Count)
        'report: metrics.ollama_models from ollama_url'        = ($m -and @($m.ollama_models) -contains 'qwen2.5:14b')
        'report: services[].state ok / starting'               = [bool](@($reports | Where-Object { @($_.body.metrics.services | Where-Object { $_.name -eq 'oksvc' -and $_.state -eq 'ok' -and $_.ok -eq $true }).Count }).Count) -and
                                                                 [bool](@($reports | Where-Object { @($_.body.metrics.services | Where-Object { $_.name -eq 'deadsvc' -and $_.state -eq 'starting' -and $_.ok -eq $false }).Count }).Count)
        'user action: git_sync done, ##RESULT uptodate'        = ((Get-Final 'u-git').body.status -eq 'done' -and $gitText -match '##RESULT \{"result":"uptodate"')
        'user action: ran through actions.ps1 with spec.json (tools passed)' = ((Test-Path $specPath) -and ([IO.File]::ReadAllText($specPath) | ConvertFrom-Json).tools.git -eq $git)
        'user action: bad args refused by the agent'           = ((Get-Final 'u-bad').body.status -eq 'failed' -and (Get-Text 'u-bad') -match 'action rejected by agent')
        'system action refused in the user lane'               = ((Get-Final 'u-sys').body.status -eq 'failed' -and (Get-Text 'u-sys') -match 'does not run in the user lane')
    }
    foreach ($k in $checks.Keys) { if ($checks[$k]) { Write-Host "  ok   $k" -ForegroundColor Green } else { $fail++; Write-Host "  FAIL $k" -ForegroundColor Red } }
    if ($fail) {
        Write-Host '--- u-git output ---'; Write-Host $gitText
        Write-Host '--- agent.log (tail) ---'; Get-Content (Join-Path $data 'agent.log') -Tail 30 -ErrorAction SilentlyContinue | Write-Host
    }
    Write-Host "$($checks.Count - $fail) passed, $fail failed"
} finally {
    if ($agent) { Stop-Process -Id $agent.Id -Force -ErrorAction SilentlyContinue }
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($Scratch) } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Stop-Process -Id $srv.Id -Force -ErrorAction SilentlyContinue
    $sink.Close()
    Start-Sleep -Milliseconds 500
    Remove-Item -LiteralPath $Scratch -Recurse -Force -ErrorAction SilentlyContinue
}
exit $fail
