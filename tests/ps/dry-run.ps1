# Dry runs that prove the installer and watchdog flows without touching the machine:
#   1. install.ps1 -DryRun against fixtures\bootstrap.json + a freshly built bundle (sha patched in),
#      with -ForgeHome in a scratch dir (nothing under ProgramData is read or written).
#   2. watchdog.ps1 -DryRun on a scratch ForgeHome (real junction to release 2.0.0, scratch only) with a
#      fake hub on a high port that reports a healthy peer at 8.0.0 -> expect "[dry-run] update <VERSION> -> 8.0.0"
#      (2.2: a fleet-bad 9.9.9 peer is skipped and the update lease is requested from the leader first).
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File tests\ps\dry-run.ps1 [-Scratch <dir>] [-Port 19052]
param([string]$Scratch = (Join-Path ([IO.Path]::GetTempPath()) "forge-dryrun-$([guid]::NewGuid().ToString('n').Substring(0,8))"), [int]$Port = 19052)
$ErrorActionPreference = 'Stop'
if ($Port -lt 19000 -or $Port -gt 19299) { throw 'test servers use ports 19000-19299 only (never the live 8787; Phase 3 team ranges: CONTRACT-phase3 9)' }
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
New-Item -ItemType Directory -Force $Scratch | Out-Null
. (Join-Path $Repo 'tools\build-bundle.ps1') -NoRun

Write-Host "`n########## installer dry run ##########" -ForegroundColor Magenta
$b = New-ForgeBundle -Root $Repo -OutDir (Join-Path $Scratch 'dist')
$fx = Get-Content (Join-Path $PSScriptRoot 'fixtures\bootstrap.json') -Raw | ConvertFrom-Json
$fx.bundle_version = $b.version; $fx.bundle_sha256 = $b.sha256
$fxPath = Join-Path $Scratch 'bootstrap.json'
[IO.File]::WriteAllText($fxPath, (ConvertTo-Json -InputObject $fx -Depth 6), (New-Object Text.UTF8Encoding($false)))
& (Join-Path $Repo 'agent\install.ps1') -DryRun -Hub 'http://forge-01.example.ts.net:8787' -Token 'agent-FAKE-test-token' `
    -ForgeHome (Join-Path $Scratch 'Forge-install') -BootstrapFile $fxPath -BundleFile $b.path

Write-Host "`n########## installer -NewFleet dry run (2.3 F1) ##########" -ForegroundColor Magenta
$nfHome = Join-Path $Scratch 'Forge-newfleet'
$nfArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $Repo 'agent\install.ps1'), '-NewFleet', '-DryRun', '-NoBrowser',
            '-ForgeHome', $nfHome, '-BundleFile', $b.path)
$nfOut = (& powershell.exe @($nfArgs + @('-AdminPin', '123456')) 2>&1 | Out-String)
Write-Host $nfOut
$nfPrompt = (& powershell.exe @nfArgs 2>&1 | Out-String)      # no -AdminPin: the dry run must not prompt
$script:NewFleetChecks = [ordered]@{
    'new fleet: tokens generated, no peers, local bundle'  = ($nfOut -match 'generated agent_token \+ cluster_token' -and $nfOut -match "first hub of a new fleet \(no peers\), bundle $([regex]::Escape($b.version))")
    'new fleet: signing key step'                          = ($nfOut -match 'New-ForgeSigningKey' -or $nfOut -match 'signing key: reusing')
    'new fleet: admin PIN accepted (-AdminPin)'           = ($nfOut -match 'admin PIN (set|accepted)')
    'new fleet: no PIN prompt in a dry run without -AdminPin' = ($nfPrompt -match '\[dry-run\] prompt for a 6-digit admin PIN')
    'new fleet: first-run claim + claim URL'               = ($nfOut -match 'firstrun-claim\.json' -and $nfOut -match '\[dry-run\] open http://localhost:8787/\?claim=[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}')
    'new fleet: joins through the local hub'               = ($nfOut -match '\[dry-run\] join \S+ via local hub')
    'new fleet: nothing written'                           = (-not (Test-Path -LiteralPath $nfHome))
    # with S's Set-ForgeDataAcl (2.3) the root steps stay icacls (3) and data\ + data\private\ + bin\ come from one
    # Set-ForgeDataAcl step; without it the 2.2-style plan prints 5 icacls steps
    'new fleet: ACL steps printed one per line'            = $(if ($nfOut -match '\[dry-run\] Set-ForgeDataAcl: ') { [regex]::Matches($nfOut, '\[dry-run\] icacls ').Count -eq 3 -and $nfOut -match 'Set-ForgeDataAcl: [^;]+\\data \[' } else { [regex]::Matches($nfOut, '\[dry-run\] icacls ').Count -eq 5 })
    'new fleet: data\private secured before secrets/claim' = ($nfOut.IndexOf('[dry-run] secure ') -ge 0 -and $nfOut.IndexOf('[dry-run] secure ') -lt $nfOut.IndexOf('firstrun-claim.json'))
    'new fleet: no exception'                              = ($nfOut -notmatch 'FAILED|Exception|error' -and $nfPrompt -notmatch 'FAILED|Exception')
}

Write-Host "`n########## watchdog dry run ##########" -ForegroundColor Magenta
$fh = Join-Path $Scratch 'Forge-wd'
# child scope: dot-sourcing would otherwise overwrite $Port/$ForgeHome/$DryRun here with the watchdog's defaults
& { . (Join-Path $Repo 'watchdog\watchdog.ps1') -NoRun; Install-ReleaseZip -ForgeHome $fh -ZipPath $b.path -ExpectedSha $b.sha256 | Out-Null; Set-CurrentRelease $fh $b.version }
$data = Join-Path $fh 'data'; New-Item -ItemType Directory -Force $data | Out-Null
[IO.File]::WriteAllText((Join-Path $data 'config.json'), '{"node_id":"dryrun-node","agent_token":"a","cluster_token":"c","ui_token":"u","port":8787}')
# job dir cleanup: one job dir untouched for 20 days, one fresh, one old but still listed in running.json
$wrk = Join-Path $fh 'work'
foreach ($j in 'old-job', 'new-job', 'run-job') { New-Item -ItemType Directory -Force (Join-Path $wrk "jobs\$j") | Out-Null; [IO.File]::WriteAllText((Join-Path $wrk "jobs\$j\out.log"), 'x') }
foreach ($j in 'old-job', 'run-job') { $old = (Get-Date).AddDays(-20); (Get-Item (Join-Path $wrk "jobs\$j\out.log")).LastWriteTime = $old; (Get-Item (Join-Path $wrk "jobs\$j")).LastWriteTime = $old }
[IO.File]::WriteAllText((Join-Path $data 'agent-config.json'), (ConvertTo-Json -InputObject @{ node_id = 'dryrun-node'; work_root_current = $wrk }))
[IO.File]::WriteAllText((Join-Path $data 'running.json'), '{"lane":"user","jobs":[{"id":"run-job","pid":1}]}')
$peerVer = '8.0.0'   # above the repo VERSION; the fake also advertises a fleet-bad 9.9.9
$py = if ($env:FORGE_TEST_PYTHON) { $env:FORGE_TEST_PYTHON } else { (Get-Command python.exe -ErrorAction Stop).Source }
$srv = Start-Process -FilePath $py -ArgumentList '-I', "`"$(Join-Path $PSScriptRoot 'fake_hub.py')`"", $Port, $b.version, $peerVer, ('ab' * 32) -PassThru -WindowStyle Hidden
try {
    Start-Sleep -Seconds 2
    if ($Port -eq 8787) { throw 'refusing: port resolved to the live hub port' }
    $wdOut = (& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Repo 'watchdog\watchdog.ps1') -ForgeHome $fh -Port $Port -DryRun | Out-String)
    Write-Host $wdOut
    # 2.2 rollout checks: fleet-bad 9.9.9 skipped, lease requested from the leader before activation, and the
    # lease client against the fake leader (granted / denied+holder / pre-2.2 404 / unreachable).
    $fakeBase = "http://127.0.0.1:$Port"; $leader = "$fakeBase/peer"   # computed here: dot-sourcing the watchdog below resets $Port to 8787
    $checks = [ordered]@{
        'watchdog picks 8.0.0, never the fleet-bad 9.9.9'  = ($wdOut -match [regex]::Escape("update $($b.version) -> $peerVer from atlas") -and $wdOut -notmatch '-> 9\.9\.9')
        'update lease requested from the leader first'     = ($wdOut -match [regex]::Escape("[dry-run] request update lease for $peerVer from $leader"))
        'activation still flips current after the lease'   = ($wdOut -match [regex]::Escape("[dry-run] switch current -> releases\$peerVer"))
        '2.3: frozen launcher + agent loop go to <FH>\bin' = ($wdOut -match [regex]::Escape('[dry-run] install bin\wd-launcher.ps1') -and $wdOut -match [regex]::Escape('[dry-run] install bin\agent-loop.cmd') -and -not (Test-Path (Join-Path $fh 'bin')))
        '2.3: live tasks of another ForgeHome never migrated' = ($wdOut -notmatch 'migrate Forge')
        'old job dirs pruned (1: fresh + running kept)'    =($wdOut -match [regex]::Escape("[dry-run] remove 1 job dir(s) older than 14 days under $wrk\jobs") -and (Test-Path (Join-Path $wrk 'jobs\old-job')))
    }
    & {
        . (Join-Path $Repo 'watchdog\watchdog.ps1') -NoRun
        $checks['lease granted']              = ((Request-UpdateLease $leader 'c' 'grant-node' '2.0.1').kind -eq 'granted')
        $d = Request-UpdateLease $leader 'c' 'other-node' '2.0.1'
        $checks['lease denied, holder parsed'] = ($d.kind -eq 'denied' -and $d.holder -eq 'canary-1' -and (Get-LeaseDecision -Result $d -Now 1).reason -eq 'waiting for update lease (holder canary-1)')
        $checks['pre-2.2 hub (404) = unsupported'] = ((Request-UpdateLease $fakeBase 'c' 'n' '2.0.1').kind -eq 'unsupported')
        $e = Request-UpdateLease "$fakeBase/broken" 'c' 'n' '2.0.1'
        $checks['leader answering 500 = error -> wait, even after 10 min without leader'] = ($e.kind -eq 'error' -and $e.code -eq 500 -and (Get-LeaseDecision -Result $e -NoLeaderSince 400 -Now 1000).action -eq 'wait')
        $checks['no hub listening = unreachable']  = ((Request-UpdateLease 'http://127.0.0.1:19099' 'c' 'n' '2.0.1' 2).kind -eq 'unreachable')
        $checks['lease release']                    = (Remove-UpdateLease $leader 'c' 'grant-node' '2.0.1')
        $checks['fleet-bad report (POST /cluster/bad-version)'] = (Send-BadVersion $leader 'c' 'grant-node' '2.0.1')
        $checks['fleet-bad report fails cleanly on a pre-2.2 hub'] = (-not (Send-BadVersion $fakeBase 'c' 'n' '2.0.1'))
    }
    foreach ($k in $script:NewFleetChecks.Keys) { $checks[$k] = $script:NewFleetChecks[$k] }
    $fail = 0
    foreach ($k in $checks.Keys) { if ($checks[$k]) { Write-Host "  ok   $k" -ForegroundColor Green } else { $fail++; Write-Host "  FAIL $k" -ForegroundColor Red } }
    Write-Host "$($checks.Count - $fail) passed, $fail failed"
    $script:DryRunFailures = $fail
} finally {
    Stop-Process -Id $srv.Id -Force -ErrorAction SilentlyContinue
    & cmd.exe /d /c "rmdir `"$fh\current`"" | Out-Null
    Remove-Item -LiteralPath $Scratch -Recurse -Force -ErrorAction SilentlyContinue
}
exit [int]$script:DryRunFailures
