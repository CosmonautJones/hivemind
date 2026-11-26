# Fast, deterministic tests for the PowerShell side of Forge (no Pester 5 on the fleet: Pester 3.4 ships
# with Windows). Each script is dot-sourced with -NoRun inside its own scope, so nothing real runs.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File tests\ps\run-tests.ps1
# Exit code = number of failures.
param([string]$Scratch = (Join-Path ([IO.Path]::GetTempPath()) ("forge-ps-tests-" + [guid]::NewGuid().ToString('n').Substring(0, 8))))

$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$script:Pass = 0; $script:Fail = 0; $script:Failures = New-Object System.Collections.Generic.List[string]

function Test-Case([string]$Name, [scriptblock]$Body) {
    try { & $Body; $script:Pass++; Write-Host "  ok   $Name" -ForegroundColor Green }
    catch { $script:Fail++; $script:Failures.Add("$Name : $($_.Exception.Message)"); Write-Host "  FAIL $Name : $($_.Exception.Message)" -ForegroundColor Red }
}
function Assert-Equal($Expected, $Actual, [string]$Msg = '') {
    $e = ConvertTo-Json -InputObject $Expected -Compress -Depth 5; $a = ConvertTo-Json -InputObject $Actual -Compress -Depth 5
    if ($e -ne $a) { throw "expected $e but got $a $Msg" }
}
function Assert-True($Cond, [string]$Msg = 'condition false') { if (-not $Cond) { throw $Msg } }
function Assert-Throws([scriptblock]$Body, [string]$Like = '*') {
    $threw = $false
    try { & $Body } catch { $threw = $true; if ($_.Exception.Message -notlike $Like) { throw "threw '$($_.Exception.Message)', expected like '$Like'" } }
    if (-not $threw) { throw 'expected an exception' }
}

# ---------------- 0. every .ps1 parses ----------------
Write-Host 'parse'
foreach ($f in Get-ChildItem -LiteralPath $Repo -Recurse -Filter *.ps1 -File | Where-Object { $_.FullName -notmatch '\\(dist|\.venv|node_modules)\\' }) {
    Test-Case "parses: $($f.FullName.Substring($Repo.Length + 1))" {
        $t = $null; $e = $null
        [void][System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$t, [ref]$e)
        Assert-Equal 0 @($e).Count (($e | ForEach-Object { "L$($_.Extent.StartLineNumber) $($_.Message)" }) -join '; ')
    }
}

# ---------------- 1. agent ----------------
Write-Host 'agent'
& {
    . (Join-Path $Repo 'agent\forge-agent.ps1') -NoRun
    $disks = @(@{ name = 'C:'; free_gb = 40.5 }, @{ name = 'D:'; free_gb = 812.0 }, @{ name = 'E:'; free_gb = 120 })

    Test-Case 'work root: fixed drive with most free space' { Assert-Equal 'D:\forge' (Select-WorkRoot -Disks $disks) }
    Test-Case 'work root: settings override wins' { Assert-Equal 'C:\work' (Select-WorkRoot -Disks $disks -Override ' C:\work ' -Previous 'E:\forge') }
    Test-Case 'work root: earlier auto choice is sticky while its drive exists' { Assert-Equal 'E:\forge' (Select-WorkRoot -Disks $disks -Previous 'E:\forge') }
    Test-Case 'work root: earlier choice dropped when drive is gone' { Assert-Equal 'D:\forge' (Select-WorkRoot -Disks $disks -Previous 'Z:\forge') }
    Test-Case 'work root: tie broken by drive letter' { Assert-Equal 'C:\forge' (Select-WorkRoot -Disks @(@{ name = 'D:'; free_gb = 5 }, @{ name = 'C:'; free_gb = 5 })) }
    Test-Case 'work root: no disks -> system drive' { Assert-Equal "$env:SystemDrive\forge" (Select-WorkRoot -Disks @()) }

    Test-Case 'hub urls: localhost first, dedupe, trailing slashes, localhost aliases dropped' {
        $r = Merge-HubUrls -Local 'http://127.0.0.1:8787' -Urls @('http://forge-01:8787/', 'http://localhost:8787', 'http://FORGE-01:8787', '', $null, 'http://atlas:8787')
        Assert-Equal @('http://127.0.0.1:8787', 'http://forge-01:8787', 'http://atlas:8787') $r
    }
    Test-Case 'hub urls: empty list -> localhost only' { Assert-Equal @('http://127.0.0.1:8787') (Merge-HubUrls -Urls @()) }

    $urls = @('http://127.0.0.1:8787', 'http://a:8787', 'http://b:8787', 'http://c:8787')
    Test-Case 'hub order: sticky localhost -> list order' { Assert-Equal $urls (Get-HubCandidates -Urls $urls -Sticky 'http://127.0.0.1:8787') }
    Test-Case 'hub order: sticky remote tried first, then the rest in order' {
        Assert-Equal @('http://b:8787', 'http://127.0.0.1:8787', 'http://a:8787', 'http://c:8787') (Get-HubCandidates -Urls $urls -Sticky 'http://b:8787')
    }
    Test-Case 'hub order: 60 s localhost retry puts localhost before sticky remote' {
        Assert-Equal @('http://127.0.0.1:8787', 'http://b:8787', 'http://a:8787', 'http://c:8787') (Get-HubCandidates -Urls $urls -Sticky 'http://b:8787' -RetryLocal $true)
    }
    Test-Case 'hub order: retry flag is a no-op when already on localhost' {
        Assert-Equal $urls (Get-HubCandidates -Urls $urls -Sticky 'http://127.0.0.1:8787' -RetryLocal $true)
    }
    Test-Case 'hub order: sticky no longer listed is still tried first' {
        Assert-Equal @('http://old:8787', 'http://127.0.0.1:8787', 'http://a:8787', 'http://b:8787', 'http://c:8787') (Get-HubCandidates -Urls $urls -Sticky 'http://old:8787/')
    }
    Test-Case 'hub order: no sticky -> list order' { Assert-Equal $urls (Get-HubCandidates -Urls $urls -Sticky $null) }

    Test-Case 'max_jobs: hub setting > config > default; 0 allowed' {
        Assert-Equal 4 (Get-EffectiveMaxJobs -Setting 4 -Config 1)
        Assert-Equal 1 (Get-EffectiveMaxJobs -Setting $null -Config 1)
        Assert-Equal 2 (Get-EffectiveMaxJobs -Setting $null -Config $null)
        Assert-Equal 0 (Get-EffectiveMaxJobs -Setting 0 -Config 3)
    }
    Test-Case 'log tails: ANSI + NUL stripped' { Assert-Equal 'red plain' (Remove-Ansi "$([char]27)[31mred$([char]27)[0m pl`0ain") }
    Test-Case 'agent version string still matches the hub regex' {
        $ver = (Get-Content (Join-Path $Repo 'VERSION') -Raw).Trim()
        Assert-True ((Get-Content (Join-Path $Repo 'agent\forge-agent.ps1') -Raw) -match "\`$AgentVersion\s*=\s*'$([regex]::Escape($ver))'") "AgentVersion != VERSION $ver"
    }

    Test-Case 'lanes: system jobs (run_as system / engine action|admin) only in the system lane' {
        $sys1 = [pscustomobject]@{ id = 'a'; engine = 'action'; run_as = 'system' }
        $sys2 = [pscustomobject]@{ id = 'b'; engine = 'admin' }                 # run_as missing -> still system
        $sys3 = [pscustomobject]@{ id = 'c'; engine = 'shell'; run_as = 'system' }
        $usr = [pscustomobject]@{ id = 'd'; engine = 'shell' }
        Assert-True (Test-JobLane $sys1 'system'); Assert-True (Test-JobLane $sys2 'system'); Assert-True (Test-JobLane $sys3 'system')
        Assert-True (-not (Test-JobLane $sys1 'user')); Assert-True (-not (Test-JobLane $sys2 'user')); Assert-True (-not (Test-JobLane $sys3 'user'))
        Assert-True (Test-JobLane $usr 'user'); Assert-True (-not (Test-JobLane $usr 'system'))
    }
    Test-Case 'lanes: allow_admin defaults to true; false / "false" / 0 disable' {
        Assert-True (Test-AllowAdmin ([pscustomobject]@{})); Assert-True (Test-AllowAdmin $null); Assert-True (Test-AllowAdmin ([pscustomobject]@{ allow_admin = $true }))
        Assert-True (-not (Test-AllowAdmin ([pscustomobject]@{ allow_admin = $false }))); Assert-True (-not (Test-AllowAdmin ([pscustomobject]@{ allow_admin = 'false' }))); Assert-True (-not (Test-AllowAdmin ([pscustomobject]@{ allow_admin = 0 })))
    }
    Test-Case 'lanes: job ids used in sys-<id> dirs are path-safe' {
        Assert-True (Test-SafeJobId '18bcfe568000fa9c9'); Assert-True (Test-SafeJobId 'job-0001')
        Assert-True (-not (Test-SafeJobId '..\x')); Assert-True (-not (Test-SafeJobId 'a b')); Assert-True (-not (Test-SafeJobId '')); Assert-True (-not (Test-SafeJobId 'a"&calc'))
    }
    Test-Case 'lanes: system-lane report body is minimal (no metrics/procs/logs/info)' {
        $b = New-SystemLaneReport -NodeId 'n' -Capacity 1 -Running @() -Boot $true -AgentVersion '2.1.0' -BundleVersion '2.1.0' -AgentTime 1700000000.25
        Assert-Equal @('agent_time', 'agent_version', 'boot', 'bundle_version', 'capacity', 'lane', 'node_id', 'running') @($b.Keys | Sort-Object)
        Assert-Equal 'system' $b.lane; Assert-Equal 1700000000.25 $b.agent_time
        Assert-True ((ConvertTo-Json -InputObject $b -Compress) -match '"running":\[\]') 'running must be an (empty) array'
        $b = New-SystemLaneReport -NodeId 'n' -Capacity 0 -Running @('sys-1') -Boot $false -AgentVersion 'x' -BundleVersion 'x'
        Assert-True ((ConvertTo-Json -InputObject $b -Compress) -match '"running":\["sys-1"\]') 'one running id must stay a JSON array'
    }

    # ---- jobs: timeout, status lines, orphan re-attach (pure parts) ----
    Test-Case 'jobs: timeout_s honored (default 7200); timed out at >= timeout' {
        Assert-Equal 7200 (Get-JobTimeoutSec ([pscustomobject]@{ id = 'a' })); Assert-Equal 30 (Get-JobTimeoutSec @{ timeout_s = 30 })
        Assert-Equal 90 (Get-JobTimeoutSec @{ timeout_s = '90.5' }); Assert-Equal 7200 (Get-JobTimeoutSec @{ timeout_s = 0 }); Assert-Equal 7200 (Get-JobTimeoutSec @{ timeout_s = 'x' })
        Assert-True (Test-JobTimedOut 100 130 30); Assert-True (-not (Test-JobTimedOut 100 129 30)); Assert-True (-not (Test-JobTimedOut 0 1e9 30))
    }
    Test-Case 'jobs: agent status lines of brief jobs carry the [forge] tag the hub brief filter drops' {
        Assert-Equal "[forge] [n1] ollama started in C:\w`n" (Format-AgentJobLine ([pscustomobject]@{ source = 'brief' }) 'n1' 'ollama started in C:\w')
        Assert-Equal "[n1] shell started`n" (Format-AgentJobLine ([pscustomobject]@{ source = 'ui' }) 'n1' 'shell started')
        $noise = '^\s*(pulling |verifying |writing manifest|success\b|\[ollama\]|\[forge\])'   # hub BRIEF_NOISE
        $out = (Format-AgentJobLine @{ source = 'brief' } 'nova' 'ollama (model qwen2.5:14b) started in C:\forge\scratch\j1') + "[ollama] generating`nThe brief.`n"
        Assert-Equal @('The brief.') @($out -split "`n" | Where-Object { $_.Trim() -and $_ -notmatch $noise })
    }
    Test-Case 'jobs: running records keep no paths; re-attach only when pid AND command line name the job dir' {
        $run = @{ 'j1' = @{ pid = 11; engine = 'claude'; offset = 120; erroffset = 0; err = $null; started = 5.5; timeout_s = 60; source = 'brief'; resume_tool = $null; resume_id = $null; cancelled = $false; out = 'C:\x\out.log' } }
        $recs = ConvertTo-RunningRecords $run
        Assert-Equal 1 @($recs).Count; Assert-Equal 'j1' $recs[0].id; Assert-Equal 120 $recs[0].offset; Assert-Equal 'brief' $recs[0].source
        Assert-True (-not $recs[0].Contains('out')) 'paths must not be persisted'
        $j = ConvertTo-Json -InputObject ([ordered]@{ jobs = $recs }) -Depth 5 | ConvertFrom-Json; Assert-True ($j.jobs -is [array]) 'one record must stay an array'
        $recs2 = @([pscustomobject]@{ id = 'j1'; pid = 11 }, [pscustomobject]@{ id = 'j2'; pid = 12 }, [pscustomobject]@{ id = 'j3'; pid = 13 },
                   [pscustomobject]@{ id = '..\evil'; pid = 14 }, [pscustomobject]@{ id = 'j5'; pid = 15 })
        $procs = @{ 11 = @{ pid = 11; cmd = 'cmd.exe /s /c ""C:\t\claude.exe" -p < "C:\W\JOBS\j1\prompt.txt" > "C:\W\jobs\j1\out.log" 2>&1"' }
                    12 = @{ pid = 12; cmd = 'C:\Windows\notepad.exe' }                                   # pid reused by something else
                    14 = @{ pid = 14; cmd = 'cmd.exe C:\w\jobs\..\evil\x' }
                    15 = @{ pid = 15; cmd = 'cmd.exe "C:\w\jobs\j55\out.log"' } }                        # other job's dir (prefix)
        $plan = Select-ReattachJobs -Records $recs2 -Procs $procs -JobsDir 'C:\w\jobs'
        Assert-Equal @('j1') @($plan.attach | ForEach-Object { $_.id }); Assert-Equal @('j2', 'j3', 'j5') @($plan.gone | ForEach-Object { $_.id })
        $p2 = Select-ReattachJobs -Records @([pscustomobject]@{ id = 'a1'; pid = 7 }) -Procs @{ 7 = @{ pid = 7; cmd = 'powershell.exe -File "C:\w\jobs\sys-a1\task.ps1"' } } -JobsDir 'C:\w\jobs' -Lane system
        Assert-Equal @('a1') @($p2.attach | ForEach-Object { $_.id })
    }
}

# ---------------- 1a. 2.2: sessions, transcripts, jobs, services (agent\sessions.ps1, services.ps1) ----------------
Write-Host 'agent 2.2'
New-Item -ItemType Directory -Force $Scratch | Out-Null
& {
    . (Join-Path $Repo 'agent\forge-agent.ps1') -NoRun
    $fx = Join-Path $PSScriptRoot 'fixtures\sessions'
    $claudeFile = Join-Path $fx 'claude\projects\C--Users-dev-proj\11111111-2222-4333-8444-555555555555.jsonl'
    $codexFile = Join-Path $fx 'codex\2026\11\20\rollout-2026-11-20T11-00-00-0aaa1111-2222-7333-8444-555555555555.jsonl'
    $legacyFile = Join-Path $fx 'codex\2026\08\08\rollout-2026-08-08T21-10-47-9f0d19f4-0000-4000-8000-000000000001.jsonl'
    $now = ConvertTo-EpochFromDate (Get-Date)
    function Get-Fixture([string]$Path, [string]$Tool) { $m = New-SessionMeta $Tool $Path; $null = Update-SessionMetaFromFile $m 4MB; $m }
    function Get-AllEvents([string]$Tool, [string]$Path) { $evs = New-Object System.Collections.Generic.List[object]; $n = @{}; foreach ($l in (Read-JsonlLines -Path $Path).lines) { foreach ($e in (ConvertFrom-SessionLine $Tool $l.text $n)) { $evs.Add($e) } }; , $evs.ToArray() }

    Test-Case 'redact: spec prefixes (sk-, ghp_, xox, AKIA, eyJ) + 10 chars -> [redacted]; short strings kept' {
        Assert-Equal 'key [redacted] ok' (Protect-SecretText 'key sk-ant-api03-ABCDEFGHIJ ok')
        Assert-Equal '[redacted] [redacted] [redacted] [redacted]' (Protect-SecretText 'ghp_1234567890ab xoxb-1234567890 AKIAABCDEFGHIJKL eyJhbGciOiJIUzI1NiJ9')
        Assert-Equal 'sk-short and task-list' (Protect-SecretText 'sk-short and task-list')
        Assert-Equal '[redacted]' (Protect-SecretText 'github_pat_ABCDEFGHIJ1234')
        Assert-Equal '' (Protect-SecretText $null)
    }
    Test-Case 'redact: PEM keys, URL credentials, Bearer, KEY=value, connection strings (value only)' {
        $pem = "key:`n-----BEGIN OPENSSH PRIVATE KEY-----`nb3BlbnNzaC1rZXktdjEAAAAA`n-----END OPENSSH PRIVATE KEY-----`nafter"
        Assert-Equal "key:`n[redacted private key]`nafter" (Protect-SecretText $pem)
        Assert-Equal 'x [redacted private key]' (Protect-SecretText "x -----BEGIN RSA PRIVATE KEY-----`nMIIEowIBAAKCAQEA (cut")
        Assert-Equal 'postgres://[redacted]@db:5432/app and https://[redacted]@github.com/o/r.git' (Protect-SecretText 'postgres://admin:hunter22@db:5432/app and https://user:ghs_abc123@github.com/o/r.git')
        Assert-Equal 'origin	https://github.com/Example/Repo.git (fetch) http://127.0.0.1:8787/x' (Protect-SecretText 'origin	https://github.com/Example/Repo.git (fetch) http://127.0.0.1:8787/x')
        Assert-Equal 'Authorization: Bearer [redacted]' (Protect-SecretText 'Authorization: Bearer abcdefghijklmnop0123456789')
        Assert-Equal 'PASSWORD=[redacted] AWS_SECRET_ACCESS_KEY=[redacted]' (Protect-SecretText 'PASSWORD=s3cretpass AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG')
        Assert-Equal 'password: [redacted]' (Protect-SecretText 'password: hunter22')
        Assert-Equal '"agent_token": "[redacted]", "input_tokens": 123456' (Protect-SecretText '"agent_token": "Zk3j_x9Qa-pL0", "input_tokens": 123456')
        Assert-Equal 'Server=db;Database=app;User Id=sa;Password=[redacted];' (Protect-SecretText 'Server=db;Database=app;User Id=sa;Password=P@ss-w0rd!;')
        Assert-Equal 'DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=[redacted];EndpointSuffix=core.windows.net' (Protect-SecretText 'DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=abcDEF123+/xyz==;EndpointSuffix=core.windows.net')
        Assert-Equal 'ANTHROPIC_API_KEY=[redacted]' (Protect-SecretText 'ANTHROPIC_API_KEY=sk-ant-api03-ABCDEFGHIJKLMN')
        Assert-Equal 'the token is fine; max_tokens: 4096' (Protect-SecretText 'the token is fine; max_tokens: 4096')
    }
    Test-Case 'title: whitespace collapsed, redacted BEFORE the 140-char cut' {
        Assert-Equal 'a b c' (ConvertTo-SessionTitle "  a`r`n  b`tc ")
        $t = ConvertTo-SessionTitle (('x' * 135) + ' sk-ABCDEFGHIJKLMNOP')
        Assert-True ($t.Length -le 140) "length $($t.Length)"; Assert-True ($t -notmatch 'sk-|ABCDEFG') 'secret fragment survived the cut'
        Assert-Equal $null (ConvertTo-SessionTitle '   ')
    }
    Test-Case 'real prompt: injected context / slash-command wrappers are not titles' {
        Assert-True (Test-RealPrompt 'fix the bug'); Assert-True (-not (Test-RealPrompt '<environment_context>x</environment_context>'))
        Assert-True (-not (Test-RealPrompt '  <command-name>/login</command-name>')); Assert-True (-not (Test-RealPrompt '# AGENTS.md instructions')); Assert-True (-not (Test-RealPrompt ''))
    }
    Test-Case 'json line parser: objects only; malformed / blank / arrays -> $null' {
        Assert-Equal 'x' (ConvertFrom-JsonLine '{"type":"x"}').type
        Assert-True ($null -eq (ConvertFrom-JsonLine '{"broken')); Assert-True ($null -eq (ConvertFrom-JsonLine '')); Assert-True ($null -eq (ConvertFrom-JsonLine '[1,2]'))
    }

    Test-Case 'claude parser (fixture): session metadata' {
        $m = Get-Fixture $claudeFile 'claude'
        Assert-Equal '11111111-2222-4333-8444-555555555555' $m.session_id; Assert-Equal 'C:\Users\dev\proj' $m.cwd; Assert-Equal 'main' $m.branch_hint
        Assert-Equal 'Fix the login bug; my key is [redacted] and it fails' $m.title
        Assert-Equal 5 $m.messages 'two real prompts + three assistant API messages (blocks sharing message.id count once)'
        Assert-Equal 168 $m.tokens 'input + cache_creation + output, once per message id (cache reads excluded)'
        Assert-Equal 'claude-opus-5-5' $m.model; Assert-Equal $false $m.remote_control; Assert-Equal 'Fix login bug' $m.ai_title; Assert-Equal 'cli' $m.entrypoint
        Assert-Equal (ConvertTo-EpochSeconds '2026-11-20T10:00:00.000Z') $m.started; Assert-Equal '2026-11-20T10:01:05.000Z' $m.last_ts_raw
        Assert-True $m.indexed 'not marked indexed'
    }
    Test-Case 'claude parser (fixture): transcript events (meta, sidechain, malformed, bookkeeping skipped)' {
        $e = Get-AllEvents 'claude' $claudeFile
        Assert-Equal 10 $e.Count
        Assert-Equal 'user/text,user/text,assistant/thinking,tool/tool_call,tool/tool_result,assistant/text,system/text,user/text,user/text,assistant/text' (($e | ForEach-Object { "$($_.role)/$($_.kind)" }) -join ',')
        Assert-Equal 'Bash' $e[3].tool_name; Assert-Equal 'command: git status' $e[3].text; Assert-Equal 'Bash' $e[4].tool_name 'tool_result gets the tool name of its tool_use'
        Assert-True ($e[2].collapsed -and $e[3].collapsed -and $e[4].collapsed) 'thinking/tool events must be collapsed'; Assert-True (-not $e[1].collapsed)
        Assert-True (($e | ForEach-Object { $_.text }) -join ' ' -notmatch 'sk-ant|ghp_') 'secret leaked into transcript'
        Assert-Equal '[image]' $e[8].text; Assert-Equal "Docs updated $([char]0x2713)" $e[9].text 'utf-8 decoding'
        Assert-Equal (ConvertTo-EpochSeconds '2026-11-20T10:00:09.000Z') $e[5].ts
    }
    Test-Case 'codex parser (fixture): metadata from session_meta / turn_context / token_count' {
        $m = Get-Fixture $codexFile 'codex'
        Assert-Equal '0aaa1111-2222-7333-8444-555555555555' $m.session_id; Assert-Equal 'C:\Users\dev\proj2' $m.cwd; Assert-Equal 'gpt-5.5-codex' $m.model
        Assert-Equal 'Summarize the repo layout please' $m.title; Assert-Equal 2 $m.messages; Assert-Equal 1234 $m.tokens; Assert-Equal 'codex_exec' $m.entrypoint
    }
    Test-Case 'codex parser (fixture): transcript events (developer/env context/event_msg skipped)' {
        $e = Get-AllEvents 'codex' $codexFile
        Assert-Equal 'user/text,assistant/thinking,tool/tool_call,tool/tool_result,tool/tool_call,tool/tool_result,assistant/text,system/text' (($e | ForEach-Object { "$($_.role)/$($_.kind)" }) -join ',')
        Assert-Equal 'shell_command' $e[2].tool_name; Assert-Equal 'command: Get-ChildItem' $e[2].text; Assert-Equal 'shell_command' $e[3].tool_name
        Assert-Equal 'apply_patch' $e[5].tool_name; Assert-Equal 'Done!' $e[5].text
        Assert-True ($e[6].text -match '\[redacted\]' -and $e[6].text -notmatch 'AKIA') 'AKIA key not redacted'
    }
    Test-Case 'codex parser (legacy 2025 format fixture): header id, bare message records, <cwd> from env context' {
        $m = Get-Fixture $legacyFile 'codex'
        Assert-Equal '9f0d19f4-0000-4000-8000-000000000001' $m.session_id; Assert-Equal 'C:\Users\dev\Temp' $m.cwd
        Assert-Equal 'Reply with exactly: OK' $m.title; Assert-Equal 2 $m.messages; Assert-Equal (ConvertTo-EpochSeconds '2026-08-08T21:10:47.000Z') $m.started
        $le = Get-AllEvents 'codex' $legacyFile; Assert-Equal 'user/text,assistant/text' (($le | ForEach-Object { "$($_.role)/$($_.kind)" }) -join ',')
    }

    Test-Case 'transcript: since=null -> last N events, cursor = end of file, at_end' {
        $size = (Get-Item $claudeFile).Length
        $b = Get-TranscriptBatch -Tool claude -Path $claudeFile -Since $null
        Assert-Equal 10 @($b.events).Count; Assert-Equal $size $b.cursor; Assert-True $b.at_end
        $b = Get-TranscriptBatch -Tool claude -Path $claudeFile -Since $null -Max 3
        Assert-Equal 'user/text,user/text,assistant/text' ((@($b.events) | ForEach-Object { "$($_.role)/$($_.kind)" }) -join ',') 'expected the LAST 3 events'
    }
    Test-Case 'transcript: since=cursor pages forward, max N per call, no gaps or repeats' {
        $all = Get-AllEvents 'claude' $claudeFile
        $got = New-Object System.Collections.Generic.List[string]; $cur = 0; $calls = 0; $names = @{}
        do {
            $b = Get-TranscriptBatch -Tool claude -Path $claudeFile -Since $cur -Max 3 -ToolNames $names
            Assert-True (@($b.events).Count -le 3) 'more than Max events'
            foreach ($e in @($b.events)) { $got.Add("$($e.kind):$($e.text)") }
            Assert-True ($b.cursor -gt $cur -or $b.at_end) 'cursor did not advance'
            $cur = $b.cursor; $calls++
        } until ($b.at_end -or $calls -gt 20)
        Assert-Equal (@($all | ForEach-Object { "$($_.kind):$($_.text)" })) @($got) 'paged events differ from a full parse'
        Assert-True ($calls -ge 4) "expected several pages, got $calls"
        $b = Get-TranscriptBatch -Tool claude -Path $claudeFile -Since $cur
        Assert-Equal 0 @($b.events).Count; Assert-Equal $cur $b.cursor; Assert-True $b.at_end
    }
    Test-Case 'transcript: tail walks back in small chunks, each byte parsed once, same events as a full parse' {
        $all = Get-AllEvents 'claude' $claudeFile
        foreach ($tb in 300, 700, 1500) {
            $b = Get-TranscriptBatch -Tool claude -Path $claudeFile -Since $null -TailBytes $tb
            Assert-Equal (@($all | ForEach-Object { "$($_.kind):$($_.text)" })) (@(@($b.events) | ForEach-Object { "$($_.kind):$($_.text)" })) "TailBytes $tb"
            Assert-Equal (Get-Item $claudeFile).Length $b.cursor
        }
        $b = Get-TranscriptBatch -Tool claude -Path $claudeFile -Since $null -TailBytes 700 -MaxTailBytes 2100
        Assert-True (@($b.events).Count -lt 10 -and @($b.events).Count -ge 1) "MaxTailBytes cap ignored ($(@($b.events).Count))"
    }
    Test-Case 'transcript: a cursor beyond the file (truncated / rewritten) restarts from the tail' {
        $b = Get-TranscriptBatch -Tool claude -Path $claudeFile -Since 99999999
        Assert-Equal 10 @($b.events).Count
    }

    # ---- transcript step (main-loop logic) with a stubbed sender ----
    $txSid = '11111111-2222-4333-8444-555555555555'
    $txFile = Join-Path $Scratch 'tx-session.jsonl'
    function New-TxSess { $ss = New-SessionIndexState; $m = New-SessionMeta 'claude' $txFile; $m.session_id = $txSid; $m.mtime = 1000.0; $ss.files[$txFile] = $m; $ss }
    Test-Case 'transcript step: a stale watch since (report failed / hub switch) never resends a posted batch' {
        Copy-Item $claudeFile $txFile -Force
        $ss = New-TxSess; $tx = New-TranscriptState; $script:posts = New-Object System.Collections.Generic.List[object]
        $send = { param($P, $B) $script:posts.Add($B); $true }
        Invoke-TranscriptStep -Watches @(@{ tool = 'claude'; session_id = $txSid; since = $null }) -TxState $tx -SessState $ss -NodeId 'n1' -Now 1001 -Send $send
        Assert-Equal 1 $script:posts.Count; $c0 = [long]$script:posts[0].cursor; Assert-Equal 10 @($script:posts[0].events).Count
        [IO.File]::AppendAllText($txFile, '{"type":"user","message":{"role":"user","content":"next question"},"timestamp":"2026-11-20T10:03:00.000Z","sessionId":"x"}' + "`n")
        Invoke-TranscriptStep -Watches @(@{ tool = 'claude'; session_id = $txSid; since = $c0 }) -TxState $tx -SessState $ss -NodeId 'n1' -Now 1002 -Send $send
        Assert-Equal 2 $script:posts.Count; Assert-Equal 'next question' @($script:posts[1].events)[0].text; $c1 = [long]$script:posts[1].cursor
        # the hub never stored batch 2 in time (report failed, hub switched): the watch still says since=c0
        Invoke-TranscriptStep -Watches @(@{ tool = 'claude'; session_id = $txSid; since = $c0 }) -TxState $tx -SessState $ss -NodeId 'n1' -Now 1003 -Send $send
        Assert-Equal 2 $script:posts.Count 'batch from the same since was posted twice'
        [IO.File]::AppendAllText($txFile, '{"type":"user","message":{"role":"user","content":"third"},"timestamp":"2026-11-20T10:04:00.000Z","sessionId":"x"}' + "`n")
        Invoke-TranscriptStep -Watches @(@{ tool = 'claude'; session_id = $txSid; since = $c0 }) -TxState $tx -SessState $ss -NodeId 'n1' -Now 1004 -Send $send
        Assert-Equal 3 $script:posts.Count; Assert-Equal @('third') @(@($script:posts[2].events) | ForEach-Object { $_.text }) 'continues after the last posted batch'
        Assert-True ([long]$script:posts[2].cursor -gt $c1)
        # a failed POST is not remembered: the same since is retried
        $tx2 = New-TranscriptState; $script:n = 0
        $fail = { param($P, $B) $script:n++; $false }
        Invoke-TranscriptStep -Watches @(@{ tool = 'claude'; session_id = $txSid; since = $c0 }) -TxState $tx2 -SessState $ss -NodeId 'n1' -Now 1005 -Send $fail
        Invoke-TranscriptStep -Watches @(@{ tool = 'claude'; session_id = $txSid; since = $c0 }) -TxState $tx2 -SessState $ss -NodeId 'n1' -Now 1006 -Send $fail
        Assert-Equal 2 $script:n
    }
    Test-Case 'transcript step: missing session reported at most once per 60 s, with error' {
        $ss = New-TxSess; $tx = New-TranscriptState; $script:posts = New-Object System.Collections.Generic.List[object]
        $send = { param($P, $B) $script:posts.Add($B); $true }
        $w = @(@{ tool = 'claude'; session_id = 'deadbeef-0000-4000-8000-000000000000'; since = 5 })
        foreach ($t in 100, 130, 159) { Invoke-TranscriptStep -Watches $w -TxState $tx -SessState $ss -NodeId 'n1' -Now $t -Send $send }
        Assert-Equal 1 $script:posts.Count; Assert-Equal 'session not found on this node' $script:posts[0].error; Assert-Equal $true $script:posts[0].done
        Invoke-TranscriptStep -Watches $w -TxState $tx -SessState $ss -NodeId 'n1' -Now 160 -Send $send
        Assert-Equal 2 $script:posts.Count
    }
    Test-Case 'transcript step: more than 4 watches rotate (the 5th+ are served); one tail per cycle' {
        $ws = @(1..6 | ForEach-Object { @{ tool = 'claude'; session_id = "s$_"; since = 1 } })
        $a = Select-RotatingWatches -Watches $ws -Offset 0; Assert-Equal @('s1', 's2', 's3', 's4') @($a.items | ForEach-Object { $_.session_id }); Assert-Equal 4 $a.next
        $b = Select-RotatingWatches -Watches $ws -Offset $a.next; Assert-Equal @('s5', 's6', 's1', 's2') @($b.items | ForEach-Object { $_.session_id })
        $c = Select-RotatingWatches -Watches $ws[0..1] -Offset 7; Assert-Equal 2 @($c.items).Count
        Assert-Equal 0 @((Select-RotatingWatches -Watches @()).items).Count
        $ss = New-TxSess; $tx = New-TranscriptState; $script:posts = New-Object System.Collections.Generic.List[object]
        $send = { param($P, $B) $script:posts.Add($B); $true }
        $tails = @(1..5 | ForEach-Object { @{ tool = 'claude'; session_id = "0000000$_-0000-4000-8000-000000000000"; since = $null } })
        Invoke-TranscriptStep -Watches $tails -TxState $tx -SessState $ss -NodeId 'n1' -Now 100 -Send $send
        Invoke-TranscriptStep -Watches $tails -TxState $tx -SessState $ss -NodeId 'n1' -Now 101 -Send $send
        Assert-Equal @('00000001-0000-4000-8000-000000000000', '00000005-0000-4000-8000-000000000000') @($script:posts | ForEach-Object { $_.session_id }) 'one tail per cycle, rotating'
    }

    $sdir = Join-Path $Scratch 'sess'; $cRoot = Join-Path $sdir 'claude'; $xRoot = Join-Path $sdir 'codex'
    New-Item -ItemType Directory -Force (Join-Path $cRoot 'C--Users-dev-proj'), (Join-Path $xRoot '2026\11\20') | Out-Null
    $c1 = Join-Path $cRoot 'C--Users-dev-proj\11111111-2222-4333-8444-555555555555.jsonl'; Copy-Item $claudeFile $c1
    Copy-Item $codexFile (Join-Path $xRoot '2026\11\20')
    (Get-Item $c1).LastWriteTime = Get-Date; (Get-Item (Join-Path $xRoot '2026\11\20\*.jsonl')).LastWriteTime = Get-Date
    New-Item -ItemType Directory -Force (Join-Path $cRoot 'C--Users-dev-proj\11111111-2222-4333-8444-555555555555\subagents') | Out-Null
    Copy-Item $claudeFile (Join-Path $cRoot 'C--Users-dev-proj\11111111-2222-4333-8444-555555555555\subagents\agent-a1.jsonl')
    $st = New-SessionIndexState
    Test-Case 'index cycle: claude top-level + codex recursive; subagent files ignored; every session sent once' {
        $c = Invoke-SessionIndexCycle -State $st -NodeId 'n1' -ClaudeRoot $cRoot -CodexRoot $xRoot -Now $now
        Assert-Equal 2 @($c.sessions).Count; Assert-True $c.full
        Assert-Equal @('claude', 'codex') @(@($c.sessions) | ForEach-Object { $_.tool } | Sort-Object)
        $s = @($c.sessions) | Where-Object { $_.tool -eq 'claude' }
        Assert-Equal @('tool', 'session_id', 'node_id', 'cwd', 'git_remote', 'git_branch', 'title', 'started', 'last_activity', 'messages', 'tokens', 'state', 'remote_control', 'pid', 'model', 'ai_title', 'entrypoint') @($s.Keys)
        Assert-Equal 'n1' $s.node_id; Assert-Equal 'live' $s.state 'just copied = written < 90 s ago'
        Confirm-SessionsSent $st $c $now
    }
    Test-Case 'index cycle: unchanged -> nothing sent; appended line -> only that session, parsed incrementally' {
        $c = Invoke-SessionIndexCycle -State $st -NodeId 'n1' -ClaudeRoot $cRoot -CodexRoot $xRoot -Now ($now + 1)
        Assert-Equal 0 @($c.sessions).Count
        $off = $st.files[$c1].offset
        [IO.File]::AppendAllText($c1, '{"type":"user","message":{"role":"user","content":"one more thing"},"timestamp":"2026-11-20T10:02:00.000Z","sessionId":"x"}' + "`n")
        (Get-Item $c1).LastWriteTime = (Get-Date)
        $c = Invoke-SessionIndexCycle -State $st -NodeId 'n1' -ClaudeRoot $cRoot -CodexRoot $xRoot -Now ($now + 2)
        Assert-Equal 1 @($c.sessions).Count; Assert-Equal 6 @($c.sessions)[0].messages
        Assert-True ($st.files[$c1].offset -gt $off) 'offset did not advance'
        Confirm-SessionsSent $st $c ($now + 2)
    }
    Test-Case 'index cycle: state transitions are sent (live -> idle -> done)' {
        $c = Invoke-SessionIndexCycle -State $st -NodeId 'n1' -ClaudeRoot $cRoot -CodexRoot $xRoot -Now ($now + 3600)
        Assert-Equal @('idle', 'idle') @(@($c.sessions) | ForEach-Object { $_.state }); Confirm-SessionsSent $st $c ($now + 3600)
    }
    Test-Case 'index cycle: forced session (after a resume job) is resent even if unchanged' {
        $st.force['0aaa1111-2222-7333-8444-555555555555'] = $true
        $c = Invoke-SessionIndexCycle -State $st -NodeId 'n1' -ClaudeRoot $cRoot -CodexRoot $xRoot -Now ($now + 3601)
        Assert-Equal @('codex') @(@($c.sessions) | ForEach-Object { $_.tool }); Confirm-SessionsSent $st $c ($now + 3601)
        Assert-True (-not $st.force.ContainsKey('0aaa1111-2222-7333-8444-555555555555'))
    }
    Test-Case 'index cycle: deleted file -> removed id; byte budget defers work to the next cycle' {
        Remove-Item $c1 -Force
        $c = Invoke-SessionIndexCycle -State $st -NodeId 'n1' -ClaudeRoot $cRoot -CodexRoot $xRoot -Now ($now + 3602)
        Assert-Equal @('11111111-2222-4333-8444-555555555555') @($c.removed); Confirm-SessionsSent $st $c ($now + 3602)
        $st2 = New-SessionIndexState
        $c = Invoke-SessionIndexCycle -State $st2 -NodeId 'n1' -ClaudeRoot $cRoot -CodexRoot $xRoot -Now $now -BudgetBytes 600
        Assert-Equal 0 @($c.sessions).Count 'a half-parsed file must not be sent'
        for ($i = 0; $i -lt 30 -and -not @($c.sessions).Count; $i++) { $c = Invoke-SessionIndexCycle -State $st2 -NodeId 'n1' -ClaudeRoot $cRoot -CodexRoot $xRoot -Now $now -BudgetBytes 600 }
        Assert-Equal 2 @($c.sessions)[0].messages 'complete after several budgeted cycles'
    }
    Test-Case 'jsonl reader: partial trailing line waits; complete JSON tail accepted; oversized line skipped' {
        $f = Join-Path $Scratch 'p.jsonl'
        [IO.File]::WriteAllText($f, "{`"a`":1}`n{`"b`":")
        $r = Read-JsonlLines -Path $f -AcceptPartialTail; Assert-Equal 1 $r.lines.Count; Assert-Equal 8 $r.next
        [IO.File]::AppendAllText($f, '2}'); $r = Read-JsonlLines -Path $f -Offset 8 -AcceptPartialTail; Assert-Equal 1 $r.lines.Count; Assert-Equal 15 $r.next
        [IO.File]::WriteAllText($f, ('{"big":"' + ('z' * 5000) + '"}' + "`n" + '{"c":3}' + "`n"))
        $r = Read-JsonlLines -Path $f -MaxBytes 1000; Assert-Equal 0 $r.lines.Count; Assert-Equal 0 $r.next 'budget window: no skip, retry later'
        $r = Read-JsonlLines -Path $f -MaxBytes 1000 -SkipOversizeAt 1000; Assert-Equal 0 $r.lines.Count; Assert-Equal 5011 $r.next; Assert-Equal 1 $r.skipped
        $r = Read-JsonlLines -Path $f -Offset $r.next -MaxBytes 1000; Assert-Equal '{"c":3}' $r.lines[0].text
        $f2 = Join-Path $Scratch 'crlf.jsonl'; [IO.File]::WriteAllText($f2, "{`"a`":1}`r`n`r`n{`"b`":2}`r`n")
        $r = Read-JsonlLines -Path $f2; Assert-Equal @('{"a":1}', '{"b":2}') @($r.lines | ForEach-Object { $_.text })
    }

    Test-Case 'session state: live < 90 s or owning process alive; idle < 24 h; done older' {
        Assert-Equal 'live' (Get-SessionState -LastWrite 1000 -Now 1089); Assert-Equal 'idle' (Get-SessionState -LastWrite 1000 -Now 1090)
        Assert-Equal 'live' (Get-SessionState -LastWrite 0 -Now 999999 -ProcAlive $true)
        Assert-Equal 'idle' (Get-SessionState -LastWrite 0 -Now 86399); Assert-Equal 'done' (Get-SessionState -LastWrite 0 -Now 86400)
    }
    Test-Case 'process kind: CLI claude/codex vs desktop apps' {
        Assert-Equal 'claude' (Get-AiProcessKind 'claude.exe' '"C:\Users\u\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe" --dangerously-skip-permissions')
        Assert-Equal $null (Get-AiProcessKind 'claude.exe' '"C:\Program Files\WindowsApps\Claude_6.2_x64__abc\app\claude.exe"')
        Assert-Equal $null (Get-AiProcessKind 'claude.exe' '"C:\x\claude.exe" --type=renderer --user-data-dir=C:\x')
        Assert-Equal 'claude' (Get-AiProcessKind 'node.exe' 'node C:\npm\node_modules\@anthropic-ai\claude-code\cli.js --resume 1')
        Assert-Equal 'codex' (Get-AiProcessKind 'codex.exe' 'codex.exe exec resume 0aaa1111-2222-7333-8444-555555555555 -')
        Assert-Equal $null (Get-AiProcessKind 'codex.exe' '"C:\Program Files\WindowsApps\OpenAI.Codex_26.1\app\resources\codex.exe" app-server')
        Assert-Equal $null (Get-AiProcessKind 'node.exe' 'node desktop-commander\dist\index.js'); Assert-Equal $null (Get-AiProcessKind 'python.exe' 'python x')
    }
    Test-Case 'process match: session id from --resume / --session-id / -r / codex resume' {
        $id = '11111111-2222-4333-8444-555555555555'
        Assert-Equal $id (Get-SessionIdFromCmd "claude.exe -p --resume $id --permission-mode default")
        Assert-Equal $id (Get-SessionIdFromCmd "claude.exe --session-id=$id"); Assert-Equal $id (Get-SessionIdFromCmd "claude -r `"$($id.ToUpper())`"")
        Assert-Equal $id (Get-SessionIdFromCmd "codex.exe exec --skip-git-repo-check --sandbox workspace-write resume $id -")
        Assert-Equal $null (Get-SessionIdFromCmd 'codex.exe exec resume --last'); Assert-Equal $null (Get-SessionIdFromCmd 'claude.exe --dangerously-skip-permissions')
    }
    Test-Case 'process match: pid map (start time must match), then command line, then newest file since process start' {
        $procs = @(@{ pid = 100; kind = 'claude'; cmd = 'claude.exe'; started = 1000.0 }, @{ pid = 200; kind = 'codex'; cmd = 'codex.exe'; started = 5000.0 },
                   @{ pid = 300; kind = 'claude'; cmd = 'claude.exe -p --resume cccccccc-0000-4000-8000-000000000003'; started = 6000.0 })
        $sess = @(@{ tool = 'claude'; session_id = 'aaaaaaaa-0000-4000-8000-000000000001'; last_write = 9000.0 },
                  @{ tool = 'codex'; session_id = 'bbbbbbbb-0000-4000-8000-000000000002'; last_write = 4000.0 },
                  @{ tool = 'codex'; session_id = 'dddddddd-0000-4000-8000-000000000004'; last_write = 7000.0 },
                  @{ tool = 'codex'; session_id = 'eeeeeeee-0000-4000-8000-000000000005'; last_write = 8000.0 })
        $map = @(@{ pid = 100; session_id = 'aaaaaaaa-0000-4000-8000-000000000001'; proc_start = 1001.0 })
        $r = Find-SessionPids -Procs $procs -Sessions $sess -PidMap $map
        Assert-Equal 100 $r['aaaaaaaa-0000-4000-8000-000000000001']; Assert-Equal 300 $r['cccccccc-0000-4000-8000-000000000003']
        Assert-Equal 200 $r['eeeeeeee-0000-4000-8000-000000000005'] 'codex fallback: newest codex file written since it started'
        Assert-True (-not $r.ContainsKey('bbbbbbbb-0000-4000-8000-000000000002')) 'file older than the process matched'
        Assert-True (-not $r.ContainsKey('dddddddd-0000-4000-8000-000000000004')) 'one process may own one session only'
        $r = Find-SessionPids -Procs $procs[0..0] -Sessions $sess[0..0] -PidMap @(@{ pid = 100; session_id = 'zzzzzzzz-0000-4000-8000-000000000009'; proc_start = 50.0 })
        Assert-True (-not $r.ContainsKey('zzzzzzzz-0000-4000-8000-000000000009')) 'pid reuse: stale pid file trusted'
        Assert-Equal 100 $r['aaaaaaaa-0000-4000-8000-000000000001'] 'then falls back to newest file'
    }
    Test-Case 'process match: a terminal codex never claims a finished `codex exec` job session (keeps the 409 guard)' {
        # terminal `codex` A since 09:00 on session X (last write 09:05); a Forge `codex exec` wrote Z 09:10-09:12 and exited
        $procs = @(@{ pid = 500; kind = 'codex'; cmd = '"C:\npm\codex.exe"'; started = 32400.0 })
        $sess = @(@{ tool = 'codex'; session_id = 'xxxxxxxx-0000-4000-8000-00000000000a'; last_write = 32700.0; started = 32401.0; entrypoint = 'codex_cli_rs' },
                  @{ tool = 'codex'; session_id = 'zzzzzzzz-0000-4000-8000-00000000000b'; last_write = 33120.0; started = 33000.0; entrypoint = 'codex_exec' })
        $r = Find-SessionPids -Procs $procs -Sessions $sess
        Assert-Equal 500 $r['xxxxxxxx-0000-4000-8000-00000000000a']; Assert-True (-not $r.ContainsKey('zzzzzzzz-0000-4000-8000-00000000000b')) 'interactive process claimed a batch session'
        # a running `codex exec` (no id on its command line) gets the exec session, not the terminal one
        $r = Find-SessionPids -Procs @(@{ pid = 600; kind = 'codex'; cmd = 'codex.exe exec --skip-git-repo-check --sandbox workspace-write -'; started = 32990.0 }) -Sessions $sess
        Assert-Equal 600 $r['zzzzzzzz-0000-4000-8000-00000000000b']; Assert-True (-not $r.ContainsKey('xxxxxxxx-0000-4000-8000-00000000000a'))
        # claude: -p processes only match sdk sessions; sessions started after the process beat ones only written later
        Assert-True (Test-BatchProcess 'claude' 'claude.exe -p --output-format text'); Assert-True (-not (Test-BatchProcess 'claude' 'claude.exe --dangerously-skip-permissions'))
        Assert-True (Test-BatchSession 'sdk-cli'); Assert-True (Test-BatchSession 'codex_exec'); Assert-True (-not (Test-BatchSession 'cli')); Assert-True (-not (Test-BatchSession $null))
        $s2 = @(@{ tool = 'claude'; session_id = 'old00000-0000-4000-8000-000000000001'; last_write = 2000.0; started = 100.0; entrypoint = 'cli' },
                @{ tool = 'claude'; session_id = 'new00000-0000-4000-8000-000000000002'; last_write = 1500.0; started = 1001.0; entrypoint = 'cli' })
        $r = Find-SessionPids -Procs @(@{ pid = 700; kind = 'claude'; cmd = 'claude.exe'; started = 1000.0 }) -Sessions $s2
        Assert-Equal 700 $r['new00000-0000-4000-8000-000000000002'] 'a session started by this process wins over an older one written later'
    }
    Test-Case 'process match: ~/.claude/sessions/<pid>.json map (procStart FILETIME -> epoch)' {
        $d = Join-Path $Scratch 'pids'; New-Item -ItemType Directory -Force $d | Out-Null
        $ft = [DateTime]::new(2026, 11, 24, 23, 21, 51, [DateTimeKind]::Utc).ToFileTimeUtc()
        [IO.File]::WriteAllText((Join-Path $d '4242.json'), "{`"pid`":4242,`"sessionId`":`"AAAAAAAA-0000-4000-8000-000000000001`",`"cwd`":`"C:\\x`",`"procStart`":$ft,`"status`":`"busy`"}")
        [IO.File]::WriteAllText((Join-Path $d '4242.abc.key'), 'not json'); [IO.File]::WriteAllText((Join-Path $d 'junk.json'), '{')
        $m = Read-ClaudePidMap $d
        Assert-Equal 1 @($m).Count; Assert-Equal 4242 $m[0].pid; Assert-Equal 'aaaaaaaa-0000-4000-8000-000000000001' $m[0].session_id
        Assert-Equal ([double][DateTimeOffset]::new(2026, 11, 24, 23, 21, 51, [TimeSpan]::Zero).ToUnixTimeSeconds()) $m[0].proc_start
    }
    Test-Case 'git: status header -> branch/ahead/behind/dirty; log lines -> commits (subjects redacted)' {
        $s = ConvertFrom-GitStatus @('## main...origin/main [ahead 2, behind 1]', ' M a.txt'); Assert-Equal 'main' $s.branch; Assert-Equal 2 $s.ahead; Assert-Equal 1 $s.behind; Assert-True $s.dirty
        $s = ConvertFrom-GitStatus @('## feature/x.y...origin/feature/x.y'); Assert-Equal 'feature/x.y' $s.branch; Assert-True (-not $s.dirty); Assert-Equal 0 $s.ahead
        Assert-Equal 'dev' (ConvertFrom-GitStatus @('## dev')).branch; Assert-Equal 'main' (ConvertFrom-GitStatus @('## No commits yet on main')).branch
        $us = [char]0x1f
        $c = ConvertFrom-GitLog @("abc1234def${us}Dev Person${us}1795000000${us}Fix sk-ABCDEFGHIJKLMNOP leak", '', 'garbage')
        Assert-Equal 1 @($c).Count; Assert-Equal 'abc1234def' $c[0].sha; Assert-Equal 1795000000 $c[0].ts; Assert-Equal 'Fix [redacted] leak' $c[0].subject; Assert-Equal 'Dev Person' $c[0].author
    }
    Test-Case 't3: listing supported only if `t3 project --help` lists a list subcommand' {
        $no = "DESCRIPTION`n  Manage projects.`n`nSUBCOMMANDS`n  add       Add a project.`n  remove    Remove a project.`n  rename    Rename a project."
        Assert-True (-not (Test-T3ListSupport $no)); Assert-True (Test-T3ListSupport ($no + "`n  list      List projects.")); Assert-True (-not (Test-T3ListSupport ''))
    }

    # ---- job command lines (SPEC D / F) ----
    $tools = @{ claude = 'C:\t\claude.exe'; codex = 'C:\t\codex.cmd' }
    $pf = 'C:\w\jobs\j1\prompt.txt'
    function Get-Cmd($Job, $Config = ([pscustomobject]@{}), $T = $tools) { Get-JobCommand -Job ([pscustomobject]$Job) -Tools $T -Config $Config -PromptFile $pf -JobDir 'C:\w\jobs\j1' -AgentDir 'C:\F\agent' -OutFile 'C:\w\jobs\j1\out.log' }
    $clr = 'set "ANTHROPIC_API_KEY=" && set "ANTHROPIC_AUTH_TOKEN=" && '
    Test-Case 'job cmd: defaults claude acceptEdits / codex workspace-write; full_access bypasses' {
        Assert-Equal "$clr`"C:\t\claude.exe`" -p --permission-mode acceptEdits --output-format text < `"$pf`"" (Get-Cmd @{ engine = 'claude' }).inner
        Assert-Equal "`"C:\t\codex.cmd`" exec --skip-git-repo-check --sandbox workspace-write - < `"$pf`"" (Get-Cmd @{ engine = 'codex' }).inner
        Assert-Equal "`"C:\t\codex.cmd`" exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox - < `"$pf`"" (Get-Cmd @{ engine = 'codex'; full_access = $true }).inner
        $s = Get-Cmd @{ engine = 'shell' }; Assert-Equal 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\w\jobs\j1\task.ps1"' $s.inner; Assert-Equal 'ps1' $s.write; Assert-Equal 'out' $s.redirect
    }
    Test-Case 'job cmd: claude resume = -p --resume <id> --permission-mode <mode> --output-format text < prompt (API key still cleared)' {
        $c = Get-Cmd @{ engine = 'claude'; full_access = $true; resume = [pscustomobject]@{ tool = 'claude'; session_id = '11111111-2222-4333-8444-555555555555' } }
        Assert-Equal "$clr`"C:\t\claude.exe`" -p --resume 11111111-2222-4333-8444-555555555555 --permission-mode bypassPermissions --output-format text < `"$pf`"" $c.inner
        Assert-Equal '11111111-2222-4333-8444-555555555555' $c.resume_id
        $c = Get-Cmd @{ engine = 'claude'; resume = @{ tool = 'claude'; session_id = '11111111-2222-4333-8444-555555555555' } } ([pscustomobject]@{ claude_use_api_key = $true })
        Assert-True (-not $c.inner.StartsWith('set ')) 'claude_use_api_key keeps the key'
        Assert-Equal "$clr`"C:\t\claude.exe`" -p --permission-mode acceptEdits --output-format text --model haiku < `"$pf`"" (Get-Cmd @{ engine = 'claude'; model = 'haiku' }).inner
    }
    Test-Case 'job cmd: codex resume = exec <sandbox flags> resume <id> - < prompt (flags before the subcommand)' {
        Assert-Equal "`"C:\t\codex.cmd`" exec --skip-git-repo-check --sandbox workspace-write resume 0aaa1111-2222-7333-8444-555555555555 - < `"$pf`"" (Get-Cmd @{ engine = 'codex'; resume = @{ tool = 'codex'; session_id = '0AAA1111-2222-7333-8444-555555555555' } }).inner
        Assert-Equal "`"C:\t\codex.cmd`" exec --skip-git-repo-check --sandbox workspace-write -m gpt-5.5 resume 0aaa1111-2222-7333-8444-555555555555 - < `"$pf`"" (Get-Cmd @{ engine = 'codex'; model = 'gpt-5.5'; resume = @{ tool = 'codex'; session_id = '0aaa1111-2222-7333-8444-555555555555' } }).inner
    }
    Test-Case 'job cmd: refuses unsafe resume ids / model names / mismatched tools / missing engines' {
        Assert-Equal 'bad resume session id' (Get-Cmd @{ engine = 'claude'; resume = @{ tool = 'claude'; session_id = 'x" & calc & "' } }).error
        Assert-True ((Get-Cmd @{ engine = 'claude'; model = 'haiku && calc' }).error -like 'bad model name*')
        Assert-True ((Get-Cmd @{ engine = 'claude'; resume = @{ tool = 'codex'; session_id = '11111111-2222-4333-8444-555555555555' } }).error -like '*does not match*')
        Assert-True ((Get-Cmd @{ engine = 'shell'; resume = @{ tool = 'shell'; session_id = '11111111-2222-4333-8444-555555555555' } }).error -like '*cannot resume*')
        Assert-Equal "engine 'claude' is not installed on this machine" (Get-Cmd @{ engine = 'claude' } ([pscustomobject]@{}) @{}).error
        Assert-Equal "engine 'nope' is not installed on this machine" (Get-Cmd @{ engine = 'nope' }).error
    }
    Test-Case 'job cmd: ollama runs agent\ollama-job.ps1 (REST), model default qwen2.5:14b, runner writes out.log itself' {
        Assert-Equal "engine 'ollama' is not installed on this machine" (Get-Cmd @{ engine = 'ollama' }).error
        $c = Get-Cmd @{ engine = 'ollama'; model = 'llama3.2:1b' } ([pscustomobject]@{ ollama_url = 'http://127.0.0.1:18951' })
        Assert-Equal "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"C:\F\agent\ollama-job.ps1`" -PromptFile `"$pf`" -Model llama3.2:1b -BaseUrl http://127.0.0.1:18951 -OutFile `"C:\w\jobs\j1\out.log`"" $c.inner
        Assert-Equal 'err' $c.redirect
        $c = Get-Cmd @{ engine = 'ollama' } ([pscustomobject]@{}) @{ ollama = 'C:\o\ollama.exe' }
        Assert-True ($c.inner -match '-Model qwen2\.5:14b -BaseUrl http://127\.0\.0\.1:11434 ') $c.inner
        Assert-True ((Get-Cmd @{ engine = 'ollama' } ([pscustomobject]@{ ollama_url = 'http://x:1/ & calc' })).error -like 'bad ollama_url*')
    }
    Test-Case 'job cmd: ollama only for job kind text (kind missing = text)' {
        $o = [pscustomobject]@{ ollama_url = 'http://127.0.0.1:19051' }
        Assert-True (-not (Get-Cmd @{ engine = 'ollama'; kind = 'text' } $o).error); Assert-True (-not (Get-Cmd @{ engine = 'ollama' } $o).error)
        Assert-Equal "engine 'ollama' runs text jobs only (job kind 'code')" (Get-Cmd @{ engine = 'ollama'; kind = 'code' } $o).error
    }
    Test-Case 'safe ids: session ids / model names' {
        Assert-True (Test-SafeSessionId '0aaa1111-2222-7333-8444-555555555555'); Assert-True (-not (Test-SafeSessionId '-rf')); Assert-True (-not (Test-SafeSessionId 'a b c d e f'))
        Assert-True (Test-SafeModelName 'qwen2.5:14b'); Assert-True (Test-SafeModelName 'library/llama3.2:1b-q4'); Assert-True (-not (Test-SafeModelName 'x;y')); Assert-True (-not (Test-SafeModelName '-x'))
    }

    # ---- services (SPEC H) ----
    Test-Case 'services: defaults (t3 check + ollama when_tool), t3_autostart:false drops t3, explicit [] = none' {
        $d = Get-ForgeServices ([pscustomobject]@{})
        Assert-Equal @('t3', 'ollama') @($d | ForEach-Object { $_.name }); Assert-Equal 'start-t3.ps1' $d[0].check
        Assert-Equal '127.0.0.1:11434' $d[1].env.OLLAMA_HOST; Assert-Equal 'http://127.0.0.1:11434/api/version' $d[1].health; Assert-Equal 'ollama' $d[1].when_tool
        $d2 = Get-ForgeServices ([pscustomobject]@{ t3_autostart = $false }); Assert-Equal @('ollama') @($d2 | ForEach-Object { $_.name })
        $d3 = Get-ForgeServices ('{"services":[]}' | ConvertFrom-Json); Assert-Equal 0 @($d3).Count
        $j = Get-ForgeServices ('{"services":[{"name":"x","health":"http://h/","start":"a.exe b","env":{"K":"V"}}]}' | ConvertFrom-Json)
        Assert-Equal 'x' $j[0].name; Assert-Equal 'V' $j[0].env.K
    }
    Test-Case 'services: keep-alive decision (ok / skip / start / grace wait / backoff / down)' {
        Assert-Equal 'skip' (Get-ServiceDecision -ToolPresent $false -Healthy $true)
        Assert-Equal 'ok' (Get-ServiceDecision -Healthy $true -Now 100 -LastStart 99)
        Assert-Equal 'start' (Get-ServiceDecision -Healthy $false -Now 1000)
        Assert-Equal 'wait' (Get-ServiceDecision -Healthy $false -Now 1000 -LastStart 900 -Fails 1) 'within 120 s grace'
        Assert-Equal 'start' (Get-ServiceDecision -Healthy $false -Now 1000 -LastStart 880 -Fails 2)
        Assert-Equal 'wait' (Get-ServiceDecision -Healthy $false -Now 1000 -LastStart 600 -Fails 3) '3 failed starts -> 10 min backoff'
        Assert-Equal 'start' (Get-ServiceDecision -Healthy $false -Now 1300 -LastStart 600 -Fails 3)
        Assert-Equal 'down' (Get-ServiceDecision -Healthy $false -CanStart $false -Now 1000)
    }
    Test-Case 'services: start command placeholders and status "since" only moves on a change' {
        $c = Split-ServiceCommand '<ollama.exe> serve' @{ ollama = 'C:\o\ollama.exe' }; Assert-Equal 'C:\o\ollama.exe' $c.exe; Assert-Equal 'serve' $c.args
        Assert-Equal $null (Split-ServiceCommand '<ollama.exe> serve' @{})
        $c = Split-ServiceCommand '"C:\Program Files\x\y.exe" --a b'; Assert-Equal 'C:\Program Files\x\y.exe' $c.exe; Assert-Equal '--a b' $c.args
        $t = @{}
        Assert-True (Update-ServiceStatus $t 'ollama' $false 10); Assert-True (-not (Update-ServiceStatus $t 'ollama' $false 20)); Assert-Equal 10 $t.ollama.since
        Assert-True (Update-ServiceStatus $t 'ollama' $true 30); Assert-Equal 30 $t.ollama.since
        $m = Get-ServiceMetrics $t; Assert-Equal 1 @($m).Count; Assert-Equal 'ollama' $m[0].name; Assert-Equal $true $m[0].ok
    }
}
& {
    . (Join-Path $Repo 'agent\ollama-job.ps1') -PromptFile 'x' -Model 'm' -OutFile 'x' -NoRun
    Test-Case 'ollama job: model presence (untagged name matches :latest)' {
        Assert-True (Test-OllamaHasModel @('qwen2.5:14b', 'llama3:latest') 'qwen2.5:14b'); Assert-True (Test-OllamaHasModel @('llama3:latest') 'llama3')
        Assert-True (-not (Test-OllamaHasModel @('qwen2.5:7b') 'qwen2.5:14b')); Assert-True (-not (Test-OllamaHasModel @() 'x'))
    }
    Test-Case 'ollama job: pull progress = one line per status and per 10% step' {
        $seen = @{}
        $lines = @(foreach ($m in @(@{ status = 'pulling manifest' }, @{ status = 'pulling manifest' }, @{ status = 'pulling abc'; total = 1000; completed = 0 },
                                     @{ status = 'pulling abc'; total = 1000; completed = 50 }, @{ status = 'pulling abc'; total = 1000; completed = 120 },
                                     @{ status = 'pulling abc'; total = 1000; completed = 1000 }, @{ status = 'success' })) { Format-PullProgress ([pscustomobject]$m) $seen })
        Assert-Equal 5 @($lines | Where-Object { $_ }).Count
        Assert-True ($lines[0] -eq '[ollama] pulling manifest'); Assert-True (@($lines | Where-Object { $_ -match '100%' }).Count -eq 1)
    }
}

# ---------------- 1b. allowlisted actions (agent\actions.ps1) ----------------
Write-Host 'actions'
& {
    . (Join-Path $Repo 'agent\actions.ps1')
    $cat = Read-ForgeActionCatalog (Join-Path $Repo 'hub\actions.json')
    $vec = Get-Content (Join-Path $PSScriptRoot 'fixtures\action-vectors.json') -Raw | ConvertFrom-Json
    function Get-Norm($Action, $ArgsObj) { Test-ForgeActionArgs -Catalog $cat -Name $Action -ActionArgs $ArgsObj }
    function ConvertTo-Sorted($h) { $o = [ordered]@{}; foreach ($k in @($h.Keys | Sort-Object)) { $o[$k] = $h[$k] }; $o }

    foreach ($v in @($vec.valid)) {
        Test-Case "validate ok (shared vector): $($v.action) $(ConvertTo-Json -InputObject $v.args -Compress)" {
            $got = ConvertTo-Sorted (Get-Norm $v.action $v.args)
            Assert-Equal (ConvertTo-Sorted (ConvertTo-ForgeHashtable $v.expect)) $got
        }
    }
    foreach ($v in @($vec.invalid)) {
        Test-Case "validate rejects (shared vector): $($v.action) $(ConvertTo-Json -InputObject $v.args -Compress)" {
            Assert-Throws { Get-Norm $v.action $v.args | Out-Null }
        }
    }
    Test-Case 'validate: hashtable args accepted too; non-object args rejected' {
        Assert-Equal 'Git.Git' (Get-Norm 'winget_install' @{ id = 'Git.Git' }).id
        Assert-Throws { Get-Norm 'winget_install' 'Git.Git' } '*must be an object*'
    }
    Test-Case 'validate: action names are case-sensitive' { Assert-Throws { Get-Norm 'Reboot' @{} } '*unknown action*' }

    function Get-Steps($Action, $ArgsObj, [string]$User = 'BOX\you') { @(Get-ForgeActionSteps -Name $Action -ActionArgs (Get-Norm $Action $ArgsObj) -User $User) }
    $acc = @('--accept-source-agreements', '--accept-package-agreements')
    Test-Case 'build: winget_install -> argv array (scope optional)' {
        $s = Get-Steps 'winget_install' @{ id = 'Git.Git' }
        Assert-Equal 1 $s.Count; Assert-Equal 'native' $s[0].kind; Assert-Equal 'winget' $s[0].exe
        Assert-Equal (@('install', '--id', 'Git.Git', '-e', '--silent') + $acc) @($s[0].argv)
        $s = Get-Steps 'winget_install' @{ id = 'Git.Git'; scope = 'machine' }
        Assert-Equal (@('install', '--id', 'Git.Git', '-e', '--silent') + $acc + @('--scope', 'machine')) @($s[0].argv)
    }
    Test-Case 'build: winget_upgrade one id / all' {
        Assert-Equal (@('upgrade', '--id', 'Git.Git', '-e', '--silent') + $acc) @((Get-Steps 'winget_upgrade' @{ id = 'Git.Git' })[0].argv)
        Assert-Equal (@('upgrade', '--all', '--silent') + $acc) @((Get-Steps 'winget_upgrade' @{})[0].argv)
    }
    Test-Case 'build: npm_global_install' {
        $s = Get-Steps 'npm_global_install' @{ package = '@anthropic-ai/claude-code@latest' }
        Assert-Equal 'npm' $s[0].exe; Assert-Equal @('install', '-g', '@anthropic-ai/claude-code@latest') @($s[0].argv)
    }
    Test-Case 'build: restart_task = Stop + Start with the name as ONE parameter value' {
        $s = Get-Steps 'restart_task' @{ name = 'T3 Code Server' }
        Assert-Equal 2 $s.Count; Assert-Equal 'Stop-ScheduledTask' $s[0].command; Assert-Equal 'Start-ScheduledTask' $s[1].command
        Assert-Equal 'T3 Code Server' $s[0].params.TaskName; Assert-Equal 'T3 Code Server' $s[1].params.TaskName; Assert-Equal 'Stop' $s[1].params.ErrorAction
    }
    Test-Case 'build: restart_service' {
        $s = Get-Steps 'restart_service' @{ name = 'Spooler' }
        Assert-Equal 'Restart-Service' $s[0].command; Assert-Equal 'Spooler' $s[0].params.Name; Assert-Equal $true $s[0].params.Force
    }
    Test-Case 'build: reboot default 60 s, custom delay, comment is one argv element' {
        Assert-Equal @('/r', '/t', '60', '/c', 'Forge Control reboot') @((Get-Steps 'reboot' @{})[0].argv)
        Assert-Equal @('/r', '/t', '0', '/c', 'Forge Control reboot') @((Get-Steps 'reboot' @{ delay_s = 0 })[0].argv)
        Assert-Equal 'shutdown' (Get-Steps 'reboot' @{})[0].exe
    }
    Test-Case 'build: ensure_task user (S4U for the node user) and system' {
        $s = Get-Steps 'ensure_task' @{ name = 'T3Code'; command = 'C:\Program Files\nodejs\node.exe'; args = 'server.js --port 3773'; run_as = 'user'; trigger = 'logon' }
        Assert-Equal 'Register-ForgeEnsureTask' $s[0].command
        $p = $s[0].params
        Assert-Equal 'T3Code' $p.Name; Assert-Equal 'C:\Program Files\nodejs\node.exe' $p.Command; Assert-Equal 'server.js --port 3773' $p.Arguments
        Assert-Equal 'user' $p.RunAs; Assert-Equal 'logon' $p.Trigger; Assert-Equal 'BOX\you' $p.User
        $s = Get-Steps 'ensure_task' @{ name = 'Svc'; command = 'x.exe'; run_as = 'system'; trigger = 'startup' }
        Assert-Equal '' $s[0].params.User; Assert-Equal 'system' $s[0].params.RunAs
    }
    Test-Case 'build: ensure_task run_as=user without a configured user fails' {
        Assert-Throws { Get-Steps 'ensure_task' @{ name = 'T'; command = 'x.exe'; run_as = 'user'; trigger = 'logon' } '' } "*needs the node's user*"
    }
    Test-Case 'build: hostile values stay single argv elements (no string building)' {
        # values that pass the schema but contain spaces are never split or re-quoted into a command line
        $s = Get-Steps 'restart_service' @{ name = 'My Service - x' }
        Assert-Equal 'My Service - x' $s[0].params.Name
        Assert-True ($s[0].argv -eq $null) 'cmdlet steps carry no argv'
    }
    Test-Case 'every catalog action has a builder' {
        foreach ($a in @($cat.actions)) {
            $sample = @{ winget_install = @{ id = 'A' }; winget_upgrade = @{}; npm_global_install = @{ package = 'a' }; restart_task = @{ name = 'a' }
                         restart_service = @{ name = 'a' }; reboot = @{}; ensure_task = @{ name = 'a'; command = 'a.exe'; run_as = 'system'; trigger = 'startup' } }[[string]$a.name]
            Assert-True ($null -ne $sample) "no sample for $($a.name)"
            Assert-True ((Get-Steps $a.name $sample).Count -ge 1) "no steps for $($a.name)"
        }
    }
    Test-Case 'run: cmdlet step is splatted; failure -> 1; dry run never executes' {
        Assert-Equal 0 (Invoke-ForgeActionStep @{ kind = 'cmdlet'; command = 'Write-Output'; params = @{ InputObject = 'x y' } } $false 6>$null)
        Assert-Equal 1 (Invoke-ForgeActionStep @{ kind = 'cmdlet'; command = 'Get-Item'; params = @{ LiteralPath = 'Z:\no\such\forge'; ErrorAction = 'Stop' } } $false)
        Assert-Equal 0 (Invoke-ForgeActionStep @{ kind = 'cmdlet'; command = 'Get-Item'; params = @{ LiteralPath = 'Z:\no\such\forge'; ErrorAction = 'Stop' } } $true)
    }
    Test-Case 'run: native step passes argv as separate arguments and returns the exit code' {
        function Resolve-ForgeExe([string]$Name) { (Get-Command powershell.exe).Source }   # stub: never winget/npm/shutdown here
        New-Item -ItemType Directory -Force $Scratch | Out-Null
        [IO.File]::WriteAllText((Join-Path $Scratch 'argc.ps1'), 'exit ($args.Count * 10 + $args[1].Length)')
        Assert-Equal 7 (Invoke-ForgeActionStep @{ kind = 'native'; exe = 'stub'; argv = [string[]]@('-NoProfile', '-Command', 'exit 7') } $false)
        $code = Invoke-ForgeActionStep @{ kind = 'native'; exe = 'stub'; argv = [string[]]@('-NoProfile', '-File', (Join-Path $Scratch 'argc.ps1'), 'a', 'b c d') } $false
        Assert-Equal 25 $code 'expected 2 args, second "b c d" (5 chars) kept as one'
    }
    Test-Case 'format: dry-run display quotes values with spaces' {
        Assert-Equal 'shutdown /r /t 60 /c "Forge Control reboot"' (Format-ForgeActionStep (Get-Steps 'reboot' @{})[0])
        Assert-Equal 'Restart-Service -ErrorAction Stop -Force -Name "My Svc"' (Format-ForgeActionStep (Get-Steps 'restart_service' @{ name = 'My Svc' })[0])
    }
}

# ---------------- 2. watchdog ----------------
Write-Host 'watchdog'
New-Item -ItemType Directory -Force $Scratch | Out-Null
& {
    . (Join-Path $Repo 'watchdog\watchdog.ps1') -NoRun

    Test-Case 'semver compare' {
        $cases = @(
            @('2.0.0', '2.0.1', -1), @('2.0.1', '2.0.0', 1), @('2.0.0', '2.0.0', 0), @('2.10.0', '2.9.9', 1),
            @('2.1.0-rc.1', '2.1.0', -1), @('2.1.0', '2.1.0-rc.1', 1), @('2.1.0-rc.2', '2.1.0-rc.1', 1), @('2.1.0-rc.10', '2.1.0-rc.2', 1),
            @('2.1.0-alpha', '2.1.0-beta', -1), @('v2.0.1', '2.0.0', 1), @('2.1', '2.1.0', 0), @('', '0.0.1', -1), @('1.0.0', $null, 1),
            @('2.0.0+build5', '2.0.0', 0))
        foreach ($c in $cases) { Assert-Equal $c[2] (Compare-SemVer $c[0] $c[1]) "for $($c[0]) vs $($c[1])" }
    }
    Test-Case 'versions sort descending by semver, not text' {
        Assert-Equal @('2.10.0', '2.9.0', '2.1.0', '2.1.0-rc.1', '2.0.0') (Sort-VersionsDescending @('2.0.0', '2.10.0', '2.1.0-rc.1', '2.9.0', '2.1.0'))
    }

    $peers = @(
        [pscustomobject]@{ id = 'a'; url = 'http://a'; version = '2.0.1'; bundle_sha256 = 'x' },
        [pscustomobject]@{ id = 'b'; url = 'http://b'; version = '2.0.3'; bundle_sha256 = 'y' },
        [pscustomobject]@{ id = 'c'; url = 'http://c'; version = '2.0.2'; bundle_sha256 = 'z' })
    Test-Case 'update: highest healthy peer version above ours' { Assert-Equal 'b' (Select-UpdateCandidate -Peers $peers -Current '2.0.0').id }
    Test-Case 'update: bad versions are never picked' { Assert-Equal 'c' (Select-UpdateCandidate -Peers $peers -Current '2.0.0' -Bad @('2.0.3')).id }
    Test-Case 'update: nothing when we are newest / equal' { Assert-True ($null -eq (Select-UpdateCandidate -Peers $peers -Current '2.0.3')) }
    Test-Case 'update: peers without sha256 or with junk versions are skipped' {
        $p = @([pscustomobject]@{ id = 'n'; version = '9.0.0'; bundle_sha256 = '' }, [pscustomobject]@{ id = 'j'; version = 'latest'; bundle_sha256 = 'q' })
        Assert-True ($null -eq (Select-UpdateCandidate -Peers $p -Current '2.0.0'))
    }

    Test-Case 'prune: keep newest 3 incl. current' {
        Assert-Equal @('2.0.0', '2.0.1') (Select-ReleasesToPrune -Versions @('2.0.0', '2.0.1', '2.0.2', '2.0.3', '2.0.4') -Current '2.0.4')
    }
    Test-Case 'prune: an older current (after rollback) is kept' {
        Assert-Equal @('2.0.0', '2.0.2') (Select-ReleasesToPrune -Versions @('2.0.0', '2.0.1', '2.0.2', '2.0.3', '2.0.4') -Current '2.0.1')
    }
    Test-Case 'prune: protected (rollback target) is kept' {
        Assert-Equal @('2.0.2') (Select-ReleasesToPrune -Versions @('2.0.0', '2.0.2', '2.0.3', '2.0.4') -Current '2.0.4' -Protect @('2.0.0'))
    }
    Test-Case 'prune: nothing when <= 3' { Assert-Equal @() (Select-ReleasesToPrune -Versions @('2.0.0', '2.0.1') -Current '2.0.1') }

    Test-Case 'rollback: 3 consecutive unhealthy runs after update -> rollback' {
        $p = @{ from = '2.0.0'; to = '2.0.1'; fails = 0 }
        $d1 = Get-RollbackDecision -Probation $p -CurrentVersion '2.0.1' -HealthOk $false; Assert-Equal 'wait' $d1.action; $p.fails = $d1.fails
        $d2 = Get-RollbackDecision -Probation $p -CurrentVersion '2.0.1' -HealthOk $false; Assert-Equal 'wait' $d2.action; $p.fails = $d2.fails
        $d3 = Get-RollbackDecision -Probation $p -CurrentVersion '2.0.1' -HealthOk $false; Assert-Equal 'rollback' $d3.action; Assert-Equal 3 $d3.fails
    }
    Test-Case 'rollback: healthy run confirms the update' { Assert-Equal 'confirm' (Get-RollbackDecision -Probation @{ from = '2.0.0'; to = '2.0.1'; fails = 2 } -CurrentVersion '2.0.1' -HealthOk $true).action }
    Test-Case 'rollback: restart grace period does not count' {
        $d = Get-RollbackDecision -Probation @{ from = '2.0.0'; to = '2.0.1'; fails = 2 } -CurrentVersion '2.0.1' -HealthOk $false -InGrace $true
        Assert-Equal 'skip' $d.action; Assert-Equal 2 $d.fails
    }
    Test-Case 'rollback: no probation / other version -> none' {
        Assert-Equal 'none' (Get-RollbackDecision -Probation $null -CurrentVersion '2.0.1' -HealthOk $false).action
        Assert-Equal 'none' (Get-RollbackDecision -Probation @{ from = '2.0.0'; to = '2.0.2' } -CurrentVersion '2.0.1' -HealthOk $false).action
    }

    $f = Join-Path $Scratch 'sha.bin'; [IO.File]::WriteAllText($f, 'forge', (New-Object Text.UTF8Encoding($false)))
    $known = '9d3a0e4b9fd1ffa6b2b8bd8ff4b2a5f82bb1ae09b4d5cddfd0fa7a7d4c0bd6cb'
    Test-Case 'bundle sha: matches (case-insensitive, sha256: prefix ok)' {
        $real = Get-FileSha256 $f
        Assert-True (Test-BundleSha $f $real); Assert-True (Test-BundleSha $f $real.ToUpperInvariant()); Assert-True (Test-BundleSha $f "sha256:$real")
        $ref = ([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes('forge'))) -replace '-', '').ToLowerInvariant()
        Assert-Equal $ref $real
    }
    Test-Case 'bundle sha: mismatch / empty / missing file -> false' {
        Assert-True (-not (Test-BundleSha $f $known.Replace('9', '8'))); Assert-True (-not (Test-BundleSha $f '')); Assert-True (-not (Test-BundleSha (Join-Path $Scratch 'nope') 'abc'))
    }
    Test-Case 'stale file check' {
        $now = (Get-Item $f).LastWriteTime
        Assert-True (-not (Test-StaleFile $f $now.AddSeconds(60) 120)); Assert-True (Test-StaleFile $f $now.AddSeconds(121) 120); Assert-True (Test-StaleFile (Join-Path $Scratch 'nope') $now 120)
    }

    # ---- release install / flip / rollback on a scratch ForgeHome (real junctions, scratch only) ----
    . (Join-Path $Repo 'tools\build-bundle.ps1') -NoRun
    $home1 = Join-Path $Scratch 'Forge'
    function New-TestBundle([string]$Ver, [switch]$BreakPs) {
        $src = Join-Path $Scratch "src-$Ver"
        if (Test-Path $src) { Remove-Item $src -Recurse -Force }
        New-Item -ItemType Directory -Force $src | Out-Null
        foreach ($i in 'hub', 'agent', 'watchdog', 'requirements.txt') { Copy-Item -Recurse (Join-Path $Repo $i) (Join-Path $src $i) }
        [IO.File]::WriteAllText((Join-Path $src 'VERSION'), $Ver, (New-Object Text.UTF8Encoding($false)))
        if ($BreakPs) { [IO.File]::WriteAllText((Join-Path $src 'watchdog\watchdog.ps1'), 'function broken {', (New-Object Text.UTF8Encoding($false))) }
        New-ForgeBundle -Root $src -OutDir (Join-Path $Scratch "dist-$Ver")
    }
    $b1 = New-TestBundle '2.0.0'; $b2 = New-TestBundle '2.0.1'
    Test-Case 'bundle: deterministic sha for the same tree' { $again = New-TestBundle '2.0.0'; Assert-Equal $b1.sha256 $again.sha256; $script:b1 = $again }
    $b1 = $script:b1
    Test-Case 'release: install + junction current' {
        Assert-Equal '2.0.0' (Install-ReleaseZip -ForgeHome $home1 -ZipPath $b1.path -ExpectedSha $b1.sha256 -ExpectedVersion '2.0.0')
        Set-CurrentRelease $home1 '2.0.0'
        Assert-Equal '2.0.0' (Get-CurrentVersion $home1)
        Assert-True (((Get-Item (Join-Path $home1 'current') -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) 'current is not a junction'
        Assert-True (Test-Path (Join-Path $home1 'current\watchdog\run-hub.cmd'))
    }
    Test-Case 'release: sha mismatch refused, nothing extracted' {
        Assert-Throws { Install-ReleaseZip -ForgeHome $home1 -ZipPath $b2.path -ExpectedSha $b1.sha256 } '*sha256 mismatch*'
        Assert-True (-not (Test-Path (Join-Path $home1 'releases\2.0.1')))
    }
    Test-Case 'release: advertised version must match VERSION in zip' {
        Assert-Throws { Install-ReleaseZip -ForgeHome $home1 -ZipPath $b2.path -ExpectedSha $b2.sha256 -ExpectedVersion '2.0.9' } '*!= advertised*'
    }
    Test-Case 'release: bundle with unparsable scripts refused' {
        $bb = New-TestBundle '2.0.7' -BreakPs
        Assert-Throws { Install-ReleaseZip -ForgeHome $home1 -ZipPath $bb.path -ExpectedSha $bb.sha256 } '*do not parse*'
        Assert-True (-not (Test-Path (Join-Path $home1 'releases\2.0.7')))
    }
    Test-Case 'release: refuses to overwrite the active release' {
        Assert-Throws { Install-ReleaseZip -ForgeHome $home1 -ZipPath $b1.path -ExpectedSha $b1.sha256 } '*active release*'
    }
    Test-Case 'release: upgrade flip then rollback flip keep both trees intact' {
        Install-ReleaseZip -ForgeHome $home1 -ZipPath $b2.path -ExpectedSha $b2.sha256 | Out-Null
        Set-CurrentRelease $home1 '2.0.1'; Assert-Equal '2.0.1' (Get-CurrentVersion $home1)
        Set-CurrentRelease $home1 '2.0.0'; Assert-Equal '2.0.0' (Get-CurrentVersion $home1)
        Assert-True (Test-Path (Join-Path $home1 'releases\2.0.1\hub\app.py')) 'junction removal deleted target contents'
        Assert-True (-not (Test-Path (Join-Path $home1 'current.new')))
        $rv = Get-ReleaseVersions $home1; Assert-Equal @('2.0.0', '2.0.1') @($rv | Sort-Object)
    }
    Test-Case 'release: installed dir carries bundle.zip + bundle.sha256 (publish layout) and validates as local release' {
        Assert-True (Test-Path (Join-Path $home1 'releases\2.0.1\bundle.zip'))
        Assert-Equal $b2.sha256 (Get-Content (Join-Path $home1 'releases\2.0.1\bundle.sha256') -Raw).Trim()
        Assert-True (Test-LocalRelease -ForgeHome $home1 -Version '2.0.1' -ExpectedSha $b2.sha256)
        Assert-True (-not (Test-LocalRelease -ForgeHome $home1 -Version '2.0.1' -ExpectedSha $b1.sha256)) 'wrong sha accepted'
        Assert-True (-not (Test-LocalRelease -ForgeHome $home1 -Version '2.0.9' -ExpectedSha $b2.sha256)) 'missing release accepted'
    }
    Test-Case 'release: flip to a missing release throws and leaves current alone' {
        Assert-Throws { Set-CurrentRelease $home1 '9.9.9' } '*not found*'; Assert-Equal '2.0.0' (Get-CurrentVersion $home1)
    }
    Test-Case 'system agent: task args run current\agent\forge-agent.ps1 -Lane system with the shared agent config' {
        Assert-Equal '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "C:\PD\Forge\current\agent\forge-agent.ps1" -ConfigPath "C:\PD\Forge\data\agent-config.json" -Lane system' (Get-SystemAgentTaskArgs 'C:\PD\Forge')
    }
    Test-Case 'system agent: lane detection from command lines (user restarts never kill the SYSTEM lane)' {
        Assert-Equal 'system' (Get-AgentLaneFromCmd (Get-SystemAgentTaskArgs 'C:\PD\Forge'))
        Assert-Equal 'system' (Get-AgentLaneFromCmd '"powershell.exe" -File "C:\x\forge-agent.ps1" -Child -ConfigPath "c.json" -Lane system')
        Assert-Equal 'user' (Get-AgentLaneFromCmd 'powershell.exe -File "C:\x\forge-agent.ps1" -Child -ConfigPath "c.json" -Lane user')
        Assert-Equal 'user' (Get-AgentLaneFromCmd 'powershell.exe -File "C:\x\forge-agent.ps1" -ConfigPath "c.json"')
        Assert-Equal $null (Get-AgentLaneFromCmd 'powershell.exe -File watchdog.ps1')
    }
    Test-Case 'bad-versions.txt parsing' {
        $d = Join-Path $home1 'data'; New-Item -ItemType Directory -Force $d | Out-Null
        [IO.File]::WriteAllText((Join-Path $d 'bad-versions.txt'), "# comment`r`n2.0.1`r`n`r`n 2.0.5 `r`n")
        Assert-Equal @('2.0.1', '2.0.5') (Get-BadVersions $d)
    }

    # ---- 2.2 staggered rollout: update lease + fleet-bad (SPEC-phase2 A) ----
    Test-Case 'lease: granted -> proceed; pre-2.2 leader (404) -> proceed; denied -> wait with holder' {
        $d = Get-LeaseDecision -Result @{ kind = 'granted' } -Now 1000; Assert-Equal 'proceed' $d.action; Assert-Equal $null $d.no_leader_since
        Assert-Equal 'proceed' (Get-LeaseDecision -Result @{ kind = 'unsupported'; code = 404 } -Now 1000).action
        $d = Get-LeaseDecision -Result @{ kind = 'denied'; holder = 'nova' } -NoLeaderSince 500 -Now 1000
        Assert-Equal 'wait' $d.action; Assert-Equal 'nova' $d.holder; Assert-Equal 'waiting for update lease (holder nova)' $d.reason
        Assert-Equal $null $d.no_leader_since 'a reachable leader resets the lone-survivor clock'
    }
    Test-Case 'lease: no leader reachable -> wait, lone survivor proceeds after 5 min' {
        $d = Get-LeaseDecision -Result @{ kind = 'unreachable' } -NoLeaderSince $null -Now 1000; Assert-Equal 'wait' $d.action; Assert-Equal 1000 $d.no_leader_since
        Assert-Equal 'wait' (Get-LeaseDecision -Result $null -NoLeaderSince 1000 -Now 1299).action
        $d = Get-LeaseDecision -Result @{ kind = 'unreachable'; code = 503; msg = 'no leader' } -NoLeaderSince 1000 -Now 1300; Assert-Equal 'proceed' $d.action; Assert-True ($d.reason -match 'lone survivor')
    }
    Test-Case 'lease: a leader that answers with an error -> wait, never lone survivor, clock reset' {
        foreach ($code in 401, 422, 500, 502) {
            $d = Get-LeaseDecision -Result @{ kind = 'error'; code = $code; msg = 'boom' } -NoLeaderSince 400 -Now 1000   # no leader for 10 min before
            Assert-Equal 'wait' $d.action "HTTP $code"; Assert-Equal $null $d.no_leader_since; Assert-True ($d.reason -match "HTTP $code") $d.reason
        }
    }
    Test-Case 'lease state machine over several runs: unreachable -> error resets -> unreachable 5 min -> proceed' {
        $st = @{ lease_no_leader_since = $null; lease_wait = $null; lease = $null }
        $d = Step-LeaseState -Result @{ kind = 'unreachable' } -State $st -Version '2.2.1' -Leader 'http://l' -Now 1000
        Assert-Equal 'wait' $d.action; Assert-Equal 1000 $st.lease_no_leader_since; Assert-Equal '2.2.1' $st.lease_wait.version
        $d = Step-LeaseState -Result @{ kind = 'error'; code = 500; msg = 'x' } -State $st -Version '2.2.1' -Leader 'http://l' -Now 1240
        Assert-Equal 'wait' $d.action; Assert-Equal $null $st.lease_no_leader_since 'an answering leader resets the lone-survivor clock'
        foreach ($t in 1300, 1500, 1599) { $d = Step-LeaseState -Result @{ kind = 'unreachable' } -State $st -Version '2.2.1' -Leader 'http://l' -Now $t; Assert-Equal 'wait' $d.action "t=$t" }
        Assert-Equal 1300 $st.lease_no_leader_since
        $d = Step-LeaseState -Result @{ kind = 'unreachable' } -State $st -Version '2.2.1' -Leader 'http://l' -Now 1600
        Assert-Equal 'proceed' $d.action; Assert-Equal $null $st.lease_wait; Assert-Equal $null $st.lease 'lone survivor holds no lease'
    }
    Test-Case 'lease state machine: denied waits with holder; granted stores the lease (not in dry run)' {
        $st = @{ lease_no_leader_since = 900.0; lease_wait = $null; lease = $null }
        $d = Step-LeaseState -Result @{ kind = 'denied'; holder = 'canary' } -State $st -Version '2.2.1' -Leader 'http://l' -Now 1000
        Assert-Equal 'wait' $d.action; Assert-Equal 'canary' $st.lease_wait.holder; Assert-Equal $null $st.lease_no_leader_since
        $d = Step-LeaseState -Result @{ kind = 'granted' } -State $st -Version '2.2.1' -Leader 'http://l' -Now 1060
        Assert-Equal 'proceed' $d.action; Assert-Equal $null $st.lease_wait; Assert-Equal 'http://l' $st.lease.url; Assert-Equal '2.2.1' $st.lease.version
        $st2 = @{}; $null = Step-LeaseState -Result @{ kind = 'granted' } -State $st2 -Version '2.2.1' -Leader 'http://l' -Now 1 -Record $false
        Assert-Equal $null $st2.lease 'dry run must not record a lease'
        $st3 = @{}; $null = Step-LeaseState -Result @{ kind = 'unsupported'; code = 404 } -State $st3 -Version '2.2.1' -Leader 'http://l' -Now 1; Assert-Equal $null $st3.lease
    }
    Test-Case 'lease release: confirm releases without bad version; rollback reports fleet-bad then releases' {
        $script:calls = New-Object System.Collections.Generic.List[string]
        function Send-BadVersion([string]$HubUrl, [string]$Token, [string]$NodeId, [string]$Version) { $script:calls.Add("bad $HubUrl $NodeId $Version"); $true }
        function Remove-UpdateLease([string]$LeaderUrl, [string]$Token, [string]$NodeId, [string]$BadVersion) { $script:calls.Add("release $LeaderUrl $NodeId $BadVersion"); $true }
        function Write-WdLog([string]$m) { }
        $Local = 'http://127.0.0.1:19001'; $cfg = [pscustomobject]@{ node_id = 'n1'; cluster_token = 'c' }
        $script:State = @{ lease = @{ version = '2.2.1'; url = 'http://leader:8787'; at = 1 } }
        Clear-UpdateLease $cfg $null
        Assert-Equal @('release http://leader:8787 n1 ') @($script:calls); Assert-Equal $null $script:State.lease
        $script:calls.Clear(); $script:State = @{ lease = @{ version = '2.2.1'; url = 'http://leader:8787'; at = 1 } }
        Clear-UpdateLease $cfg '2.2.1'
        Assert-Equal @('bad http://leader:8787 n1 2.2.1', 'release http://leader:8787 n1 2.2.1') @($script:calls); Assert-Equal $null $script:State.lease
        $script:calls.Clear(); $script:State = @{ lease = $null }
        Clear-UpdateLease $cfg '2.2.1'
        Assert-Equal @('bad http://127.0.0.1:19001 n1 2.2.1') @($script:calls) 'no lease held (lone survivor): still report fleet-bad via the local hub'
    }
    Test-Case 'job cleanup: only real job dirs (marker file, job-like name) older than 14 days; running jobs kept' {
        $now = 100 * 86400.0; $old = $now - 15 * 86400; $new = $now - 13 * 86400
        $dirs = @(@{ name = 'j-old'; path = 'W:\jobs\j-old'; mtime = $old; reparse = $false; marker = $true },
                  @{ name = 'j-new'; path = 'W:\jobs\j-new'; mtime = $new; reparse = $false; marker = $true },
                  @{ name = 'sys-a1'; path = 'W:\jobs\sys-a1'; mtime = $old; reparse = $false; marker = $true },
                  @{ name = 'link'; path = 'W:\jobs\link'; mtime = $old; reparse = $true; marker = $true },
                  @{ name = 'nomark'; path = 'W:\jobs\nomark'; mtime = $old; reparse = $false; marker = $false },
                  @{ name = 'run1'; path = 'W:\jobs\run1'; mtime = $old; reparse = $false; marker = $true },
                  @{ name = 'sys-run2'; path = 'W:\jobs\sys-run2'; mtime = $old; reparse = $false; marker = $true },
                  @{ name = 'we ird'; path = 'W:\jobs\we ird'; mtime = $old; reparse = $false; marker = $true })
        $sd = Select-StaleJobDirs -Dirs $dirs -Now $now -Skip @('run1', 'run2'); Assert-Equal @('W:\jobs\j-old', 'W:\jobs\sys-a1') @($sd)
        Assert-True (Test-CleanupDue $null 5000); Assert-True (-not (Test-CleanupDue 2000 5599)); Assert-True (Test-CleanupDue 2000 5600)
        $jr = Get-JobCleanupRoots ([pscustomobject]@{ work_root_current = 'D:\forge\'; work_root_auto = 'C:\forge' }); Assert-Equal @('D:\forge', 'C:\forge') @($jr)
        Assert-Equal 0 (Get-JobCleanupRoots ([pscustomobject]@{ work_root_current = '\\srv\share'; work_root_auto = 'relative\x' })).Count
        Assert-Equal 0 (Get-JobCleanupRoots ([pscustomobject]@{ work_root_current = 'C:\forge\..\Windows' })).Count
        Assert-Equal 0 (Get-JobCleanupRoots $null).Count
    }
    Test-Case 'job cleanup: removing a job dir never follows a junction the job left inside it' {
        $victim = Join-Path $Scratch 'victim'; New-Item -ItemType Directory -Force $victim | Out-Null
        [IO.File]::WriteAllText((Join-Path $victim 'keep.txt'), 'precious')
        $jd = Join-Path $Scratch 'cleanup-jobs\j-old'; New-Item -ItemType Directory -Force (Join-Path $jd 'sub') | Out-Null
        [IO.File]::WriteAllText((Join-Path $jd 'out.log'), 'x'); [IO.File]::WriteAllText((Join-Path $jd 'sub\f.txt'), 'y')
        & cmd.exe /d /c "mklink /J `"$(Join-Path $jd 'link')`" `"$victim`"" | Out-Null
        Assert-True (Test-Path (Join-Path $jd 'link\keep.txt')) 'junction not created'
        Assert-True (Remove-JobDirTree $jd) 'job dir not removed'
        Assert-True (-not (Test-Path $jd)); Assert-Equal 'precious' ([IO.File]::ReadAllText((Join-Path $victim 'keep.txt')))
    }
    Test-Case 'lease: leader url = the status that says role leader (local first)' {
        $s = @([pscustomobject]@{ url = 'http://127.0.0.1:8787'; role = 'standby' }, [pscustomobject]@{ url = 'http://forge-01:8787/'; role = 'leader' })
        Assert-Equal 'http://forge-01:8787' (Find-LeaderUrl $s); Assert-Equal $null (Find-LeaderUrl @($s[0])); Assert-Equal $null (Find-LeaderUrl @())
    }
    Test-Case 'fleet-bad: union of bad_versions from statuses; such versions are never update candidates' {
        $s = @([pscustomobject]@{ id = 'l'; url = 'u'; version = '2.2.1'; bundle_sha256 = 'a'; bad_versions = @('2.2.1') },
               [pscustomobject]@{ id = 'b'; url = 'v'; version = '2.2.0'; bundle_sha256 = 'b'; bad_versions = @('2.2.1', '2.1.9') },
               [pscustomobject]@{ id = 'c'; url = 'w'; version = '2.1.5'; bundle_sha256 = 'c' })
        $fb = Get-FleetBadVersions $s
        Assert-Equal @('2.2.1', '2.1.9') @($fb)
        Assert-Equal 'b' (Select-UpdateCandidate -Peers $s -Current '2.1.4' -Bad (@('2.0.0') + @($fb))).id
        $none = Get-FleetBadVersions @($s[2]); Assert-Equal 0 @($none).Count
    }
}

# ---------------- 3. installer ----------------
Write-Host 'installer'
& {
    . (Join-Path $Repo 'agent\install.ps1') -NoRun
    Test-Case 'placeholders detected; replaced values are not' {
        Assert-True (Test-Placeholder ('__' + 'HUB' + '__')); Assert-True (Test-Placeholder ''); Assert-True (-not (Test-Placeholder 'http://forge-01:8787'))
    }
    Test-Case 'installer source keeps exactly one HUB and one TOKEN placeholder (hub template-replaces them)' {
        $src = Get-Content (Join-Path $Repo 'agent\install.ps1') -Raw
        Assert-Equal 1 ([regex]::Matches($src, '__HUB__').Count); Assert-Equal 1 ([regex]::Matches($src, '__TOKEN__').Count)
    }
    Test-Case 'join urls: magicdns (trailing dot trimmed), tailscale ip, lan ip; only those that exist' {
        Assert-Equal @('http://forge-01.tail1.ts.net:8787', 'http://100.64.0.1:8787', 'http://192.168.1.10:8787') (Get-JoinUrls -DnsName 'forge-01.tail1.ts.net.' -TailscaleIp '100.64.0.1' -LanIp '192.168.1.10')
        Assert-Equal @('http://192.168.1.10:8787') (Get-JoinUrls -DnsName '' -TailscaleIp $null -LanIp '192.168.1.10')
        Assert-Equal @() (Get-JoinUrls)
    }
    Test-Case 'peer hub urls flattened in priority order' {
        $boot = Get-Content (Join-Path $PSScriptRoot 'fixtures\bootstrap.json') -Raw | ConvertFrom-Json
        $u = Get-PeerHubUrls -Peers @($boot.peers)
        Assert-Equal 'http://forge-01.example.ts.net:8787' $u[0]; Assert-Equal 'http://atlas.example.ts.net:8787' $u[3]; Assert-Equal 'http://nova.example.ts.net:8787' $u[5]; Assert-Equal 6 $u.Count
    }
    Test-Case 'agent hub_urls: flat list, localhost first, then peers by priority; installing hub when no peers' {
        $boot = Get-Content (Join-Path $PSScriptRoot 'fixtures\bootstrap.json') -Raw | ConvertFrom-Json
        $u = Get-AgentHubUrls -Peers @($boot.peers) -Hub 'http://x:8787'
        Assert-Equal 7 $u.Count; Assert-Equal 'http://127.0.0.1:8787' $u[0]; Assert-Equal 'http://forge-01.example.ts.net:8787' $u[1]
        Assert-True (-not ($u | Where-Object { $_ -match ' ' })) 'nested array flattened into one string'
        Assert-Equal @('http://127.0.0.1:8787', 'http://x:8787') (Get-AgentHubUrls -Peers @() -Hub 'http://x:8787/')
    }
    Test-Case 'shared config: tokens from bootstrap, node id, port, extra keys kept' {
        $boot = [pscustomobject]@{ ui_token = 'u'; agent_token = 'a'; cluster_token = 'c' }
        $c = New-SharedConfig -Bootstrap $boot -NodeId 'box' -Existing ([pscustomobject]@{ node_id = 'old'; extra = 1; ui_token = 'stale' })
        Assert-Equal @() @($c.seed_peers)
        Assert-Equal 'box' $c.node_id; Assert-Equal 'u' $c.ui_token; Assert-Equal 'c' $c.cluster_token; Assert-Equal 8787 $c.port; Assert-Equal 1 $c.extra
    }
    Test-Case 'shared config: seed_peers = bootstrap peers (id, priority, urls) and stay arrays in JSON' {
        $boot = Get-Content (Join-Path $PSScriptRoot 'fixtures\bootstrap.json') -Raw | ConvertFrom-Json
        $boot.peers[2].urls = @('http://only-one:8787')
        $c = New-SharedConfig -Bootstrap $boot -NodeId 'newbox' -Existing $null
        Assert-Equal 3 @($c.seed_peers).Count; Assert-Equal 'forge-01' $c.seed_peers[1].id; Assert-Equal 1 $c.seed_peers[1].priority
        Assert-True ($c.seed_peers[0].Keys -notcontains 'role') 'only id/priority/urls'
        $j = ConvertTo-Json -InputObject $c -Depth 6 | ConvertFrom-Json
        Assert-Equal @('http://only-one:8787') @($j.seed_peers[2].urls); Assert-True ($j.seed_peers[2].urls -is [array]) 'single url not an array'
    }
    Test-Case 'agent config: migrates v1 logs/claude_use_api_key, v2 values win, hub_urls replaced' {
        $v1 = [pscustomobject]@{ hub_url = 'http://x'; agent_token = 'SECRET'; logs = @([pscustomobject]@{ name = 'dc'; path = 'C:\dc.log' }); claude_use_api_key = $true; max_jobs = 3 }
        $v2 = [pscustomobject]@{ max_jobs = 5; work_root_auto = 'D:\forge' }
        $c = New-AgentConfig -Existing $v2 -V1 $v1 -NodeId 'box' -HubUrls @('http://127.0.0.1:8787', 'http://a:8787')
        Assert-Equal 5 $c.max_jobs; Assert-Equal $true $c.claude_use_api_key; Assert-Equal 'dc' $c.logs[0].name; Assert-Equal 'D:\forge' $c.work_root_auto
        Assert-Equal @('http://127.0.0.1:8787', 'http://a:8787') $c.hub_urls
        Assert-True (-not $c.Contains('agent_token')) 'agent token must live only in config.json'
        Assert-True (-not $c.Contains('hub_url'))
    }
    Test-Case 'agent config: user field written by installer, kept on re-run, admin flags migrated' {
        $c = New-AgentConfig -Existing $null -V1 $null -NodeId 'box' -HubUrls @('http://127.0.0.1:8787') -User 'BOX\you'
        Assert-Equal 'BOX\you' $c.user
        $c = New-AgentConfig -Existing ([pscustomobject]@{ user = 'BOX\old'; allow_admin = $false; action_dry_run = $true }) -V1 $null -NodeId 'box' -HubUrls @('x')
        Assert-Equal 'BOX\old' $c.user; Assert-Equal $false $c.allow_admin; Assert-Equal $true $c.action_dry_run
        $c = New-AgentConfig -Existing ([pscustomobject]@{ user = 'BOX\old' }) -V1 $null -NodeId 'box' -HubUrls @('x') -User 'BOX\new'
        Assert-Equal 'BOX\new' $c.user
    }
    Test-Case 'installer registers ForgeSystemAgent (SYSTEM service-style) with -Lane system' {
        $src = Get-Content (Join-Path $Repo 'agent\install.ps1') -Raw
        Assert-True ($src -match "Register-ForgeTask -Name 'ForgeSystemAgent'.*-Lane system.*-SystemService") 'ForgeSystemAgent registration missing'
        Assert-True ($src -match "Start-ScheduledTask -TaskName 'ForgeSystemAgent'") 'ForgeSystemAgent not started'
    }
    Test-Case 'agent config: single log survives as an array in JSON' {
        $c = New-AgentConfig -Existing $null -V1 ([pscustomobject]@{ logs = [pscustomobject]@{ name = 'one'; path = 'p' } }) -NodeId 'n' -HubUrls @('http://127.0.0.1:8787')
        Assert-True ((ConvertTo-Json -InputObject $c -Depth 5) -match '"logs":\s*\[') 'logs not an array'
        Assert-True ((ConvertTo-Json -InputObject $c -Depth 5) -match '"hub_urls":\s*\[') 'hub_urls not an array'
    }
    Test-Case 'installer ACLs: root Admins+SYSTEM full + user RX, then /reset /T, data user Modify, config.json user Read' {
        $plan = Get-ForgeAclPlan -ForgeHome 'C:\PD\Forge' -User 'BOX\u'
        Assert-Equal 4 $plan.Count
        Assert-Equal @('C:\PD\Forge', '/inheritance:r', '/grant:r', '*S-1-5-32-544:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F', 'BOX\u:(OI)(CI)RX', '/C', '/Q') @($plan[0].args)
        Assert-Equal @('C:\PD\Forge\*', '/reset', '/T', '/C', '/Q') @($plan[1].args)
        Assert-Equal @('C:\PD\Forge\data', '/grant:r', 'BOX\u:(OI)(CI)M', '/C', '/Q') @($plan[2].args)
        Assert-Equal @('C:\PD\Forge\data\config.json', '/inheritance:r', '/grant:r', '*S-1-5-32-544:F', '*S-1-5-18:F', 'BOX\u:R', '/C', '/Q') @($plan[3].args)
        Assert-True (-not (@($plan | ForEach-Object { $_.args }) -match ':\(OI\)\(CI\)F$' | Where-Object { $_ -like 'BOX\u*' })) 'user must never get full control'
        $src = Get-Content (Join-Path $Repo 'agent\install.ps1') -Raw
        Assert-True ($src -match 'w32tm\.exe /resync') 'w32tm /resync missing'
    }
    Test-Case 'installer semver compare' { Assert-Equal 1 (Compare-SemVer '3.12.1' '3.10.0'); Assert-Equal (-1) (Compare-SemVer '3.9.7' '3.10.0'); Assert-Equal 0 (Compare-SemVer '2.0.0' '2.0.0') }
}

# ---------------- 4. bundle builder ----------------
Write-Host 'bundle'
& {
    . (Join-Path $Repo 'tools\build-bundle.ps1') -NoRun
    Test-Case 'bundle file list: forward slashes, sorted, excludes caches, has required entries' {
        $l = Get-BundleFileList $Repo
        Assert-True ($l -contains 'VERSION'); Assert-True ($l -contains 'hub/app.py'); Assert-True ($l -contains 'watchdog/watchdog.ps1'); Assert-True ($l -contains 'watchdog/run-hub.cmd')
        Assert-True ($l -contains 'agent/forge-agent.ps1'); Assert-True ($l -contains 'requirements.txt')
        Assert-True (-not ($l | Where-Object { $_ -match '\\|__pycache__|\.pyc$' })) 'bad entries'
        Assert-True (-not ($l | Where-Object { $_ -like 'tests/*' -or $_ -like 'tools/*' })) 'tests/tools must not ship'
    }
    Test-Case 'run-hub.cmd has CRLF line endings' { Assert-True ((Get-Content (Join-Path $Repo 'watchdog\run-hub.cmd') -Raw) -match "`r`n") }
}

foreach ($j in @(Get-ChildItem -LiteralPath $Scratch -Recurse -Directory -Force -ErrorAction SilentlyContinue | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint })) { & cmd.exe /d /c "rmdir `"$($j.FullName)`"" | Out-Null }
Remove-Item -LiteralPath $Scratch -Recurse -Force -ErrorAction SilentlyContinue
Write-Host ''
Write-Host "$($script:Pass) passed, $($script:Fail) failed" -ForegroundColor $(if ($script:Fail) { 'Red' } else { 'Green' })
foreach ($m in $script:Failures) { Write-Host "  - $m" -ForegroundColor Red }
exit $script:Fail
