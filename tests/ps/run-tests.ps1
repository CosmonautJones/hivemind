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
        $b = New-SystemLaneReport -NodeId 'n' -Capacity 1 -Running 0 -Boot $true -AgentVersion '2.1.0' -BundleVersion '2.1.0'
        Assert-Equal @('agent_version', 'boot', 'bundle_version', 'capacity', 'lane', 'node_id', 'running') @($b.Keys | Sort-Object)
        Assert-Equal 'system' $b.lane
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
