# Publish a Forge bundle to a hub: POST <hub>/cluster/publish (X-Cluster-Token, body = zip).
# The hub validates it (VERSION > its own, hub/app.py compiles), switches `current`, its watchdog
# restarts it, and every other watchdog upgrades to it within a minute or two.
#   .\tools\publish.ps1                           # build from this repo, publish to localhost
#   .\tools\publish.ps1 -Hub http://forge-01:8787 -Zip dist\forge-2.0.1.zip
#   .\tools\publish.ps1 -DryRun                   # build + show what would be sent
#   .\tools\publish.ps1 -Sign                     # 2.3 S6: sign with this user's key (required once a key is pinned)
# Token: -Token, else $env:FORGE_CLUSTER_TOKEN, else cluster_token in %ProgramData%\Forge\data\private\secrets.json
# (2.3, readable from an elevated shell) or data\config.json (2.2).
param(
    [string]$Hub = 'http://localhost:8787',
    [string]$Zip,
    [string]$Token,
    [switch]$Sign,
    [string]$SigningKey,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$pubSign = $Sign; $pubKey = $SigningKey      # dot-sourcing build-bundle.ps1 rebinds its param names here
. (Join-Path $PSScriptRoot 'build-bundle.ps1') -NoRun
$Sign = $pubSign; $SigningKey = $pubKey

if (-not $Token) { $Token = $env:FORGE_CLUSTER_TOKEN }
foreach ($cfg in @((Join-Path $env:ProgramData 'Forge\data\private\secrets.json'), (Join-Path $env:ProgramData 'Forge\data\config.json'))) {
    if ($Token) { break }
    if (Test-Path -LiteralPath $cfg) { try { $Token = (Get-Content -LiteralPath $cfg -Raw | ConvertFrom-Json).cluster_token } catch {} }
}
if (-not $Zip) { $b = New-ForgeBundle -Root (Split-Path -Parent $PSScriptRoot) -Sign:$Sign -SigningKey $SigningKey; $Zip = $b.path }
elseif ($Sign) { throw '-Sign applies when building; a prebuilt -Zip is published as is (sign it with build-bundle.ps1 -Sign)' }
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
