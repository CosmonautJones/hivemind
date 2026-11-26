"""Phase 2 projects/sessions logic: git remote normalization, project keys, redaction, session state,
brief prompt builder (pure functions) and the ephemeral transcript/watch store (in memory, never replicated)."""
from __future__ import annotations

import re
import threading
from typing import Any, Optional

# token-shaped secrets: OpenAI/Anthropic (sk-, sk-ant-), GitHub (ghp_/gho_/ghu_/ghs_/ghr_, github_pat_), GitLab, Slack,
# AWS access keys, Google API keys, JWTs
SECRET_RE = re.compile(r"(?:(?<![A-Za-z0-9])sk-|gh[pousr]_|github_pat_|glpat-|xox[abprs]-|AKIA|AIza|eyJ)[A-Za-z0-9_\-]{10,}")
#                             ^ "sk-" only at a word start, so "task-1234567890" / "desk-..." stay readable
# PEM blocks (private keys and anything else armoured); an unterminated block is redacted to the end of the text
PEM_RE = re.compile(r"-----BEGIN [A-Z0-9 ]{1,60}-----[\s\S]*?(?:-----END [A-Z0-9 ]{1,60}-----|\Z)")
# key=value secrets, incl. connection strings (`Password=...;`, `AccountKey=...;`, `SharedAccessKey=...`)
KV_SECRET_RE = re.compile(r"(?i)\b(password|passwd|pwd|accountkey|sharedaccesskey|client_secret|api[_-]?key)(\s*[=:]\s*)"
                          r"(\"[^\"]*\"|'[^']*'|[^\s;,&'\"]+)")
LIVE_SECS = 90
IDLE_SECS = 24 * 3600
KEEP_DAYS = 14
TRANSCRIPT_MAX_EVENTS = 1000
EVENT_TEXT_MAX = 4000
WATCH_TTL = 30.0
TRANSCRIPT_IDLE_TTL = 600.0          # drop a transcript nobody has watched for this long
TOOLS = ("claude", "codex", "t3", "forge")
ROLES = ("user", "assistant", "tool", "system")
KINDS = ("text", "tool_call", "tool_result", "thinking")


def redact(text: Any, limit: Optional[int] = None) -> str:
    s = PEM_RE.sub("[redacted]", str(text or ""))
    s = KV_SECRET_RE.sub(lambda m: m.group(1) + m.group(2) + "[redacted]", s)
    s = SECRET_RE.sub("[redacted]", s)
    return s[:limit] if limit else s


# ---------------------------------------------------------------- remotes / keys
_REMOTE_RES = (
    re.compile(r"^[a-z][a-z0-9+.\-]*://(?:[^@/]*@)?(?P<host>[^/:]+)(?::\d+)?/(?P<path>.+)$", re.I),   # https/ssh/git URLs
    re.compile(r"^(?:[^@/\s]+@)?(?P<host>[^/:\s]+):(?P<path>[^:\s].*)$"),                            # scp-like git@host:o/r
)


def normalize_remote(url: Optional[str]) -> Optional[str]:
    """`https://github.com/Owner/Repo.git`, `git@github.com:Owner/Repo`, `ssh://git@host:22/o/r.git`
    -> `github.com/owner/repo`. None when `url` is empty or unparseable."""
    u = (url or "").strip()
    if not u:
        return None
    for rx in _REMOTE_RES:
        m = rx.match(u)
        if m:
            if rx is _REMOTE_RES[1] and "@" not in u and "." not in m.group("host"):
                continue                      # `C:\work\repo` is a path, not a remote
            path = m.group("path").strip("/")
            path = re.sub(r"\.git$", "", path, flags=re.I).strip("/")
            if not path:
                return None
            return f"{m.group('host').lower()}/{path.lower()}"
    return None


def _cwd_norm(cwd: Optional[str]) -> str:
    return (cwd or "").strip().replace("/", "\\").rstrip("\\").lower()


_MISC_RES = (
    re.compile(r"^[a-z]:\\users\\[^\\]+$"),                                        # user profile root
    re.compile(r"^[a-z]:\\windows(\\system32)?(\\.*)?$"),                           # system32 and friends
    re.compile(r"(^|\\)temp(\\|$)"),                                               # any temp dir
    re.compile(r"\\forge\\scratch(\\|$)"),                                         # Forge scratch
    # 2.3 F6a: Claude Desktop scratch workspaces (any depth below) and the user's %TEMP% (8.3 profile names too)
    re.compile(r"\\claude-data\\[^\\]+\\scratch-workspaces(\\|$)"),
    re.compile(r"\\appdata\\local\\temp(\\|$)"),
)


def is_misc_cwd(cwd: Optional[str]) -> bool:
    c = _cwd_norm(cwd)
    return not c or any(rx.search(c) for rx in _MISC_RES)


def project_key(node_id: str, cwd: Optional[str], git_remote: Optional[str]) -> str:
    """Stable project key: normalized git remote; `misc:<node>` for profile/system/temp/scratch dirs;
    else `local:<node>:<cwd-lower>`. A non-misc cwd without remote is a local project."""
    norm = normalize_remote(git_remote)
    if norm:
        return norm
    if is_misc_cwd(cwd):
        return f"misc:{node_id}"
    return f"local:{node_id}:{_cwd_norm(cwd)}"


def project_name(key: str, cwd: Optional[str] = None) -> str:
    if key.startswith("misc:"):
        return "Scratch / misc"
    if key.startswith("local:"):
        folder = (cwd or key.rsplit(":", 1)[-1]).replace("/", "\\").rstrip("\\").rsplit("\\", 1)[-1]
        return folder or key
    return key.rsplit("/", 1)[-1]


# ---------------------------------------------------------------- sessions
def session_state(last_activity: Optional[float], now: float, process_alive: bool = False) -> str:
    """live = written < 90 s ago OR owning process alive; idle = within 24 h; done = older."""
    if process_alive or (last_activity is not None and now - last_activity < LIVE_SECS):
        return "live"
    if last_activity is not None and now - last_activity < IDLE_SECS:
        return "idle"
    return "done"


def effective_state(state: Optional[str], last_activity: Optional[float], now: float, node_online: bool) -> str:
    """The agent-reported state, but a 'live' session on a node the hub no longer hears from decays by age."""
    if state == "live" and not node_online:
        return session_state(last_activity, now)
    return state or session_state(last_activity, now)


def clean_title(t: Any) -> str:
    return redact(t, 140)


# ---------------------------------------------------------------- brief prompt
def _ago(ts: Optional[float], now: float) -> str:
    if not ts:
        return "unknown"
    d = max(0, now - ts)
    if d < 90:
        return "just now"
    if d < 5400:
        return f"{int(d // 60)}m ago"
    if d < 36 * 3600:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // 86400)}d ago"


BRIEF_INSTRUCTIONS = (
    "You are a concise engineering status assistant. Using ONLY the metadata below (no code or transcripts are "
    "available), write 3-6 short bullets covering: what is happening, what changed, what is blocked, and a "
    "suggested next step. At most 120 words. No preamble.")


def build_brief_prompt(ctx: dict) -> str:
    """Pure: project metadata -> prompt. ctx = {name, now, sessions:[{tool,title,state,last_activity,messages}],
    commits:[{sha,author,ts,subject}], repos:[{node_id,branch,dirty,ahead,behind}], jobs:[{title,status,ts}]}.
    Titles/subjects are redacted and truncated; transcripts and file contents never enter the prompt."""
    now = ctx.get("now") or 0
    lines = [BRIEF_INSTRUCTIONS, "", f"Project: {redact(ctx.get('name'), 120)}"]
    sessions = ctx.get("sessions") or []
    if sessions:
        lines.append("Sessions (newest first):")
        for s in sessions[:12]:
            lines.append(f"- [{s.get('tool')}] {redact(s.get('title') or '(untitled)', 140)} | {s.get('state')} | "
                         f"{_ago(s.get('last_activity'), now)} | {s.get('messages') or 0} messages")
    commits = ctx.get("commits") or []
    if commits:
        lines.append("Recent commits:")
        for c in commits[:10]:
            lines.append(f"- {str(c.get('sha') or '')[:7]} {redact(c.get('subject'), 120)} "
                         f"({redact(c.get('author'), 40)}, {_ago(c.get('ts'), now)})")
    repos = ctx.get("repos") or []
    if repos:
        lines.append("Working copies:")
        for r in repos[:6]:
            lines.append(f"- {r.get('node_id')}: branch {r.get('branch') or '?'}, "
                         f"{'dirty' if r.get('dirty') else 'clean'}, ahead {r.get('ahead') or 0}, "
                         f"behind {r.get('behind') or 0}")
    jobs = ctx.get("jobs") or []
    if jobs:
        lines.append("Recent Forge jobs:")
        for j in jobs[:8]:
            lines.append(f"- {redact(j.get('title'), 100)} -> {j.get('status')} ({_ago(j.get('ts'), now)})")
    if len(lines) == 3:
        lines.append("(no recent activity recorded)")
    return "\n".join(lines)


def build_rollup_prompt(projects: list[dict], now: float) -> str:
    """Prompt for the all-projects roll-up (Claude). `projects`: [{name, last_activity, live, sessions_24h, brief?}]."""
    lines = ["You are a concise engineering status assistant. Using ONLY the metadata below, write a roll-up of "
             "what is active across these projects: one line per active project plus 2-3 bullets on what needs "
             "attention next. At most 200 words. No preamble.", ""]
    for p in projects[:20]:
        lines.append(f"- {redact(p.get('name'), 80)} | last activity {_ago(p.get('last_activity'), now)} | "
                     f"{p.get('live') or 0} live, {p.get('sessions_24h') or 0} sessions in 24h"
                     + (f" | latest brief: {redact(p['brief'], 240)}" if p.get("brief") else ""))
    if len(lines) == 2:
        lines.append("(no active projects)")
    return "\n".join(lines)


# ---------------------------------------------------------------- ephemeral transcripts / watchers
class Ephemeral:
    """In-memory watchers and transcripts. Lives on whichever hub is leader (standbys proxy to it)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.watch: dict[tuple, dict] = {}        # (tool, sid) -> {node_id, expires}
        self.tx: dict[tuple, dict] = {}           # (tool, sid) -> {events, seq, cursor, done, touched}

    def add_watch(self, tool: str, sid: str, node_id: str, now: float) -> None:
        with self.lock:
            self.watch[(tool, sid)] = {"node_id": node_id, "expires": now + WATCH_TTL}
            t = self.tx.get((tool, sid))
            if t:
                t["touched"] = now

    def watch_list(self, node_id: str, now: float) -> list[dict]:
        """`watch` array for that node's report response."""
        with self.lock:
            out = []
            for (tool, sid), w in self.watch.items():
                if w["node_id"] == node_id and w["expires"] > now:
                    t = self.tx.get((tool, sid))
                    out.append({"tool": tool, "session_id": sid, "since": t["cursor"] if t else None})
            return out

    def add_events(self, tool: str, sid: str, cursor: Any, events: list, done: bool, now: float) -> int:
        """Append parsed events (redacted, capped); returns number accepted. Keeps the last 1000."""
        with self.lock:
            t = self.tx.setdefault((tool, sid), {"events": [], "seq": 0, "cursor": None, "done": False, "touched": now})
            n = 0
            for ev in events or []:
                if not isinstance(ev, dict):
                    continue
                t["seq"] += 1
                e = {"seq": t["seq"], "ts": ev.get("ts"),
                     "role": ev.get("role") if ev.get("role") in ROLES else "system",
                     "kind": ev.get("kind") if ev.get("kind") in KINDS else "text",
                     "text": redact(ev.get("text"), EVENT_TEXT_MAX), "collapsed": bool(ev.get("collapsed"))}
                if ev.get("tool_name"):
                    e["tool_name"] = redact(ev["tool_name"], 80)
                t["events"].append(e)
                n += 1
            if len(t["events"]) > TRANSCRIPT_MAX_EVENTS:
                del t["events"][:-TRANSCRIPT_MAX_EVENTS]
            if cursor is not None:
                t["cursor"] = cursor
            t["done"] = bool(done)
            t["touched"] = now
            return n

    def get(self, tool: str, sid: str, since: int = 0) -> dict:
        with self.lock:
            t = self.tx.get((tool, sid))
            if not t:
                return {"events": [], "cursor": 0, "done": False, "available": False}
            return {"events": [e for e in t["events"] if e["seq"] > since], "cursor": t["seq"],
                    "done": t["done"], "available": True}

    def gc(self, now: float) -> None:
        with self.lock:
            for k in [k for k, w in self.watch.items() if w["expires"] <= now]:
                del self.watch[k]
            for k in [k for k, t in self.tx.items() if now - t["touched"] > TRANSCRIPT_IDLE_TTL and k not in self.watch]:
                del self.tx[k]
