# Hivemind - Update & Repair this machine (one click, asks for admin once).
# Finds a healthy hub, re-runs the idempotent installer (updates, re-registers tasks, rejoins the fleet),
# then opens the dashboard. Reads tokens/hub list from this machine's own config at run time.
$self = $MyInvocation.MyCommand.Path
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Start-Process powershell.exe -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$self`""
    return
}
$Host.UI.RawUI.WindowTitle = 'Hivemind - Update & Repair'
Write-Host ''
Write-Host '  Hivemind  -  Update & Repair' -ForegroundColor Cyan
Write-Host '  -----------------------------' -ForegroundColor DarkCyan
try {
    $data = Join-Path $env:ProgramData 'Forge\data'
    $cfg = Get-Content (Join-Path $data 'config.json') -Raw | ConvertFrom-Json
    $ac  = Get-Content (Join-Path $data 'agent-config.json') -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json
    $urls = @(@($ac.hub_urls) | Where-Object { $_ -and $_ -notmatch '127\.0\.0\.1|localhost' }) + @('http://127.0.0.1:8787')
    $hub = $null
    foreach ($u in $urls) {
        try { if ((Invoke-RestMethod "$($u.TrimEnd('/'))/healthz" -TimeoutSec 4).ok) { $hub = $u.TrimEnd('/'); break } } catch {}
    }
    if (-not $hub) { throw 'No Hivemind hub is reachable. Check the network / Tailscale, then try again.' }
    Write-Host "  Connecting to $hub ..." -ForegroundColor Gray
    $installer = Invoke-RestMethod "$hub/agent/install.ps1?t=$([uri]::EscapeDataString([string]$cfg.agent_token))" -TimeoutSec 60
    Invoke-Expression $installer
    Write-Host ''
    Write-Host '  All set. This machine is updated and connected.' -ForegroundColor Green
    Start-Process 'http://localhost:8787'
} catch {
    Write-Host ''
    Write-Host "  Something went wrong: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host '  Open Hivemind on another machine -> Machines to see what this one reports.' -ForegroundColor Yellow
}
Write-Host ''
Read-Host '  Press Enter to close'
