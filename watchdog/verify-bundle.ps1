# Hivemind 2.3 - signed bundle verification + secrets for the watchdog (workstream S: S2, S6). CONTRACT-phase3.md 6.4.
# Dot-sourced by watchdog\watchdog.ps1 (and tools\build-bundle.ps1 for the manifest). Functions only.
# PowerShell 5.1 / .NET 4.x: RSACryptoServiceProvider.FromXmlString + VerifyData(bytes, 'SHA256', sig).
#
# bundle.sig (zip root, JSON): {"v": 1, "version", "manifest_sha256", "sig": <base64 RSA-3072 PKCS#1 v1.5 SHA-256
# signature of the manifest bytes>, "key_id": <first 16 hex of sha256(pubkey xml)>}. The signature covers a manifest of
# file hashes, not the zip bytes, so a hub's deterministic re-zip of a signed release still verifies.
# Python twin: hub\forge_security.py (bundle_manifest, verify_release); both must produce identical manifests.

# The pinned public key (RSAKeyValue XML string) from data\private\secrets.json `signing_pubkey`, or $null.
function Get-SigningPubKey([string]$DataDir) {
    $p = Join-Path (Join-Path $DataDir 'private') 'secrets.json'
    if (-not (Test-Path -LiteralPath $p)) { return $null }
    try { $k = ([IO.File]::ReadAllText($p) | ConvertFrom-Json).signing_pubkey } catch { return $null }
    if ($k) { [string]$k } else { $null }
}

# Relative path ('/' separators) is part of the manifest? Skips root bundle.sig/zip/sha256, venv/, __pycache__/, *.pyc.
function Test-ManifestPath([string]$Rel) {
    if ($Rel -ceq 'bundle.sig' -or $Rel -ceq 'bundle.zip' -or $Rel -ceq 'bundle.sha256') { return $false }
    $parts = $Rel.Split('/')
    if ($parts[0] -ceq 'venv') { return $false }
    if ($parts -contains '__pycache__' -or $Rel.EndsWith('.pyc')) { return $false }
    $true
}

# Never-signed code (S-R4): Python loads an unchecked __pycache__\*.pyc (or a sourceless x.pyc) without looking at the
# signed .py, and the manifest skips those paths. So a release that is verified before activation must not contain any:
# no __pycache__ dir and no *.pyc / *.pyo anywhere in the extracted dir (a top-level venv\ is skipped there, because R5
# creates the release venv after verification), and no venv/, __pycache__/ or *.pyc / *.pyo entry in its bundle.zip.
# Returns the first offending relative path ('bundle.zip:<entry>' for the zip), or $null.
function Test-NeverSignedPath([string]$Rel) {
    $l = $Rel.Replace('\', '/').ToLowerInvariant()
    ($l.Split('/') -contains '__pycache__') -or $l.EndsWith('.pyc') -or $l.EndsWith('.pyo')
}

function Find-UnsignedCode([string]$ReleaseDir) {
    $rootFull = [IO.Path]::GetFullPath($ReleaseDir).TrimEnd('\')
    foreach ($top in @(Get-ChildItem -LiteralPath $rootFull -Force -ErrorAction SilentlyContinue)) {
        if ($top.PSIsContainer -and $top.Name -ieq 'venv') { continue }
        $items = @($top)
        if ($top.PSIsContainer) { $items += @(Get-ChildItem -LiteralPath $top.FullName -Recurse -Force -ErrorAction SilentlyContinue) }
        foreach ($i in $items) {
            $rel = $i.FullName.Substring($rootFull.Length + 1).Replace('\', '/')
            if (Test-NeverSignedPath $rel) { return $rel }
        }
    }
    $zip = Join-Path $rootFull 'bundle.zip'
    if (Test-Path -LiteralPath $zip -PathType Leaf) {
        $z = $null
        try { Add-Type -AssemblyName System.IO.Compression.FileSystem; $z = [IO.Compression.ZipFile]::OpenRead($zip) } catch { $z = $null }
        if ($z) {
            try {
                foreach ($e in $z.Entries) {
                    $n = $e.FullName.Replace('\', '/')
                    if ($n.Split('/')[0] -ieq 'venv' -or (Test-NeverSignedPath $n)) { return "bundle.zip:$n" }
                }
            } finally { $z.Dispose() }
        }
    }
    $null
}

function Get-ManifestFileSha256([string]$Path) {
    $sha = [Security.Cryptography.SHA256]::Create()
    $fs = [IO.File]::OpenRead($Path)
    try { ([BitConverter]::ToString($sha.ComputeHash($fs)) -replace '-', '').ToLowerInvariant() } finally { $fs.Close(); $sha.Dispose() }
}

# Manifest of $Files (relative '/' paths under $Root): "<sha256> <path>`n" per included file, ordinal-sorted.
function Get-BundleManifestFromFiles([string]$Root, [string[]]$Files) {
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $list = New-Object System.Collections.Generic.List[string]
    foreach ($f in $Files) { if ($f -and (Test-ManifestPath $f)) { $list.Add($f) } }
    $arr = $list.ToArray()
    [Array]::Sort($arr, [StringComparer]::Ordinal)
    $sb = New-Object Text.StringBuilder
    foreach ($rel in $arr) { [void]$sb.Append((Get-ManifestFileSha256 (Join-Path $rootFull $rel.Replace('/', '\')))).Append(' ').Append($rel).Append("`n") }
    $sb.ToString()
}

# Canonical manifest of an extracted release dir (UTF-8 without BOM when turned into bytes).
function Get-BundleManifest([string]$ReleaseDir) {
    $rootFull = [IO.Path]::GetFullPath($ReleaseDir).TrimEnd('\')
    $rels = @(Get-ChildItem -LiteralPath $rootFull -Recurse -File -Force | ForEach-Object { $_.FullName.Substring($rootFull.Length + 1).Replace('\', '/') })
    Get-BundleManifestFromFiles $rootFull $rels
}

function Get-Sha256Hex([byte[]]$Bytes) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { ([BitConverter]::ToString($sha.ComputeHash($Bytes)) -replace '-', '').ToLowerInvariant() } finally { $sha.Dispose() }
}

# key_id = first 16 hex of sha256(canonical RSAKeyValue XML = ToXmlString($false)); accepts private-key XML too.
function Get-ForgeKeyId([string]$PubKeyXml) {
    $rsa = New-Object Security.Cryptography.RSACryptoServiceProvider
    try { $rsa.PersistKeyInCsp = $false; $rsa.FromXmlString($PubKeyXml); $canon = $rsa.ToXmlString($false) } finally { $rsa.Dispose() }
    (Get-Sha256Hex ([Text.Encoding]::UTF8.GetBytes($canon))).Substring(0, 16)
}

# -> @{ ok = [bool]; signed = [bool]; reason = [string] }.
# No pinned key -> ok (unsigned allowed). Key pinned -> ok only if <ReleaseDir>\bundle.sig verifies against the manifest.
function Test-BundleSignature {
    param([string]$ReleaseDir, [string]$PubKeyXml)
    $sigPath = Join-Path $ReleaseDir 'bundle.sig'
    $hasSig = Test-Path -LiteralPath $sigPath
    if (-not $PubKeyXml) { return @{ ok = $true; signed = [bool]$hasSig; reason = 'no signing key pinned' } }
    $never = Find-UnsignedCode $ReleaseDir
    if ($never) { return @{ ok = $false; signed = [bool]$hasSig; reason = "bundle contains unsigned code: $never" } }
    if (-not $hasSig) { return @{ ok = $false; signed = $false; reason = 'bundle not signed' } }
    try {
        $sig = [IO.File]::ReadAllText($sigPath) | ConvertFrom-Json
        $sigBytes = [Convert]::FromBase64String([string]$sig.sig)
        $manifest = [Text.Encoding]::UTF8.GetBytes((Get-BundleManifest $ReleaseDir))
        if ("$($sig.manifest_sha256)".ToLowerInvariant() -ne (Get-Sha256Hex $manifest)) {
            return @{ ok = $false; signed = $true; reason = 'bundle signature invalid (files differ from the signed manifest)' }
        }
        $rsa = New-Object Security.Cryptography.RSACryptoServiceProvider
        try { $rsa.PersistKeyInCsp = $false; $rsa.FromXmlString($PubKeyXml); $good = $rsa.VerifyData($manifest, 'SHA256', $sigBytes) } finally { $rsa.Dispose() }
        if (-not $good) {
            $kid = ''; try { $kid = Get-ForgeKeyId $PubKeyXml } catch {}
            $why = if ($sig.key_id -and $kid -and "$($sig.key_id)" -ne $kid) { " (signed with key $($sig.key_id), pinned key is $kid)" } else { '' }
            return @{ ok = $false; signed = $true; reason = "bundle signature invalid$why" }
        }
        @{ ok = $true; signed = $true; reason = 'signature ok' }
    } catch {
        @{ ok = $false; signed = $true; reason = "bundle signature invalid ($($_.Exception.Message))" }
    }
}

# Returns the watchdog's config object with the S2 secrets (cluster_token, signing_pubkey) merged in from
# data\private\secrets.json (re-read on every run, so a rotated cluster token is picked up). Never throws.
function Merge-WdSecrets($Cfg, [string]$DataDir) {
    if (-not $Cfg) { return $Cfg }
    $p = Join-Path (Join-Path $DataDir 'private') 'secrets.json'
    if (-not (Test-Path -LiteralPath $p)) { return $Cfg }
    try { $s = [IO.File]::ReadAllText($p) | ConvertFrom-Json } catch { return $Cfg }
    foreach ($k in 'cluster_token', 'signing_pubkey') {
        if ($s.$k) { $Cfg | Add-Member -NotePropertyName $k -NotePropertyValue ([string]$s.$k) -Force }
    }
    $Cfg
}

# S-R7: undo the 2.3 hub's S2 migration before the watchdog rolls back to a pre-2.3 release. A 2.2 hub reads ui_token
# and cluster_token from data\config.json and opens data\forge.db; without this it would generate fresh tokens (drops
# out of the fleet) and start from an empty DB. The hub must be stopped first (forge.db is open). Restores:
#   config.json ui_token      <- data\private\config.json.pre23.bak (2.3 keeps only its hash)
#   config.json cluster_token <- data\private\secrets.json (current, rotation included), else the .bak
#   data\private\forge.db(-wal,-shm) -> data\ (an existing data\forge.db is kept as forge.db.pre-rollback-<stamp>)
# secrets.json and the .bak stay, so a later 2.3 start migrates again (load_config / db_path are idempotent).
# Returns , @(what was done). No data\private -> nothing to do. -DryRun: only lists. Throws if a DB file cannot move.
function Restore-Pre23Layout([string]$DataDir, [switch]$DryRun) {
    $acts = New-Object System.Collections.Generic.List[string]
    $priv = Join-Path $DataDir 'private'
    if (-not (Test-Path -LiteralPath $priv -PathType Container)) { return , $acts.ToArray() }
    function Read-Pre23Json([string]$Path) {
        if (-not (Test-Path -LiteralPath $Path)) { return $null }
        try { [IO.File]::ReadAllText($Path) | ConvertFrom-Json } catch { $null }
    }
    $cfgPath = Join-Path $DataDir 'config.json'
    $cfgObj = Read-Pre23Json $cfgPath
    $sec = Read-Pre23Json (Join-Path $priv 'secrets.json')
    $bak = Read-Pre23Json (Join-Path $priv 'config.json.pre23.bak')
    $cfg = [ordered]@{}
    if ($cfgObj) { foreach ($pp in $cfgObj.PSObject.Properties) { $cfg[$pp.Name] = $pp.Value } }
    $changed = $false
    if (-not $cfg['ui_token'] -and $bak -and $bak.ui_token) {
        $cfg['ui_token'] = [string]$bak.ui_token; $changed = $true; $acts.Add('config.json: ui_token restored from config.json.pre23.bak')
    }
    $ct = $null
    if ($sec -and $sec.cluster_token) { $ct = [string]$sec.cluster_token } elseif ($bak -and $bak.cluster_token) { $ct = [string]$bak.cluster_token }
    if (-not $cfg['cluster_token'] -and $ct) {
        $cfg['cluster_token'] = $ct; $changed = $true; $acts.Add('config.json: cluster_token restored from data\private')
    }
    if ($changed -and -not $DryRun) {
        # in place (WriteAllText keeps the file's explicit ACL), UTF-8 without BOM
        [IO.File]::WriteAllText($cfgPath, (ConvertTo-Json -InputObject $cfg -Depth 8), (New-Object Text.UTF8Encoding($false)))
    }
    $src = Join-Path $priv 'forge.db'
    if (Test-Path -LiteralPath $src) {
        $dst = Join-Path $DataDir 'forge.db'
        $stamp = Get-Date -Format 'yyyyMMddHHmmss'
        if (Test-Path -LiteralPath $dst) {
            $acts.Add("kept the existing data\forge.db as forge.db.pre-rollback-$stamp")
            if (-not $DryRun) {
                foreach ($sfx in '', '-wal', '-shm') { if (Test-Path -LiteralPath "$dst$sfx") { Move-Item -LiteralPath "$dst$sfx" -Destination (Join-Path $DataDir "forge.db.pre-rollback-$stamp$sfx") -Force } }
            }
        }
        foreach ($sfx in '', '-wal', '-shm') {
            if (-not (Test-Path -LiteralPath "$src$sfx")) { continue }
            $acts.Add("moved data\private\forge.db$sfx -> data\forge.db$sfx")
            if ($DryRun) { continue }
            $moved = $false
            for ($i = 0; $i -lt 20 -and -not $moved; $i++) {        # the stopped hub may take a moment to let go
                try { Move-Item -LiteralPath "$src$sfx" -Destination "$dst$sfx" -Force -ErrorAction Stop; $moved = $true }
                catch { if ($i -eq 19) { throw "cannot move $src$sfx back to data\: $($_.Exception.Message)" }; Start-Sleep -Milliseconds 500 }
            }
        }
    }
    , $acts.ToArray()
}
