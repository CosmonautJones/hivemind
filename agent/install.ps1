# Hivemind (Forge Control) node installer (SPEC-phase1 §9, 2.3: SPEC-phase3 F1). Run in an ELEVATED PowerShell.
# Join an existing fleet with the one-liner from "Add machine" (single-use enrollment code, 10 min):
#   irm '<hub>/agent/install.ps1?code=<ABCD-EFGH>' | iex
# The hub serving this file fills in $Hub and $Code (or $Token for 2.2 links) below. Idempotent: re-run to upgrade/repair.
# Start a NEW fleet on this machine (from a repo checkout or with -BundleFile): tokens, bundle signing key, admin PIN,
# first hub, then the browser opens http://localhost:8787/?claim=<one-time code> (signs this browser in):
#   .\install.ps1 -NewFleet [-BundleFile forge-2.3.0.zip] [-NoBrowser]
# Migrates v1 nodes (removes tasks ForgeControl-Hub / ForgeControl-Agent, retires %USERPROFILE%\forge-agent).
#
# Local / test use:
#   .\install.ps1 -Hub http://forge-01:8787 -Code ABCD-EFGH   (or -Token <agent_token>)
#   .\install.ps1 -DryRun -BootstrapFile fixture.json -BundleFile forge-2.0.0.zip   # print every action
#   .\install.ps1 -NewFleet -DryRun -AdminPin 123456 -BundleFile forge-2.3.0.zip     # print every first-run step
#   (-AdminPin only with -DryRun: a real run prompts, or reads FORGE_ADMIN_PIN; re-running -NewFleet keeps the
#    existing admin PIN unless -ResetPin is given)
#   . .\install.ps1 -NoRun                                                           # dot-source helpers
param(
    [string]$Hub = '__HUB__',
    [string]$Token = '__TOKEN__',
    [string]$Code = '__CODE__',
    [string]$ForgeHome = (Join-Path $env:ProgramData 'Forge'),
    [switch]$DryRun,
    [string]$BootstrapFile,
    [string]$BundleFile,
    [switch]$NewFleet,
    [string]$AdminPin,
    [switch]$ResetPin,
    [switch]$NoBrowser,
    [switch]$NoRun
)

$InstallerVersion = '2.3.0'
$Port = 8787

# ======================= pure helpers (unit-tested) =======================

function Test-Placeholder([string]$Value) { (-not $Value) -or ($Value -like '__*__') }

function Compare-SemVer([string]$A, [string]$B) {
    $pa = @("$A".TrimStart('v').Split('-')[0].Split('.') | ForEach-Object { [int]("0$_" -replace '\D', '') })
    $pb = @("$B".TrimStart('v').Split('-')[0].Split('.') | ForEach-Object { [int]("0$_" -replace '\D', '') })
    for ($i = 0; $i -lt 3; $i++) {
        $x = if ($i -lt $pa.Count) { $pa[$i] } else { 0 }; $y = if ($i -lt $pb.Count) { $pb[$i] } else { 0 }
        if ($x -lt $y) { return -1 }; if ($x -gt $y) { return 1 }
    }
    $ra = "$A".Contains('-'); $rb = "$B".Contains('-')
    if ($ra -and -not $rb) { return -1 }; if ($rb -and -not $ra) { return 1 }
    0
}

# URLs this node announces in POST /cluster/join (only those that exist), MagicDNS first.
function Get-JoinUrls {
    param([string]$DnsName, [string]$TailscaleIp, [string]$LanIp, [int]$Port = 8787)
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($h in @("$DnsName".TrimEnd('.'), $TailscaleIp, $LanIp)) {
        if ($h -and $h.Trim()) { $u = "http://$($h.Trim()):$Port"; if (-not $out.Contains($u)) { $out.Add($u) } }
    }
    , $out.ToArray()
}

# Peer URLs flattened in priority order (agent hub_urls tail).
function Get-PeerHubUrls {
    param([object[]]$Peers = @(), [string]$SelfId)
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($p in @($Peers | Where-Object { $_ } | Sort-Object -Property @{ Expression = { if ($null -ne $_.priority) { [int]$_.priority } else { 9999 } } }, id)) {
        foreach ($u in @($p.urls)) { $n = "$u".TrimEnd('/'); if ($n -and -not $out.Contains($n)) { $out.Add($n) } }
    }
    , $out.ToArray()
}

# Agent hub_urls: localhost first, then peer URLs by priority; the installing hub if no peers are known.
function Get-AgentHubUrls {
    param([object[]]$Peers = @(), [string]$Hub)
    $out = New-Object System.Collections.Generic.List[string]
    $out.Add('http://127.0.0.1:8787')
    foreach ($u in (Get-PeerHubUrls -Peers $Peers)) { if (-not $out.Contains($u)) { $out.Add($u) } }
    if ($out.Count -eq 1 -and $Hub -and -not (Test-Placeholder $Hub)) { $out.Add($Hub.TrimEnd('/')) }
    , $out.ToArray()
}

function New-SharedConfig {
    param($Bootstrap, [string]$NodeId, $Existing, [int]$Port = 8787)
    $h = [ordered]@{}
    if ($Existing) { foreach ($p in $Existing.PSObject.Properties) { $h[$p.Name] = $p.Value } }
    $h['node_id'] = $NodeId
    foreach ($k in 'ui_token', 'agent_token', 'cluster_token') { if ($Bootstrap.$k) { $h[$k] = [string]$Bootstrap.$k } }
    # seed_peers: without it a fresh hub only knows itself and elects itself leader (HUB-CONTRACT.md)
    $seeds = @()
    foreach ($p in @($Bootstrap.peers)) {
        if ($p -and $p.id) { $seeds += [ordered]@{ id = [string]$p.id; priority = $p.priority; urls = @($p.urls | Where-Object { $_ } | ForEach-Object { [string]$_ }) } }
    }
    if ($seeds.Count -or -not $h.Contains('seed_peers')) { $h['seed_peers'] = $seeds }
    if (-not $h.Contains('port') -or -not $h['port']) { $h['port'] = $Port }
    $h
}

# agent-config.json: keep v2 values, else migrate v1 ones (logs, claude_use_api_key, tools, max_jobs, interval).
# `user` (2.1) = the account the user lane runs as; the SYSTEM lane's ensure_task(run_as=user) registers S4U tasks for it.
function New-AgentConfig {
    param($Existing, $V1, [string]$NodeId, [string[]]$HubUrls, [string]$User)
    $h = [ordered]@{ node_id = $NodeId; hub_urls = @($HubUrls) }
    if ($User) { $h['user'] = $User } elseif ($Existing -and $Existing.user) { $h['user'] = [string]$Existing.user }
    foreach ($src in @($V1, $Existing)) {
        if (-not $src) { continue }
        foreach ($k in 'logs', 'claude_use_api_key', 'tools', 'max_jobs', 'interval', 'work_root_auto', 'allow_admin', 'action_dry_run', 'system_max_jobs') {
            if ($null -ne $src.$k) { $h[$k] = $src.$k }
        }
    }
    if (-not $h.Contains('logs')) { $h['logs'] = @() }
    $h['logs'] = @($h['logs'])
    $h
}

# icacls calls, in order. Lock down the ROOT only, then make every child inherit from it (applying /inheritance:r
# with /T strips each file's ACL, and (OI)(CI) grants don't apply to files -> nobody could read the release).
#   root:        Administrators + SYSTEM full, installing user (OI)(CI)RX  -> releases\, current, venv\ are read-only to the user
#   data\:       + user Modify (agent-config.json, logs, running.json, hub state)
#   config.json: explicit Administrators + SYSTEM full, user Read (tokens; the user lane only reads it)
function Get-ForgeAclPlan([string]$ForgeHome, [string]$User) {
    $data = Join-Path $ForgeHome 'data'; $cfg = Join-Path $data 'config.json'
    , @(
        @{ what = "icacls $ForgeHome : Administrators + SYSTEM full, $User read+execute, inheritance removed (root only)"
           args = @($ForgeHome, '/inheritance:r', '/grant:r', '*S-1-5-32-544:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F', "${User}:(OI)(CI)RX", '/C', '/Q') },
        @{ what = "icacls $ForgeHome\* /reset /T : everything inherits the root ACL"
           args = @((Join-Path $ForgeHome '*'), '/reset', '/T', '/C', '/Q') },
        @{ what = "icacls $data : + $User modify"
           args = @($data, '/grant:r', "${User}:(OI)(CI)M", '/C', '/Q') },
        @{ what = "icacls $cfg : explicit Administrators + SYSTEM full, $User read only"
           args = @($cfg, '/inheritance:r', '/grant:r', '*S-1-5-32-544:F', '*S-1-5-18:F', "${User}:R", '/C', '/Q') }
    )
}

# ---- 2.3 F1: new fleet / enrollment codes ----

# 24 random bytes, urlsafe base64 without padding (32 chars), like Python's secrets.token_urlsafe(24).
function New-UrlSafeToken([int]$Bytes = 24) {
    $b = New-Object byte[] $Bytes
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($b) } finally { $rng.Dispose() }
    [Convert]::ToBase64String($b).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

# Fleet tokens for -NewFleet. An existing config.json keeps its tokens (re-running -NewFleet never splits a fleet).
function New-FleetSecrets($Existing = $null) {
    $a = if ($Existing -and $Existing.agent_token) { [string]$Existing.agent_token } else { New-UrlSafeToken 24 }
    $c = if ($Existing -and $Existing.cluster_token) { [string]$Existing.cluster_token } else { New-UrlSafeToken 24 }
    @{ agent_token = $a; cluster_token = $c }
}

# Single-use codes (S3 format): 8 chars of Crockford base32 (no I/L/O/U), shown as XXXX-XXXX.
$CrockfordAlphabet = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
function New-ClaimCode {
    $b = New-Object byte[] 8
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($b) } finally { $rng.Dispose() }
    $s = -join ($b | ForEach-Object { $CrockfordAlphabet[$_ -band 31] })
    $s.Substring(0, 4) + '-' + $s.Substring(4, 4)
}

# Stored form of a code: sha256 hex of the code upper-cased without the dash (codes compare case-insensitively).
function Get-ClaimCodeHash([string]$Code) {
    $n = ("$Code" -replace '[-\s]', '').ToUpperInvariant()
    $sha = [Security.Cryptography.SHA256]::Create()
    try { ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($n))) -replace '-', '').ToLowerInvariant() } finally { $sha.Dispose() }
}

# Writes data\private\firstrun-claim.json {code_sha256, expires, label} (the hub imports it once, S3/S4) and returns
# the code for the claim URL. -DryRun: returns a code, writes nothing.
function New-FirstRunClaim([string]$DataDir, [int]$TtlSec = 600, [switch]$DryRun) {
    $code = New-ClaimCode
    if ($DryRun) { return $code }
    $priv = Join-Path $DataDir 'private'
    if (-not (Test-Path -LiteralPath $priv)) { New-Item -ItemType Directory -Force $priv | Out-Null }
    $exp = [Math]::Round([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0 + $TtlSec, 3)
    Write-JsonFile (Join-Path $priv 'firstrun-claim.json') ([ordered]@{ code_sha256 = (Get-ClaimCodeHash $code); expires = $exp; label = 'first device' })
    $code
}

function Test-AdminPin([string]$Pin) { "$Pin" -match '^\d{6}$' }

# Where the -NewFleet admin PIN comes from: 'keep' (an admin_pin_hash already exists: re-running -NewFleet never
# silently replaces it; -ResetPin to change it), 'param' (-AdminPin: dry runs only, a PIN on the command line is
# visible to every process), 'env' (FORGE_ADMIN_PIN), 'prompt', or 'skip' (dry run without a PIN: never prompt).
function Get-AdminPinSource([string]$ExistingHash, [bool]$ResetPin, [string]$AdminPin, [string]$EnvPin, [bool]$DryRun) {
    if ($AdminPin -and -not $DryRun) { throw '-AdminPin is only for -DryRun (a PIN on the command line is visible to other processes); enter it at the prompt or set FORGE_ADMIN_PIN' }
    if ($ExistingHash -and -not $ResetPin) { return 'keep' }
    if ($AdminPin) { return 'param' }
    if ($EnvPin) { return 'env' }
    if ($DryRun) { return 'skip' }
    'prompt'
}

# icacls arguments that make data\private Administrators + SYSTEM only (inheritance off). Applied before any secret
# or claim file is written there, and again after the 2.2-style ACL fallback (its /reset /T would undo it).
function Get-ForgePrivateAclArgs([string]$PrivDir) {
    @($PrivDir, '/inheritance:r', '/grant:r', '*S-1-5-32-544:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F', '/C', '/Q')
}

# The -NewFleet "bootstrap": what /cluster/bootstrap would return, built locally (no peers: this is the first hub).
function New-NewFleetBootstrap([hashtable]$Secrets, [string]$Version, [string]$Sha256, [string]$SigningPubKey = $null) {
    [pscustomobject]@{ agent_token = $Secrets.agent_token; cluster_token = $Secrets.cluster_token; peers = @()
                       bundle_version = $Version; bundle_sha256 = $Sha256; signing_pubkey = $SigningPubKey }
}

# Secrets for data\private\secrets.json (S2) from a bootstrap: only the keys that are set.
function Get-BootstrapSecrets($Bootstrap, [string]$PinHash = $null, [string]$SigningPubKey = $null) {
    $h = @{}
    foreach ($k in 'cluster_token', 'ui_token_hash', 'signing_pubkey') { if ($Bootstrap -and $Bootstrap.$k) { $h[$k] = [string]$Bootstrap.$k } }
    if ($SigningPubKey) { $h['signing_pubkey'] = $SigningPubKey }
    if ($PinHash) { $h['admin_pin_hash'] = $PinHash }
    $h
}

# Bootstrap URL: enrollment code (2.3) or agent token (2.2 links, re-runs).
function Get-BootstrapUrl([string]$Hub, [string]$Code, [string]$Token) {
    $h = $Hub.TrimEnd('/')
    if (-not (Test-Placeholder $Code)) { return "$h/cluster/bootstrap?code=$([uri]::EscapeDataString($Code.Trim()))" }
    "$h/cluster/bootstrap?t=$([uri]::EscapeDataString($Token))"
}

function Get-ZipVersion([string]$ZipPath) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $z = [IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $e = $z.Entries | Where-Object { $_.FullName -eq 'VERSION' } | Select-Object -First 1
        if (-not $e) { return $null }
        $r = New-Object IO.StreamReader($e.Open())
        try { $r.ReadToEnd().Trim() } finally { $r.Close() }
    } finally { $z.Dispose() }
}

function Get-FileSha256([string]$Path) {
    $sha = [Security.Cryptography.SHA256]::Create()
    $fs = [IO.File]::OpenRead($Path)
    try { ([BitConverter]::ToString($sha.ComputeHash($fs)) -replace '-', '').ToLowerInvariant() } finally { $fs.Close(); $sha.Dispose() }
}

function Write-JsonFile([string]$Path, $Obj) {
    [IO.File]::WriteAllText($Path, (ConvertTo-Json -InputObject $Obj -Depth 8), (New-Object Text.UTF8Encoding($false)))
}

function Read-JsonFile([string]$Path) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $null }
    try { Get-Content -LiteralPath $Path -Raw -ErrorAction Stop | ConvertFrom-Json } catch { $null }
}

if ($NoRun) { return }

# ======================= actions =======================

function Write-Step([string]$m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Invoke-Step([string]$What, [scriptblock]$Do) {
    if ($DryRun) { Write-Host "  [dry-run] $What" -ForegroundColor Yellow; return $true }
    Write-Host "  - $What"
    try { & $Do; $true } catch { Write-Host "    FAILED: $($_.Exception.Message)" -ForegroundColor Red; $false }
}

function Find-Python {
    $cands = New-Object System.Collections.Generic.List[string]
    try { foreach ($l in @(& py.exe -0p 2>$null)) { if ("$l" -match '([A-Za-z]:\\.+python\.exe)') { $cands.Add($Matches[1]) } } } catch {}
    foreach ($g in @(Get-ChildItem "$env:ProgramFiles\Python3*\python.exe", "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue)) { $cands.Add($g.FullName) }
    $c = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue | Where-Object { $_.Source -notmatch 'WindowsApps' } | Select-Object -First 1
    if ($c) { $cands.Add($c.Source) }
    $best = $null
    foreach ($exe in $cands) {
        try {
            $v = ("$(& $exe -c "import sys;print('%d.%d.%d'%sys.version_info[:3])" 2>$null)").Trim()
            if ($v -match '^\d+\.\d+\.\d+$' -and (Compare-SemVer $v '3.10.0') -ge 0) {
                # prefer machine-wide installs (the SYSTEM watchdog runs pip from the venv)
                $score = if ($exe -like "$env:ProgramFiles*") { 1 } else { 0 }
                if (-not $best -or $score -gt $best.score -or ($score -eq $best.score -and (Compare-SemVer $v $best.version) -gt 0)) { $best = @{ exe = $exe; version = $v; score = $score } }
            }
        } catch {}
    }
    $best
}

function Find-Tailscale {
    $c = Get-Command tailscale.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { return $c.Source }
    $p = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
    if (Test-Path $p) { return $p }
    $null
}

function Get-TailscaleStatus([string]$Exe) {
    if (-not $Exe) { return $null }
    try { (& $Exe status --json 2>$null | Out-String) | ConvertFrom-Json } catch { $null }
}

function Get-LanIp {
    try {
        $c = Get-NetIPConfiguration -ErrorAction Stop | Where-Object {
            $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up' -and $_.InterfaceAlias -notmatch 'Tailscale|vEthernet|Loopback' } | Select-Object -First 1
        if ($c) { return [string]($c.IPv4Address | Select-Object -First 1).IPAddress }
    } catch {}
    $null
}

function Register-ForgeTask {
    param([string]$Name, [string]$Exe, [string]$Arguments, [string]$Dir, [string]$User, [switch]$System, [switch]$SystemService)
    $a = New-ScheduledTaskAction -Execute $Exe -Argument $Arguments -WorkingDirectory $Dir
    $boot = New-ScheduledTaskTrigger -AtStartup; $boot.Delay = 'PT15S'
    if ($SystemService) {
        # long-running SYSTEM process (ForgeSystemAgent): same settings as ForgeAgent, SYSTEM principal
        $triggers = @($boot)
        $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -StartWhenAvailable
        $p = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    } elseif ($System) {
        $rep = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
        $triggers = @($boot, $rep)
        $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
            -MultipleInstances IgnoreNew -StartWhenAvailable
        $p = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    } else {
        $triggers = @($boot)
        $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -StartWhenAvailable
        $p = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Limited
    }
    Register-ScheduledTask -TaskName $Name -Action $a -Trigger $triggers -Settings $s -Principal $p -Force | Out-Null
}

function Wait-Healthz([int]$Seconds = 60) {
    $end = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $end) {
        try { if ((Invoke-WebRequest "http://127.0.0.1:$Port/healthz" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200) { return $true } } catch {}
        Start-Sleep -Seconds 2
    }
    $false
}

function Send-Join([string]$Base, [string]$ClusterToken, [string]$Id, [string[]]$Urls) {
    $body = ConvertTo-Json -InputObject @{ id = $Id; urls = @($Urls) } -Compress
    Invoke-RestMethod -Uri "$($Base.TrimEnd('/'))/cluster/join" -Method Post -Headers @{ 'X-Cluster-Token' = $ClusterToken } `
        -ContentType 'application/json' -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 15 -UseBasicParsing
}

function ConvertFrom-SecurePin([Security.SecureString]$S) {
    $p = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($S)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($p) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p) }
}

function Read-AdminPin {
    for ($i = 0; $i -lt 3; $i++) {
        $a = ConvertFrom-SecurePin (Read-Host -AsSecureString 'Choose a 6-digit admin PIN (asked again for admin and destructive actions)')
        $b = ConvertFrom-SecurePin (Read-Host -AsSecureString 'Repeat the admin PIN')
        if ($a -ne $b) { Write-Host '  the PINs differ, try again' -ForegroundColor DarkYellow; continue }
        if (-not (Test-AdminPin $a)) { Write-Host '  the PIN must be exactly 6 digits' -ForegroundColor DarkYellow; continue }
        return $a
    }
    throw 'no admin PIN set (3 attempts)'
}

# 2.3 R7 task specs (watchdog.ps1 Get-ForgeTaskSpecs): @{ name; user ('SYSTEM' or the node user); command; arguments; restart }
function Register-ForgeTaskSpec($Spec, [string]$User, [string]$Dir) {
    $isSystem = "$($Spec.user)" -match '^(SYSTEM|NT AUTHORITY\\SYSTEM)$'
    $runUser = if ($isSystem) { $null } elseif ($Spec.user -and "$($Spec.user)" -notmatch '^(user|<user>)$') { [string]$Spec.user } else { $User }
    $a = New-ScheduledTaskAction -Execute ([string]$Spec.command) -Argument ([string]$Spec.arguments) -WorkingDirectory $Dir
    $boot = New-ScheduledTaskTrigger -AtStartup; $boot.Delay = 'PT15S'
    $triggers = @($boot)
    if ("$($Spec.name)" -eq 'ForgeWatchdog') {
        $triggers += New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
        $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -MultipleInstances IgnoreNew -StartWhenAvailable
    } elseif ($Spec.restart) {
        $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -StartWhenAvailable
    } else {
        $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable
    }
    if ($isSystem) { $p = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\SYSTEM' -LogonType ServiceAccount -RunLevel Highest }
    else { $p = New-ScheduledTaskPrincipal -UserId $runUser -LogonType S4U -RunLevel Limited }
    Register-ScheduledTask -TaskName ([string]$Spec.name) -Action $a -Trigger $triggers -Settings $s -Principal $p -Force | Out-Null
}

# Runs $Body with watchdog.ps1 dot-sourced in a CHILD scope (its params would overwrite ours). -> $Body's output.
function Invoke-WithWatchdog([string]$WatchdogScript, [scriptblock]$Body, [object[]]$ArgList = @()) {
    & { param($w, $b, $al) . $w -NoRun; & $b @al } $WatchdogScript $Body $ArgList
}

function Install-Forge {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    if ($DryRun) { Write-Host "Forge installer $InstallerVersion DRY RUN - nothing will be changed" -ForegroundColor Yellow }

    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) {
        if (-not $DryRun) { Write-Host 'Please run this in an elevated (Run as administrator) PowerShell.' -ForegroundColor Red; return }
        Write-Host '  (not elevated - fine for a dry run)' -ForegroundColor DarkGray
    }
    if ($NewFleet) { $Hub = "http://127.0.0.1:$Port" }
    if ((Test-Placeholder $Hub) -and -not $BootstrapFile) { Write-Host 'No hub URL: use the one-liner from the dashboard (Add machine), pass -Hub and -Code, or start a fleet with -NewFleet.' -ForegroundColor Red; return }
    if (Test-Placeholder $Hub) { $Hub = 'http://hub.invalid:8787' }
    $Hub = $Hub.TrimEnd('/')
    # 2.3 helpers: S (secrets.ps1, new-signing-key.ps1) from this checkout; for `irm | iex` from the extracted release below
    $repoRoot = if ($PSScriptRoot) { Split-Path -Parent $PSScriptRoot } else { $null }
    $loadS = { param($dir) $sp = Join-Path $dir 'secrets.ps1'; if (Test-Path -LiteralPath $sp) { . $sp; $true } else { $false } }

    $user   = "$env:USERDOMAIN\$env:USERNAME"
    $nodeId = $env:COMPUTERNAME.ToLower()
    $data   = Join-Path $ForgeHome 'data'
    $rels   = Join-Path $ForgeHome 'releases'
    $cur    = Join-Path $ForgeHome 'current'
    $venvPy = Join-Path $ForgeHome 'venv\Scripts\python.exe'
    $v1Dir  = Join-Path $env:USERPROFILE 'forge-agent'
    Write-Host "Forge Control ${InstallerVersion}: node '$nodeId' for $user -> $ForgeHome (hub $Hub)"

    # ---- 1. python ----
    Write-Step 'Python >= 3.10'
    $py = Find-Python
    if (-not $py) {
        Invoke-Step 'winget install Python.Python.3.12 (machine scope)' {
            & winget install --id Python.Python.3.12 -e --scope machine --accept-source-agreements --accept-package-agreements --silent | Out-Host
        } | Out-Null
        $py = Find-Python
        if (-not $py -and $DryRun) { $py = @{ exe = "$env:ProgramFiles\Python312\python.exe"; version = '3.12 (to be installed)' } }
        if (-not $py) { throw 'Python 3.10+ not found after winget install; install it and re-run.' }
    }
    Write-Host "  using $($py.exe) ($($py.version))"

    # ---- 2. bootstrap + bundle ----
    $pinHash = $null; $pubKey = $null
    if ($NewFleet) {
        Write-Step 'New fleet: tokens, signing key, admin PIN'
        $existingCfg = Read-JsonFile (Join-Path $data 'config.json')
        $fleet = New-FleetSecrets $existingCfg
        if ($existingCfg -and $existingCfg.agent_token) { Write-Host '  existing fleet tokens found in data\config.json: keeping them' }
        else { Write-Host '  generated agent_token + cluster_token (24 random bytes each)' }
        if (-not $BundleFile) {
            if (-not $repoRoot -or -not (Test-Path -LiteralPath (Join-Path $repoRoot 'tools\build-bundle.ps1'))) { throw '-NewFleet needs -BundleFile or a repo checkout (tools\build-bundle.ps1)' }
            . (Join-Path $repoRoot 'tools\build-bundle.ps1') -NoRun
            $built = New-ForgeBundle -Root $repoRoot -OutDir (Join-Path $env:TEMP 'hivemind-bundle')
            $BundleFile = $built.path
            Write-Host "  built bundle $($built.version) from $repoRoot -> $BundleFile"
        }
        $bv = Get-ZipVersion $BundleFile
        # bundle signing key (S6, tools\new-signing-key.ps1): private key DPAPI-protected in this user's profile
        $keyPath = Join-Path $env:LOCALAPPDATA 'Hivemind\signing-key.dpapi'; $pubPath = Join-Path $env:LOCALAPPDATA 'Hivemind\signing-pubkey.xml'
        $nsk = if ($repoRoot) { Join-Path $repoRoot 'tools\new-signing-key.ps1' } else { $null }
        if (Test-Path -LiteralPath $pubPath) { $pubKey = (Get-Content -LiteralPath $pubPath -Raw).Trim(); Write-Host "  signing key: reusing $pubPath" }
        elseif ($DryRun) { Write-Host "  [dry-run] New-ForgeSigningKey -> $keyPath (DPAPI) + $pubPath; the public key is pinned in secrets.json" -ForegroundColor Yellow }
        elseif ($nsk -and (Test-Path -LiteralPath $nsk)) {
            . $nsk -NoRun
            try { $pubKey = New-ForgeSigningKey -KeyPath $keyPath -PubKeyPath $pubPath; Write-Host "  - signing key created: $keyPath (public key pinned)" }
            catch { Write-Host "  - signing key NOT created ($($_.Exception.Message)); bundles stay unsigned until you pin one (Machines > Advanced)" -ForegroundColor DarkYellow }
        } else { Write-Host '  signing key skipped (no tools\new-signing-key.ps1 here); pin one later' -ForegroundColor DarkYellow }
        # admin PIN (S7): prompted twice; -AdminPin only with -DryRun; FORGE_ADMIN_PIN for unattended runs
        $existingSec = Read-JsonFile (Join-Path $data 'private\secrets.json')
        $existingHash = $null
        foreach ($src in @($existingSec, $existingCfg)) { if (-not $existingHash -and $src -and $src.admin_pin_hash) { $existingHash = [string]$src.admin_pin_hash } }
        $envPin = $env:FORGE_ADMIN_PIN
        if ($envPin) { Remove-Item -LiteralPath 'Env:\FORGE_ADMIN_PIN' -ErrorAction SilentlyContinue }
        $pinSource = Get-AdminPinSource -ExistingHash $existingHash -ResetPin ([bool]$ResetPin) -AdminPin $AdminPin -EnvPin $envPin -DryRun ([bool]$DryRun)
        if ($pinSource -eq 'keep') { $pinHash = $existingHash; Write-Host '  admin PIN: keeping the existing one (re-run with -ResetPin to replace it)' }
        elseif ($pinSource -eq 'skip') { Write-Host '  [dry-run] prompt for a 6-digit admin PIN (twice), hash it (New-ForgePinHash)' -ForegroundColor Yellow }
        else {
            $pin = if ($pinSource -eq 'param') { $AdminPin } elseif ($pinSource -eq 'env') { $envPin } else { Read-AdminPin }
            $envPin = $null
            if (-not (Test-AdminPin $pin)) { throw 'the admin PIN must be exactly 6 digits' }
            if ($repoRoot) { $null = . $loadS (Join-Path $repoRoot 'agent') }      # dot-sourced: functions land in this scope
            try { $pinHash = New-ForgePinHash $pin; Write-Host "  admin PIN set ($($pinHash.Split('$')[0]))" }
            catch {
                if (-not $DryRun) { throw "cannot hash the admin PIN: $($_.Exception.Message)" }
                Write-Host "  [dry-run] admin PIN accepted (hash helper unavailable: $($_.Exception.Message))" -ForegroundColor Yellow
            }
            $pin = $null
        }
        $boot = New-NewFleetBootstrap -Secrets $fleet -Version $bv -Sha256 (Get-FileSha256 $BundleFile) -SigningPubKey $pubKey
        Write-Host "  first hub of a new fleet (no peers), bundle $bv"
    }
    Write-Step 'Cluster bootstrap'
    if ($NewFleet) { Write-Host '  new fleet: bootstrap built locally' }
    elseif ($BootstrapFile) { $boot = Get-Content -LiteralPath $BootstrapFile -Raw | ConvertFrom-Json; Write-Host "  bootstrap from file $BootstrapFile" }
    else {
        $burl = Get-BootstrapUrl -Hub $Hub -Code $Code -Token $Token
        $boot = Invoke-RestMethod -Uri $burl -TimeoutSec 30 -UseBasicParsing
        if (-not (Test-Placeholder $Code)) { Write-Host '  enrollment code redeemed' }
    }
    foreach ($k in 'agent_token', 'cluster_token', 'bundle_version', 'bundle_sha256') { if (-not $boot.$k) { throw "bootstrap response is missing $k" } }
    $ver = [string]$boot.bundle_version
    Write-Host "  bundle $ver, $(@($boot.peers).Count) peer(s): $((@($boot.peers) | ForEach-Object { $_.id }) -join ', ')"

    foreach ($d in $ForgeHome, $data, $rels) { if (-not (Test-Path $d)) { Invoke-Step "create $d" { New-Item -ItemType Directory -Force $d | Out-Null } | Out-Null } }

    $relDir = Join-Path $rels $ver
    if (Test-Path (Join-Path $relDir 'VERSION')) { Write-Host "  release $ver already present" }
    else {
        $zip = $BundleFile
        if (-not $zip) {
            $zip = Join-Path $env:TEMP "forge-$ver.zip"
            Invoke-Step "download $Hub/cluster/bundle -> $zip" {
                Invoke-WebRequest -Uri "$Hub/cluster/bundle" -Headers @{ 'X-Cluster-Token' = [string]$boot.cluster_token } -OutFile $zip -UseBasicParsing -TimeoutSec 300
            } | Out-Null
        }
        if (Test-Path -LiteralPath $zip) {
            $sha = Get-FileSha256 $zip
            if ($sha -ne ([string]$boot.bundle_sha256).ToLowerInvariant()) { throw "bundle sha256 mismatch: got $sha, expected $($boot.bundle_sha256)" }
            $zv = Get-ZipVersion $zip
            if ($zv -ne $ver) { throw "bundle VERSION '$zv' != bootstrap bundle_version '$ver'" }
            Write-Host "  bundle sha256 verified ($sha), VERSION $zv"
        } elseif (-not $DryRun) { throw "bundle not downloaded" }
        Invoke-Step "extract bundle -> $relDir" {
            $stage = "$relDir._staging"
            if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            [IO.Compression.ZipFile]::ExtractToDirectory($zip, $stage)
            [IO.Directory]::Move($stage, $relDir)
        } | Out-Null
    }
    $curVer = if (Test-Path (Join-Path $cur 'VERSION')) { (Get-Content (Join-Path $cur 'VERSION') -Raw).Trim() } else { $null }
    if ($curVer -and (Compare-SemVer $curVer $ver) -ge 0 -and (Test-Path (Join-Path $rels $curVer))) {
        Write-Host "  current is $curVer (>= $ver); keeping it"
        $relDir = Join-Path $rels $curVer
    } else {
        Invoke-Step "junction $cur -> $relDir" {
            if (Test-Path $cur) {
                if (((Get-Item $cur -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { & cmd.exe /d /c "rmdir `"$cur`"" | Out-Null }
                else { Move-Item $cur "$cur.bak-$(Get-Date -Format yyyyMMddHHmmss)" }
            }
            & cmd.exe /d /c "mklink /J `"$cur`" `"$relDir`"" | Out-Null
            if (-not (Test-Path (Join-Path $cur 'VERSION'))) { throw 'mklink /J failed' }
        } | Out-Null
    }

    # ---- venv ----
    Write-Step 'Python venv'
    if (-not (Test-Path $venvPy)) { Invoke-Step "$($py.exe) -m venv $ForgeHome\venv" { & $py.exe -m venv (Join-Path $ForgeHome 'venv'); if ($LASTEXITCODE) { throw "venv failed ($LASTEXITCODE)" } } | Out-Null }
    $req = Join-Path $cur 'requirements.txt'
    Invoke-Step "pip install -r $req" {
        & $venvPy -m pip install --disable-pip-version-check -q -r $req | Out-Host
        if ($LASTEXITCODE) { throw "pip install failed ($LASTEXITCODE)" }
        [IO.File]::WriteAllText((Join-Path $data 'requirements.sha256'), (Get-FileSha256 $req), (New-Object Text.UTF8Encoding($false)))
    } | Out-Null

    # ---- config ----
    Write-Step 'Configuration'
    $cfgPath = Join-Path $data 'config.json'
    $shared = New-SharedConfig -Bootstrap $boot -NodeId $nodeId -Existing (Read-JsonFile $cfgPath) -Port $Port
    Invoke-Step "write $cfgPath (node_id=$nodeId, tokens + $(@($shared.seed_peers).Count) seed_peers from bootstrap)" { Write-JsonFile $cfgPath $shared } | Out-Null
    # S2 secrets (data\private\secrets.json, Administrators + SYSTEM only): S's Write-ForgeSecrets. When it wrote the
    # cluster token there, config.json keeps only node_id/agent_token/port/seed_peers (else the 2.3 hub migrates it).
    $sLoaded = $false
    foreach ($dir in @($(if ($repoRoot) { Join-Path $repoRoot 'agent' }), (Join-Path $relDir 'agent'))) { if ($dir -and -not $sLoaded) { $sLoaded = . $loadS $dir } }
    $sec = Get-BootstrapSecrets -Bootstrap $boot -PinHash $pinHash -SigningPubKey $pubKey
    # data\private must be Administrators + SYSTEM only BEFORE a secret or the first-run claim lands in it
    $privDir = Join-Path $data 'private'
    $sDataAcl = $null
    if ($sLoaded -and (Get-Command Set-ForgeDataAcl -ErrorAction SilentlyContinue)) { try { $sDataAcl = @(Set-ForgeDataAcl -ForgeHome $ForgeHome -User $user -DryRun | Where-Object { $_ -and $_.path }) } catch { $sDataAcl = $null } }
    if ($sec.Count -or $NewFleet) {
        $secured = Invoke-Step "secure $privDir (Administrators + SYSTEM only) before writing secrets" {
            if (-not (Test-Path -LiteralPath $privDir)) { New-Item -ItemType Directory -Force $privDir | Out-Null }
            if ($sDataAcl -and $sDataAcl.Count) { $null = Set-ForgeDataAcl -ForgeHome $ForgeHome -User $user }
            else { & icacls.exe @(Get-ForgePrivateAclArgs $privDir) | Out-Null; if ($LASTEXITCODE) { throw "icacls exited $LASTEXITCODE" } }
        }
        if (-not $secured) { throw "cannot restrict $privDir to Administrators + SYSTEM; no secrets written" }
    }
    if ($sLoaded -and $sec.Count) {
        $secPath = Join-Path (Join-Path $data 'private') 'secrets.json'
        Invoke-Step "write $secPath ($(@($sec.Keys | Sort-Object) -join ', '))" {
            $secPath = Write-ForgeSecrets -DataDir $data -Secrets $sec
            $sj = Read-JsonFile $secPath
            if ($sj -and $sj.cluster_token) {
                foreach ($k in 'cluster_token', 'ui_token') { if ($shared.Contains($k)) { $shared.Remove($k) } }
                Write-JsonFile $cfgPath $shared
            }
        } | Out-Null
    } elseif ($sec.Count) { Write-Host '  secrets.ps1 not available: secrets stay in config.json (the 2.3 hub moves them on first start)' -ForegroundColor DarkYellow }
    if ($NewFleet) {
        $claimCode = New-FirstRunClaim -DataDir $data -TtlSec 1800 -DryRun:$DryRun
        if ($DryRun) { Write-Host "  [dry-run] write $data\private\firstrun-claim.json (single-use first-run code, 30 min)" -ForegroundColor Yellow }
        else { Write-Host "  - first-run code written ($data\private\firstrun-claim.json, 30 min)" }
    }
    $hubUrls = Get-AgentHubUrls -Peers @($boot.peers) -Hub $Hub
    $agentCfgPath = Join-Path $data 'agent-config.json'
    $v1Cfg = Read-JsonFile (Join-Path $v1Dir 'agent-config.json')
    $agentCfg = New-AgentConfig -Existing (Read-JsonFile $agentCfgPath) -V1 $v1Cfg -NodeId $nodeId -HubUrls $hubUrls -User $user
    Invoke-Step "write $agentCfgPath (hub_urls: $($agentCfg.hub_urls -join ', '); $(@($agentCfg.logs).Count) extra log(s)$(if ($v1Cfg) { '; migrated from v1' }))" { Write-JsonFile $agentCfgPath $agentCfg } | Out-Null

    Write-Step 'Permissions'
    # Code (releases, current, venv) runs as SYSTEM (watchdog, ForgeSystemAgent), so the user account must not be able
    # to change it: Administrators + SYSTEM full, the user read+execute only. data\ is the user lane's working dir.
    # 2.3: data\ + data\private\ ACLs come from S's Set-ForgeDataAcl when it has a plan (user Modify on data\ is gone);
    # a 2.2-style plan (user Modify on data\) is the fallback.
    # (plans are returned as `, @(...)`: assign, then unroll through the pipeline; never wrap the call in @())
    $sPlan = $null
    if ($sLoaded -and (Get-Command Set-ForgeDataAcl -ErrorAction SilentlyContinue)) { try { $sPlan = Set-ForgeDataAcl -ForgeHome $ForgeHome -User $user -DryRun } catch { $sPlan = $null } }
    $sPlan = @($sPlan | Where-Object { $_ -and $_.path })
    $plan = Get-ForgeAclPlan -ForgeHome $ForgeHome -User $user
    $plan = @($plan | Where-Object { $_ })
    if ($sPlan.Count) { $plan = @($plan[0], $plan[1], $plan[3]) }
    foreach ($a in $plan) {
        Invoke-Step $a.what { & icacls.exe @($a.args) | Out-Null; if ($LASTEXITCODE) { throw "icacls exited $LASTEXITCODE" } } | Out-Null
    }
    if ($sPlan.Count) {
        Invoke-Step "Set-ForgeDataAcl: $(@($sPlan | ForEach-Object { "$($_.path) [$(@($_.grant) -join ', ')]" }) -join '; ')" { $null = Set-ForgeDataAcl -ForgeHome $ForgeHome -User $user } | Out-Null
    } elseif ($DryRun -or (Test-Path -LiteralPath $privDir)) {
        Invoke-Step "icacls $privDir : Administrators + SYSTEM only again (the reset above made it inherit data\)" {
            & icacls.exe @(Get-ForgePrivateAclArgs $privDir) | Out-Null; if ($LASTEXITCODE) { throw "icacls exited $LASTEXITCODE" }
        } | Out-Null
    }
    Invoke-Step 'w32tm /resync (job/lease clocks; agents report agent_time)' {
        $ErrorActionPreference = 'Continue'
        Start-Service -Name w32time -ErrorAction SilentlyContinue
        $o = & w32tm.exe /resync 2>&1 | Out-String
        if ($LASTEXITCODE) { Write-Host "    w32tm /resync: $($o.Trim()) (not fatal)" -ForegroundColor DarkYellow }
    } | Out-Null

    # ---- 3. firewall + power ----
    Write-Step 'Firewall + power'
    Invoke-Step 'firewall rule "Forge Control hub (8787)" inbound TCP 8787 from LocalSubnet, 100.64.0.0/10' {
        Remove-NetFirewallRule -DisplayName 'Forge Control hub (8787)' -ErrorAction SilentlyContinue
        New-NetFirewallRule -DisplayName 'Forge Control hub (8787)' -Direction Inbound -Protocol TCP -LocalPort $Port `
            -RemoteAddress LocalSubnet, 100.64.0.0/10 -Action Allow -Profile Any | Out-Null
    } | Out-Null
    Invoke-Step 'powercfg: no standby/hibernate on AC' {
        powercfg /change standby-timeout-ac 0 | Out-Null; powercfg /change hibernate-timeout-ac 0 | Out-Null
    } | Out-Null

    # ---- 4. tailscale (never blocks) ----
    Write-Step 'Tailscale'
    $ts = $null; $tsDns = $null; $tsIp = $null
    try {
        $ts = Find-Tailscale
        if (-not $ts) {
            Invoke-Step 'winget install Tailscale.Tailscale (machine scope)' {
                & winget install --id Tailscale.Tailscale -e --scope machine --accept-source-agreements --accept-package-agreements --silent | Out-Host
            } | Out-Null
            $ts = Find-Tailscale
        }
        $st = Get-TailscaleStatus $ts
        if ($ts -and (-not $st -or $st.BackendState -ne 'Running')) {
            Invoke-Step 'tailscale up --unattended (background; prints the sign-in URL)' {
                $o = Join-Path $env:TEMP 'forge-tailscale-up.txt'
                Start-Process -FilePath $ts -ArgumentList 'up', '--unattended' -WindowStyle Hidden -RedirectStandardError $o | Out-Null
                Start-Sleep -Seconds 8
                $m = [regex]::Match("$(Get-Content $o -Raw -ErrorAction SilentlyContinue)", 'https://login\.tailscale\.com/\S+')
                if ($m.Success) { Write-Host "  Sign in to Tailscale: $($m.Value)" -ForegroundColor Green }
                else { Write-Host '  Tailscale is starting; if it needs a login run: tailscale up --unattended' }
            } | Out-Null
            $st = Get-TailscaleStatus $ts
        }
        if ($st -and $st.Self) {
            $tsDns = [string]$st.Self.DNSName
            $tsIp = @($st.Self.TailscaleIPs | Where-Object { $_ -match '^\d+\.\d+\.\d+\.\d+$' }) | Select-Object -First 1
        }
        Write-Host "  tailscale: $(if ($ts) { $ts } else { 'not installed' }) dns=$tsDns ip=$tsIp"
    } catch { Write-Host "  tailscale step skipped: $($_.Exception.Message)" -ForegroundColor DarkYellow }
    $lan = Get-LanIp
    $myUrls = Get-JoinUrls -DnsName $tsDns -TailscaleIp $tsIp -LanIp $lan -Port $Port
    Write-Host "  this node's urls: $($myUrls -join ', ')"

    # ---- 6a. retire v1 ----
    Write-Step 'Migrate from v1'
    foreach ($t in 'ForgeControl-Hub', 'ForgeControl-Agent') {
        if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
            Invoke-Step "remove v1 task $t" { Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName $t -Confirm:$false } | Out-Null
        }
    }
    $v1Procs = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -match 'forge-agent\\forge-agent\.ps1' })
    if ($v1Procs.Count) { Invoke-Step "stop $($v1Procs.Count) v1 agent process(es)" { $v1Procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } } | Out-Null }
    if (Test-Path (Join-Path $v1Dir 'agent-config.json')) {
        Invoke-Step "retire $v1Dir\agent-config.json (jobs history kept)" { Move-Item (Join-Path $v1Dir 'agent-config.json') (Join-Path $v1Dir 'agent-config.json.v1-migrated') -Force } | Out-Null
    }

    # ---- 6b. tasks ----
    Write-Step 'Scheduled tasks'
    foreach ($t in 'ForgeWatchdog', 'ForgeSystemAgent', 'ForgeAgent', 'ForgeHub') {
        if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) { Invoke-Step "stop $t" { Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue } | Out-Null }
    }
    $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | ForEach-Object { $_.OwningProcess } | Sort-Object -Unique | Where-Object { $_ -gt 4 })
    if ($listeners.Count) { Invoke-Step "stop process(es) listening on $Port (pid $($listeners -join ', '))" { $listeners | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue } } | Out-Null }
    $ps = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File"
    # 2.3 R2/R7: frozen watchdog launcher in <FH>\bin + task specs from the release's watchdog (R). Missing (older
    # bundle) -> the 2.2 registrations below.
    $wdScript = @((Join-Path $cur 'watchdog\watchdog.ps1'), $(if ($repoRoot) { Join-Path $repoRoot 'watchdog\watchdog.ps1' })) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
    $specs = @(); $hasLauncher = $false
    if ($wdScript) {
        try {
            $probe = Invoke-WithWatchdog $wdScript { param($fh)
                $l = [bool](Get-Command Install-WdLauncher -ErrorAction SilentlyContinue)
                $sp = $null; if (Get-Command Get-ForgeTaskSpecs -ErrorAction SilentlyContinue) { $sp = Get-ForgeTaskSpecs -ForgeHome $fh }
                @{ launcher = $l; specs = @($sp | Where-Object { $_ }) } } @($ForgeHome)
            $hasLauncher = [bool]$probe.launcher; $specs = @($probe.specs | Where-Object { $_ -and $_.name })
        } catch { Write-Host "  watchdog helpers unavailable: $($_.Exception.Message)" -ForegroundColor DarkYellow }
    }
    if ($hasLauncher) {
        Invoke-Step "install the watchdog launcher -> $ForgeHome\bin (Install-WdLauncher)" {
            Invoke-WithWatchdog $wdScript { param($fh) Install-WdLauncher -ForgeHome $fh | Out-Null } @($ForgeHome) } | Out-Null
    }
    if ($specs.Count) {
        foreach ($sp in $specs) {
            Invoke-Step "register $($sp.name) ($($sp.user)$(if ($sp.restart) { ', restart on failure' })): $($sp.command) $($sp.arguments)" {
                Register-ForgeTaskSpec -Spec $sp -User $user -Dir $data } | Out-Null
        }
    } else {
    Invoke-Step "register ForgeHub (S4U $user, at startup): cmd /c $cur\watchdog\run-hub.cmd" {
        Register-ForgeTask -Name 'ForgeHub' -Exe 'cmd.exe' -Arguments "/d /c `"$cur\watchdog\run-hub.cmd`"" -Dir $cur -User $user
    } | Out-Null
    Invoke-Step "register ForgeAgent (S4U $user, at startup): $cur\agent\forge-agent.ps1" {
        Register-ForgeTask -Name 'ForgeAgent' -Exe 'powershell.exe' -Arguments "$ps `"$cur\agent\forge-agent.ps1`" -ConfigPath `"$agentCfgPath`"" -Dir $data -User $user
    } | Out-Null
    Invoke-Step "register ForgeSystemAgent (SYSTEM, at startup, restart on failure): $cur\agent\forge-agent.ps1 -Lane system" {
        Register-ForgeTask -Name 'ForgeSystemAgent' -Exe 'powershell.exe' -Arguments "$ps `"$cur\agent\forge-agent.ps1`" -ConfigPath `"$agentCfgPath`" -Lane system" -Dir $data -SystemService
    } | Out-Null
    Invoke-Step "register ForgeWatchdog (SYSTEM, every 1 min + at startup): $cur\watchdog\watchdog.ps1" {
        Register-ForgeTask -Name 'ForgeWatchdog' -Exe 'powershell.exe' -Arguments "$ps `"$cur\watchdog\watchdog.ps1`" -ForgeHome `"$ForgeHome`"" -Dir $data -System
    } | Out-Null
    }

    # ---- 5. join (leader answers; a standby forwards), then start the hub ----
    # config.json seed_peers (written above) is what keeps a fresh hub from electing itself leader.
    Write-Step 'Join cluster + start'
    $ct = [string]$boot.cluster_token
    $isLocalHub = $Hub -match '^https?://(127\.0\.0\.1|localhost)(:|/|$)'
    $joined = $false
    if (-not $myUrls.Count) { Write-Host '  no reachable URL for this node (no LAN IP / Tailscale); other nodes cannot reach its hub' -ForegroundColor DarkYellow }
    elseif (-not $isLocalHub) { $joined = Invoke-Step "join $nodeId at $Hub [$($myUrls -join ', ')]" { Send-Join $Hub $ct $nodeId $myUrls | Out-Null } }
    Invoke-Step 'start ForgeHub and wait for /healthz' {
        Start-ScheduledTask -TaskName 'ForgeHub'
        if (-not (Wait-Healthz 60)) { throw "local hub did not answer /healthz in 60 s; see $data\hub.log" }
    } | Out-Null
    if ($myUrls.Count -and -not $joined -and -not $DryRun) {
        # retry through the local hub, which forwards to the leader once it has synced
        $ok = $false
        for ($i = 0; $i -lt 6 -and -not $ok; $i++) {
            try { Send-Join "http://127.0.0.1:$Port" $ct $nodeId $myUrls | Out-Null; $ok = $true; Write-Host "  - joined $nodeId via local hub" }
            catch { Start-Sleep -Seconds 5 }
        }
        if (-not $ok) { Write-Host '  join failed (no leader reachable yet); re-run the installer later or join from the UI' -ForegroundColor DarkYellow }
    } elseif ($DryRun -and $isLocalHub) { Write-Host "  [dry-run] join $nodeId via local hub" -ForegroundColor Yellow }
    Invoke-Step 'start ForgeAgent + ForgeSystemAgent + ForgeWatchdog' { Start-ScheduledTask -TaskName 'ForgeAgent'; Start-ScheduledTask -TaskName 'ForgeSystemAgent'; Start-ScheduledTask -TaskName 'ForgeWatchdog' } | Out-Null

    $lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Forge Control.url'
    Invoke-Step "desktop shortcut $lnk -> http://localhost:$Port" {
        [IO.File]::WriteAllText($lnk, "[InternetShortcut]`r`nURL=http://localhost:$Port/`r`n", (New-Object Text.UTF8Encoding($false)))
    } | Out-Null

    if ($NewFleet) {
        $claimUrl = "http://localhost:$Port/?claim=$claimCode"
        if ($DryRun) { Write-Host "  [dry-run] open $claimUrl" -ForegroundColor Yellow }
        elseif (-not $NoBrowser) { Invoke-Step "open $claimUrl (signs this browser in)" { Start-Process $claimUrl } | Out-Null }
        Write-Host ''
        Write-Host "New fleet ready. Sign in on this machine: $claimUrl" -ForegroundColor Green
        Write-Host "  (single use, 30 minutes). Add more machines from Machines > Add machine." -ForegroundColor Green
    }
    Write-Host ''
    Write-Host "Done. Open http://localhost:$Port on this machine. '$nodeId' should appear in the fleet within ~30 s." -ForegroundColor Green
    Write-Host "Logs: $data\hub.log, agent.log, watchdog.log.  For Claude Code / Codex jobs sign in once: 'claude' and 'codex login'."
}

Install-Forge
