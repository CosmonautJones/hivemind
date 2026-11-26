"""Phase 2 scheduler logic as pure functions: node scoring, engine choice, usage-limit (cooldown) parsing,
requeue rules. No I/O, no clock: callers pass `now`."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, tzinfo
from typing import Iterable, Optional

LIMIT_RE = re.compile(r"usage limit|rate limit|limit reached|try again (?:at|in)|5-hour limit|quota", re.I)
# What actually decides a cooldown: the CLI's own limit messages, not prose that merely mentions "rate limit" or "quota".
STRICT_LIMIT_RE = re.compile(
    r"usage limit (?:reached|hit|exceeded)|limit reached\|\d{9,}|5-hour limit (?:reached|hit)|"
    r"(?:hit|reached|exceeded) your (?:[\w-]+ )?(?:usage |session |rate )?limit\b|"
    r"rate[ -]limit(?:ed)?(?: error)?[: ]+(?:reached|exceeded|hit)|\b429\b.{0,40}rate[ -]limit|"
    r"exceeded your (?:current )?quota|quota (?:exceeded|exhausted)", re.I)
LIMIT_TAIL_LINES = 20
DEFAULT_COOLDOWN_S = 30 * 60
MAX_COOLDOWN_S = 12 * 3600
DEFAULT_PREFERENCE = ("claude", "codex", "ollama")
REQUEUE_MAX_OUTPUT = 1024
REQUEUE_MAX_RUNTIME_S = 60
MIN_NEW_AGENT = (2, 2, 0)          # ollama engine, resume jobs, sessions/watch need agent >= 2.2.0

_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
_DUR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b", re.I)
_IN_RE = re.compile(r"(?:try again|resets?|available again|retry)\s+in\s+([^.\n|]{1,60})", re.I)
_AT_RE = re.compile(r"(?:try again|resets?|available again|retry|until)\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?",
                    re.I)
_EPOCH_RE = re.compile(r"limit reached\|(\d{10})")


def parse_version(v: Optional[str]) -> tuple:
    try:
        return tuple(int(p) for p in str(v).strip().split("."))
    except ValueError:
        return (0,)


def agent_is_new(version: Optional[str]) -> bool:
    return parse_version(version) >= MIN_NEW_AGENT


def detect_limit(text: str) -> bool:
    """True if `text` looks like a usage-limit / rate-limit message from claude or codex."""
    return bool(text and LIMIT_RE.search(text))


def limit_message(output: str) -> Optional[str]:
    """The usage-limit message in the LAST 20 lines of a job's output (the matching line plus the next one, which often
    carries the reset time), or None. Only CLI-style limit messages count (STRICT_LIMIT_RE)."""
    lines = [ln for ln in (output or "").splitlines() if ln.strip()][-LIMIT_TAIL_LINES:]
    for i in range(len(lines) - 1, -1, -1):
        if len(lines[i]) <= 400 and STRICT_LIMIT_RE.search(lines[i]):
            return "\n".join(lines[i:i + 2])
    return None


def _duration_s(chunk: str) -> Optional[float]:
    total, found = 0.0, False
    for num, unit in _DUR_RE.findall(chunk):
        total += float(num) * _UNITS[unit[0].lower()]
        found = True
    return total if found else None


def parse_reset(text: str, now: float, tz: Optional[tzinfo] = None) -> Optional[float]:
    """Epoch seconds when the limit resets, parsed from the message, or None.
    Understands `limit reached|<epoch>`, `try again in 2 hours 15 minutes`, `try again at 3:45 PM` / `resets 5pm`
    (wall-clock in `tz`, default hub local time; rolls to tomorrow if already past)."""
    m = _EPOCH_RE.search(text)
    if m:
        return float(m.group(1))
    m = _IN_RE.search(text)
    if m:
        d = _duration_s(m.group(1))
        if d:
            return now + d
    m = _AT_RE.search(text)
    if m:
        hour, minute, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower().replace(".", "")
        if ap and not 1 <= hour <= 12:
            return None
        if ap == "pm" and hour < 12:
            hour += 12
        elif ap == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59 or (not ap and m.group(2) is None):
            return None                      # "until 5" without am/pm or minutes: too ambiguous
        base = datetime.fromtimestamp(now, tz) if tz else datetime.fromtimestamp(now).astimezone()
        target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target.timestamp() <= now:
            target += timedelta(days=1)
        return target.timestamp()
    return None


def cooldown_until(text: str, now: float, tz: Optional[tzinfo] = None) -> Optional[float]:
    """None if `text` is not a usage-limit message, else the epoch the engine is usable again
    (parsed reset time clamped to [now+60 s, now+12 h], default now + 30 min)."""
    if not detect_limit(text):
        return None
    until = parse_reset(text, now, tz)
    if until is None:
        return now + DEFAULT_COOLDOWN_S
    return min(max(until, now + 60), now + MAX_COOLDOWN_S)


def active_cooldowns(rows: Iterable[tuple[str, float]], now: float) -> dict[str, float]:
    return {e: u for e, u in rows if u and u > now}


def wait_reason_cooldown(engine: str, until: float, tz: Optional[tzinfo] = None) -> str:
    hhmm = (datetime.fromtimestamp(until, tz) if tz else datetime.fromtimestamp(until).astimezone()).strftime("%H:%M")
    return f"{engine} cooling down until {hhmm}"


def score_node(cpu: Optional[float], mem_pct: Optional[float], active_jobs: int, is_leader: bool, has_gpu: bool,
               engine: str, work_root_free_gb: Optional[float]) -> float:
    """Placement score (higher wins). Missing cpu counts as 100 (unknown = busy), missing mem as 0."""
    return (100 - (cpu if cpu is not None else 100.0) * 0.5 - (mem_pct or 0) * 0.3 - active_jobs * 15
            - (10 if is_leader else 0) + (40 if has_gpu and engine == "ollama" else 0)
            - (50 if work_root_free_gb is not None and work_root_free_gb < 20 else 0))


def rank_key(score: float, free: int, priority: int, node_id: str) -> tuple:
    """Sort key (ascending = best first): score desc, then most free capacity, then priority number, then id."""
    return (-score, -free, priority, node_id)


def any_engines(preference: Iterable[str], kind: Optional[str]) -> list[str]:
    """Engines an `any` job may resolve to: ollama only for kind 'text' (briefs, summaries), never for code."""
    return [e for e in preference if e != "ollama" or kind == "text"]


def pick_engine(preference: Iterable[str], available: dict[str, bool], cooldowns: dict[str, float]) -> Optional[str]:
    """First engine in `preference` that is not cooling down and has a capable node (`available[engine]`)."""
    for e in preference:
        if e in cooldowns:
            continue
        if available.get(e):
            return e
    return None


def should_requeue(output_len: int, ran_s: float, attempts: int, cancel_requested: bool = False) -> bool:
    """A job lost to node loss is requeued once, only if it barely started (< 1 KB output, < 60 s)."""
    return (not cancel_requested and attempts < 1 and output_len < REQUEUE_MAX_OUTPUT
            and ran_s < REQUEUE_MAX_RUNTIME_S)
