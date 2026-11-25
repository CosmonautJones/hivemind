# Build the Forge bundle (SPEC-phase1 §8): zip of hub/ agent/ watchdog/ requirements.txt VERSION
#   -> dist\forge-<ver>.zip + dist\forge-<ver>.zip.sha256
# Entry names use '/' (Python's zipfile on the hub expects that) and fixed timestamps in sorted order,
# so the same tree always yields the same sha256.
#   .\tools\build-bundle.ps1 [-Root <repo>] [-OutDir <dir>]
#   . .\tools\build-bundle.ps1 -NoRun     # dot-source New-ForgeBundle / Get-BundleFileList
param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [string]$OutDir,
    [switch]$NoRun
)

$BundleIncludes = @('hub', 'agent', 'watchdog', 'requirements.txt', 'VERSION')
$BundleExcludeRegex = '(^|/)(__pycache__|\.pytest_cache|\.mypy_cache|node_modules|\.venv|venv)(/|$)|\.py[co]$|(^|/)\.DS_Store$|\.log$'

# Relative '/'-separated paths of every file that goes in the bundle, sorted ordinally.
function Get-BundleFileList([string]$Root) {
    $rootFull = (Resolve-Path -LiteralPath $Root).Path.TrimEnd('\')
    $list = New-Object System.Collections.Generic.List[string]
    foreach ($inc in $BundleIncludes) {
        $p = Join-Path $rootFull $inc
        if (-not (Test-Path -LiteralPath $p)) { throw "bundle input missing: $inc" }
        $items = if ((Get-Item -LiteralPath $p).PSIsContainer) { Get-ChildItem -LiteralPath $p -Recurse -File -Force } else { @(Get-Item -LiteralPath $p) }
        foreach ($f in $items) {
            $rel = $f.FullName.Substring($rootFull.Length + 1).Replace('\', '/')
            if ($rel -notmatch $BundleExcludeRegex) { $list.Add($rel) }
        }
    }
    $arr = $list.ToArray()
    [Array]::Sort($arr, [StringComparer]::Ordinal)
    , $arr
}

function Get-FileSha256([string]$Path) {
    $sha = [Security.Cryptography.SHA256]::Create()
    $fs = [IO.File]::OpenRead($Path)
    try { ([BitConverter]::ToString($sha.ComputeHash($fs)) -replace '-', '').ToLowerInvariant() } finally { $fs.Close(); $sha.Dispose() }
}

function New-ForgeBundle {
    param([string]$Root, [string]$OutDir)
    $rootFull = (Resolve-Path -LiteralPath $Root).Path.TrimEnd('\')
    if (-not $OutDir) { $OutDir = Join-Path $rootFull 'dist' }
    $ver = (Get-Content -LiteralPath (Join-Path $rootFull 'VERSION') -Raw).Trim()
    if ($ver -notmatch '^\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?$') { throw "VERSION '$ver' is not semver" }
    New-Item -ItemType Directory -Force $OutDir | Out-Null
    $zipPath = Join-Path (Resolve-Path -LiteralPath $OutDir).Path "forge-$ver.zip"
    if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
    Add-Type -AssemblyName System.IO.Compression
    $stamp = New-Object DateTimeOffset (2020, 1, 1, 0, 0, 0, [TimeSpan]::Zero)
    $files = Get-BundleFileList $rootFull
    $fs = [IO.File]::Open($zipPath, [IO.FileMode]::CreateNew)
    try {
        $zip = New-Object IO.Compression.ZipArchive($fs, [IO.Compression.ZipArchiveMode]::Create)
        try {
            foreach ($rel in $files) {
                $e = $zip.CreateEntry($rel, [IO.Compression.CompressionLevel]::Optimal)
                $e.LastWriteTime = $stamp
                $src = [IO.File]::OpenRead((Join-Path $rootFull $rel.Replace('/', '\')))
                $dst = $e.Open()
                try { $src.CopyTo($dst) } finally { $dst.Close(); $src.Close() }
            }
        } finally { $zip.Dispose() }
    } finally { $fs.Close() }
    $sha = Get-FileSha256 $zipPath
    [IO.File]::WriteAllText("$zipPath.sha256", "$sha  forge-$ver.zip`n", (New-Object Text.UTF8Encoding($false)))
    [pscustomobject]@{ version = $ver; path = $zipPath; sha256 = $sha; files = $files.Count; bytes = (Get-Item -LiteralPath $zipPath).Length }
}

if ($NoRun) { return }
$ErrorActionPreference = 'Stop'
$r = New-ForgeBundle -Root $Root -OutDir $OutDir
Write-Host "built $($r.path)  ($($r.files) files, $($r.bytes) bytes)"
Write-Host "sha256 $($r.sha256)"
$r
