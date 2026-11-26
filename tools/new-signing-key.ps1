# Hivemind 2.3 - create the fleet's bundle signing key (workstream S, item S6). CONTRACT-phase3.md section 6.6.
# Run on the publishing machine. Private key: RSA-3072, DPAPI (CurrentUser) protected at
# %LOCALAPPDATA%\Hivemind\signing-key.dpapi, never leaves the machine. Public key (RSAKeyValue XML) is printed and
# written to -PubKeyPath; pin it with PUT /api/security/signing-key (or install.ps1 -NewFleet does both).
#   .\tools\new-signing-key.ps1 [-Force]
#   . .\tools\new-signing-key.ps1 -NoRun     # dot-source New-ForgeSigningKey / Invoke-ForgeSign / Get-ForgeSigningPubKey
param(
    [string]$KeyPath = $(if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'Hivemind\signing-key.dpapi' }),
    [string]$PubKeyPath = $(if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'Hivemind\signing-pubkey.xml' }),
    [switch]$Force,
    [switch]$NoRun
)

$ForgeSigningEntropy = [Text.Encoding]::UTF8.GetBytes('Hivemind bundle signing key v1')
$ForgeDefaultKeyPath = $(if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'Hivemind\signing-key.dpapi' })

function Read-ForgeSigningKeyXml([string]$KeyPath) {
    if (-not $KeyPath) { $KeyPath = $ForgeDefaultKeyPath }
    if (-not $KeyPath) { throw 'no signing key path (LOCALAPPDATA is not set; pass -KeyPath)' }
    if (-not (Test-Path -LiteralPath $KeyPath)) { throw "no signing key at $KeyPath (run tools\new-signing-key.ps1)" }
    Add-Type -AssemblyName System.Security
    $plain = [Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($KeyPath), $ForgeSigningEntropy, [Security.Cryptography.DataProtectionScope]::CurrentUser)
    try { [Text.Encoding]::UTF8.GetString($plain) } finally { [Array]::Clear($plain, 0, $plain.Length) }
}

# -> public key XML string. Refuses to overwrite an existing key unless -Force.
function New-ForgeSigningKey([string]$KeyPath, [string]$PubKeyPath, [switch]$Force) {
    if (-not $KeyPath) { $KeyPath = $ForgeDefaultKeyPath }
    if (-not $KeyPath) { throw 'no signing key path (LOCALAPPDATA is not set; pass -KeyPath)' }
    if (-not $PubKeyPath) { $PubKeyPath = Join-Path (Split-Path -Parent $KeyPath) 'signing-pubkey.xml' }
    if ((Test-Path -LiteralPath $KeyPath) -and -not $Force) { throw "signing key already exists at $KeyPath (pass -Force to replace it; every hub must then re-pin the new key)" }
    Add-Type -AssemblyName System.Security
    $rsa = New-Object Security.Cryptography.RSACryptoServiceProvider(3072)
    try {
        $rsa.PersistKeyInCsp = $false
        $priv = [Text.Encoding]::UTF8.GetBytes($rsa.ToXmlString($true))
        $pub = $rsa.ToXmlString($false)
    } finally { $rsa.Dispose() }
    try { $blob = [Security.Cryptography.ProtectedData]::Protect($priv, $ForgeSigningEntropy, [Security.Cryptography.DataProtectionScope]::CurrentUser) }
    finally { [Array]::Clear($priv, 0, $priv.Length) }
    foreach ($d in @((Split-Path -Parent $KeyPath), (Split-Path -Parent $PubKeyPath))) { if ($d -and -not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Force $d | Out-Null } }
    [IO.File]::WriteAllBytes($KeyPath, $blob)
    [IO.File]::WriteAllText($PubKeyPath, $pub, (New-Object Text.UTF8Encoding($false)))
    $pub
}

# The public key XML of the DPAPI-protected private key.
function Get-ForgeSigningPubKey([string]$KeyPath) {
    $rsa = New-Object Security.Cryptography.RSACryptoServiceProvider
    try { $rsa.PersistKeyInCsp = $false; $rsa.FromXmlString((Read-ForgeSigningKeyXml $KeyPath)); $rsa.ToXmlString($false) } finally { $rsa.Dispose() }
}

# -> base64 RSA PKCS#1 v1.5 SHA-256 signature of $Data (bytes) with the DPAPI-protected key.
function Invoke-ForgeSign([byte[]]$Data, [string]$KeyPath) {
    $rsa = New-Object Security.Cryptography.RSACryptoServiceProvider
    try {
        $rsa.PersistKeyInCsp = $false
        $rsa.FromXmlString((Read-ForgeSigningKeyXml $KeyPath))
        if ($rsa.KeySize -lt 3072) { throw "signing key is only $($rsa.KeySize) bits" }
        [Convert]::ToBase64String($rsa.SignData($Data, 'SHA256'))
    } finally { $rsa.Dispose() }
}

if ($NoRun) { return }
$ErrorActionPreference = 'Stop'
$pub = New-ForgeSigningKey -KeyPath $KeyPath -PubKeyPath $PubKeyPath -Force:$Force
Write-Host "signing key: $KeyPath (DPAPI, this user on this machine only)"
Write-Host "public key : $PubKeyPath"
Write-Host 'pin it on the fleet: Machines > Advanced > Signing key (PUT /api/security/signing-key), then build with tools\build-bundle.ps1 -Sign'
$pub
