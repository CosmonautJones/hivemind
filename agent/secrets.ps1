# Hivemind 2.3 - secret split, data ACLs, token rotation, admin PIN hash (workstream S: S2, S5, S7).
# CONTRACT-phase3.md sections 4.1, 4.3, 6.2. Dot-sourced by agent\forge-agent.ps1 and agent\install.ps1 (F calls these;
# S owns them) and by the watchdog migration (R). Functions only, no side effects at dot-source time. PowerShell 5.1.
#
# Layout: data\config.json (Admins+SYSTEM full, user read) keeps node_id, agent_token, port, seed_peers and the
# non-secret hub keys. data\private\secrets.json (Admins+SYSTEM only, inheritance off) holds ui_token_hash,
# cluster_token(+_prev, rotated), signing_pubkey, admin_pin_hash. A filtered (Medium) job can read config.json only.

$ForgeAdminsSid = '*S-1-5-32-544'
$ForgeSystemSid = '*S-1-5-18'
$ForgeSecretKeys = @('ui_token_hash', 'cluster_token', 'cluster_token_prev', 'rotated', 'signing_pubkey', 'admin_pin_hash')

# <ForgeHome>\data\private : secrets.json, forge.db*, backup\ (Administrators + SYSTEM only, inheritance off).
function Get-ForgePrivateDir([string]$DataDir) { Join-Path $DataDir 'private' }

function Read-ForgeJsonHashtable([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        $o = [IO.File]::ReadAllText($Path) | ConvertFrom-Json
        $h = @{}
        if ($o) { foreach ($k in $o.PSObject.Properties) { $h[$k.Name] = $k.Value } }
        $h
    } catch { $null }
}

# Merged view of data\config.json + data\private\secrets.json as a hashtable (secrets win). Readable only with an
# admin token (hub, agents, watchdog, installer); a filtered job gets only config.json. Never throws; $null if neither.
function Read-ForgeSecrets([string]$DataDir) {
    $cfg = Read-ForgeJsonHashtable (Join-Path $DataDir 'config.json')
    $sec = $null
    try { $sec = Read-ForgeJsonHashtable (Join-Path (Get-ForgePrivateDir $DataDir) 'secrets.json') } catch { $sec = $null }
    if ($null -eq $cfg -and $null -eq $sec) { return $null }
    $h = @{}
    if ($cfg) { foreach ($k in $cfg.Keys) { $h[$k] = $cfg[$k] } }
    if ($sec) { foreach ($k in $sec.Keys) { if ($null -ne $sec[$k]) { $h[$k] = $sec[$k] } } }
    $h
}

# icacls argument list that makes a dir Admins+SYSTEM only (inheritance off; children inherit).
function Get-ForgePrivateAclArgs([string]$Path) {
    , @($Path, '/inheritance:r', '/grant:r', "${ForgeAdminsSid}:(OI)(CI)F", "${ForgeSystemSid}:(OI)(CI)F", '/C', '/Q')
}

# Writes data\private\secrets.json (UTF-8, no BOM), merged into the existing file: keys in $Secrets replace, a $null
# value removes the key. Creates data\private with its Admins+SYSTEM-only ACL first (-SkipAcl: tests on a scratch dir,
# which a non-elevated runner could otherwise not read back). Returns the path. -DryRun: writes nothing.
function Write-ForgeSecrets([string]$DataDir, [hashtable]$Secrets, [switch]$DryRun, [switch]$SkipAcl) {
    $priv = Get-ForgePrivateDir $DataDir
    $path = Join-Path $priv 'secrets.json'
    if ($DryRun) { return $path }
    if (-not (Test-Path -LiteralPath $priv)) {
        New-Item -ItemType Directory -Force $priv | Out-Null
        if (-not $SkipAcl) {
            $a = Get-ForgePrivateAclArgs $priv
            & icacls.exe @a | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "icacls failed to protect $priv (exit $LASTEXITCODE)" }
        }
    }
    $cur = Read-ForgeJsonHashtable $path
    if (-not $cur) { $cur = @{} }
    foreach ($k in @($Secrets.Keys)) { if ($null -eq $Secrets[$k]) { $cur.Remove($k) } else { $cur[$k] = $Secrets[$k] } }
    $ordered = [ordered]@{}
    foreach ($k in @($cur.Keys | Sort-Object)) { $ordered[$k] = $cur[$k] }
    $tmp = "$path.tmp"
    [IO.File]::WriteAllText($tmp, (ConvertTo-Json -InputObject $ordered -Depth 5), (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $tmp -Destination $path -Force
    $path
}

# ACL plan for the 2.3 layout (CONTRACT 4.1); returns , @( @{ path; grant = @('...'); inherit = [bool]; args = @(icacls args) } )
# and applies it unless -DryRun (missing dirs are created first; a missing config.json is skipped). Called by
# install.ps1 (F) and by the watchdog migration (R) - never by the hub. $User = 'DOMAIN\user' of the node's user.
function Set-ForgeDataAcl([string]$ForgeHome, [string]$User, [switch]$DryRun) {
    $data = Join-Path $ForgeHome 'data'
    $priv = Get-ForgePrivateDir $data
    $bin = Join-Path $ForgeHome 'bin'
    $full = @("${ForgeAdminsSid}:(OI)(CI)F", "${ForgeSystemSid}:(OI)(CI)F")
    $plan = @(
        # data\: admin-token processes write; the user (and so a filtered job) only reads
        @{ path = $data; grant = @($full + "${User}:(OI)(CI)RX"); inherit = $false },
        # data\private\: secrets.json, forge.db, backups - no user access at all
        @{ path = $priv; grant = $full; inherit = $false },
        # data\config.json: explicit, so it stays user-readable whatever data\ inherits
        @{ path = (Join-Path $data 'config.json'); grant = @("${ForgeAdminsSid}:F", "${ForgeSystemSid}:F", "${User}:R"); inherit = $false },
        # bin\: wd-launcher.ps1 / agent-loop.cmd (code SYSTEM runs) - user read + execute only
        @{ path = $bin; grant = @($full + "${User}:(OI)(CI)RX"); inherit = $false }
    )
    foreach ($e in $plan) { $e.args = @($e.path, '/inheritance:r', '/grant:r') + @($e.grant) + @('/C', '/Q') }
    if (-not $DryRun) {
        foreach ($d in @($data, $priv, $bin)) { if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Force $d | Out-Null } }
        foreach ($e in $plan) {
            if (-not (Test-Path -LiteralPath $e.path)) { continue }
            $a = $e.args
            & icacls.exe @a | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "icacls failed on $($e.path) (exit $LASTEXITCODE)" }
        }
        # files moved into private\ keep their old ACEs: re-inherit (the hub does the same after moving forge.db)
        foreach ($f in @(Get-ChildItem -LiteralPath $priv -Force -ErrorAction SilentlyContinue)) { & icacls.exe $f.FullName /reset /T /C /Q | Out-Null }
    }
    , @($plan)
}

# PBKDF2-HMAC-SHA256 hash of a 6-digit admin PIN, format: pbkdf2_sha256$200000$<salt b64>$<hash b64> (same as the hub).
function New-ForgePinHash([string]$Pin, [int]$Iterations = 200000, [byte[]]$Salt) {
    if ($Pin -notmatch '^\d{6}$') { throw 'admin PIN must be exactly 6 digits' }
    if (-not $Salt) { $Salt = New-Object byte[] 16; $rng = [Security.Cryptography.RandomNumberGenerator]::Create(); $rng.GetBytes($Salt); $rng.Dispose() }
    $kdf = New-Object Security.Cryptography.Rfc2898DeriveBytes($Pin, $Salt, $Iterations, [Security.Cryptography.HashAlgorithmName]::SHA256)
    try { $dk = $kdf.GetBytes(32) } finally { $kdf.Dispose() }
    "pbkdf2_sha256`$$Iterations`$$([Convert]::ToBase64String($Salt))`$$([Convert]::ToBase64String($dk))"
}

# $true when $Pin matches a New-ForgePinHash / hub hash.
function Test-ForgePinHash([string]$Pin, [string]$Hash) {
    $p = "$Hash".Split('$')
    if ($p.Count -ne 4 -or $p[0] -ne 'pbkdf2_sha256') { return $false }
    try {
        $want = [Convert]::FromBase64String($p[3])
        $kdf = New-Object Security.Cryptography.Rfc2898DeriveBytes($Pin, [Convert]::FromBase64String($p[2]), [int]$p[1], [Security.Cryptography.HashAlgorithmName]::SHA256)
        try { $dk = $kdf.GetBytes($want.Length) } finally { $kdf.Dispose() }
        $diff = 0; for ($i = 0; $i -lt $want.Length; $i++) { $diff = $diff -bor ($want[$i] -bxor $dk[$i]) }
        $diff -eq 0
    } catch { $false }
}

# Report response `rotate: {agent_token, grace_until}` -> returns the new agent token (string) to use from now on,
# or $null when the response carries no rotation or the agent already uses that token. The caller swaps its
# X-Agent-Token header; the local hub rewrites data\config.json.agent_token itself (it adopts the replicated row).
function Update-AgentTokenFromResponse($Resp) {
    if (-not $Resp) { return $null }
    $rot = $Resp.rotate
    if (-not $rot) { return $null }
    $t = [string]$rot.agent_token
    if (-not $t -or $t -notmatch '^[A-Za-z0-9_\-]{16,128}$') { return $null }
    $cur = $null
    try { $cur = [string](Get-Variable -Name Token -Scope Script -ValueOnly -ErrorAction Stop) } catch { $cur = $null }
    if ($t -ceq $cur) { return $null }
    $t
}
