# Publish a Forge bundle to a hub: POST <hub>/cluster/publish (X-Cluster-Token, body = zip).
# The hub validates it (VERSION > its own, hub/app.py compiles), switches `current`, its watchdog
# restarts it, and every other watchdog upgrades to it within a minute or two.
#   .\tools\publish.ps1                           # build from this repo, publish to localhost
#   .\tools\publish.ps1 -Hub http://forge-01:8787 -Zip dist\forge-2.0.1.zip
#   .\tools\publish.ps1 -DryRun                   # build + show what would be sent
# Token: -Token, else $env:FORGE_CLUSTER_TOKEN, else cluster_token in %ProgramData%\Forge\data\config.json.
param(
    [string]$Hub = 'http://localhost:8787',
    [string]$Zip,
    [string]$Token,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'build-bundle.ps1') -NoRun

if (-not $Token) { $Token = $env:FORGE_CLUSTER_TOKEN }
if (-not $Token) {
    $cfg = Join-Path $env:ProgramData 'Forge\data\config.json'
    if (Test-Path -LiteralPath $cfg) { try { $Token = (Get-Content -LiteralPath $cfg -Raw | ConvertFrom-Json).cluster_token } catch {} }
}
if (-not $Zip) { $b = New-ForgeBundle -Root (Split-Path -Parent $PSScriptRoot); $Zip = $b.path }
if (-not (Test-Path -LiteralPath $Zip)) { throw "zip not found: $Zip" }
$sha = Get-FileSha256 $Zip
$url = "$($Hub.TrimEnd('/'))/cluster/publish"
if ($DryRun) {
    Write-Host "[dry-run] POST $url  ($((Get-Item -LiteralPath $Zip).Length) bytes, sha256 $sha, token $(if ($Token) { 'set' } else { 'MISSING' }))"
    return
}
if (-not $Token) { throw 'no cluster token: pass -Token or set FORGE_CLUSTER_TOKEN' }
$resp = Invoke-RestMethod -Uri $url -Method Post -Headers @{ 'X-Cluster-Token' = $Token } -ContentType 'application/zip' `
    -Body ([IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $Zip).Path)) -TimeoutSec 120 -UseBasicParsing
Write-Host "published $Zip (sha256 $sha) to $Hub"
$resp
