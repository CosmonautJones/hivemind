# Forge Control allowlisted maintenance actions (SYSTEM lane). Catalog = hub\actions.json (same file the hub
# validates against). The agent dot-sources this file to re-validate a job's args before it starts; the job itself
# runs in a separate process:
#   powershell -File actions.ps1 -ActionRunSpec <job dir>\spec.json -ActionCatalogPath <bundle>\hub\actions.json
# spec.json = {action, args, dry_run, user}. Untrusted values only ever travel inside spec.json and are passed to
# programs as argument ARRAYS (splatting) - never concatenated into a command line.
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
        if ($type -eq 'int') {
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
            $steps.Add(@{ kind = 'native'; exe = 'winget'; argv = [string[]]$argv })
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
        default { throw "no implementation for action '$Name'" }
    }
    , $steps.ToArray()
}

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

function Resolve-ForgeExe([string]$Name) {
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

# Returns the step's exit code (0 = ok).
function Invoke-ForgeActionStep($Step, [bool]$DryRun) {
    Write-ActionOut "> $(Format-ForgeActionStep $Step)"
    if ($DryRun) { Write-ActionOut '  (dry run: not executed)'; return 0 }
    if ($Step.kind -eq 'native') {
        $exe = Resolve-ForgeExe $Step.exe
        $argv = [string[]]$Step.argv
        $global:LASTEXITCODE = 0
        & $exe @argv 2>&1 | ForEach-Object { Write-ActionOut "$_" }
        return [int]$LASTEXITCODE
    }
    try {
        $p = $Step.params
        $r = & $Step.command @p 2>&1
        foreach ($line in @($r)) { if ($null -ne $line) { Write-ActionOut "$line" } }
        return 0
    } catch { Write-ActionOut "error: $($_.Exception.Message)"; return 1 }
}

if ($ActionRunSpec) {
    $ErrorActionPreference = 'Continue'
    $ProgressPreference = 'SilentlyContinue'
    try {
        $spec = [IO.File]::ReadAllText($ActionRunSpec) | ConvertFrom-Json
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
        exit $code
    } catch {
        Write-ActionOut "action failed: $($_.Exception.Message)"
        exit 1
    }
}
