# Hivemind 2.3 - offline admin PIN reset (workstream S, S7 / review finding S-R9).
# Run in an ELEVATED PowerShell on the machine that is currently the LEADER hub (Machines page, "leader"):
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\reset-admin-pin.ps1
# It writes <ForgeHome>\data\private\pin-reset.json. data\private is Administrators + SYSTEM only, so being able to
# write it is the proof that you own this machine. Within a few seconds the leader hub clears the replicated admin PIN
# (every hub follows), ends all step-up windows, deletes the file and logs a warning event. Then set a new PIN in
# Settings (the first PIN can be set without the old one). On a standby the file waits until that hub becomes leader.
#
#   -ForgeHome <dir>  default %ProgramData%\Forge (tests point it at a scratch dir)
#   -DryRun           print what would be written; write nothing
param(
    [string]$ForgeHome = (Join-Path $env:ProgramData 'Forge'),
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$priv = Join-Path $ForgeHome 'data\private'
if (-not (Test-Path -LiteralPath $priv -PathType Container)) {
    throw "no $priv : this machine has no 2.3 secret store (an admin PIN only exists on 2.3 hubs)"
}
$path = Join-Path $priv 'pin-reset.json'
$body = ConvertTo-Json -InputObject ([ordered]@{ requested = [double][DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); by = "$env:USERDOMAIN\$env:USERNAME" }) -Compress
if ($DryRun) { Write-Host "[dry-run] would write $path : $body"; return $path }
try { [IO.File]::WriteAllText($path, $body, (New-Object Text.UTF8Encoding($false))) }
catch [UnauthorizedAccessException] { throw "cannot write $path : run this from an elevated PowerShell (Run as administrator)" }
Write-Host "PIN reset requested ($path)."
Write-Host 'The leader hub clears the admin PIN within a few seconds; then set a new one in Settings.'
$path
