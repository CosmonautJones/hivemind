# Hivemind 2.3 - least-privilege job launch (workstream S, item S1). CONTRACT-phase3.md section 6.1.
# Dot-sourced by agent\forge-agent.ps1 (both lanes; only the user lane calls it). Functions only, no side effects
# at dot-source time. PowerShell 5.1 compatible.
#
# The user-lane agent runs as an S4U task, and S4U tokens are NOT UAC-filtered: they keep High integrity with
# Administrators enabled. User-lane jobs are therefore started with a SAFER "normal user" token derived from the agent's
# own token: SaferCreateLevel(SAFER_SCOPEID_USER, SAFER_LEVELID_NORMALUSER) -> SaferComputeTokenFromLevel ->
# integrity label forced to Medium -> CreateProcessAsUser. Same user SID (profile, credentials, ~/.claude all work),
# Administrators becomes deny-only, admin privileges are removed. Stdout/stderr redirection stays in cmd.exe
# (`cmd /s /c "<inner> > out.log 2>&1"`), so no handles are inherited.
# A restricted version of the caller's own token needs no SeAssignPrimaryTokenPrivilege, so this works from the
# non-elevated test runner as well (the child is then Medium with Administrators deny-only, like the parent).
#
# Failure policy (SPEC S1): if the SAFER APIs fail, full_access jobs are refused, normal jobs still run with the
# agent's own token, and the report says job_token = 'unavailable' (the hub raises an error alert). Never silent.
# A failed LAUNCH with a good token (bad cwd, AV block, ...) fails that one job only: no fallback, no demotion (S-R5).
# While the token is unavailable, every job start retries creating it (at most every $JobTokenRetrySec seconds).
#
# job_token_mode ("full" = explicit opt-out) is read from data\config.json only (Get-JobTokenMode): agent-config.json
# is not trusted for it, because a filtered job must never be able to opt the node out (S-R6).

$script:JobTokenState = @{ initialized = $false; mode = 'filtered'; status = 'unavailable'; error = 'not initialized'; token = [IntPtr]::Zero; detail = $null; next_retry = 0.0 }
$script:JobTokenRetrySec = 60
# Test seam: how the filtered token is made (tests swap it to simulate a SAFER failure). Returns a token handle.
$script:NewFilteredToken = { [HivemindJobToken.Native]::CreateFilteredToken() }

$JobTokenSource = @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Text;

namespace HivemindJobToken {
    public static class Native {
        const uint SAFER_SCOPEID_USER = 2;
        const uint SAFER_LEVELID_NORMALUSER = 0x20000;
        const uint SAFER_LEVEL_OPEN = 1;
        const uint TOKEN_QUERY = 0x0008;
        const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;
        const int TokenGroups = 2;
        const int TokenIntegrityLevel = 25;
        const uint SE_GROUP_INTEGRITY = 0x20;
        const uint SE_GROUP_ENABLED = 0x4;
        const uint SE_GROUP_USE_FOR_DENY_ONLY = 0x10;
        const uint CREATE_SUSPENDED = 0x4;
        const uint CREATE_NEW_CONSOLE = 0x10;
        const int STARTF_USESHOWWINDOW = 1;

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        struct STARTUPINFO {
            public int cb; public string lpReserved; public string lpDesktop; public string lpTitle;
            public int dwX; public int dwY; public int dwXSize; public int dwYSize; public int dwXCountChars;
            public int dwYCountChars; public int dwFillAttribute; public int dwFlags; public short wShowWindow;
            public short cbReserved2; public IntPtr lpReserved2; public IntPtr hStdInput; public IntPtr hStdOutput;
            public IntPtr hStdError;
        }
        [StructLayout(LayoutKind.Sequential)]
        struct PROCESS_INFORMATION { public IntPtr hProcess; public IntPtr hThread; public int dwProcessId; public int dwThreadId; }
        [StructLayout(LayoutKind.Sequential)]
        struct SID_AND_ATTRIBUTES { public IntPtr Sid; public uint Attributes; }
        [StructLayout(LayoutKind.Sequential)]
        struct TOKEN_MANDATORY_LABEL { public SID_AND_ATTRIBUTES Label; }

        [DllImport("advapi32.dll", SetLastError = true)]
        static extern bool SaferCreateLevel(uint scopeId, uint levelId, uint openFlags, out IntPtr levelHandle, IntPtr reserved);
        [DllImport("advapi32.dll", SetLastError = true)]
        static extern bool SaferComputeTokenFromLevel(IntPtr levelHandle, IntPtr inToken, out IntPtr outToken, uint flags, IntPtr reserved);
        [DllImport("advapi32.dll", SetLastError = true)]
        static extern bool SaferCloseLevel(IntPtr levelHandle);
        [DllImport("advapi32.dll", SetLastError = true)]
        static extern bool SetTokenInformation(IntPtr token, int infoClass, IntPtr info, int infoLength);
        [DllImport("advapi32.dll", SetLastError = true)]
        static extern bool GetTokenInformation(IntPtr token, int infoClass, IntPtr info, int infoLength, out int returnLength);
        [DllImport("advapi32.dll", SetLastError = true)]
        static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
        [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        static extern bool ConvertStringSidToSid(string sid, out IntPtr psid);
        [DllImport("advapi32.dll", SetLastError = true)]
        static extern int GetLengthSid(IntPtr psid);
        [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        static extern bool CreateProcessAsUser(IntPtr token, string app, StringBuilder cmdLine, IntPtr procAttr,
            IntPtr threadAttr, bool inheritHandles, uint flags, IntPtr env, string cwd, ref STARTUPINFO si,
            out PROCESS_INFORMATION pi);
        [DllImport("kernel32.dll", SetLastError = true)]
        static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
        [DllImport("kernel32.dll", SetLastError = true)]
        static extern uint ResumeThread(IntPtr thread);
        [DllImport("kernel32.dll", SetLastError = true)]
        static extern bool TerminateProcess(IntPtr process, uint code);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool CloseHandle(IntPtr h);
        [DllImport("kernel32.dll")]
        static extern IntPtr LocalFree(IntPtr h);

        static Exception Win32(string what) { return new Win32Exception(Marshal.GetLastWin32Error(), what + " failed"); }

        // SAFER normal-user token from this process's token, integrity forced to Medium. Caller closes it.
        public static IntPtr CreateFilteredToken() {
            IntPtr level;
            if (!SaferCreateLevel(SAFER_SCOPEID_USER, SAFER_LEVELID_NORMALUSER, SAFER_LEVEL_OPEN, out level, IntPtr.Zero))
                throw Win32("SaferCreateLevel");
            IntPtr token;
            try {
                if (!SaferComputeTokenFromLevel(level, IntPtr.Zero, out token, 0, IntPtr.Zero))
                    throw Win32("SaferComputeTokenFromLevel");
            } finally { SaferCloseLevel(level); }
            try { SetIntegrity(token, "S-1-16-8192"); }
            catch { CloseHandle(token); throw; }
            return token;
        }

        static void SetIntegrity(IntPtr token, string sidString) {
            IntPtr sid;
            if (!ConvertStringSidToSid(sidString, out sid)) throw Win32("ConvertStringSidToSid");
            try {
                TOKEN_MANDATORY_LABEL tml = new TOKEN_MANDATORY_LABEL();
                tml.Label.Sid = sid; tml.Label.Attributes = SE_GROUP_INTEGRITY;
                int size = Marshal.SizeOf(typeof(TOKEN_MANDATORY_LABEL)) + GetLengthSid(sid);
                IntPtr buf = Marshal.AllocHGlobal(size);
                try {
                    Marshal.StructureToPtr(tml, buf, false);
                    if (!SetTokenInformation(token, TokenIntegrityLevel, buf, size)) throw Win32("SetTokenInformation(integrity)");
                } finally { Marshal.FreeHGlobal(buf); }
            } finally { LocalFree(sid); }
        }

        static IntPtr Query(IntPtr token, int cls) {
            int len;
            GetTokenInformation(token, cls, IntPtr.Zero, 0, out len);
            if (len <= 0) throw Win32("GetTokenInformation");
            IntPtr buf = Marshal.AllocHGlobal(len);
            if (!GetTokenInformation(token, cls, buf, len, out len)) { Marshal.FreeHGlobal(buf); throw Win32("GetTokenInformation"); }
            return buf;
        }

        public static string IntegrityName(int rid) {
            if (rid >= 0x4000) return "System";
            if (rid >= 0x3000) return "High";
            if (rid >= 0x2000) return "Medium";
            if (rid >= 0x1000) return "Low";
            return "Untrusted";
        }

        public static int IntegrityRid(IntPtr token) {
            IntPtr buf = Query(token, TokenIntegrityLevel);
            try {
                TOKEN_MANDATORY_LABEL tml = (TOKEN_MANDATORY_LABEL)Marshal.PtrToStructure(buf, typeof(TOKEN_MANDATORY_LABEL));
                string s = new SecurityIdentifier(tml.Label.Sid).Value;
                return Convert.ToInt32(s.Substring(s.LastIndexOf('-') + 1));
            } finally { Marshal.FreeHGlobal(buf); }
        }

        // "enabled" | "deny-only" | "absent" | "disabled" for BUILTIN\Administrators in a token
        public static string AdminsState(IntPtr token) {
            IntPtr buf = Query(token, TokenGroups);
            try {
                int count = Marshal.ReadInt32(buf);
                int size = Marshal.SizeOf(typeof(SID_AND_ATTRIBUTES));
                IntPtr first = new IntPtr(buf.ToInt64() + IntPtr.Size);   // DWORD count, padded to pointer alignment
                for (int i = 0; i < count; i++) {
                    SID_AND_ATTRIBUTES sa = (SID_AND_ATTRIBUTES)Marshal.PtrToStructure(new IntPtr(first.ToInt64() + (long)i * size), typeof(SID_AND_ATTRIBUTES));
                    if (new SecurityIdentifier(sa.Sid).Value != "S-1-5-32-544") continue;
                    if ((sa.Attributes & SE_GROUP_USE_FOR_DENY_ONLY) != 0) return "deny-only";
                    return (sa.Attributes & SE_GROUP_ENABLED) != 0 ? "enabled" : "disabled";
                }
                return "absent";
            } finally { Marshal.FreeHGlobal(buf); }
        }

        public static string Describe(IntPtr token) {
            return "integrity=" + IntegrityName(IntegrityRid(token)) + ";admins=" + AdminsState(token);
        }

        public static string DescribeSelf() {
            IntPtr t;
            if (!OpenProcessToken(System.Diagnostics.Process.GetCurrentProcess().Handle, TOKEN_QUERY, out t)) throw Win32("OpenProcessToken");
            try { return Describe(t); } finally { CloseHandle(t); }
        }

        // null when the process cannot be opened (gone, or protected)
        public static string DescribeProcess(int pid) {
            IntPtr p = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid);
            if (p == IntPtr.Zero) return null;
            try {
                IntPtr t;
                if (!OpenProcessToken(p, TOKEN_QUERY, out t)) return null;
                try { return Describe(t); } finally { CloseHandle(t); }
            } finally { CloseHandle(p); }
        }

        public static string ProcessIntegrity(int pid) {
            IntPtr p = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid);
            if (p == IntPtr.Zero) return null;
            try {
                IntPtr t;
                if (!OpenProcessToken(p, TOKEN_QUERY, out t)) return null;
                try { return IntegrityName(IntegrityRid(t)); } finally { CloseHandle(t); }
            } finally { CloseHandle(p); }
        }

        // Start <app> <cmdLine> with the token (hidden new console, like Start-Process -WindowStyle Hidden), suspended
        // until the caller has a Process object with an open handle, so ExitCode stays readable even for instant exits.
        public static System.Diagnostics.Process Start(IntPtr token, string app, string cmdLine, string cwd) {
            if (token == IntPtr.Zero) throw new ArgumentException("no filtered token (a null token would mean the caller's own token)");
            STARTUPINFO si = new STARTUPINFO();
            si.cb = Marshal.SizeOf(typeof(STARTUPINFO));
            si.dwFlags = STARTF_USESHOWWINDOW; si.wShowWindow = 0;   // SW_HIDE
            PROCESS_INFORMATION pi;
            if (!CreateProcessAsUser(token, app, new StringBuilder(cmdLine), IntPtr.Zero, IntPtr.Zero, false,
                                     CREATE_SUSPENDED | CREATE_NEW_CONSOLE, IntPtr.Zero, cwd, ref si, out pi))
                throw Win32("CreateProcessAsUser");
            System.Diagnostics.Process proc = null;
            try {
                proc = System.Diagnostics.Process.GetProcessById(pi.dwProcessId);
                IntPtr keep = proc.Handle;       // opened now: survives the exit for ExitCode
                if (ResumeThread(pi.hThread) == 0xFFFFFFFF) throw Win32("ResumeThread");
                return proc;
            } catch {
                TerminateProcess(pi.hProcess, 1);
                throw;
            } finally {
                CloseHandle(pi.hThread); CloseHandle(pi.hProcess);
            }
        }
    }
}
'@

function Add-JobTokenType {
    if (-not ('HivemindJobToken.Native' -as [type])) { Add-Type -TypeDefinition $JobTokenSource -Language CSharp -ErrorAction Stop }
}

function Get-JobTokenEpoch { [double][DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }

# 'full' | 'filtered' from <DataDir>\config.json "job_token_mode" (Administrators + SYSTEM write, user read only).
# Never from agent-config.json (S-R6). Missing / unreadable file -> 'filtered'.
function Get-JobTokenMode([string]$DataDir) {
    try {
        $c = [IO.File]::ReadAllText((Join-Path $DataDir 'config.json')) | ConvertFrom-Json
        if ("$($c.job_token_mode)" -eq 'full') { return 'full' }
    } catch {}
    'filtered'
}

# Prepare the filtered token once per agent process. Returns @{ ok = [bool]; mode = 'filtered'|'full'; error = [string] }
# (mode = what jobs will run with). Never throws. -Config job_token_mode "full" (from Get-JobTokenMode) is the opt-out.
function Initialize-JobToken {
    param([hashtable]$Config = @{})
    $st = $script:JobTokenState
    if ($st.token -ne [IntPtr]::Zero) { try { $null = [HivemindJobToken.Native]::CloseHandle($st.token) } catch {} ; $st.token = [IntPtr]::Zero }
    $st.initialized = $true; $st.detail = $null
    $st.mode = if ($Config -and "$($Config.job_token_mode)" -eq 'full') { 'full' } else { 'filtered' }
    if ($st.mode -eq 'full') {
        $st.status = 'full'; $st.error = 'job_token_mode is "full" in data\config.json (opt-out)'
        return @{ ok = $false; mode = 'full'; error = $st.error }
    }
    try {
        Add-JobTokenType
        $t = & $script:NewFilteredToken
        $d = [HivemindJobToken.Native]::Describe($t)
        if ($d -notmatch 'integrity=(Medium|Low)' -or $d -match 'admins=enabled') {
            $null = [HivemindJobToken.Native]::CloseHandle($t)
            throw "computed token is not filtered ($d)"
        }
        $st.token = $t; $st.status = 'filtered'; $st.error = $null; $st.detail = $d; $st.next_retry = 0.0
        @{ ok = $true; mode = 'filtered'; error = $null }
    } catch {
        $st.status = 'unavailable'; $st.error = "$($_.Exception.Message)"
        $st.next_retry = (Get-JobTokenEpoch) + $script:JobTokenRetrySec
        @{ ok = $false; mode = 'full'; error = $st.error }
    }
}

# $true when the held token handle is still a usable filtered token (Medium/Low, Administrators not enabled).
function Test-JobTokenHandle {
    $t = $script:JobTokenState.token
    if ($t -eq [IntPtr]::Zero) { return $false }
    try {
        $d = [HivemindJobToken.Native]::Describe($t)
        ($d -match 'integrity=(Medium|Low)') -and ($d -notmatch 'admins=enabled')
    } catch { $false }
}

# Innermost exception message (+ Win32 error code and text) of a caught error record.
function Get-JobLaunchError($ErrorRecord) {
    $e = $ErrorRecord.Exception
    while ($e.InnerException) { $e = $e.InnerException }
    $m = "$($e.Message)"
    if ($e -is [ComponentModel.Win32Exception]) { $m += " (error $($e.NativeErrorCode): $((New-Object ComponentModel.Win32Exception($e.NativeErrorCode)).Message))" }
    $m
}

# 'filtered' | 'full' | 'unavailable' : what the NEXT user-lane job will run with. Reported as metrics.job_token.
function Get-JobTokenStatus { [string]$script:JobTokenState.status }

# Adds metrics.job_token (+ metrics.job_token_error) to a user-lane report body.
function Add-JobTokenReport([hashtable]$Body) {
    if (-not $Body) { return }
    if ($null -eq $Body.metrics) { $Body.metrics = @{} }
    $err = $script:JobTokenState.error
    if ((Get-JobTokenStatus) -eq 'filtered') { $err = $null }
    if ($Body.metrics -is [hashtable]) { $Body.metrics['job_token'] = (Get-JobTokenStatus); $Body.metrics['job_token_error'] = $err }
    else {
        $Body.metrics | Add-Member -NotePropertyName job_token -NotePropertyValue (Get-JobTokenStatus) -Force
        $Body.metrics | Add-Member -NotePropertyName job_token_error -NotePropertyValue $err -Force
    }
}

# Grant the job's user SID Modify (inherited by new files) on a job dir / scratch dir so a filtered process can write
# out.log and its files. Idempotent. Returns $true on success. Never called for arbitrary project cwds (see
# Start-ForgeJobProcess): granting the user Modify on e.g. the Forge code dir would be an escalation path.
function Grant-JobDirAccess([string]$Path) {
    try {
        if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Container)) { return $false }
        $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
        $di = New-Object IO.DirectoryInfo($Path)
        $acl = $di.GetAccessControl([Security.AccessControl.AccessControlSections]::Access)
        $want = [Security.AccessControl.FileSystemRights]::Modify
        $inh = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
        foreach ($r in $acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier])) {
            if ($r.IdentityReference -eq $sid -and $r.AccessControlType -eq 'Allow' -and (($r.FileSystemRights -band $want) -eq $want) -and (($r.InheritanceFlags -band $inh) -eq $inh)) { return $true }
        }
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, ($want -bor [Security.AccessControl.FileSystemRights]::Synchronize), $inh, [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Allow)
        $acl.AddAccessRule($rule)
        $di.SetAccessControl($acl)
        $true
    } catch { $false }
}

function Test-PathUnder([string]$Path, [string]$Root) {
    if (-not $Path -or -not $Root) { return $false }
    try {
        $p = [IO.Path]::GetFullPath($Path).TrimEnd('\') + '\'
        $r = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
        $p.StartsWith($r, [StringComparison]::OrdinalIgnoreCase)
    } catch { $false }
}

# Start one user-lane job process. Returns a [System.Diagnostics.Process] (handle already open).
#   -Arguments        : cmd.exe argument string, e.g. /s /c "<inner> > "<out.log>" 2>&1" (redirection is done by cmd)
#   -WorkingDirectory : job cwd
#   -JobDir           : <work_root>\jobs\<id> (prompt.txt, out.log live here)
#   -Job              : the dispatch item (full_access, engine, ...)
# Throws (message shown in the job output) when the job must not run: full_access without the filtered token.
function Start-ForgeJobProcess {
    param([string]$Arguments, [string]$WorkingDirectory, [string]$JobDir, $Job)
    $st = $script:JobTokenState
    if (-not $st.initialized) { $null = Initialize-JobToken }
    $full = [bool]($Job -and $Job.full_access)
    # unavailable is not forever: retry (with backoff) so one transient SAFER failure does not demote the node
    if ($st.status -eq 'unavailable' -and $st.mode -ne 'full' -and (Get-JobTokenEpoch) -ge [double]$st.next_retry) {
        $null = Initialize-JobToken -Config @{ job_token_mode = $st.mode }
    }
    if ((Get-JobTokenStatus) -eq 'filtered') {
        $null = Grant-JobDirAccess $JobDir
        # <work_root>\scratch\<id> (the agent's own per-job dir) is granted too; project cwds are the user's already
        $workRoot = if ($JobDir) { Split-Path -Parent (Split-Path -Parent $JobDir) } else { $null }
        if ($workRoot -and (Test-PathUnder $WorkingDirectory (Join-Path $workRoot 'scratch'))) { $null = Grant-JobDirAccess $WorkingDirectory }
        $comspec = if ($env:ComSpec -and (Test-Path -LiteralPath $env:ComSpec)) { $env:ComSpec } else { Join-Path $env:SystemRoot 'System32\cmd.exe' }
        for ($attempt = 0; $attempt -lt 2; $attempt++) {
            try {
                return [HivemindJobToken.Native]::Start($st.token, $comspec, "`"$comspec`" $Arguments", $WorkingDirectory)
            } catch {
                $why = Get-JobLaunchError $_
                # the token is fine: THIS launch failed (bad cwd, AV block, ...). Fail this job only, never fall back.
                if (Test-JobTokenHandle) { throw "filtered launch failed: $why" }
                # the token itself is broken: report it (job_token -> unavailable, hub alert), re-create it once, retry
                $st.status = 'unavailable'; $st.error = "filtered token invalid: $why"
                if ($attempt -eq 0) {
                    $r = Initialize-JobToken -Config @{ job_token_mode = $st.mode }
                    if ($r.ok) { continue }
                }
                break
            }
        }
    }
    # no filtered token (opt-out, or it cannot be created right now): never silently elevate a full-access job
    if ($full) { throw 'refused: full access needs the filtered job token' }
    Start-Process -FilePath cmd.exe -ArgumentList $Arguments -WorkingDirectory $WorkingDirectory -WindowStyle Hidden -PassThru
}

# 'Untrusted'|'Low'|'Medium'|'High'|'System'|$null for a pid (acceptance test: user-lane jobs report Medium).
function Get-ProcessIntegrityLevel([int]$ProcessId) {
    try { Add-JobTokenType; [HivemindJobToken.Native]::ProcessIntegrity($ProcessId) } catch { $null }
}

# Suffix for the job's first output line: " as <user> (filtered token, Medium)" (CONTRACT section 5 S1).
function Get-JobLaunchNote($Proc) {
    $who = "$env:USERDOMAIN\$env:USERNAME"
    $il = $null
    if ($Proc) { $il = Get-ProcessIntegrityLevel $Proc.Id }
    $kind = switch (Get-JobTokenStatus) { 'filtered' { 'filtered token' } 'full' { 'full token (opt-out)' } default { 'full token: filtered token unavailable' } }
    " as $who ($kind$(if ($il) { ", $il" }))"
}
