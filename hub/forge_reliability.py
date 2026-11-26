"""Phase 3 workstream R (reliability), CONTRACT-phase3.md §5 "Workstream R".

R1  /healthz health gate: DB write probe + free space (503 when unhealthy) -> ctx.healthz; /cluster/status gains
    local_agent (this hub's own node, for the watchdog's release health gate), db and disk.
R3  DB safety: prepare_db() runs PRAGMA quick_check before the store opens (corrupt -> moved aside; reseed from the
    leader, or a standalone hub restores its newest healthy backup); nightly `VACUUM INTO` backups on the leader,
    GET /api/backups.
R4  job_output chunk table (ctx.append_output / ctx.job_output, GET /api/jobs/{id}/output); the presence table and the
    nodes_live view live in forge_store / app.py.
R5  report response `drain_timeout_s` (agents restart after draining that long; the orphan re-attach takes over).
R6  hub URL re-announce to the leader (POST /cluster/join); machine retire flow (POST/GET/DELETE
    /api/machines/{id}/retire) run by the settled leader's tick.

Python 3.10 compatible. No I/O at import time.
"""
from __future__ import annotations

import ipaddress
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from forge_actions import find_action, summary as action_summary
from forge_store import jloads

ONLINE_SECS = 30
LOCAL_AGENT_ONLINE_S = 90          # standbys see their own node through the 60 s nodes-row cadence
DRAIN_TIMEOUT_DEFAULT = 1800
HEALTHZ_MIN_FREE_GB = 1.0
BACKUP_KEEP = 7
BACKUP_TIME = "02:30"
ANNOUNCE_EVERY = 300
DISCOVER_EVERY = 60                # how often a hub looks at its own URL set
RETIRE_OFFLINE_S = 300             # uninstalling: the node counts as gone after 5 min without any report
RETIRE_UNINSTALL_MAX_S = 1800      # ...or fails when the uninstall job has not finished after 30 min
RESEED_ALERT_S = 3600              # db_reseeded stays visible for 1 h after the reseed finished
ACTIVE = ("queued", "starting", "running")
RETIRE_ACTIVE = ("draining", "uninstalling", "removing")
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")


def _num(v: Any, default: float) -> float:
    try:
        return default if isinstance(v, bool) or v is None else float(v)
    except (TypeError, ValueError):
        return default


def _hhmm(v: Any) -> Optional[int]:
    try:
        h, m = str(v).strip().split(":")
        h, m = int(h), int(m)
    except (ValueError, AttributeError):
        return None
    return h * 60 + m if 0 <= h < 24 and 0 <= m < 60 else None


# ===================================================================== R3: quick_check before the store opens
class DbUnavailable(RuntimeError):
    """The database file could not be checked (locked, cannot be opened, I/O error): NOT evidence of corruption."""


def quick_check(path: Path, timeout: float = 5.0) -> tuple[bool, str]:
    """(ok, detail) of PRAGMA quick_check. Damage (a malformed image, not a database, quick_check rows other than
    'ok') -> (False, detail). A transient condition (sqlite3.OperationalError: locked, unable to open, disk I/O,
    readonly) raises DbUnavailable instead: a file another process holds must never be moved aside."""
    try:
        con = sqlite3.connect(str(path), timeout=timeout)
        try:
            rows = con.execute("PRAGMA quick_check").fetchall()
        finally:
            con.close()
    except sqlite3.OperationalError as e:
        msg = str(e)
        if any(w in msg.lower() for w in ("malformed", "not a database", "corrupt")):
            return False, msg
        raise DbUnavailable(msg) from e
    except sqlite3.DatabaseError as e:             # SQLITE_CORRUPT / SQLITE_NOTADB
        return False, str(e)
    except UnicodeDecodeError as e:                # a damaged schema page: sqlite's error text itself is not UTF-8
        return False, f"malformed database (undecodable sqlite error: {e.reason})"
    ok = len(rows) == 1 and str(rows[0][0]).lower() == "ok"
    return ok, "ok" if ok else "; ".join(str(r[0]) for r in rows[:5])


def reseed_marker(db_path: Path) -> Path:
    """forge.db.reseed-pending: survives restarts (the empty DB itself cannot say it still needs a full sync)."""
    return Path(str(db_path) + ".reseed-pending")


def load_reseed_marker(db_path: Path) -> Optional[dict]:
    m = reseed_marker(db_path)
    if not m.exists():
        return None
    try:
        d = json.loads(m.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        d = {}
    return d if isinstance(d, dict) else {}


def clear_reseed_marker(db_path: Optional[Path]) -> None:
    if db_path:
        try:
            reseed_marker(Path(db_path)).unlink()
        except OSError:
            pass


def backup_dir_for(db_path: Path) -> Path:
    """data\\private\\backup next to data\\private\\forge.db (the contract path); data\\backup for a legacy layout."""
    return Path(db_path).parent / "backup"


def prepare_db(db_path: Path, cfg: dict, clock: Callable[[], float] = time.time, *, attempts: int = 4,
               lock_timeout: float = 5.0, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Run at hub start, before Store opens the file. Corrupt -> forge.db(-wal/-shm) renamed to
    forge.db.corrupt-<stamp>; a hub with peers starts empty and reseeds from the leader ("reseeded", remembered in
    forge.db.reseed-pending until a full sync succeeded), a hub without peers restores its newest backup that passes
    quick_check ("restored"). A locked / unopenable file is retried with backoff and then raises DbUnavailable
    (startup fails, nothing is moved: the watchdog restarts the hub later)."""
    db_path = Path(db_path)
    pending = load_reseed_marker(db_path)
    if not db_path.exists():
        if pending is not None:
            return dict(pending, quick_check="reseeded", reseed_pending=True)
        return {"quick_check": "ok", "fresh": True}
    t0 = time.monotonic()
    for i in range(max(1, attempts)):
        try:
            ok, detail = quick_check(db_path, lock_timeout)
            break
        except DbUnavailable:
            if i + 1 >= max(1, attempts):
                raise
            sleep(min(8.0, 0.5 * 2 ** i))
    took = round(time.monotonic() - t0, 2)
    if ok:
        if pending is not None:              # an earlier boot reseeded and no full sync has succeeded since
            return dict(pending, quick_check="reseeded", reseed_pending=True, took_s=took)
        return {"quick_check": "ok", "took_s": took}
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(clock()))
    moved = None
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(db_path) + suffix)
        if src.exists():
            dst = db_path.with_name(f"{db_path.name}.corrupt-{stamp}{suffix}")
            try:
                os.replace(src, dst)
                moved = moved or str(dst)
            except OSError as e:          # cannot move it: let the store fail loudly rather than lose the evidence
                return {"quick_check": "corrupt", "detail": f"{detail}; move failed: {e}"}
    out = {"quick_check": "reseeded", "detail": detail, "moved_to": moved, "at": clock(), "took_s": took}
    if not cfg.get("seed_peers"):
        for b in sorted(backup_dir_for(db_path).glob("forge-*.db"), reverse=True):
            if quick_check(b)[0]:
                try:
                    shutil.copy2(b, db_path)
                except OSError:
                    continue
                out.update(quick_check="restored", restored_from=str(b))
                break
    if out["quick_check"] == "reseeded" and cfg.get("seed_peers"):     # no peers: nothing to sync from
        out["reseed_pending"] = True
        try:
            reseed_marker(db_path).write_text(json.dumps(out), encoding="utf-8")
        except OSError:
            pass
    return out


# ===================================================================== R6: this hub's URL set
def _tailscale_exe() -> Optional[str]:
    exe = shutil.which("tailscale")
    if exe:
        return exe
    p = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tailscale" / "tailscale.exe"
    return str(p) if p.exists() else None


def tailscale_self(timeout: float = 5.0) -> Optional[dict]:
    """{"dns": "<magicdns name>", "ips": [ipv4...]} from `tailscale status --json`; {} when tailscale is not installed,
    None when it is installed but did not answer (the caller then skips this round instead of dropping URLs)."""
    exe = _tailscale_exe()
    if not exe:
        return {}
    try:
        flags = 0x08000000 if sys.platform == "win32" else 0          # CREATE_NO_WINDOW
        r = subprocess.run([exe, "status", "--json"], capture_output=True, timeout=timeout, creationflags=flags)
        data = json.loads(r.stdout or b"{}") if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    me = data.get("Self") or {}
    ips = [ip for ip in me.get("TailscaleIPs") or [] if isinstance(ip, str) and ":" not in ip]
    return {"dns": str(me.get("DNSName") or "").rstrip("."), "ips": ips}


# host-only / NAT ranges of virtual adapters (Hyper-V/WSL vEthernet and Docker use 172.16/12, VirtualBox 192.168.56/24):
# routable only on that host and often the same on several machines
VIRTUAL_NETS = (ipaddress.ip_network("172.16.0.0/12"), ipaddress.ip_network("192.168.56.0/24"))


def _lan_ok(ip: Any, allow_virtual: bool = False) -> bool:
    try:
        a = ipaddress.ip_address(str(ip))
    except ValueError:
        return False
    if a.version != 4 or a.is_loopback or a.is_link_local or a.is_unspecified or a in TAILSCALE_NET:
        return False
    return allow_virtual or not any(a in n for n in VIRTUAL_NETS)


def pick_lan(route_ip: Optional[str], host_ips: list) -> list[str]:
    """The default-route interface's address when there is one (what LAN peers can reach; trusted even in
    172.16/12, where some real LANs live); else the host's addresses minus the virtual-adapter ranges."""
    if route_ip and _lan_ok(route_ip, allow_virtual=True):
        return [str(route_ip)]
    out: list[str] = []
    for ip in host_ips:
        if _lan_ok(ip) and ip not in out:
            out.append(ip)
    return out


def default_route_ipv4() -> Optional[str]:
    """Source address the OS would use toward the internet (a UDP connect sends no packet)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))                # TEST-NET-1: only routed, never contacted
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def lan_ipv4() -> list[str]:
    try:
        ips = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        ips = []
    return pick_lan(default_route_ipv4(), ips)


def build_urls(port: int, ts: dict, lan: list[str]) -> list[str]:
    """MagicDNS name, Tailscale IPv4s, LAN IPv4s -> http://<x>:<port>, de-duplicated, at most 8 (JoinIn limit)."""
    hosts = ([ts["dns"]] if ts.get("dns") else []) + list(ts.get("ips") or []) + list(lan)
    out: list[str] = []
    for h in hosts:
        u = f"http://{h}:{port}"
        if h and u not in out:
            out.append(u)
    return out[:8]


def discover_urls(port: int) -> Optional[list[str]]:
    ts = tailscale_self()
    if ts is None:
        return None
    return build_urls(port, ts, lan_ipv4())


# ===================================================================== module state
class Reliability:
    def __init__(self, ctx: Any):
        self.ctx = ctx
        self.db_state: dict = dict(getattr(ctx.app.state, "db_state", None) or {"quick_check": "ok"})
        self.disk_free_gb: Callable[[Path], Optional[float]] = self._disk_free_gb
        # R6 announce: real servers only (tests drive ticks by hand and inject url_source)
        self.announce_enabled = bool(getattr(ctx.app.state, "start_monitor", False)) and ctx.cfg.get("announce", True) is not False
        self.url_source: Optional[Callable[[], Optional[list]]] = None      # None = discover_urls in a thread
        self.discovered: Optional[list] = None
        self._discovering = False
        self.last_discover: Optional[float] = None
        self.last_urls: Optional[list] = None
        self.last_announce: Optional[float] = None
        self.announces = 0
        self.last_backup_day: Optional[str] = None
        # R3: VACUUM INTO runs in a daemon thread on real servers (a big DB must not stall the leader's tick);
        # tests drive ticks by hand and get it inline
        self.backup_async = bool(getattr(ctx.app.state, "start_monitor", False))
        self.backup_thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()

    # ---- config
    def cfg_num(self, key: str, default: float) -> float:
        return _num(self.ctx.cfg.get(key), default)

    def port(self) -> int:
        return int(_num(self.ctx.cfg.get("port") or os.environ.get("FORGE_PORT"), 8787))

    @staticmethod
    def _disk_free_gb(path: Path) -> Optional[float]:
        try:
            return round(shutil.disk_usage(str(path)).free / 1024 ** 3, 2)
        except OSError:
            return None

    def db_path(self) -> Optional[Path]:
        return self.ctx.store.db_path()

    def backup_dir(self) -> Optional[Path]:
        p = self.db_path()
        return backup_dir_for(p) if p else None

    # ---- R1 healthz
    def healthz(self) -> tuple[int, dict]:
        ctx = self.ctx
        now = ctx.clock()
        try:
            # write + read back under one store lock: concurrent probes (watchdog, peers, a browser) must not see
            # each other's stamp and report a false 503
            with ctx.store.lock:
                ctx.store.x("INSERT INTO healthz_probe(id,ts) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET ts=excluded.ts", (now,))
                r = ctx.store.q("SELECT ts FROM healthz_probe WHERE id=1")
            db_ok = bool(r) and r[0]["ts"] == now
        except sqlite3.Error:
            db_ok = False
        free = self.disk_free_gb(ctx.data_dir)
        disk_ok = free is None or free >= self.cfg_num("healthz_min_free_gb", HEALTHZ_MIN_FREE_GB)
        ok = db_ok and disk_ok
        body = {"ok": ok, "node_id": ctx.node_id, "role": ctx.cluster.role, "version": ctx.releases.latest_version(),
                "running_version": ctx.releases.running_version, "db_ok": db_ok, "free_gb": free,
                "checks": {"db_write": db_ok, "disk": disk_ok}}
        return (200 if ok else 503), body

    def disk_block(self) -> dict:
        free = self.disk_free_gb(self.ctx.data_dir)
        return {"free_gb": free, "ok": free is None or free >= self.cfg_num("healthz_min_free_gb", HEALTHZ_MIN_FREE_GB)}

    # ---- R1 local agent (this hub's own node)
    def local_agent(self) -> dict:
        ctx, now = self.ctx, self.ctx.clock()
        r = ctx.store.q("SELECT last_seen, metrics, info FROM nodes_live WHERE id=?", (ctx.node_id,))
        if not r:
            return {"online": False, "agent_version": None, "bundle_version": None, "last_seen": None, "draining_to": None}
        m, info = jloads(r[0]["metrics"], {}), jloads(r[0]["info"], {})
        m = m if isinstance(m, dict) else {}
        info = info if isinstance(info, dict) else {}
        seen = r[0]["last_seen"]
        return {"online": seen is not None and now - seen < LOCAL_AGENT_ONLINE_S,
                "agent_version": info.get("agent_version"),
                "bundle_version": m.get("bundle_version") or info.get("bundle_version"),
                "last_seen": seen, "draining_to": m.get("draining_to")}

    def status_extras(self) -> dict:
        q = self.db_state.get("quick_check") or "ok"
        return {"local_agent": self.local_agent(),
                "db": {"quick_check": "reseeded" if q in ("reseeded", "restored") else "ok",
                       "reseed_pending": bool(getattr(self.ctx.cluster, "reseed_pending", False)),
                       "last_backup": self.last_backup()},
                "disk": self.disk_block()}

    # ---- R3 backups
    def list_backups(self) -> list[dict]:
        d = self.backup_dir()
        if not d or not d.is_dir():
            return []
        out = []
        for f in d.glob("forge-*.db"):
            try:
                st = f.stat()
            except OSError:
                continue
            out.append({"file": f.name, "size": st.st_size, "ts": st.st_mtime})
        return sorted(out, key=lambda b: b["file"], reverse=True)

    def last_backup(self) -> Optional[float]:
        b = self.list_backups()
        return max(x["ts"] for x in b) if b else None

    def backup_due(self, now: float) -> Optional[str]:
        """'YYYYMMDD' when today's (local) backup is due: past backup_time and no file for today yet."""
        at = _hhmm(self.ctx.cfg.get("backup_time", BACKUP_TIME))
        at = _hhmm(BACKUP_TIME) if at is None else at
        d = datetime.fromtimestamp(now).astimezone()
        if d.hour * 60 + d.minute < at:
            return None
        day = d.strftime("%Y%m%d")
        if self.last_backup_day == day:
            return None
        bd = self.backup_dir()
        if bd is not None and (bd / f"forge-{day}.db").exists():
            self.last_backup_day = day
            return None
        return day

    def run_backup(self, day: str) -> Optional[Path]:
        """VACUUM INTO <backup>\\forge-<day>.db via a second connection (WAL: no writer is blocked), then keep the
        newest `backup_keep` files."""
        src, bd = self.db_path(), self.backup_dir()
        self.last_backup_day = day                       # one attempt per day, success or not
        if not src or not bd:
            return None
        final = bd / f"forge-{day}.db"
        tmp = bd / f".forge-{day}.db.tmp"
        try:
            bd.mkdir(parents=True, exist_ok=True)
            if tmp.exists():
                tmp.unlink()
            con = sqlite3.connect(str(src))
            try:
                con.execute("VACUUM INTO ?", (str(tmp),))
            finally:
                con.close()
            os.replace(tmp, final)
        except (sqlite3.Error, OSError) as e:
            self.ctx.event(self.ctx.node_id, "error", f"nightly database backup failed: {e}")
            return None
        keep = max(1, int(self.cfg_num("backup_keep", BACKUP_KEEP)))
        for old in [b["file"] for b in self.list_backups()][keep:]:
            try:
                (bd / old).unlink()
            except OSError:
                pass
        self.ctx.event(self.ctx.node_id, "info", f"database backup {final.name} written")
        return final

    # ---- R6 announce
    def announce_tick(self, now: float) -> None:
        if not (self.announce_enabled or self.url_source is not None):
            return
        if self.last_discover is None or now - self.last_discover >= DISCOVER_EVERY:
            self.last_discover = now
            if self.url_source is not None:
                self.discovered = self.url_source()
            elif not self._discovering:
                self._discovering = True
                port = self.port()

                def run() -> None:
                    try:
                        self.discovered = discover_urls(port)
                    finally:
                        self._discovering = False
                threading.Thread(target=run, daemon=True, name="forge-discover").start()
        urls = [u.strip().rstrip("/") for u in (self.discovered or []) if isinstance(u, str) and u.strip()]
        if not urls:
            return
        changed = urls != self.last_urls
        due = self.last_announce is None or now - self.last_announce >= self.cfg_num("announce_every_s", ANNOUNCE_EVERY)
        if not (changed or due):
            return
        if self.send_join(urls):
            self.last_urls, self.last_announce = urls, now
            self.announces += 1

    def send_join(self, urls: list) -> bool:
        ctx = self.ctx
        cl = ctx.cluster
        if cl.is_leader:
            cl.join(ctx.node_id, urls)
            return True
        if not cl.leader_id:
            return False
        body = json.dumps({"id": ctx.node_id, "urls": urls}).encode()
        r = cl.forward("POST", "/cluster/join", {"x-cluster-token": ctx.cfg.get("cluster_token", ""),
                                                 "content-type": "application/json"}, body, None)
        return r is not None and r.status_code == 200

    # ---- R6 retire
    def retirement(self, nid: str) -> Optional[dict]:
        r = self.ctx.store.q("SELECT * FROM retirements WHERE node_id=?", (nid,))
        return dict(r[0]) if r else None

    @staticmethod
    def retire_view(row: dict) -> dict:
        out = {k: row.get(k) for k in ("node_id", "state", "started", "finished", "job_id", "error")}
        if out["job_id"] == "pending":
            out["job_id"] = None                     # uninstall wanted, job not queued yet
        return out

    def drain_of(self, nid: str) -> int:
        r = self.ctx.store.q("SELECT drain FROM settings WHERE node_id=?", (nid,))
        return int(r[0]["drain"] or 0) if r else 0

    def set_drain(self, nid: str, on: bool) -> None:
        st = self.ctx.store
        if st.q("SELECT 1 FROM settings WHERE node_id=?", (nid,)):
            st.touch("settings", "node_id", nid, "drain=?", (int(on),))
        elif on:
            st.x("INSERT INTO settings(node_id,drain,updated) VALUES(?,1,?)", (nid, self.ctx.clock()))

    def node_last_seen(self, nid: str) -> Optional[float]:
        r = self.ctx.store.q("SELECT last_seen, sys_last_seen FROM nodes_live WHERE id=?", (nid,))
        if not r:
            return None
        vals = [v for v in (r[0]["last_seen"], r[0]["sys_last_seen"]) if v is not None]
        return max(vals) if vals else None

    def release_queued(self, nid: str, now: float, why: str) -> None:
        """Queued jobs of a draining node: auto-placed ones go back to the pool, pinned ones are cancelled."""
        ctx = self.ctx
        with ctx.p2.sched_lock:
            for j in ctx.store.q("SELECT id, requested, req_engine, source FROM jobs WHERE node_id=? AND status='queued'",
                                 (nid,)):
                if j["source"] == "retire":
                    continue
                if j["requested"] == "auto":
                    sets = "node_id='auto', wait_reason=NULL" + (", engine='any'" if j["req_engine"] == "any" else "")
                    ctx.store.touch("jobs", "id", j["id"], sets, (), "status='queued' AND node_id=?", (nid,))
                else:
                    if ctx.store.touch("jobs", "id", j["id"], "status='cancelled', finished=?", (now,),
                                       "status='queued' AND node_id=?", (nid,)):
                        self.append(j["id"], f"\n[hub] {why}; job cancelled\n", now)

    def append(self, jid: str, text: str, now: float) -> None:
        if self.ctx.append_output is not None:
            self.ctx.append_output(jid, text, now)

    def fail_retire(self, nid: str, msg: str, now: float) -> None:
        ctx = self.ctx
        ctx.store.touch("retirements", "node_id", nid, "state='failed', finished=?, error=?", (now, msg[:300]))
        ctx.event(nid, "error", f"retiring {nid} failed: {msg}")
        ctx.safe_call(ctx.raise_alert, "retire_failed", key=f"retire_failed:{nid}", node_id=nid, level="error",
                      msg=f"Removing {nid} failed: {msg}",
                      action={"label": "Open machine", "kind": "open_machine", "target": {"node_id": nid}})

    def retire_tick(self, now: float) -> None:
        for row in self.ctx.store.q("SELECT * FROM retirements WHERE state IN ('draining','uninstalling','removing')"):
            try:
                self.retire_step(dict(row), now)
            except Exception as e:          # one bad row must not stop the others
                self.fail_retire(row["node_id"], f"internal error: {e}", now)

    def retire_step(self, r: dict, now: float) -> None:
        ctx, st, nid = self.ctx, self.ctx.store, r["node_id"]
        if r["state"] == "draining":
            self.release_queued(nid, now, f"{nid} is being removed")
            active = st.q("SELECT id FROM jobs WHERE node_id=? AND status IN ('starting','running') "
                          "AND COALESCE(source,'')<>'retire'", (nid,))
            if active and now - (r["started"] or now) < self.cfg_num("drain_timeout_s", DRAIN_TIMEOUT_DEFAULT):
                return
            for j in active:                 # drain timeout: ask the agent to stop them; removing fails the rest
                st.touch("jobs", "id", j["id"], "cancel_requested=1")
            want_uninstall = r["job_id"] == "pending"
            if not want_uninstall:
                st.touch("retirements", "node_id", nid, "state='removing'")
                ctx.event(nid, "info", f"retiring {nid}: drained, removing it from the fleet")
                return
            spec = find_action(ctx.actions_catalog, "forge_uninstall")
            if not spec:
                self.fail_retire(nid, "the forge_uninstall action is not available on this hub", now)
                return
            args = {"keep_data": False}
            jid = ctx.p2.insert_job(node_id=nid, engine="action", prompt=action_summary("forge_uninstall", args),
                                    run_as="system", action="forge_uninstall", args=args, source="retire")
            st.touch("retirements", "node_id", nid, "state='uninstalling', job_id=?", (jid,))
            ctx.event(nid, "warn", f"retiring {nid}: uninstall job {jid[:8]} queued for its system lane")
            return
        if r["state"] == "uninstalling":
            j = st.q("SELECT status, exit_code, created, started, dispatched_to FROM jobs WHERE id=?", (r["job_id"],))
            status = j[0]["status"] if j else "lost"
            seen = self.node_last_seen(nid)
            # "went silent" counts as done only once the machine picked the uninstall job up (it kills the agents, so
            # the hub may then call the job lost: failed without an exit code). A job that is still queued means the
            # machine was offline the whole time and nothing was uninstalled.
            picked = bool(j) and (status in ("starting", "running") or (
                status == "failed" and j[0]["exit_code"] is None and bool(j[0]["started"] or j[0]["dispatched_to"])))
            gone = (seen is None or now - seen >= RETIRE_OFFLINE_S) and picked
            if status == "done" or gone:
                st.touch("retirements", "node_id", nid, "state='removing'")
                ctx.event(nid, "info", f"retiring {nid}: uninstall {'finished' if status == 'done' else 'went silent'}")
            elif status in ("failed", "cancelled", "lost"):
                code = j[0]["exit_code"] if j else None
                self.fail_retire(nid, f"uninstall job {status}" + (f" (exit {code})" if code is not None else ""), now)
            elif now - (j[0]["created"] or now) >= RETIRE_UNINSTALL_MAX_S:
                self.fail_retire(nid, "machine offline; uninstall not performed" if status == "queued"
                                 else "uninstall did not finish within 30 min", now)
            return
        if r["state"] == "removing":
            self.remove_node(nid, now)
            rotated = None
            if r["rotate"]:
                import forge_security
                rot = getattr(forge_security, "rotate", None)
                if rot is None:
                    ctx.event(nid, "warn", "token rotation is not available on this hub; rotate tokens from Machines -> Advanced")
                else:
                    try:
                        rotated = rot(ctx, ["agent", "cluster"])
                    except Exception as e:  # the machine is gone either way; say so and let the user retry rotation
                        ctx.event(nid, "error", f"token rotation after retiring {nid} failed: {e}")
            st.touch("retirements", "node_id", nid, "state='done', finished=?, error=NULL", (now,))
            ctx.event(nid, "ok", f"{nid} was removed from the fleet" + (" (tokens rotated)" if rotated else ""))
            ctx.safe_call(ctx.resolve_alert, f"retire_failed:{nid}")

    def remove_node(self, nid: str, now: float) -> None:
        """Forget a retired machine on every hub (tombstones) and finish its leftover jobs."""
        ctx, st = self.ctx, self.ctx.store
        with ctx.p2.sched_lock:
            for j in st.q("SELECT id, status FROM jobs WHERE node_id=? AND status IN ('queued','starting','running')", (nid,)):
                new = "cancelled" if j["status"] == "queued" else "failed"
                if st.touch("jobs", "id", j["id"], "status=?, finished=?", (new, now), "status=?", (j["status"],)):
                    self.append(j["id"], f"\n[hub] {nid} was removed from the fleet; job {new}\n", now)
        for t in ("nodes", "settings", "peers"):
            if st.q(f"SELECT 1 FROM {t} WHERE {'node_id' if t == 'settings' else 'id'}=?", (nid,)):
                st.delete_row(t, nid)
        for t in ("sessions", "repos"):
            for x in st.q(f"SELECT id FROM {t} WHERE node_id=?", (nid,)):
                st.delete_row(t, x["id"])
        for t in ("metrics", "logs", "presence"):
            st.x(f"DELETE FROM {t} WHERE node_id=?", (nid,))

    # ---- alert sources (consumed by GET /api/health and N's engine)
    def alerts(self, now: float) -> list[dict]:
        ctx, out = self.ctx, []
        q = self.db_state.get("quick_check")
        if q in ("reseeded", "restored"):
            pending = bool(getattr(ctx.cluster, "reseed_pending", False))
            done_at = getattr(ctx.cluster, "reseeded_at", None)
            if pending or q == "restored" or (done_at and now - done_at < RESEED_ALERT_S):
                how = ("restored from backup " + Path(self.db_state.get("restored_from") or "").name if q == "restored"
                       else "waiting to copy the data from the leader" if pending else "copied the data from the leader")
                out.append({"level": "error" if pending else "warn", "kind": "db_reseeded", "node_id": ctx.node_id,
                            "msg": f"{ctx.node_id}: its database was damaged and set aside; {how}"})
        disk = self.disk_block()
        if not disk["ok"]:
            out.append({"level": "error", "kind": "low_disk_hub", "node_id": ctx.node_id,
                        "msg": f"{ctx.node_id} has only {disk['free_gb']} GB free for Hivemind data"})
        with ctx.cluster.lock:
            peers = [(pid, dict(s.status)) for pid, s in ctx.cluster.ps.items() if s.healthy and s.status]
        for pid, stt in peers:
            db, dk = stt.get("db") or {}, stt.get("disk") or {}
            if isinstance(db, dict) and db.get("reseed_pending"):
                out.append({"level": "error", "kind": "db_reseeded", "node_id": pid,
                            "msg": f"{pid}: its database was damaged and set aside; waiting to copy the data from the leader"})
            if isinstance(dk, dict) and dk.get("ok") is False:
                out.append({"level": "error", "kind": "low_disk_hub", "node_id": pid,
                            "msg": f"{pid} has only {dk.get('free_gb')} GB free for Hivemind data"})
        return out

    # ---- tick
    def on_tick(self, now: float, settled: bool) -> None:
        self.announce_tick(now)
        if not settled:
            return
        day = None if self.backup_running() else self.backup_due(now)
        if day:
            self.start_backup(day)
        self.retire_tick(now)

    def backup_running(self) -> bool:
        t = self.backup_thread
        return t is not None and t.is_alive()

    def start_backup(self, day: str) -> None:
        if not self.backup_async:
            self.run_backup(day)
            return
        self.last_backup_day = day                # claimed before the thread starts: one attempt per day
        t = threading.Thread(target=self._backup_thread_main, args=(day,), daemon=True, name="forge-backup")
        self.backup_thread = t
        t.start()

    def _backup_thread_main(self, day: str) -> None:
        try:
            self.run_backup(day)
        except Exception as e:                    # never let the thread die silently
            self.ctx.safe_call(self.ctx.event, self.ctx.node_id, "error", f"nightly database backup failed: {e}")


def register(app: Any, ctx: Any) -> None:
    """Add this workstream's routes to `app` and its hooks to `ctx` (forge_ctx.HubContext)."""
    rel = Reliability(ctx)
    app.state.reliability = rel
    store = ctx.store
    ctx.store.ensure_views()

    if rel.db_state.get("quick_check") not in (None, "ok"):
        d = rel.db_state
        ctx.event(ctx.node_id, "error", f"database failed PRAGMA quick_check ({d.get('detail')}); moved aside to "
                  f"{Path(d.get('moved_to') or '?').name}; "
                  + (f"restored backup {Path(d['restored_from']).name}" if d.get("restored_from")
                     else "waiting for a full copy from the leader"))

    # ---- slots
    ctx.healthz = rel.healthz
    ctx.append_output = lambda jid, text, now: store.append_chunk(jid, text, now)
    ctx.job_output = lambda jid: store.job_output_text(jid)
    ctx.status_extras.append(rel.status_extras)
    ctx.alert_sources.append(rel.alerts)
    ctx.on_tick.append(rel.on_tick)

    def report_hook(nid: str, body: dict, resp: dict, now: float) -> None:      # R5: agents drain this long at most
        resp["drain_timeout_s"] = int(rel.cfg_num("drain_timeout_s", DRAIN_TIMEOUT_DEFAULT))
    ctx.on_report.append(report_hook)

    # ---- routes
    @app.get("/api/jobs/{jid}/output", dependencies=[Depends(ctx.require_ui)])
    def job_output(jid: str, since_seq: int = 0) -> dict:
        r = store.q("SELECT id, status, output, output_min_seq, created, finished FROM jobs WHERE id=?", (jid,))
        if not r:
            raise HTTPException(404, "no such job")
        j = r[0]
        since = max(0, since_seq)
        chunks = store.job_chunks(jid, since)
        if not chunks and not store.q("SELECT 1 FROM job_output WHERE job_id=? LIMIT 1", (jid,)):
            legacy = j["output"] or ""                     # 2.2-written job: the column is one chunk, seq 1
            chunks = [{"seq": 1, "ts": j["finished"] or j["created"], "text": legacy}] if legacy and since < 1 else []
            min_seq = 1
        else:
            min_seq = j["output_min_seq"] or 1
        return {"chunks": chunks, "next_seq": chunks[-1]["seq"] if chunks else since, "status": j["status"],
                "truncated": since + 1 < min_seq}

    @app.get("/api/backups", dependencies=[Depends(ctx.require_ui)])
    def backups() -> dict:
        return {"backups": rel.list_backups(), "last_backup": rel.last_backup(), "dir": str(rel.backup_dir() or "")}

    @app.post("/api/machines/{nid}/retire", dependencies=[Depends(ctx.require_ui)])
    def retire(nid: str, request: Request, body: dict = Body(default={})):
        nid = nid.strip().lower()
        if not isinstance(body, dict) or body.get("confirm") is not True:
            raise HTTPException(400, "confirmation required")
        uninstall, rotate = body.get("uninstall", True), body.get("rotate_tokens", False)
        if not isinstance(uninstall, bool) or not isinstance(rotate, bool):
            raise HTTPException(400, "uninstall and rotate_tokens must be true or false")
        hubs = [p["id"] for p in ctx.cluster.peers()]
        if not store.q("SELECT 1 FROM nodes WHERE id=?", (nid,)) and nid not in hubs:
            raise HTTPException(404, "no such machine")
        ctx.require_step_up(request)
        cur = rel.retirement(nid)
        if cur and cur["state"] in RETIRE_ACTIVE:
            raise HTTPException(409, f"{nid} is already being removed ({cur['state']})")
        if nid in hubs and len(hubs) <= 1:
            raise HTTPException(409, f"{nid} is the only hub; it cannot be removed")
        if nid == ctx.node_id and not uninstall:
            raise HTTPException(409, f"{nid} is the hub answering this request; remove it with uninstall, or from another hub")
        if uninstall and not find_action(ctx.actions_catalog, "forge_uninstall"):
            raise HTTPException(409, "uninstall is not available on this hub yet; retry with uninstall: false")
        now = ctx.clock()
        principal = getattr(request.state, "principal", None) or {}
        prev_drain = rel.drain_of(nid)          # a drain the user set by hand survives a cancelled retirement
        rel.set_drain(nid, True)
        # job_id 'pending' marks "uninstall wanted" until the uninstall job exists (no extra column needed)
        store.x("INSERT OR REPLACE INTO retirements(node_id,state,started,finished,job_id,rotate,error,requested_by,"
                "prev_drain,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (nid, "draining", now, None, "pending" if uninstall else None, int(rotate), None,
                 str(principal.get("label") or principal.get("session_id") or "ui")[:64], prev_drain, now))
        rel.release_queued(nid, now, f"{nid} is being removed")
        ctx.event(nid, "warn", f"removing {nid} from the fleet (drain{', uninstall' if uninstall else ''}"
                  f"{', rotate tokens' if rotate else ''})")
        return JSONResponse({"ok": True, "state": "draining"}, status_code=202)

    @app.get("/api/machines/{nid}/retire", dependencies=[Depends(ctx.require_ui)])
    def retire_get(nid: str) -> dict:
        row = rel.retirement(nid.strip().lower())
        if not row:
            raise HTTPException(404, "machine is not being removed")
        return rel.retire_view(row)

    @app.delete("/api/machines/{nid}/retire", dependencies=[Depends(ctx.require_ui)])
    def retire_cancel(nid: str) -> dict:
        nid = nid.strip().lower()
        row = rel.retirement(nid)
        if not row:
            raise HTTPException(404, "machine is not being removed")
        if row["state"] not in ("draining", "failed"):
            raise HTTPException(409, f"cannot cancel: removal is already {row['state']}")
        rel.set_drain(nid, bool(row.get("prev_drain")))
        store.delete_row("retirements", nid)
        ctx.event(nid, "info", f"removal of {nid} cancelled")
        ctx.safe_call(ctx.resolve_alert, f"retire_failed:{nid}", by="user")
        return {"ok": True}
