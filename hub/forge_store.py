"""SQLite storage for the Forge hub: schema, time-sortable ids, generic replication helpers."""
from __future__ import annotations

import contextlib
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

Clock = Callable[[], float]

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
  id TEXT PRIMARY KEY, created REAL, last_seen REAL, online INTEGER DEFAULT 0,
  addr TEXT, info TEXT DEFAULT '{}', metrics TEXT DEFAULT '{}', procs TEXT DEFAULT '[]',
  agents TEXT DEFAULT '[]', capacity INTEGER DEFAULT 0, updated REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS metrics (node_id TEXT, ts REAL, cpu REAL, mem REAL, gpu REAL, disk REAL);
CREATE INDEX IF NOT EXISTS ix_metrics ON metrics(node_id, ts);
CREATE TABLE IF NOT EXISTS logs (node_id TEXT, name TEXT, ts REAL, text TEXT, PRIMARY KEY(node_id, name));
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, node_id TEXT, engine TEXT, prompt TEXT, cwd TEXT,
  full_access INTEGER, status TEXT, created REAL, started REAL, finished REAL,
  exit_code INTEGER, pid INTEGER, output TEXT DEFAULT '', cancel_requested INTEGER DEFAULT 0,
  source TEXT, requested TEXT, updated REAL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_jobs_node ON jobs(node_id, status);
CREATE INDEX IF NOT EXISTS ix_jobs_updated ON jobs(updated);
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, ts REAL, node_id TEXT, level TEXT, msg TEXT, updated REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS peers (
  id TEXT PRIMARY KEY, priority INTEGER, urls TEXT DEFAULT '[]', version TEXT, role TEXT,
  last_ok REAL, updated REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS settings (
  node_id TEXT PRIMARY KEY, work_root TEXT, max_jobs INTEGER, updated REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, key TEXT, name TEXT, group_name TEXT, pinned INTEGER DEFAULT 0, created REAL, updated REAL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_projects_key ON projects(key);
CREATE TABLE IF NOT EXISTS project_alias (id TEXT PRIMARY KEY, project_id TEXT, updated REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, tool TEXT, session_id TEXT, node_id TEXT, project_key TEXT, cwd TEXT, git_remote TEXT,
  git_branch TEXT, title TEXT, started REAL, last_activity REAL, messages INTEGER, tokens INTEGER, state TEXT,
  remote_control INTEGER DEFAULT 0, pid INTEGER, model TEXT, updated REAL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_sessions_key ON sessions(project_key);
CREATE TABLE IF NOT EXISTS repos (
  id TEXT PRIMARY KEY, node_id TEXT, cwd TEXT, git_remote TEXT, project_key TEXT, branch TEXT, dirty INTEGER DEFAULT 0,
  ahead INTEGER DEFAULT 0, behind INTEGER DEFAULT 0, commits TEXT DEFAULT '[]', updated REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS briefs (
  id TEXT PRIMARY KEY, project_id TEXT, text TEXT, ts REAL, model TEXT, job_id TEXT, updated REAL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_briefs_project ON briefs(project_id, ts);
CREATE TABLE IF NOT EXISTS cooldowns (engine TEXT PRIMARY KEY, until REAL, reason TEXT, updated REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS bad_versions (version TEXT PRIMARY KEY, ts REAL, reason TEXT, updated REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS leases (
  id TEXT PRIMARY KEY, node_id TEXT, version TEXT, granted REAL, expires REAL, reached INTEGER DEFAULT 0,
  updated REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tombstones (tbl TEXT, id TEXT, updated REAL, PRIMARY KEY(tbl, id));
"""

# ---- Phase 3 (2.3.0) tables: CONTRACT-phase3.md §3. One block per workstream; each team edits ONLY its own block. ----
SCHEMA_P3 = """
-- [S] security (all replicated)
CREATE TABLE IF NOT EXISTS ui_sessions (
  id TEXT PRIMARY KEY, label TEXT, created REAL, last_seen REAL, expires REAL, revoked INTEGER DEFAULT 0,
  stepup_until REAL, user_agent TEXT, ip TEXT, updated REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS enroll_codes (
  id TEXT PRIMARY KEY, kind TEXT, label TEXT, created REAL, expires REAL, used REAL, used_by TEXT, created_by TEXT,
  updated REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS secrets (
  name TEXT PRIMARY KEY, value TEXT, prev_value TEXT, rotated REAL, updated REAL DEFAULT 0);
-- [R] reliability
CREATE TABLE IF NOT EXISTS job_output (
  id TEXT PRIMARY KEY, job_id TEXT, seq INTEGER, ts REAL, text TEXT, updated REAL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_job_output ON job_output(job_id, seq);
CREATE INDEX IF NOT EXISTS ix_job_output_updated ON job_output(updated);
CREATE TABLE IF NOT EXISTS presence (
  node_id TEXT PRIMARY KEY, last_seen REAL, addr TEXT, metrics TEXT DEFAULT '{}', procs TEXT DEFAULT '[]',
  agents TEXT DEFAULT '[]', capacity INTEGER DEFAULT 0, sys_last_seen REAL, sys_capacity INTEGER, sys_version TEXT);
CREATE TABLE IF NOT EXISTS healthz_probe (id INTEGER PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS retirements (
  node_id TEXT PRIMARY KEY, state TEXT, started REAL, finished REAL, job_id TEXT, rotate INTEGER DEFAULT 0,
  error TEXT, requested_by TEXT, prev_drain INTEGER DEFAULT 0, updated REAL DEFAULT 0);
-- [N] notifications
CREATE TABLE IF NOT EXISTS alerts (
  id TEXT PRIMARY KEY, key TEXT, kind TEXT, node_id TEXT, level TEXT, msg TEXT, opened REAL, resolved REAL,
  resolved_by TEXT, self_healing INTEGER DEFAULT 0, last_notified REAL, notify_count INTEGER DEFAULT 0,
  data TEXT DEFAULT '{}', updated REAL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_alerts_key ON alerts(key, resolved);
CREATE TABLE IF NOT EXISTS push_subs (
  id TEXT PRIMARY KEY, endpoint TEXT, p256dh TEXT, auth TEXT, session_id TEXT, label TEXT, created REAL,
  last_ok REAL, fails INTEGER DEFAULT 0, updated REAL DEFAULT 0);
-- [shared, lead-owned] hub-wide preferences, namespaced keys: notify.* (N), upkeep.* (F), ui.* (U), security.* (S)
CREATE TABLE IF NOT EXISTS prefs (key TEXT PRIMARY KEY, value TEXT, updated REAL DEFAULT 0);
-- [F] first run + upkeep
CREATE TABLE IF NOT EXISTS maintenance_runs (
  id TEXT PRIMARY KEY, action TEXT, args TEXT DEFAULT '{}', targets TEXT DEFAULT '[]', idx INTEGER DEFAULT 0,
  status TEXT, current_job TEXT, results TEXT DEFAULT '[]', source TEXT, created REAL, started REAL, finished REAL,
  stop_on_failure INTEGER DEFAULT 1, created_by TEXT, updated REAL DEFAULT 0);
"""

# ---- Phase 4 (2.4.0): chat conversations with image attachments (hub/forge_chat.py) ----
SCHEMA_P4 = """
CREATE TABLE IF NOT EXISTS uploads (
  id TEXT PRIMARY KEY, mime TEXT, name TEXT, size INTEGER, sha256 TEXT, data TEXT, created REAL, updated REAL DEFAULT 0);
"""

# columns added after 2.0 (ALTER TABLE on existing databases; replication tolerates either side lacking them)
MIGRATIONS = (
    ("nodes", "sys_last_seen", "REAL"), ("nodes", "sys_capacity", "INTEGER DEFAULT 0"), ("nodes", "sys_version", "TEXT"),
    ("jobs", "run_as", "TEXT DEFAULT 'user'"), ("jobs", "action", "TEXT"), ("jobs", "args", "TEXT"),
    # 2.2: resume jobs, models, briefs, scheduler
    ("jobs", "resume", "TEXT"), ("jobs", "project_id", "TEXT"), ("jobs", "attempts", "INTEGER DEFAULT 0"),
    ("jobs", "wait_reason", "TEXT"), ("jobs", "model", "TEXT"), ("jobs", "req_engine", "TEXT"),
    # 2.2.1: who holds a dispatch (stale-update fencing), job kind (code|text), canary flag for the rollout lease
    ("jobs", "dispatched_to", "TEXT"), ("jobs", "kind", "TEXT"), ("settings", "canary", "INTEGER DEFAULT 0"),
    ("jobs", "prev_holder", "TEXT"), ("jobs", "requeued_at", "REAL"),
    # ---- 2.3 (CONTRACT-phase3.md §3.2); one line per workstream ----
    ("jobs", "output_seq", "INTEGER DEFAULT 0"), ("jobs", "output_len", "INTEGER DEFAULT 0"),            # [R] R4
    ("jobs", "output_min_seq", "INTEGER DEFAULT 0"), ("settings", "drain", "INTEGER DEFAULT 0"),         # [R] R4, R6
    ("retirements", "prev_drain", "INTEGER DEFAULT 0"),                                                  # [R] R6
    ("jobs", "notify", "INTEGER DEFAULT 0"),                                                             # [N] N2
    ("jobs", "run_id", "TEXT"), ("projects", "sync_nodes", "TEXT"),                                      # [F] F2, F3
    ("settings", "wu_window", "TEXT"), ("settings", "wu_last", "TEXT"),                                  # [F] F4
    # ---- 2.4: chat (forge_chat.py): conversation thread, image attachments, hub-assigned CLI session ----
    ("jobs", "thread_id", "TEXT"), ("jobs", "attachments", "TEXT"), ("jobs", "session", "TEXT"),
    ("jobs", "scratch_of", "TEXT"), ("jobs", "chat_text", "TEXT"),
)

# replicated table -> primary key column
REPLICATED = {"peers": "id", "nodes": "id", "jobs": "id", "events": "id", "settings": "node_id",
              # 2.2 (transcripts and watchers are ephemeral: in memory on the leader, never replicated)
              "projects": "id", "project_alias": "id", "sessions": "id", "repos": "id", "briefs": "id",
              "cooldowns": "engine", "bad_versions": "version", "leases": "id",
              # 2.3 (presence and healthz_probe are local only, never replicated)
              "ui_sessions": "id", "enroll_codes": "id", "secrets": "name",                          # [S]
              "job_output": "id", "retirements": "node_id",                                         # [R]
              "alerts": "id", "push_subs": "id",                                                    # [N]
              "prefs": "key", "maintenance_runs": "id",                                              # [shared] [F]
              "uploads": "id"}                                                                      # 2.4 chat


def _nodes_live_sql() -> str:
    """[R] R4: the `nodes_live` view (see Store.ensure_views)."""
    fresh = "p.last_seen IS NOT NULL AND p.last_seen >= COALESCE(n.last_seen, 0)"
    sfresh = "p.sys_last_seen IS NOT NULL AND p.sys_last_seen >= COALESCE(n.sys_last_seen, 0)"
    cols = [f"CASE WHEN {fresh} THEN p.{c} ELSE n.{c} END AS {c}"
            for c in ("last_seen", "addr", "metrics", "procs", "agents", "capacity")]
    cols += [f"CASE WHEN {sfresh} THEN p.{c} ELSE n.{c} END AS {c}" for c in ("sys_last_seen", "sys_capacity", "sys_version")]
    return ("DROP VIEW IF EXISTS nodes_live; " "CREATE VIEW nodes_live AS SELECT n.id AS id, n.created AS created, "
            "n.online AS online, n.info AS info, n.updated AS updated, " + ", ".join(cols)
            + " FROM nodes n LEFT JOIN presence p ON p.node_id = n.id;")


def new_id(clock: Clock = time.time) -> str:
    """Globally unique, time-sortable text id (11 hex chars of epoch ms + 6 random hex)."""
    return f"{int(clock() * 1000):011x}{secrets.token_hex(3)}"


def jloads(s: Optional[str], default: Any) -> Any:
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default


class Store:
    def __init__(self, path: Path, clock: Clock = time.time):
        self.clock = clock
        self._archive_v1(path)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.executescript(SCHEMA_P3)
        self.db.executescript(SCHEMA_P4)
        self.lock = threading.RLock()
        self._cols: dict[str, list[str]] = {}
        self._migrate()
        self.ensure_views()                      # [R] R4 nodes_live (after the migrations: it names migrated columns)

    def _migrate(self) -> None:
        for table, col, decl in MIGRATIONS:
            if col not in [r[1] for r in self.db.execute(f"PRAGMA table_info({table})")]:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    @staticmethod
    def _archive_v1(path: Path) -> None:
        """A v1 database (integer job ids, no `updated`) is moved aside, never modified."""
        if not path.exists():
            return
        con = sqlite3.connect(str(path))
        try:
            cols = [r[1] for r in con.execute("PRAGMA table_info(jobs)")]
        finally:
            con.close()
        if cols and "updated" not in cols:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(path) + suffix)
                if p.exists():
                    p.rename(path.with_name(f"{path.name}.v1-{stamp}.bak{suffix}"))

    def now(self) -> float:
        return self.clock()

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def x(self, sql: str, args: tuple = ()) -> int:
        with self.lock:
            return self.db.execute(sql, args).lastrowid

    def xc(self, sql: str, args: tuple = ()) -> int:
        """Execute and return the number of rows changed (for conditional check-and-set UPDATEs)."""
        with self.lock:
            return self.db.execute(sql, args).rowcount

    @contextlib.contextmanager
    def tx(self) -> Iterator[None]:
        with self.lock:
            self.db.execute("BEGIN")
            try:
                yield
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")

    # ---- row helpers that always stamp `updated` ----
    # `updated` never goes backwards for a row (max(now, previous + 1 ms)): a row last written by a hub whose clock ran
    # ahead still gets a strictly newer stamp from a slower hub, so last-write-wins replication keeps the newest write.
    STAMP = "updated=MAX(?, COALESCE(updated, 0) + 0.001)"

    def touch(self, table: str, pk: str, key: str, sets: str, args: tuple = (), where: str = "",
              wargs: tuple = ()) -> int:
        """UPDATE <table> SET <sets>, updated=<monotonic now> WHERE <pk>=? [AND <where>]; returns rows changed."""
        return self.xc(f"UPDATE {table} SET {sets}, {self.STAMP} WHERE {pk}=?" + (f" AND {where}" if where else ""),
                       (*args, self.now(), key, *wargs))

    def event(self, node_id: Optional[str], level: str, msg: str) -> None:
        ts = self.now()
        self.x("INSERT INTO events(id,ts,node_id,level,msg,updated) VALUES(?,?,?,?,?,?)",
               (new_id(self.clock), ts, node_id, level, msg, ts))

    def tombstone(self, table: str, key: str) -> None:
        self.x("INSERT OR REPLACE INTO tombstones(tbl,id,updated) VALUES(?,?,?)", (table, key, self.now()))

    def delete_row(self, table: str, key: str) -> None:
        self.x(f"DELETE FROM {table} WHERE {REPLICATED[table]}=?", (key,))
        self.tombstone(table, key)

    # ---- replication ----
    def columns(self, table: str) -> list[str]:
        if table not in self._cols:
            self._cols[table] = [r[1] for r in self.q(f"PRAGMA table_info({table})")]
        return self._cols[table]

    def changed_since(self, table: str, since: float, limit: int) -> list[dict]:
        return [dict(r) for r in self.q(
            f"SELECT * FROM {table} WHERE updated > ? ORDER BY updated, {REPLICATED[table]} LIMIT ?", (since, limit))]

    def upsert_rows(self, table: str, rows: list[dict], force: bool = False, restamp: Optional[float] = None) -> int:
        """Insert/replace rows whose incoming `updated` >= local `updated` (any row if `force`: the sender is
        authoritative, used after a demoted leader's own writes must be discarded). Returns rows applied.
        `restamp` (leader merge pulls): applied rows get updated = max(theirs, restamp), so standbys whose sync cursor
        is already past the row's original stamp still pull it from the leader."""
        pk, cols, n = REPLICATED[table], self.columns(table), 0
        marks = ",".join("?" * len(cols))
        for row in rows:
            cur = self.q(f"SELECT updated FROM {table} WHERE {pk}=?", (row.get(pk),))
            if cur and not force and (cur[0]["updated"] or 0) > (row.get("updated") or 0):
                continue
            tomb = self.q("SELECT updated FROM tombstones WHERE tbl=? AND id=?", (table, row.get(pk)))
            if tomb and not force and tomb[0]["updated"] > (row.get("updated") or 0):
                continue
            if restamp is not None:
                if cur and (cur[0]["updated"] or 0) == (row.get("updated") or 0):
                    continue                      # same version we already have: nothing to re-announce
                row = dict(row, updated=max(row.get("updated") or 0, restamp))
            self.x(f"INSERT OR REPLACE INTO {table}({','.join(cols)}) VALUES({marks})",
                   tuple(row.get(c) for c in cols))
            n += 1
        return n

    def apply_deleted(self, items: list[dict], force: bool = False, restamp: Optional[float] = None) -> None:
        for it in items:
            t, k, u = it.get("table"), it.get("id"), it.get("updated") or 0
            if t not in REPLICATED:
                continue
            cur = self.q(f"SELECT updated FROM {t} WHERE {REPLICATED[t]}=?", (k,))
            if cur and not force and (cur[0]["updated"] or 0) > u:
                continue  # re-created after the delete
            if restamp is not None:
                if not cur:
                    continue                      # nothing to delete here: keep our own tombstone (if any) as is
                u = max(u, restamp)
            self.x(f"DELETE FROM {t} WHERE {REPLICATED[t]}=?", (k,))
            self.x("INSERT OR REPLACE INTO tombstones(tbl,id,updated) VALUES(?,?,?)", (t, k, u))

    # ---- [R] reliability helpers (CONTRACT-phase3.md §1.2: R may add helpers at the end of Store) ----
    # R4 presence: `nodes_live` = the replicated `nodes` row with the local, unreplicated `presence` row laid over it
    # whenever presence is at least as fresh (agents report to the leader every few seconds; `nodes` itself is only
    # rewritten every 60 s). Every reader of last_seen / metrics / procs / agents / capacity / addr / sys_* uses it.
    def ensure_views(self) -> None:
        with self.lock:
            self.db.executescript(_nodes_live_sql())

    # R4 job output: append-only chunk rows `<job_id>:<seq 8 digits>` (<= CHUNK_MAX chars each). Chunks before
    # jobs.output_min_seq are trimmed (deleted locally on every hub) once the kept text passes OUTPUT_KEEP chars.
    CHUNK_MAX = 64_000
    OUTPUT_KEEP = 256_000

    def append_chunk(self, job_id: str, text: str, now: float) -> int:
        """Append `text` to a job's output as new chunk rows; returns the last seq (0 = nothing appended). The first
        append of a job that still has a legacy jobs.output (written by a 2.2 leader, or by the hub at insert) seeds
        chunk 1 with it, so the reader rule "chunks if any, else jobs.output" never drops text. Callers serialize
        appends per job (p2.sched_lock)."""
        if not text:
            return 0
        with self.lock:
            r = self.db.execute("SELECT output, output_seq, output_len, output_min_seq FROM jobs WHERE id=?",
                                (job_id,)).fetchone()
            if r is None:
                return 0
            seq, total, min_seq = r["output_seq"] or 0, r["output_len"] or 0, r["output_min_seq"] or 0
            # after a failover the replicated jobs row can lag its job_output rows: never reuse a seq that exists
            top = self.db.execute("SELECT COALESCE(MAX(seq),0) m FROM job_output WHERE job_id=?", (job_id,)).fetchone()["m"]
            seeded = top > 0
            seq = max(seq, top)
            parts = []
            if seq == 0 and not seeded and r["output"]:
                parts.append(r["output"])
            parts += [text[i:i + self.CHUNK_MAX] for i in range(0, len(text), self.CHUNK_MAX)]
            self.db.execute("BEGIN")
            try:
                for part in parts:
                    seq += 1
                    total += len(part)
                    self.db.execute("INSERT OR REPLACE INTO job_output(id,job_id,seq,ts,text,updated) VALUES(?,?,?,?,?,?)",
                                    (f"{job_id}:{seq:08d}", job_id, seq, now, part, now))
                min_seq = max(min_seq, 1)
                keep = self._trim_point(job_id, min_seq, seq)
                if keep > min_seq:
                    self.db.execute("DELETE FROM job_output WHERE job_id=? AND seq<?", (job_id, keep))
                    min_seq = keep
                self.db.execute(f"UPDATE jobs SET output_seq=?, output_len=?, output_min_seq=?, {self.STAMP} WHERE id=?",
                                (seq, total, min_seq, now, job_id))
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")
            return seq

    def _trim_point(self, job_id: str, min_seq: int, last: int) -> int:
        """First seq to keep so that the kept chunks hold at most OUTPUT_KEEP chars (the newest chunk always stays)."""
        rows = self.db.execute("SELECT seq, LENGTH(text) n FROM job_output WHERE job_id=? AND seq>=? ORDER BY seq DESC",
                               (job_id, min_seq)).fetchall()
        kept, first = 0, last
        for row in rows:
            if row["seq"] != last and kept + (row["n"] or 0) > self.OUTPUT_KEEP:
                break
            kept += row["n"] or 0
            first = row["seq"]
        return max(first, min_seq)

    def job_chunks(self, job_id: str, since_seq: int = 0, limit: int = 10_000) -> list:
        return [dict(r) for r in self.q("SELECT seq, ts, text FROM job_output WHERE job_id=? AND seq>? ORDER BY seq LIMIT ?",
                                        (job_id, since_seq, limit))]

    def job_output_text(self, job_id: str, legacy: Optional[str] = None) -> str:
        """Reader rule (mixed fleet): the chunks if any exist for the job, else jobs.output. Trimmed output starts
        with "[...truncated...]" (as in 2.2)."""
        rows = self.q("SELECT seq, text FROM job_output WHERE job_id=? ORDER BY seq", (job_id,))
        if not rows:
            if legacy is None:
                r = self.q("SELECT output FROM jobs WHERE id=?", (job_id,))
                legacy = (r[0]["output"] if r else "") or ""
            return legacy
        out = "".join(r["text"] or "" for r in rows)
        return ("[...truncated...]\n" + out) if rows[0]["seq"] > 1 else out

    def job_output_tail(self, job_id: str, n: int = 400, legacy: Optional[str] = None) -> str:
        rows = self.q("SELECT text FROM job_output WHERE job_id=? ORDER BY seq DESC LIMIT 2", (job_id,))
        if not rows:
            return (legacy if legacy is not None else self.job_output_text(job_id))[-n:]
        return "".join(r["text"] or "" for r in reversed(rows))[-n:]

    def delete_job_cascade(self, job_id: str) -> None:
        """Delete a job (with a tombstone) and its chunk rows. Standbys drop the chunks when they apply the tombstone."""
        self.delete_row("jobs", job_id)
        self.x("DELETE FROM job_output WHERE job_id=?", (job_id,))

    def cleanup_chunks(self, job_ids: set, deleted_jobs: set) -> int:
        """Standby side of trimming / retention, after a sync page: drop the chunks of deleted jobs, chunks below their
        job's output_min_seq, and chunks whose job was deleted (tombstoned) before they arrived. Returns rows deleted."""
        n = 0
        for jid in deleted_jobs:
            n += self.xc("DELETE FROM job_output WHERE job_id=?", (jid,))
        for jid in set(job_ids) - set(deleted_jobs):
            r = self.q("SELECT output_min_seq FROM jobs WHERE id=?", (jid,))
            if r:
                n += self.xc("DELETE FROM job_output WHERE job_id=? AND seq<?", (jid, r[0]["output_min_seq"] or 0))
            elif self.q("SELECT 1 FROM tombstones WHERE tbl='jobs' AND id=?", (jid,)):
                n += self.xc("DELETE FROM job_output WHERE job_id=?", (jid,))
        return n

    def db_path(self) -> Optional[Path]:
        r = self.q("PRAGMA database_list")
        f = next((x["file"] for x in r if x["name"] == "main"), None)
        return Path(f) if f else None
