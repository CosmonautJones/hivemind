# Agent smoke test against a fake hub on a high port (never 8787): the worker (-Child) reports, gets
# settings {work_root, max_jobs} + hub_urls, runs one shell job in work_root\scratch, streams output and the
# exit code back. Everything lives in a scratch dir (config.local_hub + work_root_auto point there).
# 2.2 additions (fake hub = fake_agent_hub.py on -AgentPort, fake ollama REST = fake_ollama.py on -AgentPort+1):
#   - session index of a scratch Claude home (fixture session rewritten to live in a scratch git repo) + a codex
#     fixture -> /api/agent/sessions (git remote/branch, redacted title, t3 unsupported) and /api/agent/commits,
#   - watch -> /api/agent/transcript: tail first, then follows the cursor (the resumed turn shows up),
#   - resume job: exact claude command line via a FAKE claude (cfg.tools.claude), API key cleared, session cwd,
#     rescan after the job,
#   - ollama job: model missing -> pull with progress -> generate (UTF-8),
#   - keep-alive services -> metrics.services (one healthy, one unhealthy that gets "started").
#   - jobs: own scratch\<jobid> cwd, brief job output = model text after the hub's noise filter, timeout_s kill,
#     worker killed mid-job -> the restarted worker re-attaches (boot report lists it in running; output not repeated),
#     every report carries running + agent_time.
# Python: $env:FORGE_TEST_PYTHON or python.exe on PATH.
# Nothing real is touched: real ~/.claude and ~/.codex are never read (cfg.claude_home / codex_sessions_dir).
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File tests\ps\agent-smoke.ps1 [-Scratch <dir>] [-AgentPort 19050]
param([string]$Scratch = (Join-Path ([IO.Path]::GetTempPath()) "forge-agent-smoke-$([guid]::NewGuid().ToString('n').Substring(0,8))"), [int]$AgentPort = 19050)
$ErrorActionPreference = 'Stop'
if ($AgentPort -lt 19000 -or $AgentPort -gt 19298) { throw 'smoke test servers use ports 19000-19299 only (Phase 3 team ranges: CONTRACT-phase3 9)' }
$OllamaPort = $AgentPort + 1
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$data = Join-Path $Scratch 'data'; $wr = Join-Path $Scratch 'work'; $log = Join-Path $Scratch 'posts.jsonl'; $olog = Join-Path $Scratch 'ollama.jsonl'
$bin = Join-Path $Scratch 'bin'; $gitRepo = Join-Path $Scratch 'repo'; $chome = Join-Path $Scratch 'claude-home'; $codexDir = Join-Path $Scratch 'codex'
New-Item -ItemType Directory -Force $data, $wr, $bin, $gitRepo, (Join-Path $codexDir '2026\11\20') | Out-Null
$enc = New-Object Text.UTF8Encoding($false)

# --- scratch git repo with one commit + origin remote ---
$git = (Get-Command git.exe -ErrorAction Stop).Source
& $git -C $gitRepo init -q -b main 2>$null
[IO.File]::WriteAllText((Join-Path $gitRepo 'README.md'), "hi`n", $enc)
& $git -C $gitRepo add README.md
& $git -C $gitRepo -c user.name=Smoke -c user.email=smoke@example.invalid -c commit.gpgsign=false commit -q -m 'initial commit'
& $git -C $gitRepo remote add origin https://github.com/Example/Repo.git

# --- scratch sessions: the claude fixture moved into the scratch repo; the codex fixture as is ---
$Sid = '11111111-2222-4333-8444-555555555555'
$fx = Join-Path $PSScriptRoot 'fixtures\sessions'
$slugDir = Join-Path $chome 'projects\scratch-repo'; New-Item -ItemType Directory -Force $slugDir | Out-Null
$sessFile = Join-Path $slugDir "$Sid.jsonl"
$repoJson = $gitRepo.Replace('\', '\\')
[IO.File]::WriteAllText($sessFile, ([IO.File]::ReadAllText((Join-Path $fx "claude\projects\C--Users-dev-proj\$Sid.jsonl")).Replace('C:\\Users\\dev\\proj', $repoJson)), $enc)
Copy-Item (Join-Path $fx 'codex\2026\11\20\*.jsonl') (Join-Path $codexDir '2026\11\20')
Get-ChildItem $chome, $codexDir -Recurse -File | ForEach-Object { $_.LastWriteTime = Get-Date }
$initialSize = (Get-Item $sessFile).Length

# --- fake claude / t3: print args, the API key the agent left us, and stdin; append the resumed turn ---
[IO.File]::WriteAllText((Join-Path $bin 'fake-claude.cmd'), "@echo off`r`nif `"%~1`"==`"--version`" (echo 9.9.9 fake-claude& exit /b 0)`r`necho FAKE-CLAUDE %*`r`necho ANTHROPIC_API_KEY=[%ANTHROPIC_API_KEY%]`r`nfindstr `"^`"`r`npowershell -NoProfile -ExecutionPolicy Bypass -File `"%~dp0fake-claude.ps1`"`r`nexit /b 0`r`n", $enc)
[IO.File]::WriteAllText((Join-Path $bin 'fake-claude.ps1'), @'
$f = $env:FAKE_SESSION_FILE
$u = '{"type":"user","message":{"role":"user","content":"please continue the docs"},"timestamp":"2026-11-25T10:00:00.000Z","sessionId":"s","isSidechain":false}'
$a = '{"type":"assistant","message":{"id":"msg_resume","role":"assistant","model":"claude-opus-5-5","content":[{"type":"text","text":"resumed ok"}],"usage":{"input_tokens":1,"output_tokens":1}},"timestamp":"2026-11-25T10:00:01.000Z","sessionId":"s","isSidechain":false}'
[IO.File]::AppendAllText($f, "$u`n$a`n", (New-Object Text.UTF8Encoding($false)))
'@, $enc)
[IO.File]::WriteAllText((Join-Path $bin 'fake-t3.cmd'), "@echo off`r`necho SUBCOMMANDS`r`necho   add       Add a project.`r`nexit /b 0`r`n", $enc)

[IO.File]::WriteAllText((Join-Path $data 'config.json'), '{"node_id":"smoke-node","agent_token":"smoke-token"}', $enc)
$cfg = @{ node_id = 'smoke-node'; local_hub = "http://127.0.0.1:$AgentPort"; hub_urls = @("http://127.0.0.1:$AgentPort"); work_root_auto = $wr; interval = 2; logs = @()
          tools = @{ claude = (Join-Path $bin 'fake-claude.cmd'); t3 = (Join-Path $bin 'fake-t3.cmd') }
          claude_home = $chome; codex_sessions_dir = $codexDir; sessions_interval_s = 3; commits_interval_s = 6; services_interval_s = 4
          ollama_url = "http://127.0.0.1:$OllamaPort"
          services = @(@{ name = 'fakesvc'; health = "http://127.0.0.1:$OllamaPort/api/version" },
                       @{ name = 'deadsvc'; health = "http://127.0.0.1:$OllamaPort/nope"; start = 'cmd.exe /c exit 0' }) }
[IO.File]::WriteAllText((Join-Path $data 'agent-config.json'), (ConvertTo-Json -InputObject $cfg -Depth 5), $enc)
[IO.File]::WriteAllText((Join-Path $data 'watchdog.json'), '{"last_run":"2026-01-01T00:00:00Z","actions":["test"]}', $enc)
$py = if ($env:FORGE_TEST_PYTHON) { $env:FORGE_TEST_PYTHON } else { (Get-Command python.exe -ErrorAction Stop).Source }
$srv = Start-Process -FilePath $py -ArgumentList '-I', "`"$(Join-Path $PSScriptRoot 'fake_agent_hub.py')`"", $AgentPort, "`"$wr`"", "`"$log`"", $Sid -PassThru -WindowStyle Hidden
$osrv = Start-Process -FilePath $py -ArgumentList '-I', "`"$(Join-Path $PSScriptRoot 'fake_ollama.py')`"", $OllamaPort, "`"$olog`"" -PassThru -WindowStyle Hidden
$agent = $null
function Get-Posts { if (Test-Path $log) { @(Get-Content $log -Encoding UTF8 | ForEach-Object { $_ | ConvertFrom-Json }) } else { @() } }
function Get-Final([string]$Id) { Get-Posts | Where-Object { $_.path -eq "/api/agent/jobs/$Id/update" -and $_.body.status -in 'done', 'failed', 'cancelled' } | Select-Object -First 1 }
function Get-Text([string]$Id) { (@(Get-Posts | Where-Object { $_.path -eq "/api/agent/jobs/$Id/update" }) | ForEach-Object { $_.body.append }) -join '' }
$fail = 0
try {
    Start-Sleep -Seconds 2
    $env:ANTHROPIC_API_KEY = 'sk-fake-should-be-cleared-0000'; $env:FAKE_SESSION_FILE = $sessFile
    $agentArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$(Join-Path $Repo 'agent\forge-agent.ps1')`"", '-Child', '-ConfigPath', "`"$(Join-Path $data 'agent-config.json')`"")
    $agent = Start-Process powershell.exe -PassThru -WindowStyle Hidden -ArgumentList $agentArgs
    Remove-Item Env:\ANTHROPIC_API_KEY, Env:\FAKE_SESSION_FILE
    $deadline = (Get-Date).AddSeconds(180); $restarted = $false; $firstAgent = $agent.Id
    $ready = { (Get-Final 'job-0005') -and @(Get-Posts | Where-Object { $_.path -eq '/api/agent/transcript' -and (@($_.body.events) | Where-Object { $_.text -eq 'resumed ok' }) }).Count -and
               @(Get-Posts | Where-Object { $_.path -eq '/api/agent/commits' }).Count -and
               @(Get-Posts | Where-Object { $_.path -eq '/api/agent/report' -and @($_.body.metrics.services).Count -eq 2 }).Count }
    while ((Get-Date) -lt $deadline -and -not (& $ready)) {
        if (-not $restarted -and (Get-Text 'job-0005') -match 'part1') {
            # kill only the worker (its job keeps running), then start a new worker: it must re-attach to job-0005
            Start-Sleep -Seconds 2
            Stop-Process -Id $agent.Id -Force; $restarted = $true; $restartAt = @(Get-Posts).Count
            Start-Sleep -Seconds 1
            $agent = Start-Process powershell.exe -PassThru -WindowStyle Hidden -ArgumentList $agentArgs
        }
        Start-Sleep -Seconds 1
    }
    Start-Sleep -Seconds 4   # one more session cycle after the resume job
    $ErrorActionPreference = 'Continue'
    $posts = Get-Posts
    $reports = @($posts | Where-Object { $_.path -eq '/api/agent/report' })
    $last = $reports[-1].body
    $done = Get-Final 'job-0001'; $out = Get-Text 'job-0001'
    $sessPosts = @($posts | Where-Object { $_.path -eq '/api/agent/sessions' })
    $allSess = @($sessPosts | ForEach-Object { @($_.body.sessions) })
    $cl = @($allSess | Where-Object { $_.session_id -eq $Sid }) | Select-Object -First 1
    $tx = @($posts | Where-Object { $_.path -eq '/api/agent/transcript' -and $_.body.session_id -eq $Sid })
    $res = Get-Final 'job-0002'; $resText = Get-Text 'job-0002'
    $resIdx = [array]::IndexOf($posts, $res)
    $after = @($posts | Select-Object -Skip ([Math]::Max(0, $resIdx)) | Where-Object { $_.path -eq '/api/agent/sessions' } | ForEach-Object { @($_.body.sessions) } | Where-Object { $_.session_id -eq $Sid })
    $oll = Get-Final 'job-0003'; $ollText = Get-Text 'job-0003'
    $to = Get-Final 'job-0004'; $toText = Get-Text 'job-0004'; $ra = Get-Final 'job-0005'; $raText = Get-Text 'job-0005'
    if (-not $restarted) { $restartAt = 0 }
    $ologs = @(if (Test-Path $olog) { Get-Content $olog | ForEach-Object { $_ | ConvertFrom-Json } })
    $commits = @($posts | Where-Object { $_.path -eq '/api/agent/commits' }) | Select-Object -First 1
    $svc = @((@($reports | Where-Object { @($_.body.metrics.services).Count -gt 0 }) | Select-Object -Last 1).body.metrics.services)   # a restarted worker reports none until its first services step
    $raw = if (Test-Path $log) { [IO.File]::ReadAllText($log) } else { '' }
    $agentLog = Get-Content (Join-Path $data 'agent.log') -Raw -ErrorAction SilentlyContinue
    $checks = [ordered]@{
        'reports sent with agent token'           = ($reports.Count -ge 2 -and $reports[0].token -eq 'smoke-token')
        'metrics.work_root = settings.work_root'  = ($last.metrics.work_root -eq $wr)
        'metrics.work_root_free_gb present'       = ($null -ne $last.metrics.work_root_free_gb)
        'metrics.watchdog = watchdog.json'        = ($last.metrics.watchdog.actions[0] -eq 'test')
        'capacity = max_jobs 3 - 1 running job'  = (@($reports | Where-Object { $_.body.capacity -eq 2 }).Count -ge 1 -and @($reports | Where-Object { -not $_.body.boot -and $_.body.capacity -lt 2 }).Count -eq 0)
        'job ran in its own work_root\scratch\<id>' = ($out -match [regex]::Escape("started in $wr\scratch\job-0001") -and (Test-Path (Join-Path $wr 'scratch\job-0001')))
        'job output streamed'                     = ($out -match 'hello from forge job')
        'job failed with exit 3'                  = ($done -and $done.body.status -eq 'failed' -and $done.body.exit_code -eq 3)
        'hub_urls persisted from response'        = ((Get-Content (Join-Path $data 'agent-config.json') -Raw | ConvertFrom-Json).hub_urls -contains 'http://peer-a.invalid:8787')
        'job dirs under work_root\jobs'           = (Test-Path (Join-Path $wr 'jobs\job-0001\out.log'))
        'agent.log heartbeat file'                = (Test-Path (Join-Path $data 'agent.log'))
        # ---- 2.2 ----
        'sessions: claude + codex indexed'        = ($cl -and @($allSess | Where-Object { $_.tool -eq 'codex' -and $_.session_id -eq '0aaa1111-2222-7333-8444-555555555555' }).Count -ge 1)
        'sessions: git remote/branch from cwd'    = ($cl -and $cl.git_remote -eq 'https://github.com/Example/Repo.git' -and $cl.git_branch -eq 'main' -and $cl.cwd -eq $gitRepo)
        'sessions: title redacted, t3 unsupported' = ($cl -and $cl.title -eq 'Fix the login bug; my key is [redacted] and it fails' -and $sessPosts[0].body.t3 -eq 'unsupported' -and $sessPosts[0].body.node_id -eq 'smoke-node')
        'no fixture secret in any post'           = ($raw -notmatch 'sk-ant-api03-FAKE|ghp_FAKE|AKIAFAKE|sk-fake-should')
        'transcript: first call = tail + cursor'  = ($tx.Count -ge 1 -and @($tx[0].body.events).Count -eq 10 -and [long]$tx[0].body.cursor -eq $initialSize -and $tx[0].body.done -eq $true)
        'transcript: follows cursor (resumed turn)' = (@($tx | Select-Object -Skip 1 | Where-Object { [long]$_.body.cursor -gt $initialSize -and (@($_.body.events) | Where-Object { $_.text -eq 'resumed ok' -and $_.role -eq 'assistant' }) }).Count -eq 1 -and
                                                  @($tx | Where-Object { @($_.body.events) | Where-Object { $_.text -match 'Fix the login bug' } }).Count -eq 1)
        'resume: exact claude command line'       = ($resText -match [regex]::Escape("FAKE-CLAUDE -p --resume $Sid --permission-mode acceptEdits --output-format text") -and $resText -match 'please continue the docs')
        'resume: ANTHROPIC_API_KEY cleared'       = ($resText -match [regex]::Escape('ANTHROPIC_API_KEY=[]'))
        'resume: ran in the session cwd'          = ($resText -match [regex]::Escape("claude resume $Sid started in $gitRepo") -and $res.body.status -eq 'done')
        'resume: session rescanned (+2 messages)' = (@($after | Where-Object { $_.messages -eq ($cl.messages + 2) }).Count -ge 1)
        'commits: repo + last commit posted'      = ($commits -and $commits.body.repos[0].git_remote -eq 'https://github.com/Example/Repo.git' -and $commits.body.repos[0].commits[0].subject -eq 'initial commit' -and $commits.body.repos[0].branch -eq 'main' -and $commits.body.repos[0].dirty -eq $false)
        'ollama: pulled once with progress'       = (@($ologs | Where-Object { $_.path -eq '/api/pull' }).Count -eq 1 -and $ollText -match 'pulling 0123abcd 100%' -and $ollText -match 'not present; pulling')
        'ollama: generated (utf-8), done exit 0'  = ($oll -and $oll.body.status -eq 'done' -and $oll.body.exit_code -eq 0 -and $ollText -match "Brief: all good $([char]0x2713)")
        'services: metrics ok/not ok + restart'   = (@($svc | Where-Object { $_.name -eq 'fakesvc' -and $_.ok -eq $true }).Count -eq 1 -and @($svc | Where-Object { $_.name -eq 'deadsvc' -and $_.ok -eq $false }).Count -eq 1 -and $agentLog -match 'service deadsvc: unhealthy, started cmd\.exe')
        'agent_version 2.4.0 reported'            = ($reports[0].body.agent_version -eq '2.4.0')
        'reports carry agent_time + running ids'  = (@($reports | Where-Object { [double]$_.body.agent_time -lt 1e9 -or $null -eq $_.body.running }).Count -eq 0 -and @($reports | Where-Object { @($_.body.running) -contains 'job-0004' }).Count -ge 1)
        'brief: output minus hub noise = model text' = ((@($ollText -split "`r?`n" | Where-Object { $_.Trim() -and $_ -notmatch '^\s*(pulling |verifying |writing manifest|success\b|\[ollama\]|\[forge\])' }) -join '|') -eq "Brief: all good $([char]0x2713)")
        'timeout: job killed after timeout_s, failed + note' = ($to -and $to.body.status -eq 'failed' -and $toText -match 'timed out after 3 s' -and $toText -notmatch 'never')
        'orphan: worker restart re-attached job-0005' = ($restarted -and $ra -and $ra.body.status -eq 'done' -and $ra.body.exit_code -eq 0 -and $agentLog -match 're-attached after agent restart')
        'orphan: output streamed once (no repeat)'   = (([regex]::Matches($raText, 'part1')).Count -eq 1 -and ([regex]::Matches($raText, 'part2')).Count -eq 1)
        'orphan: boot report after restart lists it' = (@($posts | Select-Object -Skip $restartAt | Where-Object { $_.path -eq '/api/agent/report' -and $_.body.boot -eq $true -and @($_.body.running) -contains 'job-0005' }).Count -eq 1)
        'running.json empty once jobs finished'    = (@((Get-Content (Join-Path $data 'running.json') -Raw | ConvertFrom-Json).jobs).Count -eq 0)
    }
    foreach ($k in $checks.Keys) { if ($checks[$k]) { Write-Host "  ok   $k" -ForegroundColor Green } else { $fail++; Write-Host "  FAIL $k" -ForegroundColor Red } }
    if ($fail) {
        Write-Host '--- agent.log ---'; Get-Content (Join-Path $data 'agent.log') -ErrorAction SilentlyContinue | Select-Object -Last 30
        Write-Host '--- job-0002 ---'; Write-Host $resText; Write-Host '--- job-0003 ---'; Write-Host $ollText; Write-Host '--- job-0004 ---'; Write-Host $toText; Write-Host '--- job-0005 ---'; Write-Host $raText
    }
    Write-Host "$($checks.Count - $fail) passed, $fail failed"
} finally {
    if ($agent) { & taskkill.exe /T /F /PID $agent.Id 2>$null | Out-Null }
    Stop-Process -Id $srv.Id, $osrv.Id -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    Remove-Item -LiteralPath $Scratch -Recurse -Force -ErrorAction SilentlyContinue
}
exit $fail
