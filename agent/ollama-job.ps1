# Forge job runner for engine "ollama" (SPEC-phase2 F). Started by forge-agent.ps1 as its own process:
#   powershell -File ollama-job.ps1 -PromptFile <p> -Model <m> -BaseUrl http://127.0.0.1:11434 -OutFile <out.log>
# REST only (no CLI parsing): GET /api/tags; if the model is missing, POST /api/pull (stream, progress lines
# into the job output) once; then POST /api/generate {model, prompt, stream:false}. Output is appended to
# OutFile as UTF-8 (the agent streams that file to the hub). Exit 0 = done, 1 = failed.
param(
    [Parameter(Mandatory = $true)][string]$PromptFile,
    [Parameter(Mandatory = $true)][string]$Model,
    [string]$BaseUrl = 'http://127.0.0.1:11434',
    [Parameter(Mandatory = $true)][string]$OutFile,
    [int]$TimeoutSec = 3600,
    [switch]$NoRun
)

$Utf8 = New-Object Text.UTF8Encoding($false)

function Write-JobOut([string]$Text) { [IO.File]::AppendAllText($OutFile, "$Text`n", $Utf8) }

# the leading comma keeps byte[] intact (a bare return would unroll it into object[])
function ConvertTo-JsonBytes($Obj) { , [byte[]][Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -InputObject $Obj -Compress -Depth 5)) }

# "qwen2.5:14b" matches tag list names exactly; an untagged name also matches "<name>:latest".
function Test-OllamaHasModel([object[]]$Names, [string]$Model) {
    $want = @($Model); if ($Model -notmatch ':') { $want += "$($Model):latest" }
    foreach ($n in @($Names)) { if ($want -contains [string]$n) { return $true } }
    $false
}

# One progress line per status change, and per 10% step of a download.
function Format-PullProgress($Msg, [hashtable]$Seen) {
    $st = [string]$Msg.status
    if ($Msg.total -and $Msg.completed -ne $null -and [double]$Msg.total -gt 0) {
        $pct = [int][Math]::Floor(100 * [double]$Msg.completed / [double]$Msg.total)
        $step = [int]([Math]::Floor($pct / 10) * 10)
        $key = "$st|$step"
        if ($Seen.ContainsKey($key)) { return $null }
        $Seen[$key] = $true
        return ('[ollama] {0} {1}% ({2:N1} / {3:N1} MB)' -f $st, $step, ([double]$Msg.completed / 1MB), ([double]$Msg.total / 1MB))
    }
    if (-not $st -or $Seen.ContainsKey($st)) { return $null }
    $Seen[$st] = $true
    "[ollama] $st"
}

if ($NoRun) { return }

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$base = $BaseUrl.TrimEnd('/')
try {
    $prompt = [IO.File]::ReadAllText($PromptFile, $Utf8)
    $tags = Invoke-RestMethod -Uri "$base/api/tags" -Method Get -TimeoutSec 15 -UseBasicParsing -DisableKeepAlive
    $names = @(@($tags.models) | ForEach-Object { $_.name; $_.model } | Where-Object { $_ })
    if (-not (Test-OllamaHasModel $names $Model)) {
        Write-JobOut "[ollama] model $Model not present; pulling it (one time)"
        $req = [Net.HttpWebRequest]::Create("$base/api/pull")
        $req.Method = 'POST'; $req.ContentType = 'application/json'; $req.Timeout = $TimeoutSec * 1000; $req.ReadWriteTimeout = 600000
        $req.Proxy = $null; $req.KeepAlive = $false
        $b = ConvertTo-JsonBytes @{ model = $Model; name = $Model; stream = $true }
        $req.ContentLength = $b.Length
        $rs = $req.GetRequestStream(); $rs.Write($b, 0, $b.Length); $rs.Close()
        $resp = $req.GetResponse()
        $sr = New-Object IO.StreamReader($resp.GetResponseStream(), $Utf8)
        $seen = @{}; $ok = $false
        try {
            while ($null -ne ($line = $sr.ReadLine())) {
                if (-not $line.Trim()) { continue }
                $m = $null; try { $m = $line | ConvertFrom-Json } catch { continue }
                if ($m.error) { throw "pull failed: $($m.error)" }
                $t = Format-PullProgress $m $seen
                if ($t) { Write-JobOut $t }
                if ($m.status -eq 'success') { $ok = $true }
            }
        } finally { $sr.Close(); $resp.Close() }
        if (-not $ok) { throw "pull of $Model did not report success" }
    }
    Write-JobOut "[ollama] generating with $Model"
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $r = Invoke-RestMethod -Uri "$base/api/generate" -Method Post -ContentType 'application/json; charset=utf-8' `
        -Body (ConvertTo-JsonBytes @{ model = $Model; prompt = $prompt; stream = $false }) -TimeoutSec $TimeoutSec -UseBasicParsing -DisableKeepAlive
    if ($r.error) { throw "generate failed: $($r.error)" }
    Write-JobOut ([string]$r.response)
    $tok = if ($r.eval_count) { ", $($r.eval_count) tokens" } else { '' }
    Write-JobOut ("[ollama] done in {0:N1} s{1}" -f $sw.Elapsed.TotalSeconds, $tok)
    exit 0
} catch {
    try { Write-JobOut "[ollama] error: $($_.Exception.Message)" } catch {}
    exit 1
}
