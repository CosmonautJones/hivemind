# Hivemind 2.3 - first run + upkeep, agent side (workstream F: F2, F3, F5, F6b). CONTRACT-phase3.md section 6.3.
# Dot-sourced by agent\forge-agent.ps1. Functions only, no side effects at dot-source time. PowerShell 5.1 compatible.
#   F5  metrics.macs (Wake-on-LAN targets) + the report response `wol: [{mac, broadcast, port}]` -> magic packets
#   F6b metrics.ollama_models (installed model names) + metrics.ollama_models_dir
#   F2/F3 user-lane `action` jobs (tool updates, git_sync, ollama_pull): Get-UserActionCommand builds the job command

$script:UpkeepCache = @{ macs = $null; macs_at = [datetime]::MinValue; models = $null; models_at = [datetime]::MinValue }

# ======================= pure helpers (unit-tested) =======================

# 'aa:bb:cc:dd:ee:ff' / 'AABBCCDDEEFF' / 'AA-BB-...' -> 'AA-BB-CC-DD-EE-FF'; $null when not a usable unicast MAC.
function ConvertTo-NetMac([string]$Mac) {
    $h = ("$Mac" -replace '[^0-9A-Fa-f]', '').ToUpperInvariant()
    if ($h.Length -ne 12 -or $h -eq '000000000000' -or $h -eq 'FFFFFFFFFFFF') { return $null }
    (0..5 | ForEach-Object { $h.Substring($_ * 2, 2) }) -join '-'
}

# 6 x 0xFF + 16 x MAC = 102 bytes.
function New-MagicPacket([string]$Mac) {
    $m = ConvertTo-NetMac $Mac
    if (-not $m) { throw "bad MAC '$Mac'" }
    $b = [byte[]]($m.Split('-') | ForEach-Object { [Convert]::ToByte($_, 16) })
    $p = New-Object byte[] 102
    for ($i = 0; $i -lt 6; $i++) { $p[$i] = 0xFF }
    for ($r = 0; $r -lt 16; $r++) { [Array]::Copy($b, 0, $p, 6 + $r * 6, 6) }
    , $p
}

# Adapter rows (Name, MacAddress, Status, IPs = @(@{ip; prefix})) -> report entries for up adapters with an IPv4.
function Select-WolInterfaces([object[]]$Adapters = @()) {
    $out = New-Object System.Collections.Generic.List[object]
    foreach ($a in @($Adapters | Where-Object { $_ })) {
        if ("$($a.Status)" -ne 'Up') { continue }
        $mac = ConvertTo-NetMac ([string]$a.MacAddress)
        if (-not $mac) { continue }
        foreach ($ip in @($a.IPs)) {
            if (-not $ip -or "$($ip.ip)" -notmatch '^\d+\.\d+\.\d+\.\d+$' -or "$($ip.ip)" -like '169.254.*' -or "$($ip.ip)" -like '127.*') { continue }
            $out.Add([ordered]@{ iface = [string]$a.Name; mac = $mac; ip = [string]$ip.ip; prefix = [int]$ip.prefix })
        }
    }
    , $out.ToArray()
}

# Valid wol items from a report response (untrusted): MAC, IPv4 broadcast, port 1-65535 (default 9). At most 8.
function Get-WolRequests($Resp) {
    $out = New-Object System.Collections.Generic.List[object]
    if (-not $Resp -or -not $Resp.PSObject.Properties['wol']) { return , $out.ToArray() }
    foreach ($w in @($Resp.wol)) {
        if (-not $w -or $out.Count -ge 8) { continue }
        $mac = ConvertTo-NetMac ([string]$w.mac)
        $bc = [string]$w.broadcast; if (-not $bc) { $bc = '255.255.255.255' }
        $ipObj = $null
        if (-not $mac -or -not [Net.IPAddress]::TryParse($bc, [ref]$ipObj) -or $ipObj.AddressFamily -ne 'InterNetwork') { continue }
        $port = 9; if ($w.port) { $port = [int]$w.port }
        if ($port -lt 1 -or $port -gt 65535) { continue }
        $out.Add(@{ mac = $mac; broadcast = $bc; port = $port })
    }
    , $out.ToArray()
}

# ======================= runtime =======================

# [ @{ iface; mac = 'AA-BB-CC-DD-EE-FF'; ip; prefix = [int] } ] for up, physical, IPv4 adapters (WoL targets).
function Get-NetworkMacs {
    $rows = New-Object System.Collections.Generic.List[object]
    try {
        foreach ($a in @(Get-NetAdapter -Physical -ErrorAction Stop)) {
            $ips = @(Get-NetIPAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                ForEach-Object { @{ ip = [string]$_.IPAddress; prefix = [int]$_.PrefixLength } })
            $rows.Add([pscustomobject]@{ Name = $a.Name; MacAddress = $a.MacAddress; Status = "$($a.Status)"; IPs = $ips })
        }
    } catch { return , @() }
    Select-WolInterfaces $rows.ToArray()
}

# Installed ollama model names via the local API (2 s timeout); $null when ollama is not answering (= unknown).
function Get-OllamaModelNames([string]$BaseUrl = 'http://127.0.0.1:11434') {
    try {
        $r = Invoke-RestMethod -Uri "$($BaseUrl.TrimEnd('/'))/api/tags" -TimeoutSec 2 -UseBasicParsing
        , @(@($r.models) | Where-Object { $_ -and $_.name } | ForEach-Object { [string]$_.name })
    } catch { $null }
}

# Adds metrics.macs (F5), metrics.ollama_models and metrics.ollama_models_dir (F6b) to a user-lane report body.
# Cached (adapters 10 min, models 10 min) so the 5 s report loop stays cheap. Never throws.
function Add-UpkeepReport([hashtable]$Body) {
    try {
        if (-not $Body) { return }
        if ($null -eq $Body.metrics) { $Body.metrics = @{} }
        $m = $Body.metrics
        $c = $script:UpkeepCache
        $now = Get-Date
        $wolOn = $true
        if ($cfg -and $cfg.PSObject.Properties['wol_enabled'] -and $cfg.wol_enabled -eq $false) { $wolOn = $false }
        if ($wolOn) {
            if ($null -eq $c.macs -or ($now - $c.macs_at).TotalMinutes -ge 10) { $c.macs = @(Get-NetworkMacs); $c.macs_at = $now }
            if (@($c.macs).Count) { $m['macs'] = @($c.macs) }
        }
        if ($Tools -and $Tools['ollama']) {
            if (($now - $c.models_at).TotalMinutes -ge 10 -or $null -eq $c.models) {
                $url = if ($cfg -and $cfg.ollama_url) { [string]$cfg.ollama_url } else { 'http://127.0.0.1:11434' }
                $names = Get-OllamaModelNames $url
                if ($null -ne $names) { $c.models = @($names) }
                $c.models_at = $now
            }
            if ($null -ne $c.models) { $m['ollama_models'] = @($c.models) }
            if (Get-Command Get-OllamaModelsDir -ErrorAction SilentlyContinue) {
                $d = Get-OllamaModelsDir -Config $cfg -WorkRoot $script:WorkRoot
                if ($d) { $m['ollama_models_dir'] = $d }
            }
        }
    } catch { }
}

# Handles the F fields of a report response: `wol: [{mac, broadcast, port}]` -> Send-WakeOnLan for each. Never throws.
function Invoke-UpkeepResponse($Resp) {
    try {
        foreach ($w in (Get-WolRequests $Resp)) {
            $ok = Send-WakeOnLan -Mac $w.mac -Broadcast $w.broadcast -Port $w.port
            if (Get-Command Write-AgentLog -ErrorAction SilentlyContinue) { Write-AgentLog "wake-on-LAN $($w.mac) -> $($w.broadcast):$($w.port) $(if ($ok) { 'sent' } else { 'FAILED' })" }
        }
    } catch { }
}

# Sends one magic packet (6 x 0xFF + 16 x MAC) over UDP to $Broadcast:$Port. Returns $true when sent.
function Send-WakeOnLan([string]$Mac, [string]$Broadcast = '255.255.255.255', [int]$Port = 9) {
    $udp = $null
    try {
        $pkt = New-MagicPacket $Mac
        $udp = New-Object Net.Sockets.UdpClient
        $udp.EnableBroadcast = $true
        $ep = New-Object Net.IPEndPoint ([Net.IPAddress]::Parse($Broadcast)), $Port
        [void]$udp.Send($pkt, $pkt.Length, $ep)
        $true
    } catch { $false } finally { if ($udp) { $udp.Close() } }
}

# F2/F3: command for a user-lane `action` job (G-F1). Validates against the bundle catalog (the action must be
# lane "user"), writes <job dir>\spec.json and returns @{ inner = <command run under cmd /s /c> } or @{ error }.
function Get-UserActionCommand {
    param($Job, $Catalog, [string]$JobDir, [string]$AgentDir, [string]$CatalogPath, [hashtable]$Tools = @{}, $Config, [string]$User)
    if (-not $Catalog) { return @{ error = "action catalog missing ($CatalogPath)" } }
    $name = [string]$Job.action
    $spec = Get-ForgeActionSpec $Catalog $name
    if (-not $spec) { return @{ error = "action rejected by agent: unknown action '$name'" } }
    if ("$($spec.lane)" -ne 'user') { return @{ error = "action rejected by agent: '$name' runs in the system lane" } }
    try {
        $norm = Test-ForgeActionArgs -Catalog $Catalog -Name $name -ActionArgs $Job.args
        $null = Get-ForgeActionSteps -Name $name -ActionArgs $norm -User $User
    } catch { return @{ error = "action rejected by agent: $($_.Exception.Message)" } }
    $tp = [ordered]@{}
    foreach ($k in 'claude', 'codex', 't3', 'git', 'ollama') { if ($Tools[$k]) { $tp[$k] = [string]$Tools[$k] } }
    $dry = [bool]($Config -and $Config.PSObject.Properties['action_dry_run'] -and $Config.action_dry_run)
    $specPath = Join-Path $JobDir 'spec.json'
    $obj = [ordered]@{ action = $name; args = $norm; dry_run = $dry; user = $User; tools = $tp }
    [IO.File]::WriteAllText($specPath, (ConvertTo-Json -InputObject $obj -Depth 6), (New-Object Text.UTF8Encoding($false)))
    @{ inner = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $AgentDir 'actions.ps1')`" -ActionRunSpec `"$specPath`" -ActionCatalogPath `"$CatalogPath`""
       spec = $specPath }
}
