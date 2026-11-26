# Phase 3 workstream F (first run + upkeep), PowerShell side. Fast and deterministic; nothing real is installed,
# updated or rebooted. Uses only scratch dirs and (for Wake-on-LAN) a UDP sink on 127.0.0.1 in F's port range.
#   agent\actions.ps1   new action builders, git_sync on real scratch repos, Windows Update with fake COM objects,
#                       free_disk selection, the uninstall script generator (never run), ##RESULT lines
#   agent\upkeep.ps1    MAC normalisation, magic packet, WoL request parsing + a real UDP send, user-lane action command
#   agent\services.ps1  OLLAMA_MODELS placement, "starting" state
#   agent\forge-agent.ps1  user-lane action jobs belong to the user lane
#   agent\install.ps1   -NewFleet helpers (tokens, claim code + hash, first-run claim file, bootstrap URL / secrets)
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File tests\ps\p3-upkeep.ps1 [-WolPort 19250]
param([string]$Scratch = (Join-Path ([IO.Path]::GetTempPath()) ("forge-p3f-" + [guid]::NewGuid().ToString('n').Substring(0, 8))),
      [int]$WolPort = 19250)

$ErrorActionPreference = 'Stop'
if (($WolPort -lt 19250 -or $WolPort -gt 19279) -and ($WolPort -lt 19280 -or $WolPort -gt 19299)) { throw 'workstream F test ports are 19250-19279 (or the lead range 19280-19299) only' }
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Py = 'C:\Users\you\forge-hub\.venv\Scripts\python.exe'
New-Item -ItemType Directory -Force $Scratch | Out-Null
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

# ---------------- actions.ps1 ----------------
Write-Host 'actions (2.3)'
& {
    . (Join-Path $Repo 'agent\actions.ps1')
    $cat = Read-ForgeActionCatalog (Join-Path $Repo 'hub\actions.json')
    function Get-Steps($Action, $ArgsObj) { @(Get-ForgeActionSteps -Name $Action -ActionArgs (Test-ForgeActionArgs -Catalog $cat -Name $Action -ActionArgs $ArgsObj) -User 'BOX\u') }

    Test-Case 'build: tool updates are cmdlet steps (Invoke-ForgeToolUpdate)' {
        foreach ($t in 'claude', 'codex', 't3') { $s = Get-Steps "update_$t" @{}; Assert-Equal 'Invoke-ForgeToolUpdate' $s[0].command; Assert-Equal $t $s[0].params.Tool }
    }
    Test-Case 'build: update_ollama = winget upgrade Ollama.Ollama; "no upgrade" exit is ok' {
        $s = Get-Steps 'update_ollama' @{}
        Assert-Equal 'winget' $s[0].exe; Assert-Equal @('upgrade', '--id', 'Ollama.Ollama', '-e', '--silent', '--accept-source-agreements', '--accept-package-agreements') @($s[0].argv)
        Assert-Equal @(-1978335189) @($s[0].ok_exit)
    }
    Test-Case 'build: git_sync / ollama_pull / windows_update / free_disk / forge_uninstall' {
        Assert-Equal 'D:\code\my repo' (Get-Steps 'git_sync' @{ cwd = 'D:\code\my repo' })[0].params.Cwd
        Assert-Equal @('pull', 'qwen2.5:14b') @((Get-Steps 'ollama_pull' @{ model = 'qwen2.5:14b' })[0].argv)
        Assert-Equal 'never' (Get-Steps 'windows_update' @{})[0].params.Reboot
        Assert-Equal 'Invoke-ForgeFreeDisk' (Get-Steps 'free_disk' @{})[0].command
        Assert-Equal $false (Get-Steps 'forge_uninstall' @{})[0].params.KeepData
        Assert-Equal $true (Get-Steps 'forge_uninstall' @{ keep_data = $true })[0].params.KeepData
    }
    Test-Case 'validate: bool type is strict' {
        Assert-Throws { Test-ForgeActionArgs -Catalog $cat -Name 'forge_uninstall' -ActionArgs @{ keep_data = 'true' } } '*true or false*'
    }
    Test-Case 'run: native ok_exit maps to 0, other codes kept' {
        function Resolve-ForgeExe([string]$Name) { (Get-Command powershell.exe).Source }
        Assert-Equal 0 (Invoke-ForgeActionStep @{ kind = 'native'; exe = 'x'; argv = [string[]]@('-NoProfile', '-Command', 'exit 5'); ok_exit = @(5) } $false)
        Assert-Equal 6 (Invoke-ForgeActionStep @{ kind = 'native'; exe = 'x'; argv = [string[]]@('-NoProfile', '-Command', 'exit 6'); ok_exit = @(5) } $false)
    }
    Test-Case 'run: a cmdlet step may set its exit code ($script:ActionExit)' {
        function Invoke-T { $script:ActionExit = 3; $script:ActionResult = @{ result = 'x' } }
        Assert-Equal 3 (Invoke-ForgeActionStep @{ kind = 'cmdlet'; command = 'Invoke-T'; params = @{} } $false)
        Assert-Equal 'x' $script:ActionResult.result
    }
    Test-Case 'result line: ##RESULT + compact json; default result from the exit code' {
        Assert-Equal '##RESULT {"result":"ok","exit":0,"action":"free_disk"}' (Format-ActionResult (Get-DefaultActionResult 'free_disk' 0 $false))
        Assert-Equal 'failed' (Get-DefaultActionResult 'x' 2 $false).result; Assert-Equal 'dry_run' (Get-DefaultActionResult 'x' 0 $true).result
    }
    Test-Case 'tool update plan: claude native -> claude update; npm shim -> npm; codex npm; t3 update; missing -> none' {
        Assert-Equal @('update') @((Get-ToolUpdatePlan 'claude' 'C:\Users\u\.local\bin\claude.exe' $true).argv)
        $p = Get-ToolUpdatePlan 'claude' 'C:\Users\u\AppData\Roaming\npm\claude.cmd' $true; Assert-Equal 'npm' $p.exe; Assert-Equal @('install', '-g', '@anthropic-ai/claude-code@latest') @($p.argv)
        Assert-Equal $null (Get-ToolUpdatePlan 'claude' $null $true)
        Assert-Equal @('install', '-g', '@openai/codex@latest') @((Get-ToolUpdatePlan 'codex' 'C:\x\codex.cmd' $true).argv)
        Assert-Equal 'codex' (Get-ToolUpdatePlan 'codex' 'C:\x\codex.exe' $false).exe
        Assert-Equal @('update') @((Get-ToolUpdatePlan 't3' 'C:\x\t3.cmd' $false).argv); Assert-Equal $null (Get-ToolUpdatePlan 't3' $null $true)
    }
    Test-Case 'tool update: not installed -> result not_installed, exit 0' {
        function Resolve-ForgeExe([string]$Name) { throw "$Name not found" }
        $script:ActionResult = $null; $script:ActionExit = $null
        Invoke-ForgeToolUpdate -Tool 't3' 6>$null | Out-Null
        Assert-Equal 'not_installed' $script:ActionResult.result; Assert-Equal $null $script:ActionExit
    }
    Test-Case 'git sync decision table (never pushes; only fast-forwards a clean, not-ahead branch)' {
        Assert-Equal 'no_upstream' (Get-GitSyncDecision -HasUpstream $false -Dirty $false -Behind 3)
        Assert-Equal 'uptodate' (Get-GitSyncDecision -HasUpstream $true -Dirty $true -Ahead 2 -Behind 0)
        Assert-Equal 'diverged' (Get-GitSyncDecision -HasUpstream $true -Dirty $false -Ahead 1 -Behind 1)
        Assert-Equal 'dirty' (Get-GitSyncDecision -HasUpstream $true -Dirty $true -Ahead 0 -Behind 1)
        Assert-Equal 'ff' (Get-GitSyncDecision -HasUpstream $true -Dirty $false -Ahead 0 -Behind 4)
    }

    # ---- git_sync on real scratch repositories ----
    $gitExe = (Get-Command git.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
    if ($gitExe) {
        $git = $gitExe
        $ErrorActionPreference = 'Continue'      # git prints warnings on stderr
        $g = { param($d) $a = @($args); & $git -C $d -c user.name=t -c user.email=t@example.invalid -c commit.gpgsign=false @a 2>&1 | Out-Null; if ($LASTEXITCODE) { throw "git $($a -join ' ') failed in $d" } }
        $root = Join-Path $Scratch 'git'
        $bare = Join-Path $root 'remote.git'; $up = Join-Path $root 'upstream'; $work = Join-Path $root 'work'
        New-Item -ItemType Directory -Force $root | Out-Null
        & $git init --bare -q $bare | Out-Null
        & $git clone -q $bare $up 2>&1 | Out-Null
        [IO.File]::WriteAllText((Join-Path $up 'a.txt'), "1`n"); & $g $up add a.txt; & $g $up commit -q -m one
        & $g $up branch -M main; & $g $up push -q -u origin main
        & $git clone -q -b main $bare $work 2>&1 | Out-Null
        $sync = { $script:ActionResult = $null; $script:ActionExit = $null; Invoke-ForgeGitSync -Cwd $work | Out-Null; $script:ActionResult }
        $push = { param($n) [IO.File]::WriteAllText((Join-Path $up 'a.txt'), "$n`n"); & $g $up commit -q -am "c$n"; & $g $up push -q }
        function Resolve-ForgeExe([string]$Name) { if ($Name -eq 'git') { return $gitExe }; throw 'no' }   # (Invoke-ForgeGitSync has its own $git)
        function Write-ActionOut([string]$s) { }
        Test-Case 'git_sync: up to date' { Assert-Equal 'uptodate' (& $sync).result }
        Test-Case 'git_sync: behind + clean -> ff (fast-forwarded)' {
            & $push 2; $r = & $sync
            Assert-Equal 'ff' $r.result; Assert-Equal 'main' $r.branch; Assert-Equal '2' ([IO.File]::ReadAllText((Join-Path $work 'a.txt'))).Trim()
        }
        Test-Case 'git_sync: behind + local changes -> dirty (nothing touched)' {
            & $push 3; [IO.File]::WriteAllText((Join-Path $work 'a.txt'), "local`n")
            Assert-Equal 'dirty' (& $sync).result; Assert-Equal "local`n" ([IO.File]::ReadAllText((Join-Path $work 'a.txt')))
            & $g $work checkout -- a.txt
        }
        Test-Case 'git_sync: local commit + remote commit -> diverged, never pushed' {
            [IO.File]::WriteAllText((Join-Path $work 'b.txt'), "b`n"); & $g $work add b.txt; & $g $work commit -q -m local
            $r = & $sync
            Assert-Equal 'diverged' $r.result; Assert-Equal 1 $r.ahead
            $remoteLog = (& $git -C $bare log --oneline main | Out-String)
            Assert-True ($remoteLog -notmatch 'local') 'the local commit must never reach the remote'
        }
        Test-Case 'git_sync: detached HEAD -> no_upstream; not a repo / missing -> error exit 1' {
            & $g $work checkout -q --detach; Assert-Equal 'no_upstream' (& $sync).result
            $script:ActionResult = $null; Invoke-ForgeGitSync -Cwd (Join-Path $root 'nope') | Out-Null
            Assert-Equal 'error' $script:ActionResult.result; Assert-Equal 1 $script:ActionExit
            $script:ActionResult = $null; $script:ActionExit = $null; Invoke-ForgeGitSync -Cwd $root | Out-Null
            Assert-Equal 'error' $script:ActionResult.result
        }
    } else { Write-Host '  (git not found: git_sync repo tests skipped)' -ForegroundColor DarkYellow }

    # ---- Windows Update with fake COM objects ----
    function New-FakeUpdate([string]$Title, [string[]]$Kb, [bool]$NeedsInput = $false) {
        $u = [pscustomobject]@{ Title = $Title; KBArticleIDs = $Kb; EulaAccepted = $false; InstallationBehavior = [pscustomobject]@{ CanRequestUserInput = $NeedsInput }; accepted = $false }
        $u | Add-Member ScriptMethod AcceptEula { $this.accepted = $true; $this.EulaAccepted = $true }
        $u
    }
    function New-FakeWuSession([object[]]$Updates, [int[]]$Codes, [bool]$Reboot) {
        $list = [pscustomobject]@{ items = $Updates; Count = @($Updates).Count }
        $list | Add-Member ScriptMethod Item { param($i) $this.items[$i] }
        $searcher = [pscustomobject]@{ list = $list }
        $searcher | Add-Member ScriptMethod Search { param($q) $script:WuQuery = $q; [pscustomobject]@{ Updates = $this.list } }
        $res = [pscustomobject]@{ codes = $Codes; RebootRequired = $Reboot }
        $res | Add-Member ScriptMethod GetUpdateResult { param($i) [pscustomobject]@{ ResultCode = $this.codes[$i] } }
        $s = [pscustomobject]@{ searcher = $searcher; res = $res; downloaded = $false }
        $s | Add-Member ScriptMethod CreateUpdateSearcher { $this.searcher }
        $s | Add-Member ScriptMethod CreateUpdateDownloader { $o = [pscustomobject]@{ Updates = $null; s = $this }; $o | Add-Member ScriptMethod Download { $this.s.downloaded = $true }; $o }
        $s | Add-Member ScriptMethod CreateUpdateInstaller { $o = [pscustomobject]@{ Updates = $null; r = $this.res }; $o | Add-Member ScriptMethod Install { $this.r }; $o }
        $s
    }
    $newColl = { $c = New-Object System.Collections.ArrayList; , $c }
    Test-Case 'windows update: installs software updates (skips ones that need input), reports KBs and reboot' {
        function Write-ActionOut([string]$s) { }
        $u1 = New-FakeUpdate 'Cumulative Update' @('5050001'); $u2 = New-FakeUpdate 'Defender' @('2267602'); $u3 = New-FakeUpdate 'Needs a click' @('1') $true
        $s = New-FakeWuSession @($u1, $u2, $u3) @(2, 4) $true
        $script:ActionResult = $null
        Invoke-ForgeWindowsUpdate -Session $s -NewCollection $newColl -SystemInfo ([pscustomobject]@{ RebootRequired = $false })
        $r = $script:ActionResult
        Assert-Equal 1 $r.installed; Assert-Equal 1 $r.failed; Assert-Equal $true $r.reboot_required; Assert-Equal 3 $r.found
        Assert-Equal @('KB5050001', 'KB2267602') @($r.kb); Assert-True $u1.accepted 'EULA accepted'; Assert-True $s.downloaded
        Assert-True ($script:WuQuery -match "IsInstalled=0" -and $script:WuQuery -match "Type='Software'")
    }
    Test-Case 'windows update: nothing found -> 0/0, reboot from SystemInfo (pending from an earlier run)' {
        function Write-ActionOut([string]$s) { }
        $s = New-FakeWuSession @() @() $false
        $script:ActionResult = $null
        Invoke-ForgeWindowsUpdate -Session $s -NewCollection $newColl -SystemInfo ([pscustomobject]@{ RebootRequired = $true })
        Assert-Equal 0 $script:ActionResult.installed; Assert-Equal $true $script:ActionResult.reboot_required; Assert-True (-not $s.downloaded)
        Assert-Throws { Invoke-ForgeWindowsUpdate -Reboot 'always' -Session $s } '*unsupported*'
    }

    Test-Case 'free disk: only items older than the cut-off, never skipped paths' {
        $now = Get-Date '2026-11-25T12:00:00'
        $items = @([pscustomobject]@{ FullName = 'a'; LastWriteTime = $now.AddDays(-8) }, [pscustomobject]@{ FullName = 'b'; LastWriteTime = $now.AddDays(-2) },
                   [pscustomobject]@{ FullName = 'c'; LastWriteTime = $now.AddDays(-30) })
        Assert-Equal @('a', 'c') @((Select-OldItems -Items $items -Now $now -MaxAgeDays 7) | ForEach-Object { $_.FullName })
        Assert-Equal @('c') @((Select-OldItems -Items $items -Now $now -MaxAgeDays 14 -Skip @('a')) | ForEach-Object { $_.FullName })
        Assert-Equal 0 (Select-OldItems -Items @() -Now $now).Count
    }
    Test-Case 'forge home from the script location (current\agent or releases\<ver>\agent)' {
        $fh = Join-Path $Scratch 'FH'; New-Item -ItemType Directory -Force (Join-Path $fh 'data'), (Join-Path $fh 'releases\2.3.0\agent') | Out-Null
        [IO.File]::WriteAllText((Join-Path $fh 'data\config.json'), '{}')
        Assert-Equal $fh (Get-ForgeHomeFromScript (Join-Path $fh 'releases\2.3.0\agent'))
        Assert-Equal $null (Get-ForgeHomeFromScript (Join-Path $Scratch 'elsewhere\agent'))
    }
    Test-Case 'forge home: a repo checkout with data\config.json is never the Forge home' {
        $repoish = Join-Path $Scratch 'checkout'; New-Item -ItemType Directory -Force (Join-Path $repoish 'data'), (Join-Path $repoish 'agent'), (Join-Path $repoish 'releases') | Out-Null
        [IO.File]::WriteAllText((Join-Path $repoish 'data\config.json'), '{}')
        Assert-Equal $null (Get-ForgeHomeFromScript (Join-Path $repoish 'agent'))
        $fh2 = Join-Path $Scratch 'FH2'; New-Item -ItemType Directory -Force (Join-Path $fh2 'data'), (Join-Path $fh2 'current\agent'), (Join-Path $fh2 'releases') | Out-Null
        [IO.File]::WriteAllText((Join-Path $fh2 'data\config.json'), '{}')
        Assert-Equal $fh2 (Get-ForgeHomeFromScript (Join-Path $fh2 'current\agent'))
        Remove-Item -LiteralPath (Join-Path $fh2 'releases') -Force
        Assert-Equal $null (Get-ForgeHomeFromScript (Join-Path $fh2 'current\agent')) 'no releases\ -> not a Forge home'
    }
    Test-Case 'uninstall: forge home must match the registered ForgeSystemAgent task' {
        $line = 'powershell.exe -NoProfile -File "C:\ProgramData\Forge\current\agent\forge-agent.ps1" -Lane system C:\ProgramData\Forge\data'
        Assert-True (Test-ForgeHomeInTaskActions 'C:\ProgramData\Forge' @($line))
        Assert-True (Test-ForgeHomeInTaskActions 'c:\programdata\forge\' @($line)) 'case / trailing slash'
        Assert-True (-not (Test-ForgeHomeInTaskActions 'C:\Users\u\forge-hub' @($line))) 'other folder accepted'
        Assert-True (-not (Test-ForgeHomeInTaskActions 'C:\ProgramData\Forg' @($line))) 'prefix accepted'
        Assert-True (-not (Test-ForgeHomeInTaskActions 'C:\ProgramData\Forge' @())) 'no task accepted'
    }
    Test-Case 'uninstall: script folder ACL is SYSTEM + Administrators only, inheritance off' {
        $sec = New-ForgeSystemOnlyDirSecurity
        Assert-True $sec.AreAccessRulesProtected 'inheritance not off'
        $sids = @($sec.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]) | ForEach-Object { $_.IdentityReference.Value } | Sort-Object)
        Assert-Equal @('S-1-5-18', 'S-1-5-32-544') $sids
    }

    function New-TestJunction([string]$Link, [string]$Target) {
        & cmd.exe /d /c "mklink /J `"$Link`" `"$Target`"" | Out-Null
        if (-not (Test-Path -LiteralPath $Link)) { throw "mklink /J failed for $Link" }
    }
    function Set-TreeTime([string]$Path, [datetime]$When) {
        foreach ($i in @(Get-ChildItem -LiteralPath $Path -Recurse -Force -Attributes !ReparsePoint) + @(Get-Item -LiteralPath $Path -Force)) { $i.LastWriteTime = $When }
    }

    Test-Case 'free disk: junctions in Temp are removed, never followed (target survives)' {
        function Write-ActionOut([string]$s) { }
        $base = Join-Path $Scratch 'fd-junction'; $root = Join-Path $base 'Temp'; $target = Join-Path $base 'precious'
        New-Item -ItemType Directory -Force $root, $target, (Join-Path $root 'old') | Out-Null
        [IO.File]::WriteAllText((Join-Path $target 'keep.txt'), 'do not delete')
        [IO.File]::WriteAllText((Join-Path $root 'old\f.txt'), '12345')
        [IO.File]::WriteAllText((Join-Path $root 'plain.txt'), 'x')
        New-TestJunction (Join-Path $root 'link') $target
        New-TestJunction (Join-Path $root 'old\inner') $target
        Assert-Equal 0 (Get-ItemBytes (Get-Item -LiteralPath (Join-Path $root 'link') -Force)) 'link sized'
        Assert-Equal 5 (Get-ItemBytes (Get-Item -LiteralPath (Join-Path $root 'old') -Force)) 'size went through the inner link'
        $script:ActionResult = $null
        Invoke-ForgeFreeDisk -Roots @(@{ dir = $root; days = 7 }) -RecycleDirs @() -Now (Get-Date).AddDays(30)
        Assert-True (Test-Path -LiteralPath (Join-Path $target 'keep.txt')) 'junction target was deleted'
        Assert-Equal 0 @(Get-ChildItem -LiteralPath $root -Force).Count 'temp not emptied'
        Assert-Equal 3 $script:ActionResult.removed
    }
    Test-Case 'free disk: a Temp root that is itself a junction is skipped' {
        function Write-ActionOut([string]$s) { }
        $base = Join-Path $Scratch 'fd-rootlink'; $target = Join-Path $base 'precious'
        New-Item -ItemType Directory -Force $target | Out-Null
        [IO.File]::WriteAllText((Join-Path $target 'keep.txt'), 'x')
        New-TestJunction (Join-Path $base 'Temp') $target
        Invoke-ForgeFreeDisk -Roots @(@{ dir = (Join-Path $base 'Temp'); days = 7 }) -RecycleDirs @() -Now (Get-Date).AddDays(30)
        Assert-True (Test-Path -LiteralPath (Join-Path $target 'keep.txt')) 'followed the root junction'
        Assert-True (-not (Test-ForgeNoReparseChain (Join-Path $base 'Temp'))); Assert-True (Test-ForgeNoReparseChain $target)
    }
    Test-Case 'free disk: scratch folders are aged by their newest file, not the folder date' {
        function Write-ActionOut([string]$s) { }
        $root = Join-Path $Scratch 'fd-scratch'
        New-Item -ItemType Directory -Force (Join-Path $root 'active\sub'), (Join-Path $root 'stale\sub') | Out-Null
        [IO.File]::WriteAllText((Join-Path $root 'active\sub\new.txt'), 'today')
        [IO.File]::WriteAllText((Join-Path $root 'stale\sub\old.txt'), 'long ago')
        Set-TreeTime (Join-Path $root 'stale') (Get-Date).AddDays(-30)
        (Get-Item -LiteralPath (Join-Path $root 'active') -Force).LastWriteTime = (Get-Date).AddDays(-30)
        Invoke-ForgeFreeDisk -Roots @(@{ dir = $root; days = 14 }) -RecycleDirs @()
        Assert-True (Test-Path -LiteralPath (Join-Path $root 'active\sub\new.txt')) 'active workspace deleted'
        Assert-True (-not (Test-Path -LiteralPath (Join-Path $root 'stale'))) 'stale workspace kept'
    }
    Test-Case "free disk: every user's recycle bin is emptied (desktop.ini kept, links not followed)" {
        function Write-ActionOut([string]$s) { }
        $base = Join-Path $Scratch 'fd-bin'; $bin = Join-Path $base 'S-1-5-21-1-2-3-1001'; $target = Join-Path $base 'precious'
        New-Item -ItemType Directory -Force $bin, $target, (Join-Path $bin '$Rdir1') | Out-Null
        [IO.File]::WriteAllText((Join-Path $target 'keep.txt'), 'x'); [IO.File]::WriteAllText((Join-Path $bin 'desktop.ini'), 'x')
        [IO.File]::WriteAllText((Join-Path $bin '$RABC.txt'), 'deleted file'); [IO.File]::WriteAllText((Join-Path $bin '$Rdir1\a.txt'), 'x')
        New-TestJunction (Join-Path $bin '$Rlink') $target
        Invoke-ForgeFreeDisk -Roots @() -RecycleDirs @($bin)
        Assert-Equal @('desktop.ini') @(Get-ChildItem -LiteralPath $bin -Force | ForEach-Object { $_.Name })
        Assert-True (Test-Path -LiteralPath (Join-Path $target 'keep.txt')) 'followed a link in the bin'
    }
    Test-Case 'uninstall script (generated, never run here): parses, stops the 4 tasks, keep_data keeps data\' {
        foreach ($keep in $true, $false) {
            $src = New-ForgeUninstallScript -ForgeHome "C:\Program Data\O'Forge" -KeepData $keep
            $t = $null; $e = $null; [void][Management.Automation.Language.Parser]::ParseInput($src, [ref]$t, [ref]$e)
            Assert-Equal 0 @($e).Count (($e | ForEach-Object { $_.Message }) -join '; ')
            Assert-True ($src -match [regex]::Escape("`$fh = 'C:\Program Data\O''Forge'")) 'quoted forge home'
            foreach ($n in 'ForgeWatchdog', 'ForgeAgent', 'ForgeHub', 'ForgeSystemAgent') { Assert-True ($src -match $n) "missing $n" }
            Assert-True ($src -match "Remove-NetFirewallRule -DisplayName 'Forge Control hub \(8787\)'")
            Assert-True ($src -match [regex]::Escape("`$keep = $(if ($keep) { '$true' } else { '$false' })")) 'keep flag'
        }
    }
}

# ---------------- upkeep.ps1 ----------------
Write-Host 'upkeep (agent)'
& {
    . (Join-Path $Repo 'agent\actions.ps1')
    . (Join-Path $Repo 'agent\upkeep.ps1')
    Test-Case 'mac normalisation' {
        Assert-Equal 'AA-BB-CC-DD-EE-FF' (ConvertTo-NetMac 'aa:bb:cc:dd:ee:ff'); Assert-Equal 'AA-BB-CC-DD-EE-01' (ConvertTo-NetMac 'AABBCCDDEE01')
        Assert-Equal $null (ConvertTo-NetMac '00-00-00-00-00-00'); Assert-Equal $null (ConvertTo-NetMac 'xyz'); Assert-Equal $null (ConvertTo-NetMac '')
    }
    Test-Case 'magic packet = 6 x FF + 16 x MAC (102 bytes)' {
        $p = New-MagicPacket 'AA-BB-CC-DD-EE-01'
        Assert-Equal 102 $p.Length; Assert-Equal 255 ([int]$p[5]); Assert-Equal 170 ([int]$p[6]); Assert-Equal 1 ([int]$p[11]); Assert-Equal 1 ([int]$p[101])
        Assert-Throws { New-MagicPacket 'nope' } '*bad MAC*'
    }
    Test-Case 'wol interfaces: up adapters with a real IPv4 only, one entry per address' {
        $rows = @([pscustomobject]@{ Name = 'Ethernet'; MacAddress = 'AA-BB-CC-DD-EE-01'; Status = 'Up'; IPs = @(@{ ip = '192.168.1.10'; prefix = 24 }, @{ ip = '169.254.3.4'; prefix = 16 }) },
                  [pscustomobject]@{ Name = 'Wi-Fi'; MacAddress = 'AA-BB-CC-DD-EE-02'; Status = 'Disconnected'; IPs = @(@{ ip = '192.168.1.2'; prefix = 8 }) },
                  [pscustomobject]@{ Name = 'NoIp'; MacAddress = 'AA-BB-CC-DD-EE-03'; Status = 'Up'; IPs = @() })
        $r = Select-WolInterfaces $rows
        Assert-Equal 1 @($r).Count; Assert-Equal 'AA-BB-CC-DD-EE-01' $r[0].mac; Assert-Equal 24 $r[0].prefix; Assert-Equal 'Ethernet' $r[0].iface
        Assert-True ((ConvertTo-Json -InputObject @{ macs = @($r) } -Compress) -match '"macs":\[') 'one adapter stays an array'
    }
    Test-Case 'wol requests from the hub response are validated (mac, IPv4 broadcast, port)' {
        $resp = '{"wol":[{"mac":"aa-bb-cc-dd-ee-01","broadcast":"192.168.1.255","port":9},{"mac":"bad"},{"mac":"AA-BB-CC-DD-EE-02","broadcast":"::1"},{"mac":"AA-BB-CC-DD-EE-03"}]}' | ConvertFrom-Json
        $r = Get-WolRequests $resp
        Assert-Equal 2 @($r).Count; Assert-Equal 'AA-BB-CC-DD-EE-01' $r[0].mac; Assert-Equal '255.255.255.255' $r[1].broadcast; Assert-Equal 9 $r[1].port
        Assert-Equal 0 (Get-WolRequests ('{"jobs":[]}' | ConvertFrom-Json)).Count
        Assert-Equal 1 (Get-WolRequests ('{"wol":{"mac":"AA-BB-CC-DD-EE-04","broadcast":"192.168.1.255","port":7}}' | ConvertFrom-Json)).Count
    }
    Test-Case "Send-WakeOnLan delivers the magic packet over UDP (sink on 127.0.0.1:$WolPort)" {
        $sink = New-Object Net.Sockets.UdpClient((New-Object Net.IPEndPoint([Net.IPAddress]::Loopback, $WolPort)))
        try {
            $sink.Client.ReceiveTimeout = 3000
            Assert-True (Send-WakeOnLan -Mac 'AA-BB-CC-DD-EE-01' -Broadcast '127.0.0.1' -Port $WolPort) 'send failed'
            $from = New-Object Net.IPEndPoint([Net.IPAddress]::Any, 0)
            $got = $sink.Receive([ref]$from)
            Assert-Equal 102 $got.Length; Assert-Equal 170 ([int]$got[6])
            Assert-True (-not (Send-WakeOnLan -Mac 'bad' -Broadcast '127.0.0.1' -Port $WolPort)) 'bad mac must not send'
        } finally { $sink.Close() }
    }
    Test-Case 'Invoke-UpkeepResponse sends each wol item and never throws' {
        $script:sent = @()
        function Send-WakeOnLan([string]$Mac, [string]$Broadcast, [int]$Port) { $script:sent += "$Mac@$Broadcast"; $true }
        Invoke-UpkeepResponse ('{"wol":[{"mac":"AA-BB-CC-DD-EE-01","broadcast":"192.168.1.255","port":9}]}' | ConvertFrom-Json)
        Invoke-UpkeepResponse $null; Invoke-UpkeepResponse 'garbage'
        Assert-Equal @('AA-BB-CC-DD-EE-01@192.168.1.255') @($script:sent)
    }
    Test-Case 'Add-UpkeepReport: macs (cached), ollama models + dir when ollama is installed, never throws' {
        function Get-NetworkMacs { , @([ordered]@{ iface = 'E'; mac = 'AA-BB-CC-DD-EE-01'; ip = '192.168.1.10'; prefix = 24 }) }
        function Get-OllamaModelNames([string]$BaseUrl) { , @('qwen2.5:14b') }
        function Get-OllamaModelsDir { param($Config, [string]$WorkRoot) 'D:\forge\ollama\models' }
        $Tools = @{ ollama = 'C:\o.exe' }; $cfg = [pscustomobject]@{}
        $b = @{ metrics = @{} }; Add-UpkeepReport $b
        Assert-Equal 'AA-BB-CC-DD-EE-01' $b.metrics.macs[0].mac; Assert-Equal @('qwen2.5:14b') @($b.metrics.ollama_models); Assert-Equal 'D:\forge\ollama\models' $b.metrics.ollama_models_dir
        $cfg = [pscustomobject]@{ wol_enabled = $false }; $b = @{ metrics = @{} }; Add-UpkeepReport $b
        Assert-True (-not $b.metrics.ContainsKey('macs')) 'wol_enabled=false: no macs'
        Add-UpkeepReport $null
    }
    Test-Case 'user-lane action command: spec.json written, system-lane / unknown / bad args refused' {
        $cat = Read-ForgeActionCatalog (Join-Path $Repo 'hub\actions.json')
        $jd = Join-Path $Scratch 'job1'; New-Item -ItemType Directory -Force $jd | Out-Null
        $job = '{"id":"j1","engine":"action","run_as":"user","action":"git_sync","args":{"cwd":"D:\\code\\r"}}' | ConvertFrom-Json
        $c = Get-UserActionCommand -Job $job -Catalog $cat -JobDir $jd -AgentDir 'C:\FH\current\agent' -CatalogPath 'C:\FH\current\hub\actions.json' -Tools @{ git = 'C:\git.exe'; node = 'x' } -Config ([pscustomobject]@{}) -User 'BOX\u'
        Assert-True (-not $c.error) "$($c.error)"
        Assert-True ($c.inner -like 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\FH\current\agent\actions.ps1" -ActionRunSpec "*spec.json" -ActionCatalogPath "C:\FH\current\hub\actions.json"')
        $spec = [IO.File]::ReadAllText((Join-Path $jd 'spec.json')) | ConvertFrom-Json
        Assert-Equal 'git_sync' $spec.action; Assert-Equal 'D:\code\r' $spec.args.cwd; Assert-Equal 'C:\git.exe' $spec.tools.git; Assert-True (-not $spec.tools.node); Assert-Equal $false $spec.dry_run
        foreach ($bad in @('{"action":"reboot","args":{}}', '{"action":"nope","args":{}}', '{"action":"git_sync","args":{"cwd":"x"}}')) {
            $r = Get-UserActionCommand -Job ($bad | ConvertFrom-Json) -Catalog $cat -JobDir $jd -AgentDir 'a' -CatalogPath 'c' -User 'u'
            Assert-True ($r.error -like 'action rejected by agent:*') "accepted $bad"
        }
        Assert-True ((Get-UserActionCommand -Job $job -Catalog $null -JobDir $jd -AgentDir 'a' -CatalogPath 'c').error -like '*catalog missing*')
    }
}

# ---------------- services.ps1 (F6b / F6c) ----------------
Write-Host 'services (2.3)'
& {
    . (Join-Path $Repo 'agent\services.ps1')
    Test-Case 'OLLAMA_MODELS dir: work_root on another drive; config wins; never when already set or on the system drive' {
        Assert-Equal 'D:\forge\ollama\models' (Get-OllamaModelsDir -Config $null -WorkRoot 'D:\forge' -Existing $null -SystemDrive 'C:')
        Assert-Equal $null (Get-OllamaModelsDir -Config $null -WorkRoot 'C:\forge' -Existing $null -SystemDrive 'C:')
        Assert-Equal $null (Get-OllamaModelsDir -Config $null -WorkRoot 'D:\forge' -Existing 'E:\models' -SystemDrive 'C:')
        Assert-Equal 'E:\m' (Get-OllamaModelsDir -Config ([pscustomobject]@{ ollama_models_dir = 'E:\m' }) -WorkRoot 'C:\forge' -Existing $null -SystemDrive 'C:')
        Assert-Equal $null (Get-OllamaModelsDir -Config $null -WorkRoot $null -Existing $null)
    }
    Test-Case 'ollama service env gets OLLAMA_MODELS (unless the service already sets it); grace default 60' {
        function Get-OllamaModelsDir { param($Config, [string]$WorkRoot) if ($WorkRoot) { "$WorkRoot\ollama\models" } }
        $d = Get-ForgeServices ([pscustomobject]@{}) 'D:\forge'
        $o = $d | Where-Object { $_.name -eq 'ollama' }
        Assert-Equal 'D:\forge\ollama\models' $o.env.OLLAMA_MODELS; Assert-Equal '127.0.0.1:11434' $o.env.OLLAMA_HOST; Assert-Equal 60 $o.starting_grace_s
        Assert-True (-not ($d | Where-Object { $_.name -eq 't3' }).env.ContainsKey('OLLAMA_MODELS')) 't3 untouched'
        $j = Get-ForgeServices ('{"services":[{"name":"ollama","start":"<ollama.exe> serve","health":"http://h/","env":{"OLLAMA_MODELS":"Z:\\m"},"starting_grace_s":90}]}' | ConvertFrom-Json) 'D:\forge'
        Assert-Equal 'Z:\m' $j[0].env.OLLAMA_MODELS; Assert-Equal 90 $j[0].starting_grace_s
        Assert-True (-not (Get-ForgeServices ([pscustomobject]@{}))[1].env.ContainsKey('OLLAMA_MODELS')) 'no work root -> no OLLAMA_MODELS'
    }
    Test-Case 'service state: ok / starting within grace after a start / down' {
        Assert-Equal 'ok' (Get-ServiceState -Ok $true -LastStart 100 -Now 101)
        Assert-Equal 'starting' (Get-ServiceState -Ok $false -LastStart 100 -Now 159)
        Assert-Equal 'down' (Get-ServiceState -Ok $false -LastStart 100 -Now 161)
        Assert-Equal 'down' (Get-ServiceState -Ok $false -LastStart 0 -Now 5)
        Assert-Equal 'starting' (Get-ServiceState -Ok $false -LastStart 100 -Now 180 -Grace 90)
    }
    Test-Case 'service metrics carry state; ok stays a bool equal to state -eq ok' {
        $t = @{ ollama = @{ ok = $false; since = 10 }; t3 = @{ ok = $true; since = 5 } }
        $m = Get-ServiceMetrics $t @{ ollama = @{ last_start = 100.0; grace = 60 } } 120
        Assert-Equal 'starting' $m[0].state; Assert-Equal $false $m[0].ok; Assert-Equal 'ok' $m[1].state; Assert-Equal $true $m[1].ok
        Assert-Equal 'down' (Get-ServiceMetrics $t @{} 1000)[0].state
    }
}

# ---------------- forge-agent.ps1 lane routing ----------------
Write-Host 'agent lanes (2.3)'
& {
    . (Join-Path $Repo 'agent\forge-agent.ps1') -NoRun
    Test-Case 'user-lane action (run_as user) runs in the user lane; other actions stay SYSTEM' {
        $ua = [pscustomobject]@{ engine = 'action'; run_as = 'user'; action = 'update_claude' }
        $sa = [pscustomobject]@{ engine = 'action'; run_as = 'system'; action = 'reboot' }
        $old = [pscustomobject]@{ engine = 'action'; action = 'reboot' }
        Assert-True (Test-JobLane $ua 'user'); Assert-True (-not (Test-JobLane $ua 'system'))
        Assert-True (Test-JobLane $sa 'system'); Assert-True (-not (Test-JobLane $sa 'user'))
        Assert-True (Test-JobLane $old 'system') 'no run_as = system (2.2 hubs)'
        Assert-True (Test-JobLane ([pscustomobject]@{ engine = 'admin'; run_as = 'user' }) 'system') 'admin is always SYSTEM'
    }
}

# ---------------- install.ps1 -NewFleet helpers ----------------
Write-Host 'installer (2.3)'
& {
    . (Join-Path $Repo 'agent\install.ps1') -NoRun
    Test-Case 'fleet tokens: 32-char urlsafe, distinct; existing tokens kept (re-running -NewFleet never splits a fleet)' {
        $s = New-FleetSecrets
        Assert-True ($s.agent_token -match '^[A-Za-z0-9_-]{32}$') $s.agent_token; Assert-True ($s.cluster_token -match '^[A-Za-z0-9_-]{32}$')
        Assert-True ($s.agent_token -ne $s.cluster_token)
        $k = New-FleetSecrets ([pscustomobject]@{ agent_token = 'a1'; cluster_token = 'c1' }); Assert-Equal 'a1' $k.agent_token; Assert-Equal 'c1' $k.cluster_token
    }
    Test-Case 'admin PIN source: existing hash kept unless -ResetPin; -AdminPin only in a dry run; env; prompt' {
        Assert-Equal 'keep' (Get-AdminPinSource -ExistingHash 'pbkdf2_sha256$1$a$b' -ResetPin $false -AdminPin '' -EnvPin '' -DryRun $false)
        Assert-Equal 'prompt' (Get-AdminPinSource -ExistingHash 'pbkdf2_sha256$1$a$b' -ResetPin $true -AdminPin '' -EnvPin '' -DryRun $false)
        Assert-Equal 'env' (Get-AdminPinSource -ExistingHash '' -ResetPin $false -AdminPin '' -EnvPin '123456' -DryRun $false)
        Assert-Equal 'param' (Get-AdminPinSource -ExistingHash '' -ResetPin $false -AdminPin '123456' -EnvPin '' -DryRun $true)
        Assert-Equal 'skip' (Get-AdminPinSource -ExistingHash '' -ResetPin $false -AdminPin '' -EnvPin '' -DryRun $true)
        Assert-Throws { Get-AdminPinSource -ExistingHash '' -ResetPin $false -AdminPin '123456' -EnvPin '' -DryRun $false } '*only for -DryRun*'
    }
    Test-Case 'data\private ACL: Administrators + SYSTEM only, inheritance removed' {
        Assert-Equal @('C:\F\data\private', '/inheritance:r', '/grant:r', '*S-1-5-32-544:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F', '/C', '/Q') @(Get-ForgePrivateAclArgs 'C:\F\data\private')
    }
    Test-Case 'install order: data\private is secured before secrets.json / firstrun-claim.json are written' {
        $src = [IO.File]::ReadAllText((Join-Path $Repo 'agent\install.ps1'))
        $secure = $src.IndexOf('secure $privDir'); $secrets = $src.IndexOf('Write-ForgeSecrets -DataDir'); $claim = $src.IndexOf('New-FirstRunClaim -DataDir $data')
        Assert-True ($secure -gt 0 -and $secure -lt $secrets -and $secure -lt $claim) "secure=$secure secrets=$secrets claim=$claim"
    }
    Test-Case 'claim code: XXXX-XXXX Crockford base32 (no I L O U), random' {
        $codes = @(1..50 | ForEach-Object { New-ClaimCode })
        foreach ($c in $codes) { Assert-True ($c -cmatch '^[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$') $c }
        Assert-True (@($codes | Select-Object -Unique).Count -ge 49) 'codes repeat'
    }
    Test-Case 'claim code hash = sha256(upper, no dash), same as Python hashlib' {
        $h = Get-ClaimCodeHash 'ab3d-ef9h'
        Assert-Equal $h (Get-ClaimCodeHash 'AB3DEF9H')
        $py = (& $Py -c "import hashlib;print(hashlib.sha256(b'AB3DEF9H').hexdigest())").Trim()
        Assert-Equal $py $h
    }
    Test-Case 'first-run claim file: data\private\firstrun-claim.json {code_sha256, expires, label}; dry run writes nothing' {
        $d = Join-Path $Scratch 'fr-data'
        $code = New-FirstRunClaim -DataDir $d -TtlSec 600
        $j = [IO.File]::ReadAllText((Join-Path $d 'private\firstrun-claim.json')) | ConvertFrom-Json
        Assert-Equal (Get-ClaimCodeHash $code) $j.code_sha256; Assert-Equal 'first device' $j.label
        $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); Assert-True ($j.expires -gt $now + 590 -and $j.expires -lt $now + 610) "expires $($j.expires)"
        $d2 = Join-Path $Scratch 'fr-dry'; $null = New-FirstRunClaim -DataDir $d2 -DryRun; Assert-True (-not (Test-Path $d2))
    }
    Test-Case 'admin PIN must be exactly 6 digits' {
        Assert-True (Test-AdminPin '012345'); foreach ($b in '12345', '1234567', 'abcdef', '', '12 345') { Assert-True (-not (Test-AdminPin $b)) $b }
    }
    Test-Case 'bootstrap URL: enrollment code first, agent token for 2.2 links' {
        Assert-Equal 'http://h:8787/cluster/bootstrap?code=AB3D-EF9H' (Get-BootstrapUrl 'http://h:8787/' 'AB3D-EF9H' 'tok')
        Assert-Equal 'http://h:8787/cluster/bootstrap?t=a%2Bb' (Get-BootstrapUrl 'http://h:8787' ('__' + 'CODE' + '__') 'a+b')
    }
    Test-Case 'secrets from bootstrap / new fleet: only set keys; the agent token is never a secrets.json key' {
        $s = Get-BootstrapSecrets ([pscustomobject]@{ agent_token = 'a'; cluster_token = 'c'; ui_token_hash = 'sha256:x'; signing_pubkey = $null }) 'pbkdf2_sha256$1$a$b'
        Assert-Equal @('admin_pin_hash', 'cluster_token', 'ui_token_hash') @($s.Keys | Sort-Object)
        $b = New-NewFleetBootstrap -Secrets @{ agent_token = 'a'; cluster_token = 'c' } -Version '2.3.0' -Sha256 'ff' -SigningPubKey '<RSAKeyValue/>'
        Assert-Equal 0 @($b.peers).Count; Assert-Equal '2.3.0' $b.bundle_version
        Assert-Equal '<RSAKeyValue/>' (Get-BootstrapSecrets $b)['signing_pubkey']
        $c = New-SharedConfig -Bootstrap $b -NodeId 'box' -Existing $null; Assert-Equal 'a' $c.agent_token; Assert-Equal 0 @($c.seed_peers).Count
    }
    Test-Case 'installer keeps exactly one CODE placeholder' {
        $src = Get-Content (Join-Path $Repo 'agent\install.ps1') -Raw
        Assert-Equal 1 ([regex]::Matches($src, '__CODE__').Count)
    }
}

foreach ($j in @(Get-ChildItem -LiteralPath $Scratch -Recurse -Directory -Force -ErrorAction SilentlyContinue | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint })) { & cmd.exe /d /c "rmdir `"$($j.FullName)`"" | Out-Null }
Remove-Item -LiteralPath $Scratch -Recurse -Force -ErrorAction SilentlyContinue
Write-Host ''
Write-Host "$($script:Pass) passed, $($script:Fail) failed" -ForegroundColor $(if ($script:Fail) { 'Red' } else { 'Green' })
foreach ($m in $script:Failures) { Write-Host "  - $m" -ForegroundColor Red }
exit $script:Fail
