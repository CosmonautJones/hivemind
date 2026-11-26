# Hivemind 2.3 - frozen watchdog launcher (workstream R, item R2). CONTRACT-phase3.md section 6.5.
# Installed ONCE as <ForgeHome>\bin\wd-launcher.ps1 (Administrators + SYSTEM full, user read + execute; NOT under
# data\) by install.ps1 / the 2.3 watchdog migration (Install-WdLauncher); the ForgeWatchdog task runs it instead of
# current\watchdog\watchdog.ps1. It must stay tiny and must never need an update: a broken watchdog.ps1 in a new
# release can then no longer stop the fleet from healing itself.
#   1. run <ForgeHome>\current\watchdog\watchdog.ps1 (passes -ForgeHome, -Port, -DryRun through)
#   2. if data\watchdog.json last_run is older than 10 min (or current has no watchdog), run the previous release's
#      watchdog instead: releases\<previous>\watchdog\watchdog.ps1, previous = highest release below current.
#      The old watchdog's probation logic then rolls the bad release back.
# Dot-source for tests:  . .\wd-launcher.ps1 -NoRun
param(
    [string]$ForgeHome = (Join-Path $env:ProgramData 'Forge'),
    [int]$Port = 8787,
    [switch]$DryRun,
    [switch]$NoRun
)

$LauncherVersion = 1

function ConvertTo-LauncherVersion([string]$V) {
    $n = New-Object int[] 3
    $p = ("$V".Trim() -replace '^[vV]', '' -replace '[-+].*$', '').Split('.')
    for ($i = 0; $i -lt 3; $i++) { $x = 0; if ($i -lt $p.Count) { [void][int]::TryParse($p[$i], [ref]$x) }; $n[$i] = $x }
    $n
}

function Compare-LauncherVersion([string]$A, [string]$B) {
    $x = ConvertTo-LauncherVersion $A; $y = ConvertTo-LauncherVersion $B
    for ($i = 0; $i -lt 3; $i++) { if ($x[$i] -ne $y[$i]) { if ($x[$i] -lt $y[$i]) { return -1 } else { return 1 } } }
    0
}

# Epoch seconds of watchdog.json last_run, $null when missing / unreadable.
function Get-WatchdogLastRun([string]$ForgeHome) {
    $f = Join-Path $ForgeHome 'data\watchdog.json'
    if (-not (Test-Path -LiteralPath $f)) { return $null }
    try {
        $j = Get-Content -LiteralPath $f -Raw -ErrorAction Stop | ConvertFrom-Json
        if (-not $j.last_run) { return $null }
        [double]([DateTimeOffset]::Parse([string]$j.last_run, [Globalization.CultureInfo]::InvariantCulture)).ToUnixTimeSeconds()
    } catch { $null }
}

# -> full path of the watchdog.ps1 to run this minute (current, or the previous release's when current is stale).
function Select-WatchdogScript {
    param([string]$ForgeHome, [double]$NowEpoch, [int]$StaleSec = 600)
    $cur = Join-Path $ForgeHome 'current\watchdog\watchdog.ps1'
    $curOk = Test-Path -LiteralPath $cur
    if ($curOk) {
        $last = Get-WatchdogLastRun $ForgeHome
        # no watchdog.json yet = fresh install: current is all there is
        if ($null -eq $last -or ($NowEpoch - $last) -le $StaleSec) { return $cur }
    }
    $curVer = $null
    $vf = Join-Path $ForgeHome 'current\VERSION'
    if (Test-Path -LiteralPath $vf) { $curVer = (Get-Content -LiteralPath $vf -Raw).Trim() }
    $best = $null
    $rel = Join-Path $ForgeHome 'releases'
    foreach ($d in @(Get-ChildItem -LiteralPath $rel -Directory -ErrorAction SilentlyContinue)) {
        if ($d.Name -notmatch '^\d+(\.\d+){0,2}([-+][0-9A-Za-z.-]+)?$') { continue }
        $wd = Join-Path $d.FullName 'watchdog\watchdog.ps1'
        if (-not (Test-Path -LiteralPath $wd) -or -not (Test-Path -LiteralPath (Join-Path $d.FullName 'VERSION'))) { continue }
        if ($curVer -and (Compare-LauncherVersion $d.Name $curVer) -ge 0) { continue }
        if (-not $best -or (Compare-LauncherVersion $d.Name $best.Name) -gt 0) { $best = $d }
    }
    if ($best) { return (Join-Path $best.FullName 'watchdog\watchdog.ps1') }
    $cur
}

if ($NoRun) { return }
$wd = Select-WatchdogScript -ForgeHome $ForgeHome -NowEpoch ([double][DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
$a = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $wd, '-ForgeHome', $ForgeHome, '-Port', $Port)
if ($DryRun) { $a += '-DryRun'; Write-Host "[wd-launcher] running $wd" }
& powershell.exe @a
exit $LASTEXITCODE
