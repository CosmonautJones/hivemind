# Workstream R (reliability) PowerShell tests, CONTRACT-phase3.md sections 5 R1/R2/R5/R7 and 6.5.
# Fast and deterministic; everything runs in a scratch dir (no tasks, no ProgramData, no network).
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File tests\ps\p3-reliability.ps1
# Exit code = number of failures.
param([string]$Scratch = (Join-Path ([IO.Path]::GetTempPath()) ("forge-p3r-" + [guid]::NewGuid().ToString('n').Substring(0, 8))))

$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$script:Pass = 0; $script:Fail = 0; $script:Failures = New-Object System.Collections.Generic.List[string]
$Utf8 = New-Object Text.UTF8Encoding($false)
New-Item -ItemType Directory -Force $Scratch | Out-Null

function Test-Case([string]$Name, [scriptblock]$Body) {
    try { & $Body; $script:Pass++; Write-Host "  ok   $Name" -ForegroundColor Green }
    catch { $script:Fail++; $script:Failures.Add("$Name : $($_.Exception.Message)"); Write-Host "  FAIL $Name : $($_.Exception.Message)" -ForegroundColor Red }
}
function Assert-Equal($Expected, $Actual, [string]$Msg = '') {
    $e = ConvertTo-Json -InputObject $Expected -Compress -Depth 5; $a = ConvertTo-Json -InputObject $Actual -Compress -Depth 5
    if ($e -ne $a) { throw "expected $e but got $a $Msg" }
}
function Assert-True($Cond, [string]$Msg = 'condition false') { if (-not $Cond) { throw $Msg } }

# A scratch ForgeHome: releases\<v>\{VERSION, watchdog\watchdog.ps1}, current = copy of one release (no junction needed).
function New-ScratchHome([string]$Name, [string[]]$Versions, [string]$Current, [switch]$NoCurrentWatchdog) {
    $fh = Join-Path $Scratch $Name
    foreach ($v in $Versions) {
        $d = Join-Path $fh "releases\$v\watchdog"; New-Item -ItemType Directory -Force $d | Out-Null
        [IO.File]::WriteAllText((Join-Path $fh "releases\$v\VERSION"), $v, $Utf8)
        [IO.File]::WriteAllText((Join-Path $d 'watchdog.ps1'), "# $v", $Utf8)
    }
    New-Item -ItemType Directory -Force (Join-Path $fh 'current\watchdog'), (Join-Path $fh 'data') | Out-Null
    [IO.File]::WriteAllText((Join-Path $fh 'current\VERSION'), $Current, $Utf8)
    if (-not $NoCurrentWatchdog) { [IO.File]::WriteAllText((Join-Path $fh 'current\watchdog\watchdog.ps1'), "# current $Current", $Utf8) }
    $fh
}
function Set-LastRun([string]$Fh, [double]$Epoch) {
    $iso = [DateTimeOffset]::FromUnixTimeSeconds([long]$Epoch).ToLocalTime().ToString('o')
    [IO.File]::WriteAllText((Join-Path $Fh 'data\watchdog.json'), (ConvertTo-Json -InputObject @{ last_run = $iso }), $Utf8)
}

# ---------------- R2 launcher ----------------
Write-Host 'launcher (R2)'
& {
    . (Join-Path $Repo 'watchdog\wd-launcher.ps1') -NoRun
    $now = 1800000000.0
    $fh = New-ScratchHome 'l1' @('2.2.0', '2.2.1', '2.3.0', '2.4.0') '2.3.0'
    $cur = Join-Path $fh 'current\watchdog\watchdog.ps1'
    Test-Case 'launcher: fresh install (no watchdog.json) runs current' { Assert-Equal $cur (Select-WatchdogScript -ForgeHome $fh -NowEpoch $now) }
    Test-Case 'launcher: watchdog.json fresh -> current' { Set-LastRun $fh ($now - 120); Assert-Equal $cur (Select-WatchdogScript -ForgeHome $fh -NowEpoch $now) }
    Test-Case 'launcher: stale > 10 min -> highest release BELOW current (never a newer staged one)' {
        Set-LastRun $fh ($now - 601)
        Assert-Equal (Join-Path $fh 'releases\2.2.1\watchdog\watchdog.ps1') (Select-WatchdogScript -ForgeHome $fh -NowEpoch $now)
    }
    Test-Case 'launcher: custom stale threshold' { Assert-Equal $cur (Select-WatchdogScript -ForgeHome $fh -NowEpoch $now -StaleSec 900) }
    Test-Case 'launcher: stale but no previous release -> current' {
        $f2 = New-ScratchHome 'l2' @('2.3.0') '2.3.0'; Set-LastRun $f2 ($now - 9999)
        Assert-Equal (Join-Path $f2 'current\watchdog\watchdog.ps1') (Select-WatchdogScript -ForgeHome $f2 -NowEpoch $now)
    }
    Test-Case 'launcher: current has no watchdog.ps1 -> previous even when fresh' {
        $f3 = New-ScratchHome 'l3' @('2.2.0', '2.3.0') '2.3.0' -NoCurrentWatchdog
        Assert-Equal (Join-Path $f3 'releases\2.2.0\watchdog\watchdog.ps1') (Select-WatchdogScript -ForgeHome $f3 -NowEpoch $now)
    }
    Test-Case 'launcher: unreadable watchdog.json counts as a fresh install' {
        $f4 = New-ScratchHome 'l4' @('2.2.0', '2.3.0') '2.3.0'
        [IO.File]::WriteAllText((Join-Path $f4 'data\watchdog.json'), '{not json', $Utf8)
        Assert-Equal (Join-Path $f4 'current\watchdog\watchdog.ps1') (Select-WatchdogScript -ForgeHome $f4 -NowEpoch $now)
    }
    Test-Case 'launcher: version compare is numeric (2.10 > 2.9)' { Assert-Equal 1 (Compare-LauncherVersion '2.10.0' '2.9.9'); Assert-Equal 0 (Compare-LauncherVersion 'v2.3' '2.3.0') }
}
Test-Case 'launcher: real run passes -ForgeHome/-Port/-DryRun to the chosen watchdog' {
    $fh = New-ScratchHome 'lrun' @('2.3.0') '2.3.0'
    $out = Join-Path $Scratch 'lrun-args.txt'
    [IO.File]::WriteAllText((Join-Path $fh 'current\watchdog\watchdog.ps1'), "param([string]`$ForgeHome, [int]`$Port, [switch]`$DryRun)`r`n[IO.File]::WriteAllText('$out', `"`$ForgeHome|`$Port|`$DryRun`")`r`nexit 7", $Utf8)
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Repo 'watchdog\wd-launcher.ps1') -ForgeHome $fh -Port 19140 -DryRun | Out-Null
    Assert-Equal 7 $LASTEXITCODE 'exit code passed through'
    Assert-Equal "$fh|19140|True" ([IO.File]::ReadAllText($out))
}

# ---------------- watchdog helpers (R1, R2, R5, R7) ----------------
Write-Host 'watchdog (R1 R2 R5 R7)'
& {
    . (Join-Path $Repo 'watchdog\watchdog.ps1') -NoRun
    $st = { param($e = $true, $bv = '2.3.0', $run = '2.3.0', $drain = $null)
        [pscustomobject]@{ eligible = $e; running_version = $run; local_agent = [pscustomobject]@{ online = $true; bundle_version = $bv; draining_to = $drain } } }
    $okBody = [pscustomobject]@{ checks = [pscustomobject]@{ db_write = $true; disk = $true } }
    function RH($HubUp = $true, $HubOk = $true, $Body = $okBody, $Status, $Elapsed = 0, $Req = $true) {
        Get-ReleaseHealth -HubUp $HubUp -HubOk $HubOk -Body $Body -Status $Status -Version '2.3.0' -ElapsedSec $Elapsed -HealthSec 180 -AgentRequired $Req -DrainSec 1800
    }
    Test-Case 'health gate: healthz ok + eligible + agent on the version -> ok' { $r = RH -Status (& $st); Assert-True ($r.ok -and -not $r.pending) $r.reason }
    Test-Case 'health gate: hub not answering -> strike' { $r = RH -HubUp $false -Status $null; Assert-True (-not $r.ok -and -not $r.pending) }
    Test-Case 'health gate: DB write probe failing (503) -> strike' {
        $b = [pscustomobject]@{ checks = [pscustomobject]@{ db_write = $false; disk = $true } }
        $r = RH -HubOk $false -Body $b -Status (& $st); Assert-True (-not $r.ok -and -not $r.pending -and $r.reason -like '*database*')
    }
    Test-Case 'health gate: low disk (503) is pending, never a strike against the release' {
        $b = [pscustomobject]@{ free_gb = 0.4; checks = [pscustomobject]@{ db_write = $true; disk = $false } }
        $r = RH -HubOk $false -Body $b -Status (& $st); Assert-True ($r.pending -and -not $r.ok) $r.reason
    }
    Test-Case 'health gate: not eligible yet -> pending; still not eligible after 180 s -> strike' {
        Assert-True (RH -Status (& $st $false) -Elapsed 30).pending
        $r = RH -Status (& $st $false) -Elapsed 200; Assert-True (-not $r.ok -and -not $r.pending)
    }
    Test-Case 'health gate: agent still on the old version -> pending for 180 s, then strike' {
        Assert-True (RH -Status (& $st $true '2.2.0') -Elapsed 60).pending
        $r = RH -Status (& $st $true '2.2.0') -Elapsed 181; Assert-True (-not $r.ok -and -not $r.pending -and $r.reason -like '*agent did not report*')
    }
    Test-Case 'health gate: agent draining toward the version -> pending up to drain timeout + 180 s' {
        Assert-True (RH -Status (& $st $true '2.2.0' '2.3.0' '2.3.0') -Elapsed 1500).pending
        Assert-True (-not (RH -Status (& $st $true '2.2.0' '2.3.0' '2.3.0') -Elapsed 2000).pending)
    }
    Test-Case 'health gate: agent not required (it was not reporting before the flip) / newer agent ok' {
        Assert-True (RH -Status (& $st $true '2.2.0') -Elapsed 999 -Req $false).ok
        Assert-True (RH -Status (& $st $true '2.3.1')).ok
    }
    Test-Case 'health gate: hub running another version -> strike; no status (2.2 rule) -> healthz decides' {
        Assert-True (-not (RH -Status (& $st $true '2.3.0' '2.2.0')).ok)
        Assert-True (RH -Status $null).ok
    }
    Test-Case 'health gate: pending = restart grace in the 3-strike rollback (not counted)' {
        $prob = @{ from = '2.2.0'; to = '2.3.0'; fails = 2 }
        Assert-Equal 'skip' (Get-RollbackDecision -Probation $prob -CurrentVersion '2.3.0' -HealthOk $false -InGrace $true).action
        Assert-Equal 'rollback' (Get-RollbackDecision -Probation $prob -CurrentVersion '2.3.0' -HealthOk $false).action
    }
    Test-Case 'unhealthy hub: low disk only -> not restarted; db_write=false -> restart, at most 3 per hour' {
        $low = [pscustomobject]@{ free_gb = 0.3; checks = [pscustomobject]@{ db_write = $true; disk = $false } }
        $dbf = [pscustomobject]@{ free_gb = 50; checks = [pscustomobject]@{ db_write = $false; disk = $true } }
        $both = [pscustomobject]@{ free_gb = 0.1; checks = [pscustomobject]@{ db_write = $false; disk = $false } }
        $n = 1800000000.0
        Assert-Equal 'skip' (Get-UnhealthyHubAction -Body $low -Now $n).action
        Assert-Equal 'skip' (Get-UnhealthyHubAction -Body $null -Now $n).action
        $a = Get-UnhealthyHubAction -Body $dbf -Restarts @() -Now $n
        Assert-Equal 'restart' $a.action; Assert-Equal @($n) @($a.restarts)
        Assert-Equal 'restart' (Get-UnhealthyHubAction -Body $both -Now $n).action
        $r3 = @(($n - 3000), ($n - 2000), ($n - 100))
        $b = Get-UnhealthyHubAction -Body $dbf -Restarts $r3 -Now $n
        Assert-Equal 'limit' $b.action; Assert-Equal 3 @($b.restarts).Count
        $c = Get-UnhealthyHubAction -Body $dbf -Restarts @(($n - 4000), ($n - 2000), ($n - 100)) -Now $n
        Assert-Equal 'restart' $c.action 'restarts older than 1 h do not count'; Assert-Equal 3 @($c.restarts).Count
        Assert-Equal 'restart' (Get-UnhealthyHubAction -Body $dbf -Restarts @($null, '') -Now $n).action
    }
    Test-Case 'lease renewal: only while probation is pending, for the same version, every 120 s' {
        $n = 1800000000.0
        $l = [pscustomobject]@{ version = '2.3.0'; url = 'http://h:1'; at = $n - 600 }
        Assert-True (Test-LeaseRenewDue -Lease $l -Version '2.3.0' -Pending $true -Now $n)
        Assert-True (-not (Test-LeaseRenewDue -Lease $l -Version '2.3.0' -Pending $false -Now $n)) 'not pending'
        Assert-True (-not (Test-LeaseRenewDue -Lease $l -Version '2.3.1' -Pending $true -Now $n)) 'other version'
        Assert-True (-not (Test-LeaseRenewDue -Lease $null -Version '2.3.0' -Pending $true -Now $n)) 'no lease'
        $r = @{ version = '2.3.0'; url = 'http://h:1'; at = $n - 600; renewed = $n - 60 }
        Assert-True (-not (Test-LeaseRenewDue -Lease $r -Version '2.3.0' -Pending $true -Now $n)) 'renewed 60 s ago'
        $r.renewed = $n - 121; Assert-True (Test-LeaseRenewDue -Lease $r -Version '2.3.0' -Pending $true -Now $n)
    }
    Test-Case 'watchdog source: pip is bounded and cached; heartbeat before venv/pip; db_write restart wired' {
        $src = Get-Content (Join-Path $Repo 'watchdog\watchdog.ps1') -Raw
        Assert-True ($src -match '--cache-dir') 'pip cache'
        Assert-True ($src -match "\(Get-PipArgs \`$ReleaseDir\) 420") 'pip timeout'
        Assert-True ($src -match 'Write-WdHeartbeat "pip install') 'heartbeat'
        Assert-True ($src -match 'Get-UnhealthyHubAction -Body \$hz\.body') 'decision wired'
        Assert-True ($src -notmatch 'elseif \(-not \$hubOk\) \{ Write-WdLog') 'old unconditional skip gone'
    }
    Test-Case 'healthz reply: 200 ok; 503 with a health body = up but unhealthy; 503 without / 0 = down' {
        $a = ConvertFrom-HealthzReply 200 '{"ok":true,"checks":{"db_write":true,"disk":true}}'; Assert-True ($a.up -and $a.ok)
        $b = ConvertFrom-HealthzReply 503 '{"ok":false,"checks":{"db_write":true,"disk":false},"free_gb":0.2}'; Assert-True ($b.up -and -not $b.ok -and $b.body.free_gb -eq 0.2)
        $c = ConvertFrom-HealthzReply 503 '{"detail":"no leader"}'; Assert-True (-not $c.up)
        $d = ConvertFrom-HealthzReply 0 ''; Assert-True (-not $d.up -and -not $d.ok)
    }
    Test-Case 'elapsed seconds from an ISO time' {
        $iso = [DateTimeOffset]::FromUnixTimeSeconds(1800000000).ToString('o')
        Assert-Equal 100 ([int](Get-ElapsedSec $iso 1800000100)); Assert-Equal 0 ([int](Get-ElapsedSec $null 5)); Assert-Equal 0 ([int](Get-ElapsedSec 'junk' 5))
    }
    Test-Case 'venv plan: unchanged requirements -> none (no pip, no venv); own venv matching -> keep; else build' {
        Assert-Equal 'none' (Get-VenvPlan 'aa' 'aa' '')
        Assert-Equal 'none' (Get-VenvPlan '' 'aa' '')
        Assert-Equal 'build' (Get-VenvPlan 'bb' 'aa' '')
        Assert-Equal 'keep' (Get-VenvPlan 'bb' 'aa' 'bb')
        Assert-Equal 'build' (Get-VenvPlan 'cc' 'aa' 'bb')
    }
    Test-Case 'base python from pyvenv.cfg home, else the venv python' {
        $fh = Join-Path $Scratch 'py'; New-Item -ItemType Directory -Force (Join-Path $fh 'venv') | Out-Null
        $home2 = Split-Path -Parent (Get-Command powershell.exe).Source
        [IO.File]::WriteAllText((Join-Path $fh 'venv\pyvenv.cfg'), "home = $home2`r`nversion = 3.12.0", $Utf8)
        Assert-Equal (Join-Path $fh 'venv\Scripts\python.exe') (Get-BasePython $fh)   # no python.exe under that home
    }

    Test-Case 'task specs: 4 tasks; agents run bin\agent-loop.cmd <lane> under cmd.exe; watchdog runs bin\wd-launcher.ps1' {
        $s = @(Get-ForgeTaskSpecs -ForgeHome 'C:\PD\Forge')
        Assert-Equal @('ForgeHub', 'ForgeAgent', 'ForgeSystemAgent', 'ForgeWatchdog') @($s | ForEach-Object { $_.name })
        Assert-Equal '/d /s /c ""C:\PD\Forge\bin\agent-loop.cmd" user"' $s[1].arguments
        Assert-Equal '/d /s /c ""C:\PD\Forge\bin\agent-loop.cmd" system"' $s[2].arguments
        Assert-True ($s[3].arguments -like '*-File "C:\PD\Forge\bin\wd-launcher.ps1" -ForgeHome "C:\PD\Forge"') $s[3].arguments
        Assert-Equal @('user', 'user', 'system', 'system') @($s | ForEach-Object { $_.user })
        Assert-Equal 3 $s[1].restart.count; Assert-True ($null -eq $s[3].restart); Assert-Equal 'minute' $s[3].trigger
        Assert-True ($s[0].arguments -like '*current\watchdog\run-hub.cmd*')
    }
    Test-Case 'task migration: old 2.2 action -> migrate; 2.3 action -> current; another ForgeHome -> foreign (never touched)' {
        $s = @(Get-ForgeTaskSpecs -ForgeHome 'C:\PD\Forge')
        Assert-Equal 'migrate' (Test-TaskMigration $s[3] 'powershell.exe' '-NoProfile -File "C:\PD\Forge\current\watchdog\watchdog.ps1" -ForgeHome "C:\PD\Forge"' 'C:\PD\Forge')
        Assert-Equal 'migrate' (Test-TaskMigration $s[1] 'powershell.exe' '-File "C:\PD\Forge\current\agent\forge-agent.ps1" -ConfigPath "C:\PD\Forge\data\agent-config.json"' 'C:\PD\Forge')
        Assert-Equal 'current' (Test-TaskMigration $s[1] $s[1].command $s[1].arguments 'C:\PD\Forge')
        Assert-Equal 'current' (Test-TaskMigration $s[3] 'powershell.exe' '-File "C:\PD\Forge\bin\wd-launcher.ps1"' 'C:\PD\Forge')
        Assert-Equal 'foreign' (Test-TaskMigration $s[3] 'powershell.exe' '-File "C:\ProgramData\Forge\current\watchdog\watchdog.ps1"' 'C:\PD\Forge')
        Assert-Equal 'foreign' (Test-TaskMigration $s[3] 'powershell.exe' '-File "C:\PD\Forge2\current\watchdog\watchdog.ps1"' 'C:\PD\Forge')
    }
    Test-Case 'launcher install: copies to bin once; newer version recopied; -DryRun copies nothing' {
        $fh = Join-Path $Scratch 'inst'; $src = Join-Path $fh 'current\watchdog'; New-Item -ItemType Directory -Force $src | Out-Null
        Copy-Item (Join-Path $Repo 'watchdog\wd-launcher.ps1'), (Join-Path $Repo 'watchdog\agent-loop.cmd') $src
        Assert-Equal @('wd-launcher.ps1', 'agent-loop.cmd') (Install-WdLauncher -ForgeHome $fh -DryRun)
        Assert-True (-not (Test-Path (Join-Path $fh 'bin'))) 'dry run wrote files'
        Assert-Equal @('wd-launcher.ps1', 'agent-loop.cmd') (Install-WdLauncher -ForgeHome $fh)
        Assert-True ((Get-FileHash (Join-Path $fh 'bin\agent-loop.cmd')).Hash -eq (Get-FileHash (Join-Path $Repo 'watchdog\agent-loop.cmd')).Hash)
        Assert-Equal @() (Install-WdLauncher -ForgeHome $fh)
        $t = [IO.File]::ReadAllText((Join-Path $src 'wd-launcher.ps1')) -replace '\$LauncherVersion = \d+', '$LauncherVersion = 99'
        [IO.File]::WriteAllText((Join-Path $src 'wd-launcher.ps1'), $t, $Utf8)
        Assert-Equal @('wd-launcher.ps1') (Install-WdLauncher -ForgeHome $fh)
        Assert-Equal 99 (Get-LauncherFileVersion (Join-Path $fh 'bin\wd-launcher.ps1'))
        Assert-Equal 1 (Get-LauncherFileVersion (Join-Path $fh 'bin\agent-loop.cmd'))
    }
    Test-Case 'bundle carries the launcher files (they reach nodes through current\watchdog)' {
        foreach ($f in 'watchdog\wd-launcher.ps1', 'watchdog\agent-loop.cmd') { Assert-True (Test-Path (Join-Path $Repo $f)) $f }
    }
}

# ---------------- R7 agent loop ----------------
Write-Host 'agent loop (R7)'
$loopFh = Join-Path $Scratch 'loop'
New-Item -ItemType Directory -Force (Join-Path $loopFh 'bin'), (Join-Path $loopFh 'current\agent') | Out-Null
Copy-Item (Join-Path $Repo 'watchdog\agent-loop.cmd') (Join-Path $loopFh 'bin')
$loopOut = Join-Path $Scratch 'loop-args.txt'
[IO.File]::WriteAllText((Join-Path $loopFh 'current\agent\forge-agent.ps1'),
    "param([string]`$ConfigPath, [string]`$Lane, [switch]`$Child)`r`n[IO.File]::WriteAllText('$loopOut', `"`$Child|`$Lane|`$ConfigPath`")`r`nexit 3", $Utf8)
Test-Case 'agent loop: runs current\agent\forge-agent.ps1 -Child -Lane <lane> with data\agent-config.json' {
    $env:FORGE_LOOP_ONCE = '1'
    try { & cmd.exe /d /s /c "`"$(Join-Path $loopFh 'bin\agent-loop.cmd')`" system" | Out-Null; $rc = $LASTEXITCODE } finally { Remove-Item Env:FORGE_LOOP_ONCE }
    Assert-Equal 3 $rc 'worker exit code'
    Assert-Equal "True|system|$loopFh\data\agent-config.json" ([IO.File]::ReadAllText($loopOut))
}
Test-Case 'agent loop: default lane user; any other lane refused (exit 2)' {
    $env:FORGE_LOOP_ONCE = '1'
    try {
        & cmd.exe /d /s /c "`"$(Join-Path $loopFh 'bin\agent-loop.cmd')`"" | Out-Null
        Assert-True ([IO.File]::ReadAllText($loopOut) -like 'True|user|*')
        & cmd.exe /d /s /c "`"$(Join-Path $loopFh 'bin\agent-loop.cmd')`" evil" | Out-Null; Assert-Equal 2 $LASTEXITCODE
    } finally { Remove-Item Env:FORGE_LOOP_ONCE }
}
Test-Case 'agent loop + run-hub.cmd: CRLF; run-hub prefers releases\<ver>\venv' {
    Assert-True ([IO.File]::ReadAllText((Join-Path $Repo 'watchdog\agent-loop.cmd')) -match "`r`n")
    $rh = [IO.File]::ReadAllText((Join-Path $Repo 'watchdog\run-hub.cmd'))
    Assert-True ($rh -match [regex]::Escape('if exist "%FORGE_RELEASE_DIR%\venv\Scripts\python.exe" set "PY=%FORGE_RELEASE_DIR%\venv\Scripts\python.exe"'))
}

# ---------------- R5 agent drain timeout ----------------
Write-Host 'agent (R5)'
& {
    . (Join-Path $Repo 'agent\forge-agent.ps1') -NoRun
    Test-Case 'drain timeout: agent-config > hub response > 1800' {
        Assert-Equal 600 (Get-DrainTimeoutSec 600 900); Assert-Equal 900 (Get-DrainTimeoutSec $null 900)
        Assert-Equal 1800 (Get-DrainTimeoutSec $null $null); Assert-Equal 1800 (Get-DrainTimeoutSec 'junk' 0)
    }
    Test-Case 'drain timeout: exits only with jobs still running and after the timeout' {
        Assert-True (Test-DrainTimedOut 1000 2800 1800 2)
        Assert-True (-not (Test-DrainTimedOut 1000 2799 1800 2))
        Assert-True (-not (Test-DrainTimedOut 1000 9999 1800 0))     # idle: the normal "restarting onto new bundle" path
        Assert-True (-not (Test-DrainTimedOut 0 9999 1800 1))
    }
    Test-Case 'agent source: drain timeout exit saves running.json and logs the contract line' {
        $src = Get-Content (Join-Path $Repo 'agent\forge-agent.ps1') -Raw
        Assert-True ($src -match 'drain timeout: restarting with \$\(\$script:Running\.Count\) job\(s\) still running')
        Assert-True ($src -match 'draining_to = \$script:DrainTarget')
    }
}

Remove-Item -LiteralPath $Scratch -Recurse -Force -ErrorAction SilentlyContinue
Write-Host ''
Write-Host "$($script:Pass) passed, $($script:Fail) failed" -ForegroundColor $(if ($script:Fail) { 'Red' } else { 'Green' })
foreach ($m in $script:Failures) { Write-Host "  - $m" -ForegroundColor Red }
exit $script:Fail
