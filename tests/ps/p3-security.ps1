# Workstream S (security) PowerShell tests: S1 filtered job token (real child processes), S2 secrets files + ACL plan,
# S5 token pickup, S6 signing key / signed bundle / verification (cross-checked against the hub's Python verifier),
# S7 PIN hash (cross-checked against Python). No servers, no ports, nothing outside a scratch dir; never elevates.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File tests\ps\p3-security.ps1 [-Scratch <dir>]
# Python: $env:FORGE_TEST_PYTHON, else the forge-hub venv, else python.exe on PATH. Exit code = number of failures.
param([string]$Scratch = (Join-Path ([IO.Path]::GetTempPath()) ("forge-p3sec-" + [guid]::NewGuid().ToString('n').Substring(0, 8))))

$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
New-Item -ItemType Directory -Force $Scratch | Out-Null
$script:Pass = 0; $script:Fail = 0; $script:Failures = New-Object System.Collections.Generic.List[string]
$Utf8 = New-Object Text.UTF8Encoding($false)

function Test-Case([string]$Name, [scriptblock]$Body) {
    try { & $Body; $script:Pass++; Write-Host "  ok   $Name" -ForegroundColor Green }
    catch { $script:Fail++; $script:Failures.Add("$Name : $($_.Exception.Message)"); Write-Host "  FAIL $Name : $($_.Exception.Message)" -ForegroundColor Red }
}
function Assert-Equal($Expected, $Actual, [string]$Msg = '') {
    $e = ConvertTo-Json -InputObject $Expected -Compress -Depth 5; $a = ConvertTo-Json -InputObject $Actual -Compress -Depth 5
    if ($e -ne $a) { throw "expected $e but got $a $Msg" }
}
function Assert-True($Cond, [string]$Msg = 'condition false') { if (-not $Cond) { throw $Msg } }
function Assert-Throws([scriptblock]$Body, [string]$Like = '*') {
    $threw = $false
    try { & $Body } catch { $threw = $true; if ($_.Exception.Message -notlike $Like) { throw "threw '$($_.Exception.Message)', expected like '$Like'" } }
    if (-not $threw) { throw 'expected an exception' }
}

$Py = $env:FORGE_TEST_PYTHON
if (-not $Py) { $v = 'C:\Users\you\forge-hub\.venv\Scripts\python.exe'; if (Test-Path -LiteralPath $v) { $Py = $v } else { $Py = 'python.exe' } }
# run a snippet against hub\forge_security.py; returns stdout (trimmed)
function Invoke-HubPy([string]$Code, [string[]]$PyArgs = @()) {
    $f = Join-Path $Scratch ("py-" + [guid]::NewGuid().ToString('n').Substring(0, 6) + '.py')
    [IO.File]::WriteAllText($f, "import sys`nsys.path.insert(0, r'$Repo\hub')`n$Code`n", $Utf8)
    $out = & $Py $f @PyArgs 2>&1
    if ($LASTEXITCODE -ne 0) { throw "python failed: $out" }
    ("$out").Trim()
}
$me = [Security.Principal.WindowsIdentity]::GetCurrent()
$Elevated = (New-Object Security.Principal.WindowsPrincipal($me)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

# ---------------------------------------------------------------- S1 job token
Write-Host "S1 job token (runner elevated: $Elevated)"
& {
    . (Join-Path $Repo 'agent\jobtoken.ps1')
    $wr = Join-Path $Scratch 'work'; $jobs = Join-Path $wr 'jobs'; $scr = Join-Path $wr 'scratch'
    New-Item -ItemType Directory -Force $jobs, $scr | Out-Null

    Test-Case 'before Initialize-JobToken the status is unavailable' { Assert-Equal 'unavailable' (Get-JobTokenStatus) }
    Test-Case 'Initialize-JobToken computes a SAFER normal-user token (Medium, Administrators not enabled)' {
        $r = Initialize-JobToken
        Assert-True $r.ok "init failed: $($r.error)"; Assert-Equal 'filtered' $r.mode; Assert-Equal 'filtered' (Get-JobTokenStatus)
        Assert-True ($script:JobTokenState.detail -match '^integrity=Medium;admins=(deny-only|absent)$') "token: $($script:JobTokenState.detail)"
    }
    Test-Case 'parent token description (documents what the runner was)' {
        $self = [HivemindJobToken.Native]::DescribeSelf()
        Write-Host "         runner token: $self"
        if ($Elevated) { Assert-True ($self -match 'integrity=High;admins=enabled') "elevated runner should be High/admins enabled: $self" }
    }
    Test-Case 'filtered child: Medium integrity, Administrators deny-only, exit code, output redirection' {
        $id = 'jt-0001'; $jd = Join-Path $jobs $id; $cwd = Join-Path $scr $id; New-Item -ItemType Directory -Force $jd, $cwd | Out-Null
        $out = Join-Path $jd 'out.log'
        $p = Start-ForgeJobProcess -Arguments "/s /c `"(%SystemRoot%\System32\whoami.exe /groups & echo CWD=%CD% & ping -n 3 127.0.0.1 >nul) > `"$out`" 2>&1 & exit 7`"" -WorkingDirectory $cwd -JobDir $jd -Job @{ full_access = $false }
        Assert-True ($p -is [Diagnostics.Process]) 'not a Process'
        $null = $p.Handle
        Start-Sleep -Milliseconds 400
        Assert-Equal 'Medium' (Get-ProcessIntegrityLevel $p.Id)
        $desc = [HivemindJobToken.Native]::DescribeProcess($p.Id)
        Assert-True ($desc -match 'admins=(deny-only|absent)') "child token: $desc"
        $note = Get-JobLaunchNote $p
        Assert-True ($note -eq " as $env:USERDOMAIN\$env:USERNAME (filtered token, Medium)") "note: $note"
        Assert-True ($p.WaitForExit(15000)) 'child did not exit'
        Assert-Equal 7 $p.ExitCode
        $txt = [IO.File]::ReadAllText($out)
        Assert-True ($txt -match 'S-1-16-8192') "whoami: no Medium mandatory label: $($txt.Substring(0, [Math]::Min(300, $txt.Length)))"
        Assert-True ($txt -notmatch 'S-1-16-12288') 'whoami: High mandatory label present'
        foreach ($line in ($txt -split "`n" | Where-Object { $_ -match 'S-1-5-32-544' })) { Assert-True ($line -match 'deny only') "Administrators not deny-only: $line" }
        Assert-True ($txt -match [regex]::Escape("CWD=$cwd")) 'cwd not applied'
    }
    Test-Case 'a filtered child can write its job dir and scratch dir (Grant-JobDirAccess) but not a protected dir' {
        $id = 'jt-0002'; $jd = Join-Path $jobs $id; $cwd = Join-Path $scr $id; New-Item -ItemType Directory -Force $jd, $cwd | Out-Null
        $out = Join-Path $jd 'out.log'
        $p = Start-ForgeJobProcess -Arguments "/s /c `"echo hi> made.txt && echo ok > `"$out`" 2>&1`"" -WorkingDirectory $cwd -JobDir $jd -Job @{}
        $null = $p.Handle; Assert-True ($p.WaitForExit(15000)); Assert-Equal 0 $p.ExitCode
        Assert-True (Test-Path (Join-Path $cwd 'made.txt')) 'scratch write failed'
        Assert-True (Grant-JobDirAccess $jd) 'grant not idempotent'
        $sid = $me.User.Value
        $rules = @((New-Object IO.DirectoryInfo($jd)).GetAccessControl('Access').GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]) | Where-Object { $_.IdentityReference.Value -eq $sid -and -not $_.IsInherited })
        Assert-Equal 1 $rules.Count 'exactly one explicit ACE for the user'
        # a dir only Administrators may write: the filtered child is refused (Administrators is deny-only for it)
        $prot = Join-Path $Scratch 'admins-only'; New-Item -ItemType Directory -Force $prot | Out-Null
        & icacls.exe $prot /inheritance:r /grant:r '*S-1-5-32-544:(OI)(CI)F' "*$($sid):(OI)(CI)RX" /C /Q | Out-Null
        $p = Start-ForgeJobProcess -Arguments "/s /c `"echo x> `"$prot\x.txt`"`"" -WorkingDirectory $cwd -JobDir $jd -Job @{}
        $null = $p.Handle; Assert-True ($p.WaitForExit(15000))
        Assert-True ($p.ExitCode -ne 0 -and -not (Test-Path (Join-Path $prot 'x.txt'))) 'filtered child wrote into an Administrators-only dir'
        & icacls.exe $prot /reset /T /C /Q | Out-Null
    }
    Test-Case 'Grant-JobDirAccess is not applied to a cwd outside <work_root>\scratch' {
        $id = 'jt-0003'; $jd = Join-Path $jobs $id; $proj = Join-Path $Scratch 'project'; New-Item -ItemType Directory -Force $jd, $proj | Out-Null
        $p = Start-ForgeJobProcess -Arguments '/s /c "exit 0"' -WorkingDirectory $proj -JobDir $jd -Job @{}
        $null = $p.Handle; $null = $p.WaitForExit(15000)
        $sid = $me.User.Value
        $explicit = @((New-Object IO.DirectoryInfo($proj)).GetAccessControl('Access').GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]) | Where-Object { $_.IdentityReference.Value -eq $sid -and -not $_.IsInherited })
        Assert-Equal 0 $explicit.Count
        Assert-True (Test-PathUnder 'C:\w\scratch\a' 'C:\w\scratch') ; Assert-True (-not (Test-PathUnder 'C:\w\scratchy' 'C:\w\scratch'))
    }
    Test-Case 'Add-JobTokenReport sets metrics.job_token (hashtable and pscustomobject metrics)' {
        $b = @{ metrics = @{ cpu = 1 } }; Add-JobTokenReport $b
        Assert-Equal 'filtered' $b.metrics.job_token; Assert-True ($null -eq $b.metrics.job_token_error)
        $b2 = @{ metrics = [pscustomobject]@{ cpu = 1 } }; Add-JobTokenReport $b2; Assert-Equal 'filtered' $b2.metrics.job_token
        $b3 = @{}; Add-JobTokenReport $b3; Assert-Equal 'filtered' $b3.metrics.job_token
    }
    Test-Case 'job_token_mode=full: reported as full, full_access refused, normal jobs run with the own token' {
        $r = Initialize-JobToken -Config @{ job_token_mode = 'full' }
        Assert-Equal 'full' $r.mode; Assert-Equal 'full' (Get-JobTokenStatus)
        $b = @{ metrics = @{} }; Add-JobTokenReport $b; Assert-Equal 'full' $b.metrics.job_token; Assert-True ($b.metrics.job_token_error -like '*opt-out*')
        $jd = Join-Path $jobs 'jt-0004'; New-Item -ItemType Directory -Force $jd | Out-Null
        Assert-Throws { Start-ForgeJobProcess -Arguments '/s /c "exit 0"' -WorkingDirectory $jd -JobDir $jd -Job @{ full_access = $true } } 'refused: full access needs the filtered job token'
        $p = Start-ForgeJobProcess -Arguments '/s /c "exit 3"' -WorkingDirectory $jd -JobDir $jd -Job @{ full_access = $false }
        $null = $p.Handle; $null = $p.WaitForExit(15000); Assert-Equal 3 $p.ExitCode
        Assert-True ((Get-JobLaunchNote $null) -like '*(full token (opt-out))')
    }
    # one Medium child that lives long enough to be inspected; returns its integrity level
    function Get-NextJobIntegrity([string]$Id, $Job = @{}) {
        $jd = Join-Path $jobs $Id; New-Item -ItemType Directory -Force $jd | Out-Null
        $p = Start-ForgeJobProcess -Arguments '/s /c "ping -n 3 127.0.0.1 >nul"' -WorkingDirectory $jd -JobDir $jd -Job $Job
        $null = $p.Handle; Start-Sleep -Milliseconds 300
        $il = Get-ProcessIntegrityLevel $p.Id
        $null = $p.WaitForExit(15000)
        $il
    }
    $realFactory = $script:NewFilteredToken
    Test-Case 'S-R5 a bad cwd fails only that job: no fallback, status stays filtered, the next job is still Medium' {
        $null = Initialize-JobToken
        $jd = Join-Path $jobs 'jt-0005'; New-Item -ItemType Directory -Force $jd | Out-Null
        $notDir = Join-Path $jd 'a-file.txt'; [IO.File]::WriteAllText($notDir, 'x')
        foreach ($badCwd in @((Join-Path $Scratch 'no-such-dir'), $notDir)) {
            Assert-Throws { Start-ForgeJobProcess -Arguments '/s /c "exit 0"' -WorkingDirectory $badCwd -JobDir $jd -Job @{} } 'filtered launch failed: CreateProcessAsUser failed (error *'
            Assert-Equal 'filtered' (Get-JobTokenStatus)
        }
        Assert-Throws { Start-ForgeJobProcess -Arguments '/s /c "exit 0"' -WorkingDirectory $notDir -JobDir $jd -Job @{ full_access = $true } } 'filtered launch failed*'
        $b = @{ metrics = @{} }; Add-JobTokenReport $b; Assert-Equal 'filtered' $b.metrics.job_token
        Assert-Equal 'Medium' (Get-NextJobIntegrity 'jt-0006')
        Assert-Equal 'Medium' (Get-NextJobIntegrity 'jt-0007' @{ full_access = $true })
    }
    Test-Case 'S-R5 a broken token handle is re-created on the spot: the job still runs filtered (Medium)' {
        $null = Initialize-JobToken
        $script:JobTokenState.token = [IntPtr]0x7FFFFFF0         # simulate a broken token handle (CreateProcessAsUser fails)
        Assert-Equal 'Medium' (Get-NextJobIntegrity 'jt-0008' @{ full_access = $true })
        Assert-Equal 'filtered' (Get-JobTokenStatus)
    }
    Test-Case 'S-R5 token cannot be created: never silent, full_access refused, retried with backoff, recovers' {
        $null = Initialize-JobToken
        $script:NewFilteredToken = { throw 'SaferComputeTokenFromLevel failed (simulated)' }
        $script:JobTokenState.token = [IntPtr]0x7FFFFFF0
        $jd = Join-Path $jobs 'jt-0009'; New-Item -ItemType Directory -Force $jd | Out-Null
        Assert-Throws { Start-ForgeJobProcess -Arguments '/s /c "exit 0"' -WorkingDirectory $jd -JobDir $jd -Job @{ full_access = $true } } 'refused: full access*'
        Assert-Equal 'unavailable' (Get-JobTokenStatus)
        Assert-True ($script:JobTokenState.error -like '*simulated*') $script:JobTokenState.error
        Assert-True ([double]$script:JobTokenState.next_retry -gt (Get-JobTokenEpoch)) 'no backoff set'
        $p = Start-ForgeJobProcess -Arguments '/s /c "exit 0"' -WorkingDirectory $jd -JobDir $jd -Job @{}   # own token (policy)
        $null = $p.Handle; $null = $p.WaitForExit(15000); Assert-Equal 0 $p.ExitCode
        $b = @{ metrics = @{} }; Add-JobTokenReport $b; Assert-Equal 'unavailable' $b.metrics.job_token; Assert-True ($b.metrics.job_token_error)
        $script:JobTokenState.status = 'filtered'; $script:JobTokenState.token = [IntPtr]::Zero    # a zero handle must never mean "own token"
        Assert-Throws { Start-ForgeJobProcess -Arguments '/s /c "exit 0"' -WorkingDirectory $jd -JobDir $jd -Job @{ full_access = $true } } 'refused: full access*'
        Assert-Equal 'unavailable' (Get-JobTokenStatus)
        # SAFER works again: the next job after the backoff retries and is filtered again
        $script:NewFilteredToken = $realFactory
        Assert-True ((Get-NextJobIntegrity 'jt-0010') -in @('Medium', 'High')) 'job did not run'
        Assert-Equal 'unavailable' (Get-JobTokenStatus) 'retried before the backoff'
        $script:JobTokenState.next_retry = 0
        Assert-Equal 'Medium' (Get-NextJobIntegrity 'jt-0011' @{ full_access = $true })
        Assert-Equal 'filtered' (Get-JobTokenStatus)
    }
    $script:NewFilteredToken = $realFactory
    Test-Case 'S-R6 job_token_mode is read from data\config.json only, never from agent-config.json' {
        $d = Join-Path $Scratch 'mode\data'; New-Item -ItemType Directory -Force $d | Out-Null
        Assert-Equal 'filtered' (Get-JobTokenMode $d)
        [IO.File]::WriteAllText((Join-Path $d 'agent-config.json'), '{"job_token_mode":"full"}', $Utf8)
        Assert-Equal 'filtered' (Get-JobTokenMode $d)
        [IO.File]::WriteAllText((Join-Path $d 'config.json'), '{"node_id":"n","job_token_mode":"full"}', $Utf8)
        Assert-Equal 'full' (Get-JobTokenMode $d)
        [IO.File]::WriteAllText((Join-Path $d 'config.json'), '{"node_id":"n","job_token_mode":"filtered"}', $Utf8)
        Assert-Equal 'filtered' (Get-JobTokenMode $d)
        $agentSrc = [IO.File]::ReadAllText((Join-Path $Repo 'agent\forge-agent.ps1'))
        Assert-True ($agentSrc -match 'Get-JobTokenMode \(Split-Path -Parent \$ConfigPath\)') 'agent does not use Get-JobTokenMode'
        Assert-True ($agentSrc -notmatch 'job_token_mode = \[string\]\$cfg\.job_token_mode') 'agent still trusts agent-config.json'
        $null = Initialize-JobToken
    }
    Test-Case 'Get-ProcessIntegrityLevel: own process, unknown pid' {
        Assert-True ((Get-ProcessIntegrityLevel $PID) -in @('Medium', 'High')); Assert-True ($null -eq (Get-ProcessIntegrityLevel 999999))
    }
}

# ---------------------------------------------------------------- S2 / S5 / S7 secrets
Write-Host 'S2 secrets / S5 rotation / S7 PIN'
& {
    . (Join-Path $Repo 'agent\secrets.ps1')
    $data = Join-Path $Scratch 'fh\data'; New-Item -ItemType Directory -Force $data | Out-Null
    [IO.File]::WriteAllText((Join-Path $data 'config.json'), '{"node_id":"n1","agent_token":"ag","port":8787}', $Utf8)

    Test-Case 'Write-ForgeSecrets merges, removes $null keys, writes UTF-8 without BOM; Read-ForgeSecrets merges (secrets win)' {
        Assert-Equal (Join-Path $data 'private\secrets.json') (Write-ForgeSecrets $data @{ cluster_token = 'cl'; signing_pubkey = 'k' } -DryRun)
        Assert-True (-not (Test-Path (Join-Path $data 'private'))) 'dry run wrote'
        $p = Write-ForgeSecrets $data @{ cluster_token = 'cl'; ui_token_hash = 'sha256:x'; node_id = 'from-secrets' } -SkipAcl
        $null = Write-ForgeSecrets $data @{ admin_pin_hash = 'h'; ui_token_hash = $null } -SkipAcl
        $bytes = [IO.File]::ReadAllBytes($p); Assert-True ($bytes[0] -ne 0xEF) 'BOM written'
        $s = Read-ForgeSecrets $data
        Assert-Equal 'cl' $s.cluster_token; Assert-Equal 'h' $s.admin_pin_hash; Assert-Equal 'ag' $s.agent_token
        Assert-True (-not $s.ContainsKey('ui_token_hash')) 'null key kept'; Assert-Equal 'from-secrets' $s.node_id
        Assert-True ($null -eq (Read-ForgeSecrets (Join-Path $Scratch 'nope')))
    }
    Test-Case 'Write-ForgeSecrets protects a new private dir (Admins + SYSTEM only, inheritance off)' {
        $d2 = Join-Path $Scratch 'fh2\data'; New-Item -ItemType Directory -Force $d2 | Out-Null
        # a non-elevated runner cannot write into the dir it just protected (Administrators is deny-only for it)
        if ($Elevated) { $null = Write-ForgeSecrets $d2 @{ cluster_token = 'cl' } }
        else { Assert-Throws { Write-ForgeSecrets $d2 @{ cluster_token = 'cl' } } '*denied*' }
        $acl = (New-Object IO.DirectoryInfo((Join-Path $d2 'private'))).GetAccessControl('Access')
        Assert-True $acl.AreAccessRulesProtected 'inheritance not removed'
        $ids = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | ForEach-Object { $_.IdentityReference.Value } | Sort-Object -Unique)
        Assert-Equal @('S-1-5-18', 'S-1-5-32-544') $ids
        & icacls.exe (Join-Path $d2 'private') /grant "*$($me.User.Value):(OI)(CI)F" /T /C /Q | Out-Null   # owner may re-grant: lets cleanup work
    }
    Test-Case 'Set-ForgeDataAcl -DryRun plan: data read-only for the user, private admin-only, config.json readable, bin RX' {
        $plan = Set-ForgeDataAcl -ForgeHome 'C:\PD\Forge' -User 'BOX\u' -DryRun
        Assert-Equal 4 @($plan).Count
        Assert-Equal @('C:\PD\Forge\data', '/inheritance:r', '/grant:r', '*S-1-5-32-544:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F', 'BOX\u:(OI)(CI)RX', '/C', '/Q') @($plan[0].args)
        Assert-Equal @('C:\PD\Forge\data\private', '/inheritance:r', '/grant:r', '*S-1-5-32-544:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F', '/C', '/Q') @($plan[1].args)
        Assert-Equal @('C:\PD\Forge\data\config.json', '/inheritance:r', '/grant:r', '*S-1-5-32-544:F', '*S-1-5-18:F', 'BOX\u:R', '/C', '/Q') @($plan[2].args)
        Assert-Equal 'C:\PD\Forge\bin' $plan[3].path
        $userGrants = @($plan | ForEach-Object { $_.grant } | Where-Object { $_ -like 'BOX\u:*' })
        Assert-True (-not ($userGrants | Where-Object { $_ -match ':\((OI\)\(CI\))?(F|M)$|:\(OI\)\(CI\)(F|M)$|:(F|M|W)$' })) "user must never get write: $($userGrants -join ', ')"
        Assert-True (-not (@($plan[1].grant) -match 'BOX')) 'user on private'
        Assert-True (-not (Test-Path 'C:\PD')) 'dry run touched disk'
    }
    Test-Case 'New-ForgePinHash: format, 6 digits only, verifies in PowerShell and in the hub (Python)' {
        Assert-Throws { New-ForgePinHash '12345' } '*6 digits*'; Assert-Throws { New-ForgePinHash '12345a' } '*6 digits*'
        $h = New-ForgePinHash '482913'
        Assert-True ($h -match '^pbkdf2_sha256\$200000\$[A-Za-z0-9+/=]{24}\$[A-Za-z0-9+/=]{44}$') $h
        Assert-True (Test-ForgePinHash '482913' $h); Assert-True (-not (Test-ForgePinHash '482914' $h))
        Assert-Equal 'True False' (Invoke-HubPy "import forge_security as fs; print(fs.verify_pin('482913', sys.argv[1]), fs.verify_pin('000000', sys.argv[1]))" @($h))
        $fixed = New-ForgePinHash '654321' -Salt (New-Object byte[] 16)
        Assert-Equal $fixed (Invoke-HubPy "import forge_security as fs; print(fs.hash_pin('654321', salt=bytes(16)))")
    }
    Test-Case 'Update-AgentTokenFromResponse: new token once, ignores none / same / malformed' {
        $script:Token = 'old-token-0123456789'
        Assert-True ($null -eq (Update-AgentTokenFromResponse $null))
        Assert-True ($null -eq (Update-AgentTokenFromResponse ([pscustomobject]@{ jobs = @() })))
        $resp = '{"rotate":{"agent_token":"NEW_token-abcdefghij","grace_until":1700000600}}' | ConvertFrom-Json
        Assert-Equal 'NEW_token-abcdefghij' (Update-AgentTokenFromResponse $resp)
        $script:Token = 'NEW_token-abcdefghij'
        Assert-True ($null -eq (Update-AgentTokenFromResponse $resp)) 'same token returned again'
        Assert-True ($null -eq (Update-AgentTokenFromResponse ('{"rotate":{"agent_token":"bad token!"}}' | ConvertFrom-Json)))
    }
}

# ---------------------------------------------------------------- S6 signing
Write-Host 'S6 signed bundles'
& {
    . (Join-Path $Repo 'tools\build-bundle.ps1') -NoRun     # also loads verify-bundle.ps1 + new-signing-key.ps1
    $keyDir = Join-Path $Scratch 'key'; $key = Join-Path $keyDir 'signing-key.dpapi'; $pubPath = Join-Path $keyDir 'signing-pubkey.xml'
    $script:Pub = $null

    Test-Case 'New-ForgeSigningKey: RSA-3072, DPAPI blob (no plaintext key), refuses to overwrite without -Force' {
        $script:Pub = New-ForgeSigningKey -KeyPath $key -PubKeyPath $pubPath
        Assert-True ($script:Pub -match '^<RSAKeyValue><Modulus>[^<]+</Modulus><Exponent>AQAB</Exponent></RSAKeyValue>$')
        Assert-Equal $script:Pub ([IO.File]::ReadAllText($pubPath))
        $blob = [Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes($key))
        Assert-True ($blob -notmatch '<D>|RSAKeyValue') 'private key stored in plaintext'
        $rsa = New-Object Security.Cryptography.RSACryptoServiceProvider; $rsa.PersistKeyInCsp = $false; $rsa.FromXmlString($script:Pub)
        Assert-Equal 3072 $rsa.KeySize; $rsa.Dispose()
        Assert-Throws { New-ForgeSigningKey -KeyPath $key -PubKeyPath $pubPath } '*already exists*'
        Assert-Equal $script:Pub (Get-ForgeSigningPubKey $key)
    }
    Test-Case 'Invoke-ForgeSign signature verifies with the public key; key id matches Python' {
        $data = [Text.Encoding]::UTF8.GetBytes("hello`n")
        $sig = [Convert]::FromBase64String((Invoke-ForgeSign -Data $data -KeyPath $key))
        $rsa = New-Object Security.Cryptography.RSACryptoServiceProvider; $rsa.PersistKeyInCsp = $false; $rsa.FromXmlString($script:Pub)
        Assert-True ($rsa.VerifyData($data, 'SHA256', $sig)); $rsa.Dispose()
        Assert-Equal (Get-ForgeKeyId $script:Pub) (Invoke-HubPy "import forge_security as fs; print(fs.key_id(open(sys.argv[1], encoding='utf-8').read()))" @($pubPath))
    }

    $dist = Join-Path $Scratch 'dist'; $rel = Join-Path $Scratch 'rel'
    Test-Case 'build-bundle -Sign: bundle.sig at the zip root, extracted release verifies (PowerShell and Python)' {
        $b = New-ForgeBundle -Root $Repo -OutDir $dist -Sign -SigningKey $key
        Assert-True $b.signed
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $z = [IO.Compression.ZipFile]::OpenRead($b.path); $names = @($z.Entries | ForEach-Object { $_.FullName }); $z.Dispose()
        Assert-True ($names -contains 'bundle.sig') 'no bundle.sig'; Assert-Equal ($b.files + 1) $names.Count
        [IO.Compression.ZipFile]::ExtractToDirectory($b.path, $rel)
        $sig = [IO.File]::ReadAllText((Join-Path $rel 'bundle.sig')) | ConvertFrom-Json
        Assert-Equal 1 $sig.v; Assert-Equal $b.version $sig.version; Assert-Equal (Get-ForgeKeyId $script:Pub) $sig.key_id
        $t = Test-BundleSignature -ReleaseDir $rel -PubKeyXml $script:Pub
        Assert-True $t.ok $t.reason; Assert-True $t.signed
        Assert-Equal 'ok' (Invoke-HubPy "import forge_security as fs; fs.verify_release(sys.argv[1], open(sys.argv[2], encoding='utf-8').read()); print('ok')" @($rel, $pubPath))
    }
    Test-Case 'PowerShell and Python manifests are byte-identical (incl. ignored bundle.zip / venv / __pycache__)' {
        [IO.File]::WriteAllText((Join-Path $rel 'bundle.zip'), 'z', $Utf8)
        New-Item -ItemType Directory -Force (Join-Path $rel 'venv\Lib'), (Join-Path $rel 'hub\__pycache__') | Out-Null
        [IO.File]::WriteAllText((Join-Path $rel 'venv\Lib\x.py'), 'x', $Utf8); [IO.File]::WriteAllText((Join-Path $rel 'hub\__pycache__\a.pyc'), 'c', $Utf8)
        $ps = Get-Sha256Hex ([Text.Encoding]::UTF8.GetBytes((Get-BundleManifest $rel)))
        Assert-Equal $ps (Invoke-HubPy "import hashlib, forge_security as fs; print(hashlib.sha256(fs.bundle_manifest(sys.argv[1])).hexdigest())" @($rel))
        $m = Get-BundleManifest $rel
        Assert-True ($m -notmatch 'bundle\.(sig|zip)|venv/|__pycache__') 'excluded path in manifest'
        Assert-True ($m -match "(?m)^[0-9a-f]{64} hub/app\.py$") 'hub/app.py line missing'
    }
    Test-Case 'S-R4 a signed release carrying __pycache__ / *.pyc / *.pyo / a venv/ zip entry is refused' {
        $t = Test-BundleSignature -ReleaseDir $rel -PubKeyXml $script:Pub
        Assert-True (-not $t.ok -and $t.signed) 'pycache accepted'; Assert-Equal 'bundle contains unsigned code: hub/__pycache__' $t.reason
        Remove-Item -LiteralPath (Join-Path $rel 'hub\__pycache__') -Recurse -Force
        Assert-True (Test-BundleSignature -ReleaseDir $rel -PubKeyXml $script:Pub).ok 'dir venv (R5, after verification) / unreadable zip broke it'
        foreach ($f in 'hub\app.pyc', 'agent\X.PYO') {
            [IO.File]::WriteAllText((Join-Path $rel $f), 'c', $Utf8)
            $t = Test-BundleSignature -ReleaseDir $rel -PubKeyXml $script:Pub
            Assert-True (-not $t.ok -and $t.reason -like 'bundle contains unsigned code: *') "$f accepted: $($t.reason)"
            Remove-Item -LiteralPath (Join-Path $rel $f) -Force
        }
        Remove-Item -LiteralPath (Join-Path $rel 'bundle.zip') -Force
        $zf = [IO.Compression.ZipFile]::Open((Join-Path $rel 'bundle.zip'), 'Create')
        try { $null = [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zf, (Join-Path $rel 'VERSION'), 'venv/Lib/site-packages/evil.pth') } finally { $zf.Dispose() }
        $t = Test-BundleSignature -ReleaseDir $rel -PubKeyXml $script:Pub
        Assert-Equal 'bundle contains unsigned code: bundle.zip:venv/Lib/site-packages/evil.pth' $t.reason
        Assert-True (Test-BundleSignature -ReleaseDir $rel -PubKeyXml $null).ok 'no key pinned must stay 2.2 behaviour'
        Remove-Item -LiteralPath (Join-Path $rel 'bundle.zip'), (Join-Path $rel 'venv') -Recurse -Force
        Assert-True (Test-BundleSignature -ReleaseDir $rel -PubKeyXml $script:Pub).ok
    }
    Test-Case 'Test-BundleSignature: tampered file, wrong key, unsigned, no key pinned' {
        $other = Join-Path $keyDir 'other.dpapi'; $otherPub = New-ForgeSigningKey -KeyPath $other -PubKeyPath (Join-Path $keyDir 'other.xml')
        $bad = Test-BundleSignature -ReleaseDir $rel -PubKeyXml $otherPub
        Assert-True (-not $bad.ok); Assert-True ($bad.reason -like 'bundle signature invalid (signed with key*') $bad.reason
        $f = Join-Path $rel 'agent\forge-agent.ps1'; $orig = [IO.File]::ReadAllBytes($f)
        [IO.File]::AppendAllText($f, "`n# tampered", $Utf8)
        $t = Test-BundleSignature -ReleaseDir $rel -PubKeyXml $script:Pub
        Assert-True (-not $t.ok -and $t.reason -like '*files differ*') $t.reason
        [IO.File]::WriteAllBytes($f, $orig)
        Assert-True (Test-BundleSignature -ReleaseDir $rel -PubKeyXml $script:Pub).ok
        Move-Item (Join-Path $rel 'bundle.sig') (Join-Path $rel 'bundle.sig.off')
        $u = Test-BundleSignature -ReleaseDir $rel -PubKeyXml $script:Pub
        Assert-True (-not $u.ok -and -not $u.signed -and $u.reason -eq 'bundle not signed')
        $n = Test-BundleSignature -ReleaseDir $rel -PubKeyXml $null
        Assert-True ($n.ok -and $n.reason -eq 'no signing key pinned')
        Move-Item (Join-Path $rel 'bundle.sig.off') (Join-Path $rel 'bundle.sig')
    }
    Test-Case 'unsigned build still works (no -Sign) and has no bundle.sig' {
        $b = New-ForgeBundle -Root $Repo -OutDir (Join-Path $Scratch 'dist2')
        Assert-True (-not $b.signed)
        $z = [IO.Compression.ZipFile]::OpenRead($b.path); $has = @($z.Entries | Where-Object { $_.FullName -eq 'bundle.sig' }).Count; $z.Dispose()
        Assert-Equal 0 $has
    }
    Test-Case 'Get-SigningPubKey / Merge-WdSecrets read data\private\secrets.json' {
        $data = Join-Path $Scratch 'wd\data'; New-Item -ItemType Directory -Force (Join-Path $data 'private') | Out-Null
        Assert-True ($null -eq (Get-SigningPubKey $data))
        $cfg = '{"node_id":"n1","cluster_token":"old"}' | ConvertFrom-Json
        Assert-Equal 'old' (Merge-WdSecrets $cfg $data).cluster_token
        [IO.File]::WriteAllText((Join-Path $data 'private\secrets.json'), (ConvertTo-Json -InputObject @{ cluster_token = 'rotated'; signing_pubkey = $script:Pub }), $Utf8)
        Assert-Equal $script:Pub (Get-SigningPubKey $data)
        $m = Merge-WdSecrets $cfg $data
        Assert-Equal 'rotated' $m.cluster_token; Assert-Equal $script:Pub $m.signing_pubkey; Assert-Equal 'n1' $m.node_id
    }
}

# ---------------------------------------------------------------- S-R7 rollback layout, S-R9 PIN reset tool
Write-Host 'S-R7 rollback to 2.2 / S-R9 PIN reset'
& {
    . (Join-Path $Repo 'watchdog\verify-bundle.ps1')
    Test-Case 'S-R7 2.3 migration -> Restore-Pre23Layout -> a 2.2 hub finds its tokens and DB; 2.3 migrates again' {
        $data = Join-Path $Scratch 'rb\data'; New-Item -ItemType Directory -Force (Join-Path $data 'private') | Out-Null
        [IO.File]::WriteAllText((Join-Path $data 'config.json'), '{"node_id":"n1","ui_token":"ui-22","agent_token":"ag-22","cluster_token":"cl-22","port":8787,"seed_peers":[{"id":"a","priority":1,"urls":["http://a:8787"]}]}', $Utf8)
        $mig = "import sqlite3, json, forge_security as fs; from pathlib import Path; d = Path(sys.argv[1])`n" +
               "c = fs.load_config(d); db = fs.db_path(d); con = sqlite3.connect(str(db))`n" +
               "con.execute('CREATE TABLE IF NOT EXISTS t(x)'); con.execute('INSERT INTO t VALUES (1)'); con.commit(); con.close()`n" +
               "print(json.dumps({'db': db.relative_to(d).as_posix(), 'cluster': c.get('cluster_token'), 'ui': c.get('ui_token'), 'hash': c.get('ui_token_hash')}))"
        $r1 = Invoke-HubPy $mig @($data) | ConvertFrom-Json
        Assert-Equal 'private/forge.db' $r1.db
        $cfg23 = [IO.File]::ReadAllText((Join-Path $data 'config.json')) | ConvertFrom-Json
        Assert-True (-not $cfg23.ui_token -and -not $cfg23.cluster_token) 'migration left secrets in config.json'
        # the leader rotated the cluster token while 2.3 ran
        $sp = Join-Path $data 'private\secrets.json'; $sec = [IO.File]::ReadAllText($sp) | ConvertFrom-Json
        $sec.cluster_token = 'cl-rotated'; [IO.File]::WriteAllText($sp, (ConvertTo-Json -InputObject $sec), $Utf8)
        $dry = Restore-Pre23Layout $data -DryRun
        Assert-Equal 3 $dry.Count ($dry -join '; ')
        Assert-True (Test-Path (Join-Path $data 'private\forge.db')) 'dry run moved the DB'
        $acts = Restore-Pre23Layout $data
        Assert-Equal ($dry -join '|') ($acts -join '|')
        $cfg22 = [IO.File]::ReadAllText((Join-Path $data 'config.json')) | ConvertFrom-Json
        Assert-Equal 'ui-22' $cfg22.ui_token; Assert-Equal 'cl-rotated' $cfg22.cluster_token; Assert-Equal 'ag-22' $cfg22.agent_token
        Assert-Equal 1 @($cfg22.seed_peers).Count; Assert-Equal 'http://a:8787' @($cfg22.seed_peers)[0].urls[0]
        Assert-True ((Test-Path (Join-Path $data 'forge.db')) -and -not (Test-Path (Join-Path $data 'private\forge.db'))) 'DB not moved back'
        Assert-True ([IO.File]::ReadAllBytes((Join-Path $data 'config.json'))[0] -ne 0xEF) 'BOM written'
        # what a 2.2 hub does: tokens from config.json, data\forge.db - same DB rows
        Assert-Equal '1' (Invoke-HubPy "import sqlite3; print(sqlite3.connect(sys.argv[1]).execute('SELECT count(*) FROM t').fetchone()[0])" @((Join-Path $data 'forge.db')))
        # a later 2.3 start migrates again, idempotently, with the current tokens
        $r2 = Invoke-HubPy $mig @($data) | ConvertFrom-Json
        Assert-Equal 'private/forge.db' $r2.db; Assert-Equal 'cl-rotated' $r2.cluster; Assert-Equal $r1.hash $r2.hash
        Assert-Equal 0 (Restore-Pre23Layout (Join-Path $Scratch 'rb-none')).Count
    }
    Test-Case 'S-R7 watchdog rollback to a pre-2.3 release calls Restore-Pre23Layout after stopping the hub' {
        $wd = [IO.File]::ReadAllText((Join-Path $Repo 'watchdog\watchdog.ps1'))
        Assert-True ($wd -match "(?s)Compare-SemVer \`$from '2\.3\.0'\) -lt 0.{0,400}Stop-ScheduledTask -TaskName 'ForgeHub'.{0,120}Stop-PortListeners.{0,120}Restore-Pre23Layout \`$DataDir") 'hook missing'
    }
    Test-Case 'S-R9 reset-admin-pin.ps1 writes data\private\pin-reset.json (scratch ForgeHome), refuses without private' {
        $fh = Join-Path $Scratch 'pinreset'; New-Item -ItemType Directory -Force (Join-Path $fh 'data\private') | Out-Null
        $tool = Join-Path $Repo 'tools\reset-admin-pin.ps1'
        $dry = & $tool -ForgeHome $fh -DryRun 6>$null
        Assert-True (-not (Test-Path (Join-Path $fh 'data\private\pin-reset.json'))) 'dry run wrote'
        $out = & $tool -ForgeHome $fh 6>$null
        $f = Join-Path $fh 'data\private\pin-reset.json'
        Assert-Equal $f $out; Assert-True ((([IO.File]::ReadAllText($f)) | ConvertFrom-Json).requested -gt 0)
        Assert-Equal 'pin-reset.json' (Invoke-HubPy "import forge_security as fs; print(fs.PIN_RESET_FILE)")
        Assert-Throws { & $tool -ForgeHome (Join-Path $Scratch 'nofh') } '*no 2.3 secret store*'
    }
}

Remove-Item -LiteralPath $Scratch -Recurse -Force -ErrorAction SilentlyContinue
Write-Host ''
Write-Host "$($script:Pass) passed, $($script:Fail) failed" -ForegroundColor $(if ($script:Fail) { 'Red' } else { 'Green' })
foreach ($m in $script:Failures) { Write-Host "  - $m" -ForegroundColor Red }
exit $script:Fail
