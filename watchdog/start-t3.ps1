# Start this user's T3 Code server at boot when nobody is logged in.
# T3's own scheduled task (t3code-<HOST>-<user>) only fires at logon; this reuses its exact action, so it
# keeps working after `t3 update` changes the versioned path.
# Exit codes (2.2 keep-alive services, agent\services.ps1): 0 = already running, 10 = started now,
# 20 = T3 is not set up for this user (nothing to keep alive).
$ErrorActionPreference = 'SilentlyContinue'
$runtime = Join-Path $env:USERPROFILE '.t3\userdata\server-runtime.json'
if (Test-Path $runtime) {
    try {
        $r = Get-Content $runtime -Raw | ConvertFrom-Json
        if ($r.pid -and (Get-Process -Id ([int]$r.pid) -ErrorAction SilentlyContinue)) { exit 0 }
    } catch {}
}
$task = Get-ScheduledTask | Where-Object { $_.TaskName -like 't3code-*' } | Select-Object -First 1
if (-not $task) { exit 20 }
$a = $task.Actions | Select-Object -First 1
if (-not $a -or -not $a.Execute) { exit 20 }
$wd = if ($a.WorkingDirectory) { $a.WorkingDirectory } else { $env:USERPROFILE }
Start-Process -FilePath $a.Execute -ArgumentList $a.Arguments -WorkingDirectory $wd -WindowStyle Hidden
exit 10
