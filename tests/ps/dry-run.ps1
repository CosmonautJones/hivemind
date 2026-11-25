# Dry runs that prove the installer and watchdog flows without touching the machine:
#   1. install.ps1 -DryRun against fixtures\bootstrap.json + a freshly built bundle (sha patched in),
#      with -ForgeHome in a scratch dir (nothing under ProgramData is read or written).
#   2. watchdog.ps1 -DryRun on a scratch ForgeHome (real junction to release 2.0.0, scratch only) with a
#      fake hub on a high port that reports a healthy peer at 2.0.1 -> expect "[dry-run] update 2.0.0 -> 2.0.1".
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File tests\ps\dry-run.ps1 [-Scratch <dir>] [-Port 18897]
param([string]$Scratch = (Join-Path ([IO.Path]::GetTempPath()) "forge-dryrun-$([guid]::NewGuid().ToString('n').Substring(0,8))"), [int]$Port = 18897)
$ErrorActionPreference = 'Stop'
if ($Port -eq 8787) { throw 'never use the live hub port' }
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

Write-Host "`n########## watchdog dry run ##########" -ForegroundColor Magenta
$fh = Join-Path $Scratch 'Forge-wd'
# child scope: dot-sourcing would otherwise overwrite $Port/$ForgeHome/$DryRun here with the watchdog's defaults
& { . (Join-Path $Repo 'watchdog\watchdog.ps1') -NoRun; Install-ReleaseZip -ForgeHome $fh -ZipPath $b.path -ExpectedSha $b.sha256 | Out-Null; Set-CurrentRelease $fh $b.version }
$data = Join-Path $fh 'data'; New-Item -ItemType Directory -Force $data | Out-Null
[IO.File]::WriteAllText((Join-Path $data 'config.json'), '{"node_id":"dryrun-node","agent_token":"a","cluster_token":"c","ui_token":"u","port":8787}')
$peerVer = '2.0.1'
$py = (Get-Command python.exe -ErrorAction Stop).Source
$srv = Start-Process -FilePath $py -ArgumentList '-I', "`"$(Join-Path $PSScriptRoot 'fake_hub.py')`"", $Port, $b.version, $peerVer, ('ab' * 32) -PassThru -WindowStyle Hidden
try {
    Start-Sleep -Seconds 2
    if ($Port -eq 8787) { throw 'refusing: port resolved to the live hub port' }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Repo 'watchdog\watchdog.ps1') -ForgeHome $fh -Port $Port -DryRun
} finally {
    Stop-Process -Id $srv.Id -Force -ErrorAction SilentlyContinue
    & cmd.exe /d /c "rmdir `"$fh\current`"" | Out-Null
    Remove-Item -LiteralPath $Scratch -Recurse -Force -ErrorAction SilentlyContinue
}
