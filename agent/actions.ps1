# Forge Control allowlisted maintenance actions. Catalog = hub\actions.json (same file the hub validates against).
# lane "system" actions run in the SYSTEM agent; 2.3: lane "user" actions (tool updates, git_sync, ollama_pull) run
# in the user agent with the filtered job token. The agent dot-sources this file to re-validate a job's args before
# it starts; the job itself runs in a separate process:
#   powershell -File actions.ps1 -ActionRunSpec <job dir>\spec.json -ActionCatalogPath <bundle>\hub\actions.json
# spec.json = {action, args, dry_run, user, tools?}. Untrusted values only ever travel inside spec.json and are passed
# to programs as argument ARRAYS (splatting) - never concatenated into a command line.
# 2.3: every run ends with one line `##RESULT <json>` (the hub parses it: forge_upkeep.parse_result).
# Dot-source for tests:  . .\actions.ps1
param(
    [string]$ActionRunSpec,
    [string]$ActionCatalogPath
)

# ======================= pure helpers (unit-tested) =======================

function ConvertTo-ForgeHashtable($Obj) {
    if ($null -eq $Obj) { return @{} }
    if ($Obj -is [System.Collections.IDictionary]) { $h = @{}; foreach ($k in $Obj.Keys) { $h[[string]$k] = $Obj[$k] }; return $h }
    if ($Obj -is [System.Management.Automation.PSCustomObject]) { $h = @{}; foreach ($p in $Obj.PSObject.Properties) { $h[$p.Name] = $p.Value }; return $h }
    throw 'args must be an object'
}

function Read-ForgeActionCatalog([string]$Path) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { throw "action catalog not found at '$Path'" }
    [IO.File]::ReadAllText($Path) | ConvertFrom-Json
}

function Get-ForgeActionSpec($Catalog, [string]$Name) {
    foreach ($a in @($Catalog.actions)) { if ($a -and ([string]$a.name) -ceq $Name) { return $a } }
    $null
}

function Test-ForgeRuleHas($Rule, [string]$Key) { $null -ne $Rule.PSObject.Properties[$Key] }

# Re-validate args exactly like hub/forge_actions.py. Returns a hashtable with defaults applied; throws on bad input.
function Test-ForgeActionArgs {
    param($Catalog, [string]$Name, $ActionArgs)
    $spec = Get-ForgeActionSpec $Catalog $Name
    if (-not $spec) { throw "unknown action '$Name'" }
    $a = ConvertTo-ForgeHashtable $ActionArgs
    $schema = ConvertTo-ForgeHashtable $spec.args
    foreach ($k in @($a.Keys)) {
        if (-not ($schema.Keys | Where-Object { $_ -ceq $k })) { throw "unknown arg for ${Name}: $k" }
    }
    $out = @{}
    foreach ($k in @($schema.Keys)) {
        $rule = $schema[$k]; $v = $a[$k]
        if ($null -eq $v -or ($v -is [string] -and $v -eq '')) {
            if ($rule.required -eq $true) { throw "${Name}: '$k' is required" }
            if (Test-ForgeRuleHas $rule 'default') { $out[$k] = $rule.default }
            continue
        }
        $type = if ($rule.type) { [string]$rule.type } else { 'string' }
        if ($type -eq 'bool') {
            if (-not ($v -is [bool])) { throw "${Name}: '$k' must be true or false" }
        } elseif ($type -eq 'int') {
            if (-not ($v -is [int] -or $v -is [long])) { throw "${Name}: '$k' must be an integer" }
            if ((Test-ForgeRuleHas $rule 'min') -and $v -lt $rule.min) { throw "${Name}: '$k' must be between $($rule.min) and $($rule.max)" }
            if ((Test-ForgeRuleHas $rule 'max') -and $v -gt $rule.max) { throw "${Name}: '$k' must be between $($rule.min) and $($rule.max)" }
        } elseif ($type -eq 'string') {
            if (-not ($v -is [string])) { throw "${Name}: '$k' must be a string" }
            if ([regex]::IsMatch($v, '[\x00-\x1f\x7f]')) { throw "${Name}: '$k' contains control characters" }
            $max = 512; if (Test-ForgeRuleHas $rule 'max_len') { $max = [int]$rule.max_len }
            if ($v.Length -gt $max) { throw "${Name}: '$k' is too long" }
            if ((Test-ForgeRuleHas $rule 'enum') -and -not (@($rule.enum) -ccontains $v)) { throw "${Name}: '$k' must be one of $(@($rule.enum) -join ', ')" }
            if ((Test-ForgeRuleHas $rule 'pattern') -and -not [regex]::IsMatch($v, [string]$rule.pattern)) { throw "${Name}: '$k' has invalid characters" }
            if ((Test-ForgeRuleHas $rule 'not_in') -and (@($rule.not_in) -contains $v)) { throw "${Name}: '$k' may not be '$v'" }
        } else { throw "${Name}: unsupported arg type $type" }
        $out[$k] = $v
    }
    $out
}

# Steps for an action (args must already be validated). Each step is a hashtable:
#   @{ kind = 'native'; exe = 'winget'; argv = [string[]] }        -> & <resolved exe> @argv
#   @{ kind = 'cmdlet'; command = 'Restart-Service'; params = @{} } -> & <command> @params
function Get-ForgeActionSteps {
    param([string]$Name, [hashtable]$ActionArgs, [string]$User)
    $accept = @('--accept-source-agreements', '--accept-package-agreements')
    $steps = New-Object System.Collections.Generic.List[object]
    switch -CaseSensitive ($Name) {
        'winget_install' {
            $argv = @('install', '--id', [string]$ActionArgs['id'], '-e', '--silent') + $accept
            if ($ActionArgs['scope']) { $argv += @('--scope', [string]$ActionArgs['scope']) }
            $steps.Add(@{ kind = 'native'; exe = 'winget'; argv = [string[]]$argv })
        }
        'winget_upgrade' {
            if ($ActionArgs['id']) { $argv = @('upgrade', '--id', [string]$ActionArgs['id'], '-e', '--silent') + $accept }
            else { $argv = @('upgrade', '--all', '--silent') + $accept }
            $steps.Add(@{ kind = 'native'; exe = 'winget'; argv = [string[]]$argv; ok_exit = $WingetNoUpdate })
        }
        'npm_global_install' {
            $steps.Add(@{ kind = 'native'; exe = 'npm'; argv = [string[]]@('install', '-g', [string]$ActionArgs['package']) })
        }
        'restart_task' {
            $n = [string]$ActionArgs['name']
            $steps.Add(@{ kind = 'cmdlet'; command = 'Stop-ScheduledTask'; params = @{ TaskName = $n; ErrorAction = 'Stop' } })
            $steps.Add(@{ kind = 'cmdlet'; command = 'Start-ScheduledTask'; params = @{ TaskName = $n; ErrorAction = 'Stop' } })
        }
        'restart_service' {
            $steps.Add(@{ kind = 'cmdlet'; command = 'Restart-Service'; params = @{ Name = [string]$ActionArgs['name']; Force = $true; ErrorAction = 'Stop' } })
        }
        'reboot' {
            $d = 60; if ($null -ne $ActionArgs['delay_s']) { $d = [int]$ActionArgs['delay_s'] }
            $steps.Add(@{ kind = 'native'; exe = 'shutdown'; argv = [string[]]@('/r', '/t', [string]$d, '/c', 'Forge Control reboot') })
        }
        'ensure_task' {
            $runAs = [string]$ActionArgs['run_as']
            if ($runAs -eq 'user' -and -not $User) { throw "ensure_task run_as=user needs the node's user (agent-config.json 'user')" }
            $p = @{ Name = [string]$ActionArgs['name']; Command = [string]$ActionArgs['command']; Arguments = [string]$ActionArgs['args']
                    RunAs = $runAs; Trigger = [string]$ActionArgs['trigger']; User = $(if ($runAs -eq 'user') { $User } else { '' }) }
            $steps.Add(@{ kind = 'cmdlet'; command = 'Register-ForgeEnsureTask'; params = $p })
        }
        # ---- 2.3 (workstream F) ----
        'update_claude' { $steps.Add(@{ kind = 'cmdlet'; command = 'Invoke-ForgeToolUpdate'; params = @{ Tool = 'claude' } }) }
        'update_codex'  { $steps.Add(@{ kind = 'cmdlet'; command = 'Invoke-ForgeToolUpdate'; params = @{ Tool = 'codex' } }) }
        'update_t3'     { $steps.Add(@{ kind = 'cmdlet'; command = 'Invoke-ForgeToolUpdate'; params = @{ Tool = 't3' } }) }
        'update_ollama' {
            $steps.Add(@{ kind = 'native'; exe = 'winget'; argv = [string[]](@('upgrade', '--id', 'Ollama.Ollama', '-e', '--silent') + $accept); ok_exit = $WingetNoUpdate })
        }
        'git_sync'      { $steps.Add(@{ kind = 'cmdlet'; command = 'Invoke-ForgeGitSync'; params = @{ Cwd = [string]$ActionArgs['cwd'] } }) }
        'ollama_pull'   { $steps.Add(@{ kind = 'native'; exe = 'ollama'; argv = [string[]]@('pull', [string]$ActionArgs['model']) }) }
        'windows_update' {
            $r = 'never'; if ($ActionArgs['reboot']) { $r = [string]$ActionArgs['reboot'] }
            $steps.Add(@{ kind = 'cmdlet'; command = 'Invoke-ForgeWindowsUpdate'; params = @{ Reboot = $r } })
        }
        'free_disk'     { $steps.Add(@{ kind = 'cmdlet'; command = 'Invoke-ForgeFreeDisk'; params = @{} }) }
        'forge_uninstall' {
            $steps.Add(@{ kind = 'cmdlet'; command = 'Invoke-ForgeUninstall'; params = @{ KeepData = [bool]$ActionArgs['keep_data'] } })
        }
        default { throw "no implementation for action '$Name'" }
    }
    , $steps.ToArray()
}

# winget: "no applicable upgrade found" (APPINSTALLER_CLI_ERROR_UPDATE_NOT_APPLICABLE 0x8A15002B) is not a failure
$WingetNoUpdate = @(-1978335189)

# Display only (dry run / job log); never executed.
function Format-ForgeActionStep($Step) {
    $q = { param($s) if ("$s" -match '[\s"]' -or "$s" -eq '') { '"' + ("$s" -replace '"', '\"') + '"' } else { "$s" } }
    if ($Step.kind -eq 'native') { return (@($Step.exe) + @($Step.argv | ForEach-Object { & $q $_ })) -join ' ' }
    $parts = @($Step.command)
    foreach ($k in @($Step.params.Keys | Sort-Object)) {
        $v = $Step.params[$k]
        if ($v -is [bool]) { if ($v) { $parts += "-$k" } } else { $parts += "-$k"; $parts += (& $q $v) }
    }
    $parts -join ' '
}

# ======================= runtime (only inside the job process / as SYSTEM) =======================

function Write-ActionOut([string]$s) { [Console]::Out.WriteLine($s); [Console]::Out.Flush() }

# 2.3: structured result of the run (`##RESULT <json>`, last line of the output). Cmdlet steps set
# $script:ActionResult (a hashtable) and may set $script:ActionExit (exit code of that step, default 0).
$script:ActionResult = $null; $script:ActionExit = $null; $script:SpecTools = @{}
function Format-ActionResult($Result) { '##RESULT ' + (ConvertTo-Json -InputObject $Result -Compress -Depth 5) }
function Get-DefaultActionResult([string]$Action, [int]$Code, [bool]$DryRun) {
    $r = [ordered]@{ result = $(if ($DryRun) { 'dry_run' } elseif ($Code -eq 0) { 'ok' } else { 'failed' }); exit = $Code; action = $Action }
    $r
}

function Resolve-ForgeExe([string]$Name) {
    # 2.3 user lane: the agent passes the tool paths it resolved (spec.json "tools"); they must exist
    $t = $script:SpecTools[$Name]
    if ($t -and (Test-Path -LiteralPath $t)) { return [string]$t }
    switch ($Name) {
        'winget' {
            $c = Get-Command winget.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($c) { return $c.Source }
            # SYSTEM has no per-user WindowsApps alias: use the newest App Installer package directly
            $w = Get-ChildItem -Path "$env:ProgramFiles\WindowsApps\Microsoft.DesktopAppInstaller_*__8wekyb3d8bbwe\winget.exe" -ErrorAction SilentlyContinue |
                Sort-Object { [version](($_.Directory.Name -split '_')[1]) } -Descending | Select-Object -First 1
            if ($w) { return $w.FullName }
            throw 'winget.exe not found'
        }
        'npm' {
            $c = Get-Command npm.cmd -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($c) { return $c.Source }
            $p = Join-Path $env:ProgramFiles 'nodejs\npm.cmd'
            if (Test-Path -LiteralPath $p) { return $p }
            throw 'npm.cmd not found (install Node.js machine-wide first)'
        }
        'shutdown' { return (Join-Path $env:SystemRoot 'System32\shutdown.exe') }
        { $_ -in 'claude', 'codex', 't3', 'git', 'ollama' } {
            foreach ($ext in '.exe', '.cmd') {
                $c = Get-Command "$Name$ext" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
                if ($c) { return $c.Source }
            }
            foreach ($p in @("$env:USERPROFILE\.local\bin\$Name.exe", "$env:APPDATA\npm\$Name.cmd", "$env:LOCALAPPDATA\Programs\Ollama\$Name.exe",
                             "$env:ProgramFiles\Git\cmd\$Name.exe")) { if (Test-Path -LiteralPath $p) { return $p } }
            throw "$Name not found"
        }
        default { throw "unknown executable '$Name'" }
    }
}

function Register-ForgeEnsureTask {
    param([string]$Name, [string]$Command, [string]$Arguments, [string]$RunAs, [string]$Trigger, [string]$User)
    $actParams = @{ Execute = $Command }
    if ($Arguments) { $actParams['Argument'] = $Arguments }
    $action = New-ScheduledTaskAction @actParams
    if ($Trigger -eq 'logon') {
        if ($RunAs -eq 'user') { $trig = New-ScheduledTaskTrigger -AtLogOn -User $User } else { $trig = New-ScheduledTaskTrigger -AtLogOn }
    } else { $trig = New-ScheduledTaskTrigger -AtStartup; $trig.Delay = 'PT30S' }
    $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -StartWhenAvailable
    if ($RunAs -eq 'system') { $prin = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\SYSTEM' -LogonType ServiceAccount -RunLevel Highest }
    else { $prin = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Limited }
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trig -Settings $set -Principal $prin -Force -ErrorAction Stop | Out-Null
    $t = Get-ScheduledTask -TaskName $Name -ErrorAction Stop
    if ("$($t.State)" -ne 'Running') { Start-ScheduledTask -TaskName $Name -ErrorAction Stop }
    "task '$Name' registered ($RunAs, at $Trigger) and running"
}

# ======================= 2.3 (workstream F) action bodies =======================
# Pure deciders are unit-tested (tests\ps\p3-upkeep.ps1); the Invoke-* wrappers do the I/O.

# F2: how to update a tool. -> @{ exe; argv } or $null (tool not installed / nothing to run it with).
#   claude: native install (~\.local\bin\claude.exe) -> `claude update`; npm shim / not found -> npm i -g ...@latest
#   codex:  npm i -g @openai/codex@latest; without npm -> `codex update`
#   t3:     `t3 update`
function Get-ToolUpdatePlan([string]$Tool, [string]$ToolPath, [bool]$HasNpm) {
    $npmShim = ($ToolPath -match '\\npm\\' -or $ToolPath -match '\.(cmd|ps1)$')
    switch ($Tool) {
        'claude' {
            if ($ToolPath -and -not $npmShim) { return @{ exe = 'claude'; argv = [string[]]@('update') } }
            if ($HasNpm -and $ToolPath) { return @{ exe = 'npm'; argv = [string[]]@('install', '-g', '@anthropic-ai/claude-code@latest') } }
        }
        'codex' {
            if ($HasNpm -and $ToolPath) { return @{ exe = 'npm'; argv = [string[]]@('install', '-g', '@openai/codex@latest') } }
            if ($ToolPath) { return @{ exe = 'codex'; argv = [string[]]@('update') } }
        }
        't3' { if ($ToolPath) { return @{ exe = 't3'; argv = [string[]]@('update') } } }
    }
    $null
}

function Get-ToolVersionText([string]$Exe) {
    if (-not $Exe) { return $null }
    try {
        $s = "$(@(& $Exe --version 2>$null) | Select-Object -First 1)".Trim()
        if ($s.Length -gt 80) { $s = $s.Substring(0, 80) }
        if ($s) { return $s }
    } catch {}
    $null
}

function Invoke-ForgeToolUpdate([string]$Tool) {
    $path = $null; try { $path = Resolve-ForgeExe $Tool } catch {}
    $npm = $null; try { $npm = Resolve-ForgeExe 'npm' } catch {}
    $plan = Get-ToolUpdatePlan -Tool $Tool -ToolPath $path -HasNpm ([bool]$npm)
    if (-not $plan) {
        Write-ActionOut "$Tool is not installed for $env:USERNAME; nothing to update"
        $script:ActionResult = [ordered]@{ result = 'not_installed'; tool = $Tool; summary = "$Tool not installed" }
        return
    }
    $before = Get-ToolVersionText $path
    $exe = if ($plan.exe -eq $Tool) { $path } else { Resolve-ForgeExe $plan.exe }
    Write-ActionOut "> $($plan.exe) $($plan.argv -join ' ')   ($Tool $before)"
    $argv = [string[]]$plan.argv
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = 0
    & $exe @argv 2>&1 | ForEach-Object { Write-ActionOut "$_" }
    $code = [int]$LASTEXITCODE
    $after = Get-ToolVersionText $path
    $res = if ($code -ne 0) { 'failed' } elseif ($before -and $after -and $before -ne $after) { 'updated' } else { 'current' }
    $script:ActionResult = [ordered]@{ result = $res; tool = $Tool; before = $before; after = $after; exit = $code
                                       summary = $(if ($res -eq 'updated') { "$Tool $before -> $after" } elseif ($res -eq 'current') { "$Tool $after (current)" } else { "$Tool update failed (exit $code)" }) }
    $script:ActionExit = $code
}

# F3: decision after `git fetch`. Never pushes, never merges anything but a fast-forward.
function Get-GitSyncDecision {
    param([bool]$HasUpstream, [bool]$Dirty, [int]$Ahead = 0, [int]$Behind = 0)
    if (-not $HasUpstream) { return 'no_upstream' }
    if ($Behind -le 0) { return 'uptodate' }            # nothing to pull (local-only commits stay local)
    if ($Ahead -gt 0) { return 'diverged' }
    if ($Dirty) { return 'dirty' }
    'ff'
}

function Invoke-ForgeGit([string]$Git, [string]$Cwd, [string[]]$GitArgs) {
    $ErrorActionPreference = 'Continue'          # git writes progress/warnings to stderr
    $global:LASTEXITCODE = 0
    $out = @(& $Git -C $Cwd @GitArgs 2>&1 | ForEach-Object { "$_" })
    @{ code = [int]$LASTEXITCODE; out = $out }
}

function Invoke-ForgeGitSync([string]$Cwd) {
    $env:GIT_TERMINAL_PROMPT = '0'; $env:GCM_INTERACTIVE = 'never'
    $fail = { param($d) Write-ActionOut "git sync: $d"; $script:ActionResult = [ordered]@{ result = 'error'; cwd = $Cwd; detail = "$d" }; $script:ActionExit = 1 }
    if (-not (Test-Path -LiteralPath $Cwd -PathType Container)) { & $fail 'folder not found'; return }
    $git = $null; try { $git = Resolve-ForgeExe 'git' } catch { & $fail 'git not found'; return }
    $r = Invoke-ForgeGit $git $Cwd @('rev-parse', '--is-inside-work-tree')
    if ($r.code -ne 0 -or ($r.out -join '') -notmatch 'true') { & $fail 'not a git repository'; return }
    Write-ActionOut "> git fetch --prune   ($Cwd)"
    $r = Invoke-ForgeGit $git $Cwd @('fetch', '--prune', '--quiet')
    foreach ($l in $r.out) { Write-ActionOut "  $l" }
    if ($r.code -ne 0) { & $fail "fetch failed (exit $($r.code))"; return }
    $branch = ((Invoke-ForgeGit $git $Cwd @('symbolic-ref', '--short', '-q', 'HEAD')).out -join '').Trim()
    $up = Invoke-ForgeGit $git $Cwd @('rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}')
    $hasUp = ($branch -and $up.code -eq 0)
    $ahead = 0; $behind = 0; $dirty = $false
    if ($hasUp) {
        $cnt = ((Invoke-ForgeGit $git $Cwd @('rev-list', '--left-right', '--count', 'HEAD...@{u}')).out -join ' ').Trim() -split '\s+'
        if ($cnt.Count -ge 2) { $ahead = [int]$cnt[0]; $behind = [int]$cnt[1] }
        $dirty = [bool](((Invoke-ForgeGit $git $Cwd @('status', '--porcelain', '--untracked-files=no')).out -join '').Trim())
    }
    $dec = Get-GitSyncDecision -HasUpstream $hasUp -Dirty $dirty -Ahead $ahead -Behind $behind
    $res = [ordered]@{ result = $dec; branch = $branch; ahead = $ahead; behind = $behind; dirty = $dirty }
    if ($dec -eq 'ff') {
        $m = Invoke-ForgeGit $git $Cwd @('merge', '--ff-only', '--quiet', '@{u}')
        foreach ($l in $m.out) { Write-ActionOut "  $l" }
        if ($m.code -ne 0) { $res.result = 'error'; $res.detail = "fast-forward failed (exit $($m.code))"; $script:ActionExit = 1 }
    }
    Write-ActionOut "git sync: $($res.result) (branch $branch, ahead $ahead, behind $behind$(if ($dirty) { ', local changes' }))"
    $script:ActionResult = $res
}

# F4: Windows Update through the Windows Update Agent COM API (no PSWindowsUpdate). Runs as SYSTEM. Never reboots:
# the hub queues `reboot` when reboot_required, another hub is healthy and the window is still open.
# -Session / -NewCollection / -SystemInfo are injectable for tests (fake COM objects).
function Get-WuResultCode([int]$Code) { switch ($Code) { 2 { 'ok' } 3 { 'ok' } default { 'failed' } } }   # 2 succeeded, 3 succeeded with errors, 4 failed, 5 aborted

function Invoke-ForgeWindowsUpdate {
    param([string]$Reboot = 'never', $Session = $null, [scriptblock]$NewCollection = { New-Object -ComObject Microsoft.Update.UpdateColl },
          $SystemInfo = $null)
    if ($Reboot -ne 'never') { throw "unsupported reboot mode '$Reboot'" }
    if (-not $Session) { $Session = New-Object -ComObject Microsoft.Update.Session; $Session.ClientApplicationID = 'Hivemind' }
    Write-ActionOut 'searching for updates (IsInstalled=0, software)...'
    $found = $Session.CreateUpdateSearcher().Search("IsInstalled=0 and IsHidden=0 and Type='Software'")
    $todo = & $NewCollection
    $kbs = New-Object System.Collections.Generic.List[string]
    $titles = New-Object System.Collections.Generic.List[string]
    for ($i = 0; $i -lt $found.Updates.Count; $i++) {
        $u = $found.Updates.Item($i)
        if ($u.InstallationBehavior -and $u.InstallationBehavior.CanRequestUserInput) { Write-ActionOut "  skip (needs user input): $($u.Title)"; continue }
        if (-not $u.EulaAccepted) { try { $u.AcceptEula() } catch {} }
        [void]$todo.Add($u); $titles.Add([string]$u.Title)
        foreach ($kb in @($u.KBArticleIDs)) { if ($kb) { $kbs.Add("KB$kb") } }
        Write-ActionOut "  + $($u.Title)"
    }
    $installed = 0; $failed = 0; $needReboot = $false      # ($reboot would be the -Reboot parameter: names are case-insensitive)
    if ($todo.Count -gt 0) {
        Write-ActionOut "downloading $($todo.Count) update(s)..."
        $dl = $Session.CreateUpdateDownloader(); $dl.Updates = $todo; [void]$dl.Download()
        Write-ActionOut 'installing...'
        $ins = $Session.CreateUpdateInstaller(); $ins.Updates = $todo
        $r = $ins.Install()
        for ($i = 0; $i -lt $todo.Count; $i++) {
            $rc = Get-WuResultCode ([int]$r.GetUpdateResult($i).ResultCode)
            if ($rc -eq 'ok') { $installed++ } else { $failed++; Write-ActionOut "  FAILED: $($titles[$i])" }
        }
        $needReboot = [bool]$r.RebootRequired
    } else { Write-ActionOut 'no updates found' }
    if (-not $needReboot) {
        try { if (-not $SystemInfo) { $SystemInfo = New-Object -ComObject Microsoft.Update.SystemInfo }; $needReboot = [bool]$SystemInfo.RebootRequired } catch {}
    }
    Write-ActionOut "windows update: $installed installed, $failed failed$(if ($needReboot) { ', reboot required' })"
    $script:ActionResult = [ordered]@{ installed = $installed; failed = $failed; reboot_required = $needReboot; kb = @($kbs.ToArray()); found = [int]$found.Updates.Count }
}

# <ForgeHome> from this script's location. Only the real layout counts: this script lives in <FH>\current\agent or
# <FH>\releases\<ver>\agent, and <FH> has data\config.json and releases\. A repo checkout (<repo>\agent, maybe with a
# data\config.json beside it) is never taken for the Forge home.
function Get-ForgeHomeFromScript([string]$Dir = $PSScriptRoot) {
    if (-not $Dir) { return $null }
    $d = $Dir.TrimEnd('\')
    if ((Split-Path -Leaf $d) -ne 'agent') { return $null }
    $p = Split-Path -Parent $d
    if (-not $p) { return $null }
    $fh = $null
    if ((Split-Path -Leaf $p) -eq 'current') { $fh = Split-Path -Parent $p }
    else {
        $pp = Split-Path -Parent $p
        if ($pp -and (Split-Path -Leaf $pp) -eq 'releases') { $fh = Split-Path -Parent $pp }
    }
    if (-not $fh) { return $null }
    if (-not (Test-Path -LiteralPath (Join-Path $fh 'data\config.json') -PathType Leaf)) { return $null }
    if (-not (Test-Path -LiteralPath (Join-Path $fh 'releases') -PathType Container)) { return $null }
    $fh
}

# The uninstall also requires the registered ForgeSystemAgent task (which runs this job) to point into that home.
function Test-ForgeHomeInTaskActions([string]$ForgeHome, [string[]]$ActionLines = @()) {
    if (-not $ForgeHome) { return $false }
    $needle = $ForgeHome.TrimEnd('\') + '\'
    foreach ($l in @($ActionLines)) { if ($l -and $l.IndexOf($needle, [StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true } }
    $false
}

# ---- free_disk: runs as SYSTEM in folders users can write to, so it never follows a link ----
function Test-ForgeReparsePoint($Item) { $null -ne $Item -and [bool]($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) }

# Children of a directory (hidden included). The listing never goes through a link: callers check each entry.
function Get-ForgeChildItems([string]$Dir) {
    try { , @((New-Object IO.DirectoryInfo $Dir).GetFileSystemInfos()) } catch { , @() }
}

# $true when neither $Path nor any of its ancestors is a junction / symlink (a user could redirect a Temp folder).
function Test-ForgeNoReparseChain([string]$Path) {
    $d = $Path
    while ($d) {
        try { $i = Get-Item -LiteralPath $d -Force -ErrorAction Stop } catch { return $false }
        if (Test-ForgeReparsePoint $i) { return $false }
        $parent = Split-Path -Parent $d
        if (-not $parent -or $parent -eq $d) { break }
        $d = $parent
    }
    $true
}

# Newest LastWriteTime anywhere in the tree (links count by their own time and are not entered).
function Get-ForgeNewestWrite($Item) {
    $newest = $Item.LastWriteTime
    if ($Item -is [IO.DirectoryInfo] -and -not (Test-ForgeReparsePoint $Item)) {
        foreach ($c in (Get-ForgeChildItems $Item.FullName)) {
            $t = Get-ForgeNewestWrite $c
            if ($t -gt $newest) { $newest = $t }
        }
    }
    $newest
}

# Size of a tree without entering links.
function Get-ItemBytes($Item) {
    try {
        if (Test-ForgeReparsePoint $Item) { return [long]0 }
        if ($Item -is [IO.DirectoryInfo] -or ($Item.PSIsContainer -and -not ($Item -is [IO.FileInfo]))) {
            $sum = [long]0
            foreach ($c in (Get-ForgeChildItems $Item.FullName)) { $sum += Get-ItemBytes $c }
            return $sum
        }
        return [long]$Item.Length
    } catch { [long]0 }
}

# Deletes a file or a tree; a link (anywhere in the tree) is removed itself and never followed. Never throws.
# Returns the bytes of the regular files it deleted.
function Remove-ForgeItemNoFollow($Item) {
    $freed = [long]0
    try {
        if (Test-ForgeReparsePoint $Item) {
            if ($Item -is [IO.DirectoryInfo]) { [IO.Directory]::Delete($Item.FullName, $false) } else { [IO.File]::Delete($Item.FullName) }
            return $freed
        }
        if ($Item -is [IO.DirectoryInfo]) {
            foreach ($c in (Get-ForgeChildItems $Item.FullName)) { $freed += Remove-ForgeItemNoFollow $c }
            if ($Item.Attributes -band [IO.FileAttributes]::ReadOnly) { $Item.Attributes = [IO.FileAttributes]::Directory }
            [IO.Directory]::Delete($Item.FullName, $false)
        } else {
            $len = [long]$Item.Length
            if ($Item.Attributes -band [IO.FileAttributes]::ReadOnly) { $Item.Attributes = [IO.FileAttributes]::Normal }
            [IO.File]::Delete($Item.FullName)
            $freed += $len
        }
    } catch {}
    $freed
}

# Items (FileSystemInfo-like: FullName, LastWriteTime) older than $MaxAgeDays, never $Skip paths. -Deep ages a folder
# by the newest file in its tree (a scratch workspace touched yesterday is not old because its folder date is).
function Select-OldItems {
    param([object[]]$Items = @(), [datetime]$Now, [int]$MaxAgeDays = 7, [string[]]$Skip = @(), [switch]$Deep)
    , @($Items | Where-Object {
        if (-not $_ -or ($Skip -contains $_.FullName)) { return $false }
        $t = if ($Deep) { Get-ForgeNewestWrite $_ } else { $_.LastWriteTime }
        ($Now - $t).TotalDays -ge $MaxAgeDays
    })
}

# Default places: Windows\Temp and each profile's AppData\Local\Temp (7 days), <work_root>\scratch (14 days).
function Get-ForgeFreeDiskRoots {
    $roots = New-Object System.Collections.Generic.List[object]
    $roots.Add(@{ dir = (Join-Path $env:SystemRoot 'Temp'); days = 7 })
    foreach ($u in @(Get-ChildItem -LiteralPath (Join-Path $env:SystemDrive 'Users') -Directory -ErrorAction SilentlyContinue)) {
        if (Test-ForgeReparsePoint $u) { continue }
        $t = Join-Path $u.FullName 'AppData\Local\Temp'; if (Test-Path -LiteralPath $t) { $roots.Add(@{ dir = $t; days = 7 }) }
    }
    $fh = Get-ForgeHomeFromScript
    if ($fh) {
        $ac = $null; try { $ac = [IO.File]::ReadAllText((Join-Path $fh 'data\agent-config.json')) | ConvertFrom-Json } catch {}
        foreach ($wr in @($ac.work_root_current, $ac.work_root_auto) | Where-Object { $_ } | Select-Object -Unique) {
            $sd = Join-Path ([string]$wr) 'scratch'; if (Test-Path -LiteralPath $sd) { $roots.Add(@{ dir = $sd; days = 14 }) }
        }
    }
    , $roots.ToArray()
}

# Every user's recycle bin: <fixed drive>\$Recycle.Bin\<SID> folders (Clear-RecycleBin as SYSTEM empties only SYSTEM's).
function Get-ForgeRecycleDirs {
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($dr in [IO.DriveInfo]::GetDrives()) {
        if ($dr.DriveType -ne [IO.DriveType]::Fixed -or -not $dr.IsReady) { continue }
        $rb = Join-Path $dr.RootDirectory.FullName '$Recycle.Bin'
        foreach ($s in (Get-ForgeChildItems $rb)) { if ($s -is [IO.DirectoryInfo] -and $s.Name -like 'S-1-*') { $out.Add($s.FullName) } }
    }
    , $out.ToArray()
}

# -Roots / -RecycleDirs / -Now are injectable for tests (scratch folders only).
function Invoke-ForgeFreeDisk {
    param([object[]]$Roots = $null, [string[]]$RecycleDirs = $null, $Now = $null)
    if ($null -eq $Now) { $Now = Get-Date }
    if ($null -eq $Roots) { $Roots = Get-ForgeFreeDiskRoots }
    if ($null -eq $RecycleDirs) { $RecycleDirs = Get-ForgeRecycleDirs }
    $freed = [long]0; $removed = 0
    foreach ($r in @($Roots)) {
        if (-not $r -or -not (Test-Path -LiteralPath $r.dir -PathType Container)) { continue }
        if (-not (Test-ForgeNoReparseChain $r.dir)) { Write-ActionOut "  $($r.dir): skipped (it or a parent folder is a link)"; continue }
        $old = Select-OldItems -Items (Get-ForgeChildItems $r.dir) -Now $Now -MaxAgeDays $r.days -Deep
        $n = 0; $b = [long]0
        foreach ($it in $old) {
            $b += Remove-ForgeItemNoFollow $it
            if (-not (Test-Path -LiteralPath $it.FullName)) { $n++ }
        }
        if ($n) { Write-ActionOut "  $($r.dir): removed $n item(s), $([Math]::Round($b / 1MB, 1)) MB" }
        $removed += $n; $freed += $b
    }
    $bin = [long]0; $binN = 0
    foreach ($d in @($RecycleDirs)) {
        if (-not $d -or -not (Test-ForgeNoReparseChain $d)) { continue }
        foreach ($it in (Get-ForgeChildItems $d)) {
            if ($it.Name -eq 'desktop.ini') { continue }
            $bin += Remove-ForgeItemNoFollow $it
            if (-not (Test-Path -LiteralPath $it.FullName)) { $binN++ }
        }
    }
    if ($binN) { Write-ActionOut "  recycle bins: removed $binN item(s), $([Math]::Round($bin / 1MB, 1)) MB" }
    $removed += $binN; $freed += $bin
    $mb = [Math]::Round($freed / 1MB, 1)
    Write-ActionOut "free disk: $removed item(s), $mb MB freed"
    $script:ActionResult = [ordered]@{ result = 'ok'; removed = $removed; freed_mb = $mb; summary = "$mb MB freed" }
}

# R6 retire: the uninstall must outlive ForgeSystemAgent (it runs this job), so it is a one-shot SYSTEM task that starts
# 90 s later; this job only schedules it and ends `done`. The script text is fixed; the only inputs are the Forge home
# (from this script's location, checked against the ForgeSystemAgent task) and keep_data.
function New-ForgeUninstallScript([string]$ForgeHome, [bool]$KeepData) {
    $fh = $ForgeHome.Replace("'", "''")
    $keep = if ($KeepData) { '$true' } else { '$false' }
    @"
# Hivemind uninstall (generated by the forge_uninstall action). Runs once as SYSTEM, then removes itself.
`$ErrorActionPreference = 'SilentlyContinue'
`$fh = '$fh'; `$keep = $keep
foreach (`$t in 'ForgeWatchdog', 'ForgeAgent', 'ForgeHub', 'ForgeSystemAgent') { Stop-ScheduledTask -TaskName `$t; Unregister-ScheduledTask -TaskName `$t -Confirm:`$false }
Get-CimInstance Win32_Process | Where-Object { `$_.CommandLine -and `$_.CommandLine.IndexOf(`$fh, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and `$_.ProcessId -ne `$PID } | ForEach-Object { Stop-Process -Id `$_.ProcessId -Force }
Remove-NetFirewallRule -DisplayName 'Forge Control hub (8787)'
if (Test-Path -LiteralPath (Join-Path `$fh 'current')) { & cmd.exe /d /c "rmdir ```"`$fh\current```"" | Out-Null }
if (`$keep) { Get-ChildItem -LiteralPath `$fh -Force | Where-Object { `$_.Name -ne 'data' } | ForEach-Object { Remove-Item -LiteralPath `$_.FullName -Recurse -Force } }
else { Remove-Item -LiteralPath `$fh -Recurse -Force }
Unregister-ScheduledTask -TaskName 'ForgeUninstall' -Confirm:`$false
Remove-Item -LiteralPath `$PSCommandPath -Force
[IO.Directory]::Delete((Split-Path -Parent `$PSCommandPath), `$false)
"@
}

# ACL for the folder holding the generated uninstall script: SYSTEM + Administrators only, inheritance off.
function New-ForgeSystemOnlyDirSecurity {
    $sec = New-Object System.Security.AccessControl.DirectorySecurity
    $sec.SetAccessRuleProtection($true, $false)
    foreach ($sid in 'S-1-5-18', 'S-1-5-32-544') {
        $id = New-Object System.Security.Principal.SecurityIdentifier($sid)
        $sec.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($id, 'FullControl', 'ContainerInherit, ObjectInherit', 'None', 'Allow')))
    }
    $sec
}

function Invoke-ForgeUninstall([bool]$KeepData) {
    $fh = Get-ForgeHomeFromScript
    if (-not $fh) { throw 'cannot locate the Forge home (this script is not in <FH>\current\agent or <FH>\releases\<ver>\agent)' }
    $task = Get-ScheduledTask -TaskName 'ForgeSystemAgent' -ErrorAction SilentlyContinue
    $lines = @($task.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments) $($_.WorkingDirectory)" })
    if (-not (Test-ForgeHomeInTaskActions -ForgeHome $fh -ActionLines $lines)) { throw "refusing to uninstall: $fh is not the home of the registered ForgeSystemAgent task" }
    # the script runs as SYSTEM later: write it into a fresh folder only SYSTEM + Administrators can change
    $dir = Join-Path $env:SystemRoot ('Temp\forge-uninstall-' + [guid]::NewGuid().ToString('n'))
    if (Test-Path -LiteralPath $dir) { throw "refusing to reuse $dir" }
    [void][IO.Directory]::CreateDirectory($dir, (New-ForgeSystemOnlyDirSecurity))
    $path = Join-Path $dir 'uninstall.ps1'
    [IO.File]::WriteAllText($path, (New-ForgeUninstallScript -ForgeHome $fh -KeepData $KeepData), (New-Object Text.UTF8Encoding($true)))
    $act = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$path`""
    $trig = New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(90)
    $prin = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName 'ForgeUninstall' -Action $act -Trigger $trig -Principal $prin -Force -ErrorAction Stop | Out-Null
    Write-ActionOut "uninstall of $fh scheduled in 90 s (keep_data=$KeepData)"
    $script:ActionResult = [ordered]@{ result = 'scheduled'; forge_home = $fh; keep_data = $KeepData; summary = 'uninstall scheduled' }
}

# Returns the step's exit code (0 = ok).
function Invoke-ForgeActionStep($Step, [bool]$DryRun) {
    Write-ActionOut "> $(Format-ForgeActionStep $Step)"
    if ($DryRun) { Write-ActionOut '  (dry run: not executed)'; return 0 }
    if ($Step.kind -eq 'native') {
        $exe = Resolve-ForgeExe $Step.exe
        $argv = [string[]]$Step.argv
        $global:LASTEXITCODE = 0
        & $exe @argv 2>&1 | ForEach-Object { Write-ActionOut "$_" }
        $code = [int]$LASTEXITCODE
        if ($code -ne 0 -and $Step.ok_exit -and (@($Step.ok_exit) -contains $code)) { Write-ActionOut "  (exit ${code}: nothing to upgrade)"; return 0 }
        return $code
    }
    try {
        $p = $Step.params
        $script:ActionExit = $null
        $r = & $Step.command @p 2>&1
        foreach ($line in @($r)) { if ($null -ne $line) { Write-ActionOut "$line" } }
        if ($null -ne $script:ActionExit) { $c = [int]$script:ActionExit; $script:ActionExit = $null; return $c }
        return 0
    } catch { Write-ActionOut "error: $($_.Exception.Message)"; return 1 }
}

if ($ActionRunSpec) {
    $ErrorActionPreference = 'Continue'
    $ProgressPreference = 'SilentlyContinue'
    try {
        $spec = [IO.File]::ReadAllText($ActionRunSpec) | ConvertFrom-Json
        if ($spec.tools) { foreach ($tp in $spec.tools.PSObject.Properties) { $script:SpecTools[$tp.Name] = [string]$tp.Value } }
        $cat = Read-ForgeActionCatalog $ActionCatalogPath
        $norm = Test-ForgeActionArgs -Catalog $cat -Name ([string]$spec.action) -ActionArgs $spec.args
        $steps = Get-ForgeActionSteps -Name ([string]$spec.action) -ActionArgs $norm -User ([string]$spec.user)
        $who = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        Write-ActionOut "action $($spec.action) as $who$(if ($spec.dry_run) { ' (DRY RUN)' })"
        $code = 0
        foreach ($s in $steps) {
            $c = Invoke-ForgeActionStep $s ([bool]$spec.dry_run)
            if ($c -ne 0) { $code = $c; Write-ActionOut "step failed (exit $c)"; break }
        }
        $res = if ($script:ActionResult -and -not $spec.dry_run) { $script:ActionResult } else { Get-DefaultActionResult ([string]$spec.action) $code ([bool]$spec.dry_run) }
        Write-ActionOut (Format-ActionResult $res)
        exit $code
    } catch {
        Write-ActionOut "action failed: $($_.Exception.Message)"
        Write-ActionOut (Format-ActionResult ([ordered]@{ result = 'error'; exit = 1; detail = "$($_.Exception.Message)" }))
        exit 1
    }
}
