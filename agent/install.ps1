# Forge Control node installer v2 (SPEC-phase1 §9). Run in an ELEVATED PowerShell:
#   irm '<hub>/agent/install.ps1?t=<agent_token>' | iex
# The hub serving this file fills in $Hub and $Token below. Idempotent: re-run to upgrade/repair.
# Migrates v1 nodes (removes tasks ForgeControl-Hub / ForgeControl-Agent, retires %USERPROFILE%\forge-agent).
#
# Local / test use:
#   .\install.ps1 -Hub http://forge-01:8787 -Token <agent_token>
#   .\install.ps1 -DryRun -BootstrapFile fixture.json -BundleFile forge-2.0.0.zip   # print every action
#   . .\install.ps1 -NoRun                                                           # dot-source helpers
param(
    [string]$Hub = '__HUB__',
    [string]$Token = '__TOKEN__',
    [string]$ForgeHome = (Join-Path $env:ProgramData 'Forge'),
    [switch]$DryRun,
    [string]$BootstrapFile,
    [string]$BundleFile,
    [switch]$NoRun
)

$InstallerVersion = '2.1.0'
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
    if ((Test-Placeholder $Hub) -and -not $BootstrapFile) { Write-Host 'No hub URL: use the one-liner from the dashboard, or pass -Hub and -Token.' -ForegroundColor Red; return }
    if (Test-Placeholder $Hub) { $Hub = 'http://hub.invalid:8787' }
    $Hub = $Hub.TrimEnd('/')

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
    Write-Step 'Cluster bootstrap'
    if ($BootstrapFile) { $boot = Get-Content -LiteralPath $BootstrapFile -Raw | ConvertFrom-Json; Write-Host "  bootstrap from file $BootstrapFile" }
    else { $boot = Invoke-RestMethod -Uri "$Hub/cluster/bootstrap?t=$([uri]::EscapeDataString($Token))" -TimeoutSec 30 -UseBasicParsing }
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
    $hubUrls = Get-AgentHubUrls -Peers @($boot.peers) -Hub $Hub
    $agentCfgPath = Join-Path $data 'agent-config.json'
    $v1Cfg = Read-JsonFile (Join-Path $v1Dir 'agent-config.json')
    $agentCfg = New-AgentConfig -Existing (Read-JsonFile $agentCfgPath) -V1 $v1Cfg -NodeId $nodeId -HubUrls $hubUrls -User $user
    Invoke-Step "write $agentCfgPath (hub_urls: $($agentCfg.hub_urls -join ', '); $(@($agentCfg.logs).Count) extra log(s)$(if ($v1Cfg) { '; migrated from v1' }))" { Write-JsonFile $agentCfgPath $agentCfg } | Out-Null

    Write-Step 'Permissions'
    # Code (releases, current, venv) runs as SYSTEM (watchdog, ForgeSystemAgent), so the user account must not be able
    # to change it: Administrators + SYSTEM full, the user read+execute only. data\ is the user lane's working dir.
    foreach ($a in (Get-ForgeAclPlan -ForgeHome $ForgeHome -User $user)) {
        Invoke-Step $a.what { & icacls.exe @($a.args) | Out-Null; if ($LASTEXITCODE) { throw "icacls exited $LASTEXITCODE" } } | Out-Null
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

    Write-Host ''
    Write-Host "Done. Open http://localhost:$Port on this machine. '$nodeId' should appear in the fleet within ~30 s." -ForegroundColor Green
    Write-Host "Logs: $data\hub.log, agent.log, watchdog.log.  For Claude Code / Codex jobs sign in once: 'claude' and 'codex login'."
}

Install-Forge
