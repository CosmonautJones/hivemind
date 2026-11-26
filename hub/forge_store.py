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
)

# replicated table -> primary key column
REPLICATED = {"peers": "id", "nodes": "id", "jobs": "id", "events": "id", "settings": "node_id",
              # 2.2 (transcripts and watchers are ephemeral: in memory on the leader, never replicated)
              "projects": "id", "project_alias": "id", "sessions": "id", "repos": "id", "briefs": "id",
              "cooldowns": "engine", "bad_versions": "version", "leases": "id"}


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
        self.lock = threading.RLock()
        self._cols: dict[str, list[str]] = {}
        self._migrate()

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
