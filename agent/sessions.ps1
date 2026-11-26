# Forge agent (user lane): session index (SPEC-phase2 B), live transcripts (C), git info.
# Dot-sourced by forge-agent.ps1. Functions only; nothing runs on load. Pure helpers are unit-tested in
# tests\ps\run-tests.ps1 against tests\ps\fixtures\sessions\*.
#
# Cost rules (the agent loop runs every 5 s and must never stall):
#   - session files are parsed incrementally (per-file byte offset; only appended bytes are read),
#   - each index cycle has a byte + time budget; unfinished files continue next cycle,
#   - git lookups are cached per cwd for 10 min and capped per cycle,
#   - JSON is parsed with JavaScriptSerializer (much faster than ConvertFrom-Json on PS 5.1).

Add-Type -AssemblyName System.Web.Extensions -ErrorAction SilentlyContinue

# ======================= text helpers =======================

# Shared by titles, transcript text (tool results stream `cat .env`, `git remote -v`, ...) and commit subjects.
# Applied in order; each is (regex, replacement). Keep in sync with the hub's forge_projects.SECRET_RE.
#   1. PEM private keys (whole block; a block cut off by a size limit is redacted to the end)
#   2. credentials in URLs: scheme://user:password@host -> scheme://[redacted]@host
#   3. Authorization headers: Bearer / Basic <token>
#   4. KEY=value / "key": "value" / connection strings: names containing PASSWORD, PASSWD, SECRET, TOKEN, API_KEY,
#      ACCESS_KEY, ACCOUNTKEY, PRIVATE_KEY (value only; purely numeric values such as token counts are kept)
#   5. prefixed tokens: sk- (incl. sk-ant-), ghp_, xox, AKIA, eyJ (JWT), github_pat_, glpat-, AIza
$script:ForgeSecretRules = @(
    @{ re = '-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)'; to = '[redacted private key]' },
    @{ re = '(?<=://)[^/\s:@]+:[^@\s/]+(?=@)'; to = '[redacted]' },
    @{ re = '(?i)\b((?:Bearer|Basic)\s+)[A-Za-z0-9._~+/\-]{16,}=*'; to = '${1}[redacted]' },
    @{ re = '(?i)\b([A-Z0-9_.\-]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|ACCESS_?KEY|ACCOUNT_?KEY|PRIVATE_?KEY)[A-Z0-9_.\-]*)(["'']?\s*[=:]\s*["'']?)(?![0-9.]+(?:[\s"'',;}]|$))(?!\[redacted)[^\s"'',;}]{4,}'; to = '${1}${2}[redacted]' },
    @{ re = '(sk-|ghp_|xox|AKIA|eyJ)[A-Za-z0-9_\-]{10,}|github_pat_[A-Za-z0-9_]{10,}|glpat-[A-Za-z0-9_\-]{10,}|AIza[A-Za-z0-9_\-]{20,}'; to = '[redacted]' }
) | ForEach-Object { @{ re = (New-Object System.Text.RegularExpressions.Regex($_.re, [System.Text.RegularExpressions.RegexOptions]::Compiled)); to = $_.to } }
function Protect-SecretText([string]$Text) {
    if (-not $Text) { return $Text }
    $t = $Text
    foreach ($r in $script:ForgeSecretRules) { $t = $r.re.Replace($t, $r.to) }
    $t
}

# Redact first, then cut, so a secret is never half-kept at the cut. Huge inputs (100 KB tool output) are
# pre-trimmed with slack first: a secret crossing the cut still has >= 10 visible chars and is matched.
function Limit-ForgeText([string]$Text, [int]$Max = 4000) {
    $t = $Text
    if ($t -and $t.Length -gt ($Max + 256)) { $t = $t.Substring(0, $Max + 256) }
    $t = Protect-SecretText $t
    if ($t -and $t.Length -gt $Max) { $t = $t.Substring(0, $Max - 1) + [char]0x2026 }
    $t
}

# Cheap pre-JSON filter for lines that never matter (big bookkeeping records). Claude: top-level records that
# start with {"type":"X"} for X other than user/assistant/system/ai-title (user/assistant/attachment records
# start with {"parentUuid"). Codex: world_state / token_usage_record / event_msg other than token_count;
# for transcripts also session_meta / turn_context / event_msg.
function Test-SkipSessionLine([string]$Tool, [string]$Text, [bool]$ForTranscript = $false) {
    if ($Tool -eq 'codex') {
        $head = if ($Text.Length -gt 160) { $Text.Substring(0, 160) } else { $Text }
        if ($head -match '"type":"(world_state|token_usage_record)"') { return $true }
        if ($head.Contains('"type":"event_msg"')) { return ($ForTranscript -or -not $head.Contains('"token_count"')) }
        if ($ForTranscript -and $head -match '"type":"(session_meta|turn_context)"') { return $true }
        return $false
    }
    $ord = [StringComparison]::Ordinal
    if (-not $Text.StartsWith('{"type":"', $ord)) {
        # metadata only: tool_result user records (bulky tool output) never change title/count/model
        if (-not $ForTranscript -and $Text.IndexOf('"tool_use_id":"', $ord) -gt 0) {
            $head = if ($Text.Length -gt 200) { $Text.Substring(0, 200) } else { $Text }
            if ($head.IndexOf('"type":"user"', $ord) -gt 0) { return $true }
        }
        return $false
    }
    -not ($Text.StartsWith('{"type":"user"', $ord) -or $Text.StartsWith('{"type":"assistant"', $ord) -or $Text.StartsWith('{"type":"system"', $ord) -or
          ((-not $ForTranscript) -and $Text.StartsWith('{"type":"ai-title"', $ord)))
}

function ConvertTo-SessionTitle([string]$Text, [int]$Max = 140) {
    if (-not $Text) { return $null }
    $t = ([regex]::Replace($Text, '\s+', ' ')).Trim()
    if (-not $t) { return $null }
    Limit-ForgeText $t $Max
}

# A "real" prompt is something a person typed, not injected context (<environment_context>, slash-command
# wrappers, AGENTS.md dumps, interruption markers).
function Test-RealPrompt([string]$Text) {
    if (-not $Text) { return $false }
    $t = $Text.TrimStart()
    if (-not $t) { return $false }
    if ($t.StartsWith('<')) { return $false }
    if ($t.StartsWith('# AGENTS.md') -or $t.StartsWith('[Request interrupted') -or $t.StartsWith('Caveat:')) { return $false }
    $true
}

function ConvertTo-EpochSeconds($Value) {
    if ($null -eq $Value -or "$Value" -eq '') { return $null }
    if ($Value -is [double] -or $Value -is [int] -or $Value -is [long] -or $Value -is [decimal]) {
        $d = [double]$Value; if ($d -gt 1e11) { $d = $d / 1000 }; return $d
    }
    $dto = [DateTimeOffset]::MinValue
    if ([DateTimeOffset]::TryParse([string]$Value, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal, [ref]$dto)) {
        return [Math]::Round($dto.ToUnixTimeMilliseconds() / 1000.0, 3)
    }
    $null
}

function ConvertTo-EpochFromDate([datetime]$Date) { [Math]::Round(([DateTimeOffset]$Date.ToUniversalTime()).ToUnixTimeMilliseconds() / 1000.0, 3) }

# ======================= JSON =======================
$script:ForgeJsonParser = $null
function Get-ForgeJsonParser {
    if (-not $script:ForgeJsonParser) {
        $j = New-Object System.Web.Script.Serialization.JavaScriptSerializer
        $j.MaxJsonLength = [int]::MaxValue; $j.RecursionLimit = 256
        $script:ForgeJsonParser = $j
    }
    $script:ForgeJsonParser
}

# Dictionary (keys accessible as $o.key) or $null for blank/malformed/non-object lines.
function ConvertFrom-JsonLine([string]$Line) {
    if (-not $Line) { return $null }
    $l = $Line.Trim()
    if (-not $l.StartsWith('{')) { return $null }
    try { $o = (Get-ForgeJsonParser).DeserializeObject($l); if ($o -is [System.Collections.IDictionary]) { return $o } } catch {}
    $null
}

function ConvertTo-CompactJson($Obj) { try { (Get-ForgeJsonParser).Serialize($Obj) } catch { "$Obj" } }

# Text of a content value: string, or list of blocks {type, text}. Images become "[image]".
function Get-BlockText($Content) {
    if ($null -eq $Content) { return '' }
    if ($Content -is [string]) { return $Content }
    if ($Content -is [System.Collections.IDictionary]) { if ($null -ne $Content.text) { return [string]$Content.text }; if ($null -ne $Content.output) { return (Get-BlockText $Content.output) }; return '' }
    $parts = New-Object System.Collections.Generic.List[string]
    foreach ($b in @($Content)) {
        if ($b -is [string]) { $parts.Add($b) }
        elseif ($b -is [System.Collections.IDictionary]) {
            if ($null -ne $b.text) { $parts.Add([string]$b.text) }
            elseif ("$($b.type)" -match 'image') { $parts.Add('[image]') }
        }
    }
    $parts -join "`n"
}

# One-line summary of a tool call's input: the interesting field first, else compact JSON.
function Format-ToolInput($ToolInput) {
    if ($null -eq $ToolInput) { return '' }
    if ($ToolInput -is [string]) { return $ToolInput }
    if ($ToolInput -is [System.Collections.IDictionary]) {
        foreach ($k in 'command', 'file_path', 'path', 'pattern', 'url', 'query', 'description', 'prompt') {
            if ($ToolInput[$k]) { return "$($k): $($ToolInput[$k])" }
        }
    }
    ConvertTo-CompactJson $ToolInput
}

function New-TranscriptEvent($Ts, [string]$Role, [string]$Kind, [string]$Text, [string]$ToolName, [bool]$Collapsed = $false) {
    $e = [ordered]@{ ts = (ConvertTo-EpochSeconds $Ts); role = $Role; kind = $Kind; text = (Limit-ForgeText $Text 4000); collapsed = $Collapsed }
    if ($ToolName) { $e.tool_name = $ToolName }
    $e
}

# ======================= Claude Code jsonl =======================
# Records: type user|assistant|system (+ many bookkeeping types we skip). message.content is a string or
# blocks text / tool_use / tool_result / thinking / image. Assistant blocks of one API message arrive as
# separate records sharing message.id.

function ConvertFrom-ClaudeRecord($Rec, [hashtable]$ToolNames = @{}) {
    $out = New-Object System.Collections.Generic.List[object]
    if (-not ($Rec -is [System.Collections.IDictionary])) { return , $out.ToArray() }
    if ($Rec.isSidechain -eq $true -or $Rec.isMeta -eq $true) { return , $out.ToArray() }
    $ts = $Rec.timestamp
    $msg = $Rec.message
    switch ([string]$Rec.type) {
        'user' {
            if (-not ($msg -is [System.Collections.IDictionary])) { break }
            $c = $msg.content
            if ($c -is [string]) {
                $role = if ($c.TrimStart().StartsWith('<local-command-')) { 'system' } else { 'user' }
                $txt = if ($c.TrimStart().StartsWith('<')) { ([regex]::Replace($c, '<[^>]{1,80}>', ' ')).Trim() } else { $c }
                if ($txt) { $out.Add((New-TranscriptEvent $ts $role 'text' $txt $null $false)) }
                break
            }
            foreach ($b in @($c)) {
                if (-not ($b -is [System.Collections.IDictionary])) { continue }
                switch ([string]$b.type) {
                    'text' { if ($b.text -and -not ([string]$b.text).TrimStart().StartsWith('<system-reminder>')) { $out.Add((New-TranscriptEvent $ts 'user' 'text' ([string]$b.text) $null $false)) } }
                    'image' { $out.Add((New-TranscriptEvent $ts 'user' 'text' '[image]' $null $false)) }
                    'tool_result' {
                        $name = $null; if ($b.tool_use_id -and $ToolNames.ContainsKey([string]$b.tool_use_id)) { $name = $ToolNames[[string]$b.tool_use_id] }
                        $txt = Get-BlockText $b.content
                        if ($b.is_error -eq $true) { $txt = "[error] $txt" }
                        $out.Add((New-TranscriptEvent $ts 'tool' 'tool_result' $txt $name $true))
                    }
                }
            }
        }
        'assistant' {
            if (-not ($msg -is [System.Collections.IDictionary])) { break }
            foreach ($b in @($msg.content)) {
                if ($b -is [string]) { if ($b) { $out.Add((New-TranscriptEvent $ts 'assistant' 'text' $b $null $false)) }; continue }
                if (-not ($b -is [System.Collections.IDictionary])) { continue }
                switch ([string]$b.type) {
                    'text' { if ($b.text) { $out.Add((New-TranscriptEvent $ts 'assistant' 'text' ([string]$b.text) $null $false)) } }
                    'thinking' { if ($b.thinking) { $out.Add((New-TranscriptEvent $ts 'assistant' 'thinking' ([string]$b.thinking) $null $true)) } }
                    'tool_use' {
                        if ($b.id) { $ToolNames[[string]$b.id] = [string]$b.name }
                        $out.Add((New-TranscriptEvent $ts 'tool' 'tool_call' (Format-ToolInput $b.input) ([string]$b.name) $true))
                    }
                }
            }
        }
        'system' {
            if ($Rec.content -and ($Rec.content -is [string])) { $out.Add((New-TranscriptEvent $ts 'system' 'text' ([string]$Rec.content) $null $false)) }
        }
    }
    , $out.ToArray()
}

function New-SessionMeta([string]$Tool, [string]$Path) {
    $sid = $null
    if ($Path -match '([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$') { $sid = $Matches[1].ToLowerInvariant() }
    @{ tool = $Tool; path = $Path; session_id = $sid; offset = [long]0; size = [long]0; mtime = 0.0; indexed = $false
       cwd = $null; branch_hint = $null; title = $null; ai_title = $null; started = $null; last_ts = $null
       messages = 0; tokens = $null; model = $null; remote_control = $null; entrypoint = $null; last_msg_id = $null }
}

function Update-ClaudeMeta([hashtable]$Meta, $Rec) {
    if (-not ($Rec -is [System.Collections.IDictionary])) { return }
    if ($Rec.timestamp) {
        # only the first timestamp is converted (per-record date parsing is the hot path on big files)
        if (-not $Meta.started) { $Meta.started = ConvertTo-EpochSeconds $Rec.timestamp }
        $Meta.last_ts_raw = $Rec.timestamp
    }
    if ($Rec.cwd -and -not $Meta.cwd) { $Meta.cwd = [string]$Rec.cwd }
    if ($Rec.gitBranch) { $Meta.branch_hint = [string]$Rec.gitBranch }
    if ($Rec.entrypoint) { $Meta.entrypoint = [string]$Rec.entrypoint }
    if ($Rec.isSidechain -eq $true) { return }
    $msg = $Rec.message
    switch ([string]$Rec.type) {
        'user' {
            if ($Rec.isMeta -eq $true -or -not ($msg -is [System.Collections.IDictionary])) { break }
            $c = $msg.content; $txt = $null
            if ($c -is [string]) { $txt = $c }
            else {
                $texts = New-Object System.Collections.Generic.List[string]
                foreach ($b in @($c)) { if ($b -is [System.Collections.IDictionary] -and $b.type -eq 'text') { $texts.Add([string]$b.text) } }
                if ($texts.Count) { $txt = $texts -join "`n" }
            }
            if ($null -ne $txt) {
                if (Test-RealPrompt $txt) { $Meta.messages++; if (-not $Meta.title) { $Meta.title = ConvertTo-SessionTitle $txt } }
            }
        }
        'assistant' {
            if (-not ($msg -is [System.Collections.IDictionary])) { break }
            $mid = [string]$msg.id
            if (-not $mid -or $mid -ne $Meta.last_msg_id) {
                $Meta.messages++; $Meta.last_msg_id = $mid
                $u = $msg.usage
                if ($u -is [System.Collections.IDictionary]) {
                    [long]$n = 0; if ($Meta.tokens) { $n = [long]$Meta.tokens }
                    foreach ($k in 'input_tokens', 'cache_creation_input_tokens', 'output_tokens') { if ($u[$k]) { $n += [long]$u[$k] } }
                    $Meta.tokens = $n
                }
            }
            if ($msg.model -and "$($msg.model)" -ne '<synthetic>') { $Meta.model = [string]$msg.model }
        }
        'ai-title' { if ($Rec.aiTitle) { $Meta.ai_title = ConvertTo-SessionTitle ([string]$Rec.aiTitle) } }
        'attachment' {
            $a = $Rec.attachment
            if ($a -is [System.Collections.IDictionary] -and $a.type -eq 'remote_session_change') { $Meta.remote_control = [bool]$a.url }
        }
    }
}

# ======================= Codex jsonl =======================
# Current: {timestamp, type: session_meta|response_item|event_msg|turn_context|..., payload:{...}}.
# Legacy (2025-era): first line {id, timestamp, instructions}, then bare {type: message|function_call|...}
# records and {record_type: state}. event_msg duplicates response_item content, so it is skipped
# except token counts.

function Get-CodexPayload($Rec) {
    if (-not ($Rec -is [System.Collections.IDictionary])) { return $null }
    if ($Rec.payload -is [System.Collections.IDictionary]) { return @{ kind = [string]$Rec.type; p = $Rec.payload } }
    if ($Rec.type -and -not $Rec.record_type) { return @{ kind = 'response_item'; p = $Rec } }   # legacy bare item
    $null
}

function ConvertFrom-CodexRecord($Rec, [hashtable]$ToolNames = @{}) {
    $out = New-Object System.Collections.Generic.List[object]
    $x = Get-CodexPayload $Rec
    if (-not $x) { return , $out.ToArray() }
    $ts = $Rec.timestamp; $p = $x.p
    if ($x.kind -eq 'compacted') { $out.Add((New-TranscriptEvent $ts 'system' 'text' 'context compacted' $null $false)); return , $out.ToArray() }
    if ($x.kind -ne 'response_item') { return , $out.ToArray() }
    switch ([string]$p.type) {
        'message' {
            $txt = Get-BlockText $p.content
            if ($p.role -eq 'user') { if (Test-RealPrompt $txt) { $out.Add((New-TranscriptEvent $ts 'user' 'text' $txt $null $false)) } }
            elseif ($p.role -eq 'assistant') { if ($txt) { $out.Add((New-TranscriptEvent $ts 'assistant' 'text' $txt $null $false)) } }
        }
        'reasoning' {
            $txt = Get-BlockText $p.summary
            if ($txt) { $out.Add((New-TranscriptEvent $ts 'assistant' 'thinking' $txt $null $true)) }
        }
        { $_ -in 'function_call', 'custom_tool_call', 'local_shell_call', 'web_search_call' } {
            $name = if ($p.name) { [string]$p.name } elseif ($p.type -eq 'web_search_call') { 'web_search' } else { 'shell' }
            if ($p.call_id) { $ToolNames[[string]$p.call_id] = $name }
            $arg = if ($null -ne $p.arguments) { $p.arguments } elseif ($null -ne $p.input) { $p.input } elseif ($null -ne $p.action) { $p.action } else { '' }
            if ($arg -is [string] -and $arg.StartsWith('{')) { $j = ConvertFrom-JsonLine $arg; if ($j) { $arg = $j } }
            $out.Add((New-TranscriptEvent $ts 'tool' 'tool_call' (Format-ToolInput $arg) $name $true))
        }
        { $_ -in 'function_call_output', 'custom_tool_call_output', 'local_shell_call_output' } {
            $name = $null; if ($p.call_id -and $ToolNames.ContainsKey([string]$p.call_id)) { $name = $ToolNames[[string]$p.call_id] }
            $out.Add((New-TranscriptEvent $ts 'tool' 'tool_result' (Get-BlockText $p.output) $name $true))
        }
    }
    , $out.ToArray()
}

function Update-CodexMeta([hashtable]$Meta, $Rec) {
    if (-not ($Rec -is [System.Collections.IDictionary])) { return }
    $ts = ConvertTo-EpochSeconds $Rec.timestamp
    if ($ts) { if (-not $Meta.started) { $Meta.started = $ts }; if (-not $Meta.last_ts -or $ts -gt $Meta.last_ts) { $Meta.last_ts = $ts } }
    # legacy header line: {id, timestamp, instructions}
    if ($Rec.id -and -not $Rec.type -and -not $Rec.payload -and -not $Rec.record_type) { $Meta.session_id = ([string]$Rec.id).ToLowerInvariant(); return }
    $x = Get-CodexPayload $Rec
    if (-not $x) { return }
    $p = $x.p
    switch ($x.kind) {
        'session_meta' {
            $id = if ($p.id) { $p.id } elseif ($p.session_id) { $p.session_id } else { $null }
            if ($id) { $Meta.session_id = ([string]$id).ToLowerInvariant() }
            if ($p.cwd) { $Meta.cwd = [string]$p.cwd }
            if ($p.timestamp) { $Meta.started = ConvertTo-EpochSeconds $p.timestamp }
            if ($p.originator) { $Meta.entrypoint = [string]$p.originator }
        }
        'turn_context' {
            if ($p.cwd -and -not $Meta.cwd) { $Meta.cwd = [string]$p.cwd }
            if ($p.model) { $Meta.model = [string]$p.model }
        }
        'event_msg' {
            if ($p.type -eq 'token_count' -and $p.info -is [System.Collections.IDictionary] -and $p.info.total_token_usage -is [System.Collections.IDictionary]) {
                $t = $p.info.total_token_usage.total_tokens; if ($null -ne $t) { $Meta.tokens = [long]$t }
            }
        }
        'response_item' {
            if ($p.type -ne 'message') { break }
            $txt = Get-BlockText $p.content
            if ($p.role -eq 'user') {
                if (-not $Meta.cwd -and $txt -match '<cwd>([^<]+)</cwd>') { $Meta.cwd = ($Matches[1] -replace '^\\\\\?\\', '') }
                if (Test-RealPrompt $txt) { $Meta.messages++; if (-not $Meta.title) { $Meta.title = ConvertTo-SessionTitle $txt } }
            } elseif ($p.role -eq 'assistant') { $Meta.messages++ }
        }
    }
}

function ConvertFrom-SessionLine([string]$Tool, [string]$Line, [hashtable]$ToolNames = @{}) {
    if (-not $Line -or (Test-SkipSessionLine $Tool $Line $true)) { return , @() }
    $o = ConvertFrom-JsonLine $Line
    if (-not $o) { return , @() }
    if ($Tool -eq 'codex') { $r = ConvertFrom-CodexRecord $o $ToolNames } else { $r = ConvertFrom-ClaudeRecord $o $ToolNames }
    , $r
}

# ======================= incremental jsonl reader =======================
# Reads complete lines from byte Offset (at most MaxBytes). Returns @{lines=[@{text; end}]; next; size}.
# `end` / `next` are byte offsets just past a line's newline: the cursor for the next call. A trailing
# line without newline is returned only with -AcceptPartialTail and only if it parses as a JSON object.
# A single line longer than MaxBytes is skipped (never buffered whole) when MaxBytes >= SkipOversizeAt (4 MB);
# a smaller (budget-limited) window just returns no lines so the caller retries with a full window.
function Read-JsonlLines {
    param([string]$Path, [long]$Offset = 0, [int]$MaxBytes = 4MB, [switch]$AcceptPartialTail, [int]$SkipOversizeAt = 4MB)
    $res = @{ lines = (New-Object System.Collections.Generic.List[object]); next = $Offset; size = [long]0; skipped = 0 }
    $fs = $null
    try { $fs = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]'ReadWrite, Delete') } catch { return $res }
    try {
        $size = $fs.Length; $res.size = $size
        if ($Offset -lt 0) { $Offset = 0; $res.next = 0 }
        if ($Offset -ge $size) { return $res }
        $len = [int][Math]::Min($size - $Offset, [long]$MaxBytes)
        $buf = New-Object byte[] $len
        $null = $fs.Seek($Offset, [IO.SeekOrigin]::Begin)
        $n = 0; while ($n -lt $len) { $r = $fs.Read($buf, $n, $len - $n); if ($r -le 0) { break }; $n += $r }
        $start = 0
        while ($start -lt $n) {
            $j = [Array]::IndexOf($buf, [byte]10, $start, $n - $start)
            if ($j -lt 0) { break }
            $l = $j - $start; if ($l -gt 0 -and $buf[$j - 1] -eq 13) { $l-- }
            if ($l -gt 0) { $res.lines.Add(@{ text = [Text.Encoding]::UTF8.GetString($buf, $start, $l); end = $Offset + $j + 1 }) }
            $start = $j + 1
        }
        $res.next = $Offset + $start
        if ($start -eq 0 -and $n -eq $MaxBytes -and $MaxBytes -ge $SkipOversizeAt -and ($Offset + $n) -lt $size) {
            # one line longer than the window: skip to the byte after its newline
            $pos = $Offset + $n; $chunk = New-Object byte[] 1MB
            while ($pos -lt $size) {
                $null = $fs.Seek($pos, [IO.SeekOrigin]::Begin)
                $m = $fs.Read($chunk, 0, $chunk.Length); if ($m -le 0) { break }
                $k = [Array]::IndexOf($chunk, [byte]10, 0, $m)
                if ($k -ge 0) { $res.next = $pos + $k + 1; $res.skipped++; break }
                $pos += $m
            }
        } elseif ($AcceptPartialTail -and $start -lt $n -and ($Offset + $n) -eq $size) {
            $tail = [Text.Encoding]::UTF8.GetString($buf, $start, $n - $start)
            if ($tail.TrimEnd().EndsWith('}') -and (ConvertFrom-JsonLine $tail)) { $res.lines.Add(@{ text = $tail; end = $size }); $res.next = $size }
        }
    } finally { $fs.Close() }
    $res
}

# ======================= session files / state / processes =======================

# Claude: <ClaudeRoot>\<slug>\<uuid>.jsonl (top level only; subagents\ dirs are not sessions).
# Codex: <CodexRoot>\**\*.jsonl. Only files written since $Since.
function Get-SessionFiles {
    param([string]$ClaudeRoot, [string]$CodexRoot, [datetime]$Since = (Get-Date).AddDays(-14))
    $out = New-Object System.Collections.Generic.List[object]
    $sinceUtc = $Since.ToUniversalTime()
    if ($ClaudeRoot -and [IO.Directory]::Exists($ClaudeRoot)) {
        foreach ($d in @(try { [IO.Directory]::GetDirectories($ClaudeRoot) } catch { @() })) {
            foreach ($f in @(try { [IO.Directory]::GetFiles($d, '*.jsonl') } catch { @() })) {
                $fi = New-Object IO.FileInfo $f
                if ($fi.LastWriteTimeUtc -ge $sinceUtc) { $out.Add(@{ tool = 'claude'; path = $fi.FullName; mtime = (ConvertTo-EpochFromDate $fi.LastWriteTimeUtc); size = $fi.Length }) }
            }
        }
    }
    if ($CodexRoot -and [IO.Directory]::Exists($CodexRoot)) {
        $files = @(); try { $files = @([IO.Directory]::GetFiles($CodexRoot, '*.jsonl', [IO.SearchOption]::AllDirectories)) } catch {}
        foreach ($f in $files) {
            $fi = New-Object IO.FileInfo $f
            if ($fi.LastWriteTimeUtc -ge $sinceUtc) { $out.Add(@{ tool = 'codex'; path = $fi.FullName; mtime = (ConvertTo-EpochFromDate $fi.LastWriteTimeUtc); size = $fi.Length }) }
        }
    }
    , $out.ToArray()
}

# live = written < 90 s ago OR an owning process is alive; idle = within 24 h; done = older.
function Get-SessionState([double]$LastWrite, [double]$Now, [bool]$ProcAlive = $false) {
    if ($ProcAlive -or ($Now - $LastWrite) -lt 90) { return 'live' }
    if (($Now - $LastWrite) -lt 86400) { return 'idle' }
    'done'
}

# Which CLI does this process belong to? Desktop apps (Claude/Codex from WindowsApps, Electron helpers,
# Codex app-server/exec-server) are not terminal sessions.
function Get-AiProcessKind([string]$Name, [string]$CommandLine) {
    $n = "$Name".ToLowerInvariant(); $c = "$CommandLine"
    switch ($n) {
        'claude.exe' { if ($c -match '\\WindowsApps\\Claude_|\\AnthropicClaude\\|--type=') { return $null }; return 'claude' }
        'codex.exe' { if ($c -match '\\WindowsApps\\OpenAI\.Codex|\b(app-server|exec-server|mcp-server)\b') { return $null }; return 'codex' }
        'node.exe' {
            if ($c -match '@anthropic-ai[\\/]claude-code|claude-code[\\/]cli') { return 'claude' }
            if ($c -match '@openai[\\/]codex' -and $c -notmatch '\b(app-server|exec-server|mcp-server)\b') { return 'codex' }
        }
    }
    $null
}

function Get-SessionIdFromCmd([string]$CommandLine) {
    $u = '([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})'
    if ("$CommandLine" -match "(?:--resume|--session-id|(?<![\w-])-r)(?:\s+|=)`"?$u") { return $Matches[1].ToLowerInvariant() }
    if ("$CommandLine" -match "(?<![\w-])resume\s+(?:--?[\w-]+(?:\s+[^\s-]\S*)?\s+)*`"?$u") { return $Matches[1].ToLowerInvariant() }
    $null
}

# Batch (non-interactive) runs: `codex exec ...`, `claude -p/--print ...` processes; sessions whose entrypoint
# (claude entrypoint / codex originator) says exec or sdk. Unknown entrypoint = interactive.
function Test-BatchProcess([string]$Kind, [string]$CommandLine) {
    $c = " $CommandLine "
    if ($Kind -eq 'codex') { return ($c -match '\sexec\s') }
    if ($Kind -eq 'claude') { return ($c -match '\s(-p|--print)\s') }
    $false
}
function Test-BatchSession([string]$Entrypoint) { "$Entrypoint" -match 'exec|sdk' }

# Procs:    @{pid; kind (claude|codex); cmd; started (epoch)}
# Sessions: @{tool; session_id; last_write (epoch); started (epoch, optional); entrypoint (optional)}
# PidMap:   @{pid; session_id; proc_start (epoch)}  from %USERPROFILE%\.claude\sessions\<pid>.json
# Order: pid map (start time must match, guards pid reuse) -> --resume/--session-id in the command line ->
# fallback: a session file of that tool written since the process started, only if both are batch or both are
# interactive (a terminal `codex` must never claim the session of a finished `codex exec` job, which would drop
# the hub's 409 "live in a terminal" guard for its own session); sessions that STARTED after the process are
# preferred over ones only written later, then the most recent write. Returns @{session_id = pid}.
function Find-SessionPids {
    param([object[]]$Procs = @(), [object[]]$Sessions = @(), [object[]]$PidMap = @())
    $res = @{}; $used = @{}; $alive = @{}
    foreach ($p in @($Procs)) { if ($p) { $alive[[int]$p.pid] = $p } }
    foreach ($e in @($PidMap)) {
        if (-not $e -or -not $e.session_id) { continue }
        $p = $alive[[int]$e.pid]
        if (-not $p -or $p.kind -ne 'claude' -or $used[[int]$p.pid]) { continue }
        if ($e.proc_start -and $p.started -and [Math]::Abs([double]$p.started - [double]$e.proc_start) -gt 5) { continue }
        $res[[string]$e.session_id] = [int]$p.pid; $used[[int]$p.pid] = $true
    }
    foreach ($p in @($Procs)) {
        if (-not $p -or $used[[int]$p.pid]) { continue }
        $id = Get-SessionIdFromCmd $p.cmd
        if ($id -and -not $res.ContainsKey($id)) { $res[$id] = [int]$p.pid; $used[[int]$p.pid] = $true }
    }
    foreach ($p in @($Procs | Where-Object { $_ } | Sort-Object -Property @{ Expression = { [double]$_.started }; Descending = $true })) {
        if ($used[[int]$p.pid]) { continue }
        $pBatch = Test-BatchProcess ([string]$p.kind) ([string]$p.cmd)
        $pStart = if ($p.started) { [double]$p.started } else { $null }
        $cand = @($Sessions | Where-Object { $_ -and $_.tool -eq $p.kind -and $_.session_id -and -not $res.ContainsKey([string]$_.session_id) -and
                ((Test-BatchSession ([string]$_.entrypoint)) -eq $pBatch) -and
                ($null -eq $pStart -or [double]$_.last_write -ge ($pStart - 5)) } |
            Sort-Object -Property @{ Expression = { if ($null -ne $pStart -and $_.started -and [double]$_.started -ge ($pStart - 5)) { 0 } else { 1 } } },
                                  @{ Expression = { [double]$_.last_write }; Descending = $true }) | Select-Object -First 1
        if ($cand) { $res[[string]$cand.session_id] = [int]$p.pid; $used[[int]$p.pid] = $true }
    }
    $res
}

function Read-ClaudePidMap([string]$Dir) {
    $out = New-Object System.Collections.Generic.List[object]
    if (-not $Dir -or -not [IO.Directory]::Exists($Dir)) { return , $out.ToArray() }
    foreach ($f in @(try { [IO.Directory]::GetFiles($Dir, '*.json') } catch { @() })) {
        if ([IO.Path]::GetFileName($f) -notmatch '^\d+\.json$') { continue }
        try {
            $o = ConvertFrom-JsonLine ([IO.File]::ReadAllText($f))
            if ($o -and $o.pid -and $o.sessionId) {
                $ps = $null; if ($o.procStart) { $ps = [Math]::Round(([double]$o.procStart) / 1e7 - 11644473600, 3) }
                $out.Add(@{ pid = [int]$o.pid; session_id = ([string]$o.sessionId).ToLowerInvariant(); proc_start = $ps; cwd = [string]$o.cwd; status = [string]$o.status })
            }
        } catch {}
    }
    , $out.ToArray()
}

function Get-AiCliProcesses {
    $out = New-Object System.Collections.Generic.List[object]
    foreach ($p in @(Get-CimInstance Win32_Process -Filter "Name='claude.exe' OR Name='codex.exe' OR Name='node.exe'" -ErrorAction SilentlyContinue)) {
        $kind = Get-AiProcessKind $p.Name $p.CommandLine
        if (-not $kind) { continue }
        $st = $null; if ($p.CreationDate) { $st = ConvertTo-EpochFromDate $p.CreationDate }
        $out.Add(@{ pid = [int]$p.ProcessId; kind = $kind; cmd = "$($p.CommandLine)"; started = $st })
    }
    , $out.ToArray()
}

# ======================= git =======================

# `git status --porcelain=v1 -b` -> @{branch; ahead; behind; dirty}
function ConvertFrom-GitStatus([string[]]$Lines) {
    $r = @{ branch = $null; ahead = 0; behind = 0; dirty = $false }
    foreach ($l in @($Lines)) {
        if (-not $l) { continue }
        if ($l.StartsWith('## ')) {
            $h = $l.Substring(3)
            if ($h -match '^No commits yet on (\S+)') { $r.branch = $Matches[1] }
            elseif ($h -match '^HEAD \(no branch\)') { $r.branch = 'HEAD' }
            else { $r.branch = (($h -split ' \[', 2)[0] -split '\.\.\.', 2)[0].Trim() }
            if ($h -match 'ahead (\d+)') { $r.ahead = [int]$Matches[1] }
            if ($h -match 'behind (\d+)') { $r.behind = [int]$Matches[1] }
        } else { $r.dirty = $true }
    }
    $r
}

# `git log -5 --format=%H%x1f%an%x1f%at%x1f%s` -> commits (subjects redacted)
function ConvertFrom-GitLog([string[]]$Lines) {
    $out = New-Object System.Collections.Generic.List[object]
    foreach ($l in @($Lines)) {
        if (-not $l) { continue }
        $p = $l.Split([char]0x1f)
        if ($p.Count -lt 4 -or $p[0] -notmatch '^[0-9a-f]{7,64}$') { continue }
        $ts = 0L; [void][long]::TryParse($p[2], [ref]$ts)
        $out.Add([ordered]@{ sha = $p[0]; author = $p[1]; ts = $ts; subject = (Limit-ForgeText (($p[3..($p.Count - 1)]) -join ' ') 300) })
    }
    , $out.ToArray()
}

function Invoke-GitLines([string]$GitExe, [string]$Cwd, [string[]]$GitArgs) {
    $prev = $null
    try { $prev = [Console]::OutputEncoding; [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false) } catch {}
    try {
        $o = @(& $GitExe -C $Cwd @GitArgs 2>$null)
        @{ code = $LASTEXITCODE; lines = [string[]]@($o | ForEach-Object { "$_" }) }
    } catch { @{ code = -1; lines = [string[]]@() } }
    finally { if ($prev) { try { [Console]::OutputEncoding = $prev } catch {} } }
}

# Remote + branch for a cwd (cached by the caller for 10 min).
function Get-GitCwdInfo([string]$GitExe, [string]$Cwd) {
    $r = @{ is_repo = $false; remote = $null; branch = $null }
    if (-not $GitExe -or -not $Cwd -or -not [IO.Directory]::Exists($Cwd)) { return $r }
    # one call for non-repos (exit 128); works on fresh repos; empty output = detached HEAD
    $b = Invoke-GitLines $GitExe $Cwd @('branch', '--show-current')
    if ($b.code -ne 0) { return $r }
    $r.branch = if ($b.lines.Count -and $b.lines[0].Trim()) { $b.lines[0].Trim() } else { 'HEAD' }
    $r.is_repo = $true
    $u = Invoke-GitLines $GitExe $Cwd @('remote', 'get-url', 'origin')
    if ($u.code -eq 0 -and $u.lines.Count) { $r.remote = ($u.lines[0].Trim() -replace '//[^/@]+@', '//') }   # never ship credentials in URLs
    $r
}

function Get-RepoReport([string]$GitExe, [string]$Cwd, [string]$Remote) {
    $s = Invoke-GitLines $GitExe $Cwd @('--no-optional-locks', 'status', '--porcelain=v1', '-b')
    if ($s.code -ne 0) { return $null }
    $st = ConvertFrom-GitStatus $s.lines
    $l = Invoke-GitLines $GitExe $Cwd @('log', '-5', '--format=%H%x1f%an%x1f%at%x1f%s')
    $commits = ConvertFrom-GitLog $l.lines
    [ordered]@{ cwd = $Cwd; git_remote = $Remote; branch = $st.branch; dirty = $st.dirty; ahead = $st.ahead; behind = $st.behind
                commits = @($commits) }
}

# ======================= t3 =======================
# SPEC B: t3 sessions only if the CLI can list them as JSON. `t3 thread list --json` exists but needs a
# --project, and there is no `t3 project list`. Not used yet: the agent reports t3 'unsupported' until thread
# collection is implemented (forge-agent.ps1 Get-T3Support); this check gates that future collection.
function Test-T3ListSupport([string]$ProjectHelp) {
    if (-not $ProjectHelp) { return $false }
    $sub = $ProjectHelp -split 'SUBCOMMANDS', 2
    if ($sub.Count -lt 2) { return $false }
    $sub[1] -match '(?m)^\s+list\s'
}

# ======================= index cycle =======================

function New-SessionIndexState { @{ files = @{}; sent = @{}; last_full = 0.0; git = @{}; force = @{} } }

# Parse appended bytes into the meta. Stops at a line boundary once the stopwatch passes DeadlineMs, so a
# cycle's time budget holds even inside one big file. Returns the bytes consumed.
function Update-SessionMetaFromFile([hashtable]$Meta, [int]$MaxBytes, [Diagnostics.Stopwatch]$Clock = $null, [int]$DeadlineMs = 0) {
    $start = [long]$Meta.offset
    $r = Read-JsonlLines -Path $Meta.path -Offset $start -MaxBytes $MaxBytes -AcceptPartialTail
    $next = $r.next; $stopped = $false
    foreach ($ln in $r.lines) {
        if ($Clock -and $DeadlineMs -gt 0 -and $Clock.ElapsedMilliseconds -ge $DeadlineMs) { $next = [long]$Meta.offset; $stopped = $true; break }
        $Meta.offset = $ln.end
        if (Test-SkipSessionLine $Meta.tool $ln.text) { continue }
        $o = ConvertFrom-JsonLine $ln.text
        if (-not $o) { continue }
        if ($Meta.tool -eq 'codex') { Update-CodexMeta $Meta $o } else { Update-ClaudeMeta $Meta $o }
    }
    $Meta.offset = $next
    if ($r.size) { $Meta.size = $r.size }
    # indexed = caught up; a pending partial last line (file being written) counts as caught up
    if (-not $stopped -and ($Meta.offset -ge $r.size -or ($r.lines.Count -eq 0 -and $next -eq $start -and ($start + $MaxBytes) -ge $r.size))) { $Meta.indexed = $true }
    [long]($Meta.offset - $start)
}

function New-SessionPayload([hashtable]$Meta, [string]$NodeId, $ProcId, [string]$State, $Git) {
    $branch = if ($Git -and $Git.branch) { $Git.branch } else { $Meta.branch_hint }
    [ordered]@{
        tool = $Meta.tool; session_id = $Meta.session_id; node_id = $NodeId; cwd = $Meta.cwd
        git_remote = $(if ($Git) { $Git.remote } else { $null }); git_branch = $branch
        title = $Meta.title; started = $Meta.started; last_activity = $Meta.mtime
        messages = [int]$Meta.messages; tokens = $Meta.tokens; state = $State
        remote_control = $Meta.remote_control; pid = $ProcId; model = $Meta.model
        ai_title = $Meta.ai_title; entrypoint = $Meta.entrypoint
    }
}

function Find-SessionMeta([hashtable]$State, [string]$Tool, [string]$SessionId) {
    $sid = "$SessionId".ToLowerInvariant()
    foreach ($m in $State.files.Values) { if ($m.session_id -eq $sid -and (-not $Tool -or $m.tool -eq $Tool)) { return $m } }
    $null
}

# One index cycle. Parses within budget, then returns @{sessions; removed; pending} where pending holds the
# signatures to record (Confirm-SessionsSent) once the hub accepted the POST. Sessions are sent when new or
# changed (state transitions included), and all of them every FullEverySec.
function Invoke-SessionIndexCycle {
    param([hashtable]$State, [string]$NodeId, [string]$ClaudeRoot, [string]$CodexRoot, [string]$GitExe,
          [object[]]$Procs = @(), [object[]]$PidMap = @(), [double]$Now = (ConvertTo-EpochFromDate (Get-Date)),
          [int]$Days = 14, [int]$BudgetMs = 1000, [long]$BudgetBytes = 16MB, [int]$MaxGitLookups = 6,
          [int]$FullEverySec = 600, [int]$GitCacheSec = 600)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $since = [DateTimeOffset]::FromUnixTimeSeconds([long]($Now - $Days * 86400)).UtcDateTime
    $files = Get-SessionFiles -ClaudeRoot $ClaudeRoot -CodexRoot $CodexRoot -Since $since
    $files = @($files | Sort-Object -Property @{ Expression = { [double]$_.mtime }; Descending = $true })
    $seen = @{}; $left = $BudgetBytes
    foreach ($f in $files) {
        $seen[$f.path] = $true
        $m = $State.files[$f.path]
        if (-not $m) { $m = New-SessionMeta $f.tool $f.path; $State.files[$f.path] = $m }
        if ([long]$f.size -lt [long]$m.offset) { $m = New-SessionMeta $f.tool $f.path; $State.files[$f.path] = $m }   # truncated / rewritten
        $m.mtime = [double]$f.mtime
        while ([long]$m.offset -lt [long]$f.size -and $left -gt 0 -and $sw.ElapsedMilliseconds -lt $BudgetMs) {
            $used = Update-SessionMetaFromFile $m ([int][Math]::Min($left, 4MB)) $sw $BudgetMs
            $left -= $used
            if ($used -le 0) { break }
        }
        if ([long]$m.offset -ge [long]$f.size) { $m.indexed = $true }
    }
    $removed = New-Object System.Collections.Generic.List[string]
    foreach ($p in @($State.files.Keys)) {
        if ($seen.ContainsKey($p)) { continue }
        $sid = $State.files[$p].session_id
        $State.files.Remove($p)
        if ($sid -and $State.sent.ContainsKey($sid)) { $removed.Add($sid) }
    }
    $metas = @($State.files.Values | Where-Object { $_.indexed -and $_.session_id })
    # git info per cwd (cached, capped per cycle); cwds no session uses any more are forgotten
    $lookups = 0
    $cwdSet = @($metas | ForEach-Object { $_.cwd } | Where-Object { $_ } | Sort-Object -Unique)
    foreach ($k in @($State.git.Keys)) { if ($cwdSet -notcontains $k) { $State.git.Remove($k) } }
    foreach ($cwd in $cwdSet) {
        $g = $State.git[$cwd]
        if ($g -and ($Now - [double]$g.at) -lt $GitCacheSec) { continue }
        if ($lookups -ge $MaxGitLookups -or -not $GitExe -or $sw.ElapsedMilliseconds -ge ($BudgetMs * 2)) { continue }
        $lookups++
        $info = Get-GitCwdInfo $GitExe $cwd
        $info.at = $Now; $State.git[$cwd] = $info
    }
    $sessInfo = @($metas | ForEach-Object { @{ tool = $_.tool; session_id = $_.session_id; last_write = $_.mtime; started = $_.started; entrypoint = $_.entrypoint } })
    $pids = Find-SessionPids -Procs $Procs -Sessions $sessInfo -PidMap $PidMap
    $full = (($Now - [double]$State.last_full) -ge $FullEverySec)
    $out = New-Object System.Collections.Generic.List[object]; $pending = @{}
    foreach ($m in $metas) {
        $procId = $null; if ($pids.ContainsKey($m.session_id)) { $procId = $pids[$m.session_id] }
        $st = Get-SessionState -LastWrite $m.mtime -Now $Now -ProcAlive ($null -ne $procId)
        $m.last_state = $st; $m.pid = $procId
        $g = $null; if ($m.cwd -and $State.git[$m.cwd] -and $State.git[$m.cwd].is_repo) { $g = $State.git[$m.cwd] }
        $pl = New-SessionPayload $m $NodeId $procId $st $g
        $sig = ConvertTo-CompactJson $pl
        if ($full -or $State.force.ContainsKey($m.session_id) -or $State.sent[$m.session_id] -ne $sig) { $out.Add($pl); $pending[$m.session_id] = $sig }
    }
    @{ sessions = $out.ToArray(); removed = $removed.ToArray(); pending = $pending; full = $full }
}

function Confirm-SessionsSent([hashtable]$State, $Cycle, [double]$Now) {
    foreach ($k in $Cycle.pending.Keys) { $State.sent[$k] = $Cycle.pending[$k]; $State.force.Remove($k) }
    foreach ($r in @($Cycle.removed)) { $State.sent.Remove($r) }
    if ($Cycle.full) { $State.last_full = $Now }
}

# Repos (distinct git cwds) known from the index, for the 5-min commits report.
function Get-KnownRepoCwds([hashtable]$State) {
    , @($State.git.Keys | Where-Object { $State.git[$_].is_repo } | Sort-Object)
}

# ======================= transcript =======================
# Cursor = byte offset just past the last line included. Since $null (first call): the last Max events
# (reading backwards from the end in TailBytes steps, at most MaxTailBytes). Since N: events after N, at
# most Max per call; the cursor stops at the last line fully included so nothing is lost or repeated.
# at_end = the cursor reached the end of the file (nothing pending).
function Get-TranscriptBatch {
    param([string]$Tool, [string]$Path, $Since = $null, [int]$Max = 200, [hashtable]$ToolNames = @{},
          [int]$TailBytes = 2MB, [int]$MaxTailBytes = 8MB, [int]$ChunkBytes = 4MB)
    $size = [long]0; try { $size = (New-Object IO.FileInfo $Path).Length } catch {}
    $sinceN = $null
    if ($null -ne $Since -and "$Since" -ne '') { $tmp = 0L; if ([long]::TryParse("$Since", [ref]$tmp) -and $tmp -ge 0 -and $tmp -le $size) { $sinceN = $tmp } }
    $events = New-Object System.Collections.Generic.List[object]
    if ($null -eq $sinceN) {
        # Walk backwards in TailBytes chunks; each byte is parsed once. A chunk's first line is partial unless the
        # chunk starts at 0; it is re-read as the last line of the next (earlier) chunk. Tool names are resolved
        # within each chunk (a tool_result whose tool_use is in an earlier chunk just has no tool_name).
        $chunkEnd = $size; $cursor = $null; $scanned = [long]0
        $chunks = New-Object System.Collections.Generic.List[object]; $count = 0
        while ($chunkEnd -gt 0 -and $count -lt $Max -and $scanned -lt $MaxTailBytes) {
            $chunkStart = [Math]::Max([long]0, $chunkEnd - $TailBytes)
            $r = Read-JsonlLines -Path $Path -Offset $chunkStart -MaxBytes ([int]($chunkEnd - $chunkStart)) -AcceptPartialTail:($chunkEnd -eq $size)
            if ($null -eq $cursor) { $cursor = $r.next }
            $lines = $r.lines.ToArray()   # (@() on a List[object] of hashtables throws on PS 5.1)
            if ($chunkStart -gt 0) {
                if ($lines.Count -le 1) { if ($lines.Count -eq 1) { $chunkEnd = $lines[0].end }; if ($TailBytes -ge $MaxTailBytes) { break }; $TailBytes = [int]($TailBytes * 2); continue }   # one huge line: widen
                $nextEnd = $lines[0].end; $lines = $lines[1..($lines.Count - 1)]
            } else { $nextEnd = 0 }
            $names = @{}; $evs = New-Object System.Collections.Generic.List[object]
            foreach ($ln in $lines) { foreach ($e in (ConvertFrom-SessionLine $Tool $ln.text $names)) { if ($e) { $evs.Add($e) } } }
            if ($chunks.Count -eq 0) { foreach ($k in $names.Keys) { $ToolNames[$k] = $names[$k] } }
            $chunks.Insert(0, $evs.ToArray()); $count += $evs.Count
            $scanned += ($chunkEnd - $chunkStart); $chunkEnd = $nextEnd
        }
        foreach ($c in $chunks) { foreach ($e in $c) { $events.Add($e) } }
        $list = $events.ToArray()
        if ($list.Count -gt $Max) { $list = $list[($list.Count - $Max)..($list.Count - 1)] }
        if ($null -eq $cursor) { $cursor = [long]0 }
        return @{ events = @($list); cursor = $cursor; at_end = ($cursor -ge $size) }
    }
    $r = Read-JsonlLines -Path $Path -Offset $sinceN -MaxBytes $ChunkBytes -AcceptPartialTail
    $cursor = $sinceN
    foreach ($ln in $r.lines) {
        $evs = ConvertFrom-SessionLine $Tool $ln.text $ToolNames
        if ($events.Count -gt 0 -and ($events.Count + $evs.Count) -gt $Max) { break }
        foreach ($e in $evs) { $events.Add($e) }
        $cursor = $ln.end
    }
    if ($r.lines.Count -eq 0) { $cursor = $r.next }   # skipped oversized line(s)
    @{ events = $events.ToArray(); cursor = $cursor; at_end = ($cursor -ge $r.size) }
}

# Keep tool-name maps bounded (one per watched session).
function Limit-ToolNames([hashtable]$Map, [int]$Max = 500) {
    if ($Map.Count -le $Max) { return }
    foreach ($k in @($Map.Keys | Select-Object -First ($Map.Count - $Max))) { $Map.Remove($k) }
}
