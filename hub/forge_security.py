"""Phase 3 workstream S (security): S2 secret split, S3 enrollment codes, S4 per-device sessions, S5 token rotation,
S6 signed bundles, S7 admin PIN step-up. CONTRACT-phase3.md §4.3, §5 "Workstream S", §6.4.

Wiring (`register`): sets ctx.ui_auth, ctx.check_agent_token, ctx.check_cluster_token, ctx.require_step_up,
ctx.verify_bundle (and Releases.verify), adds on_tick / on_report / on_system_report / alert_sources / status_extras
hooks, and the S routes. Replaces the core routes POST /api/login, POST /api/logout, GET /api/enroll,
GET /cluster/bootstrap and GET /agent/install.ps1 (allowed by CONTRACT §2).

Secrets live in three places that stay in step:
- the replicated `secrets` table (authoritative fleet-wide; the leader writes it),
- `ctx.cfg` (this hub's in-memory view; every hub adopts newer table rows on its tick),
- `data/private/secrets.json` + `data/config.json` (`agent_token` only), rewritten on adoption so the watchdog and the
  agents (which read files) follow. Without a `data/private` dir (tests, dev checkouts) everything stays in config.json.

Pure helpers (no FastAPI) are module-level so tests can call them: load_config, db_path, token_hash, hash_pin,
verify_pin, normalize_code, code_id, bundle_manifest, parse_pubkey, key_id, rsa_verify, verify_release.
Python 3.10 compatible; no import-time I/O.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets as pysecrets
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from forge_bundle import BundleError, parse_version
from forge_store import jloads

SESSION_COOKIE = "forge_sid"
LEGACY_COOKIE = "forge_token"
SESSION_REFRESH_S = 600              # sliding expiry is refreshed at most this often (keeps replication quiet)
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"     # Crockford base32: no I L O U
PIN_ITERATIONS = 200_000
PIN_RE = re.compile(r"^\d{6}$")
MIN_RSA_BITS = 3072
P3_AGENT = (2, 3, 0)
RECENT_AGENT_S = 600                 # "still running" window for the rotation version gate
SIG_FILE = "bundle.sig"
MANIFEST_SKIP_ROOT = {"bundle.sig", "bundle.zip", "bundle.sha256"}
SHA256_DIGESTINFO = bytes.fromhex("3031300d060960864801650304020105000420")
SECRET_FILE_KEYS = ("ui_token_hash", "cluster_token", "cluster_token_prev", "rotated", "signing_pubkey", "admin_pin_hash")
ADOPT_NAMES = ("agent_token", "cluster_token", "ui_token_hash", "admin_pin_hash", "signing_pubkey")
SEED_NAMES = ("ui_token_hash", "admin_pin_hash", "signing_pubkey")
DEFAULTS = {"session_days": 90, "stepup_minutes": 10, "enroll_ttl_s": 600, "token_grace_s": 600}
NEVER_SIGNED_SUFFIXES = (".pyc", ".pyo")  # S-R4: compiled Python is never in a signed bundle, so never accepted in one
LEGACY_REUSE_S = 600                 # S-R3: a legacy-cookie client gets the same upgraded session for this long
LEGACY_UPGRADES_PER_HOUR = 20        # S-R3: hard cap on sessions minted from the legacy cookie
PIN_LIMITS = (5, 10, 20)             # S-R3: wrong PINs per 10 min: per principal, per IP, whole hub
PIN_WINDOW_S = 600
PIN_RESET_FILE = "pin-reset.json"    # S-R9: data/private/pin-reset.json (admin-only dir) clears the admin PIN


# ====================================================================== pure helpers
def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def token_hash(token: str) -> str:
    """S2 `ui_token_hash` format."""
    return "sha256:" + sha256_hex(token)


def ct_eq(a: Optional[str], b: Optional[str]) -> bool:
    """Constant-time string compare that tolerates None / non-ASCII."""
    if not a or not b:
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _read_json(p: Path) -> dict:
    try:
        v = json.loads(p.read_text(encoding="utf-8-sig"))
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(p: Path, obj: dict) -> None:
    """Atomic UTF-8 (no BOM) write."""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def private_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "private"


def load_config(data_dir: Path) -> dict:
    """Hook A-S1: data/config.json merged with data/private/secrets.json (secrets win).

    With a `data/private` dir, 2.2 secrets are migrated out of config.json once (a copy of the old file is kept as
    data/private/config.json.pre23.bak): `ui_token` becomes `ui_token_hash` and `cluster_token` moves to secrets.json.
    Without it (tests, dev checkouts) the 2.2 behaviour is kept: missing tokens are generated into config.json.
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    cp = data_dir / "config.json"
    cfg = _read_json(cp) if cp.exists() else {}
    priv = private_dir(data_dir)
    sp = priv / "secrets.json"
    sec = _read_json(sp) if sp.exists() else {}
    if priv.is_dir():
        try:
            new_cfg, new_sec = dict(cfg), dict(sec)
            moved = False
            if new_cfg.get("ui_token"):
                if not new_sec.get("ui_token_hash"):
                    new_sec["ui_token_hash"] = token_hash(str(new_cfg["ui_token"]))
                new_cfg.pop("ui_token")
                moved = True
            if new_cfg.get("cluster_token"):
                if not new_sec.get("cluster_token"):
                    new_sec["cluster_token"] = new_cfg["cluster_token"]
                new_cfg.pop("cluster_token")
                moved = True
            gen_cfg = not new_cfg.get("agent_token")
            if gen_cfg:
                new_cfg["agent_token"] = pysecrets.token_urlsafe(24)
            gen_sec = not new_sec.get("cluster_token")
            if gen_sec:
                new_sec["cluster_token"] = pysecrets.token_urlsafe(24)
            if moved:
                bak = priv / "config.json.pre23.bak"
                if cp.exists() and not bak.exists():
                    bak.write_bytes(cp.read_bytes())
            if moved or gen_sec:
                _write_json(sp, new_sec)          # secrets first: never drop a token from config.json before it is saved
            if moved or gen_cfg:
                _write_json(cp, new_cfg)
            cfg, sec = new_cfg, new_sec
        except OSError as e:                      # read-only private dir: keep serving with what we have
            print(f"[forge_security] secret migration skipped: {e}", file=sys.stderr)
    else:
        changed = False
        for key, nbytes in (("ui_token", 18), ("agent_token", 24), ("cluster_token", 24)):
            if key == "ui_token" and cfg.get("ui_token_hash"):
                continue
            if not cfg.get(key):
                cfg[key], changed = pysecrets.token_urlsafe(nbytes), True
        if changed:
            _write_json(cp, cfg)
    merged = dict(cfg)
    for k in SECRET_FILE_KEYS:
        if sec.get(k) is not None:
            merged[k] = sec[k]
    return merged


def _reset_acl(p: Path) -> None:
    """Re-inherit the ACL of a file moved into data/private (a moved file keeps its old ACEs otherwise)."""
    if os.name != "nt":
        return
    try:
        subprocess.run(["icacls", str(p), "/reset", "/Q"], capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def db_path(data_dir: Path) -> Path:
    """Hook A-S1 / CONTRACT §4.1: env FORGE_DB, else data/private/forge.db when that dir exists (2.2 files are moved
    there), else data/forge.db (tests)."""
    env = os.environ.get("FORGE_DB")
    if env:
        return Path(env)
    data_dir = Path(data_dir)
    priv = private_dir(data_dir)
    if not priv.is_dir():
        return data_dir / "forge.db"
    target = priv / "forge.db"
    old = data_dir / "forge.db"
    if old.exists() and not target.exists():
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(old) + suffix)
            if src.exists():
                dst = Path(str(target) + suffix)
                os.replace(src, dst)
                _reset_acl(dst)
    return target


# ---------------------------------------------------------------- PIN (S7)
def hash_pin(pin: str, salt: Optional[bytes] = None, iterations: int = PIN_ITERATIONS) -> str:
    salt = salt if salt is not None else pysecrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_pin(pin: str, stored: Optional[str]) -> bool:
    try:
        algo, iters, salt_b64, hash_b64 = str(stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt, want = base64.b64decode(salt_b64), base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", str(pin).encode("utf-8"), salt, int(iters))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, want)


# ---------------------------------------------------------------- enrollment codes (S3)
_CROCKFORD_FIX = str.maketrans({"I": "1", "L": "1", "O": "0", "U": "V"})


def new_code() -> str:
    raw = "".join(pysecrets.choice(CODE_ALPHABET) for _ in range(8))
    return raw[:4] + "-" + raw[4:]


def normalize_code(code: str) -> str:
    """Case-insensitive, dash/space-insensitive; Crockford look-alikes (I/L->1, O->0) are accepted."""
    return re.sub(r"[\s-]", "", str(code or "")).upper().translate(_CROCKFORD_FIX)


def code_id(code: str) -> str:
    """enroll_codes.id = sha256 hex of the normalized code (F's firstrun-claim.json `code_sha256` uses the same)."""
    return sha256_hex(normalize_code(code))


def format_code(code: str) -> str:
    n = normalize_code(code)
    return n[:4] + "-" + n[4:] if len(n) == 8 else n


# ---------------------------------------------------------------- signed bundles (S6)
def _file_sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def manifest_included(rel: str) -> bool:
    """CONTRACT §6.4 (+ amendment S-1): skip root bundle.sig/zip/sha256, venv/, __pycache__/ and *.pyc."""
    parts = rel.split("/")
    if rel in MANIFEST_SKIP_ROOT or parts[0] == "venv":
        return False
    return "__pycache__" not in parts and not rel.endswith(".pyc")


def unsigned_code_entry(release_dir: Path) -> Optional[str]:
    """S-R4: first path a signed bundle may never contain (any __pycache__ dir, *.pyc / *.pyo, a top-level venv/),
    else None. bundle_manifest skips these (so a re-zip of a release that already ran still hashes the same), which is
    exactly why a freshly extracted bundle that carries one must be refused: CPython would load it unverified."""
    root = Path(release_dir)
    for f in sorted(root.rglob("*")):
        rel = f.relative_to(root).as_posix()
        parts = [x.lower() for x in rel.split("/")]
        if parts[0] == "venv" or "__pycache__" in parts or rel.lower().endswith(NEVER_SIGNED_SUFFIXES):
            return rel
    return None


def bundle_manifest(release_dir: Path) -> bytes:
    """Twin of verify-bundle.ps1 Get-BundleManifest: "<sha256> <rel/path>\\n" per file, ordinal-sorted, UTF-8."""
    root = Path(release_dir)
    rows = []
    for f in root.rglob("*"):
        if f.is_file():
            rel = f.relative_to(root).as_posix()
            if manifest_included(rel):
                rows.append((rel, _file_sha(f)))
    rows.sort(key=lambda r: r[0].encode("utf-16-be"))      # .NET StringComparer.Ordinal = UTF-16 code units
    return "".join(f"{sha} {rel}\n" for rel, sha in rows).encode("utf-8")


def _b64int(s: str) -> int:
    return int.from_bytes(base64.b64decode(s.strip()), "big")


def _intb64(n: int) -> str:
    return base64.b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).decode()


def parse_pubkey(xml: str) -> tuple[int, int]:
    """(n, e) from a .NET RSAKeyValue XML string; ValueError when it is not one."""
    m = re.search(r"<Modulus>\s*([A-Za-z0-9+/=\s]+?)\s*</Modulus>", str(xml or ""))
    e = re.search(r"<Exponent>\s*([A-Za-z0-9+/=\s]+?)\s*</Exponent>", str(xml or ""))
    if not m or not e or "<RSAKeyValue>" not in str(xml):
        raise ValueError("not an RSAKeyValue public key")
    try:
        return _b64int(m.group(1)), _b64int(e.group(1))
    except (ValueError, TypeError):
        raise ValueError("bad base64 in RSAKeyValue")


def canonical_pubkey(xml: str) -> str:
    n, e = parse_pubkey(xml)
    return f"<RSAKeyValue><Modulus>{_intb64(n)}</Modulus><Exponent>{_intb64(e)}</Exponent></RSAKeyValue>"


def key_id(xml: str) -> str:
    """First 16 hex of sha256(canonical RSAKeyValue XML) (= .NET ToXmlString($false) bytes)."""
    return sha256_hex(canonical_pubkey(xml))[:16]


def rsa_verify(n: int, e: int, message: bytes, sig: bytes) -> bool:
    """RSASSA-PKCS1-v1_5 with SHA-256 (what .NET RSACryptoServiceProvider.SignData(data, 'SHA256') produces)."""
    k = (n.bit_length() + 7) // 8
    if len(sig) != k:
        return False
    s = int.from_bytes(sig, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    t = SHA256_DIGESTINFO + hashlib.sha256(message).digest()
    if k < len(t) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(em, expected)


def verify_release(release_dir: Path, pubkey_xml: str) -> dict:
    """Raise BundleError("bundle not signed" | "bundle signature invalid" | "bundle contains unsigned code: <path>"
    | "signing key invalid" (500)) unless <dir>/bundle.sig verifies. For a FRESH extraction only (publish, watchdog
    pre-activation): a release dir Python has already run from holds __pycache__ and is refused."""
    d = Path(release_dir)
    try:
        n, e = parse_pubkey(pubkey_xml)
    except ValueError:                                   # S-R8: a corrupt pinned/replicated key is a server problem
        raise BundleError("signing key invalid", 500)
    bad = unsigned_code_entry(d)
    if bad:
        raise BundleError(f"bundle contains unsigned code: {bad}", 400)
    sp = d / SIG_FILE
    if not sp.is_file():
        raise BundleError("bundle not signed", 400)
    try:
        sig = json.loads(sp.read_text(encoding="utf-8-sig"))
        raw = base64.b64decode(str(sig.get("sig") or ""), validate=True)
    except (ValueError, TypeError, AttributeError):
        raise BundleError("bundle signature invalid", 400)
    manifest = bundle_manifest(d)
    if str(sig.get("manifest_sha256") or "").lower() != hashlib.sha256(manifest).hexdigest():
        raise BundleError("bundle signature invalid", 400)
    if not rsa_verify(n, e, manifest, raw):
        raise BundleError("bundle signature invalid", 400)
    return sig


def ua_label(ua: str) -> str:
    ua = ua or ""
    browser = next((name for k, name in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"), ("CriOS", "Chrome"),
                                         ("Chrome/", "Chrome"), ("Safari/", "Safari")) if k in ua), "Browser")
    osname = next((name for k, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                                        ("Windows", "Windows"), ("Mac OS X", "Mac"), ("Linux", "Linux")) if k in ua), "")
    return f"{browser} on {osname}" if osname else browser


def _is_loopback_url(u: str) -> bool:
    host = (urlsplit(u).hostname or "").lower()
    return host in ("localhost", "::1", "") or host.startswith("127.")


class StepUpRequired(HTTPException):
    """403 rendered flat: {"detail": "...", "step_up": "required"|"setup"} (CONTRACT §5 S7)."""

    def __init__(self, detail: str, step_up: str):
        super().__init__(403, detail)
        self.step_up = step_up


# ====================================================================== runtime
class Security:
    def __init__(self, ctx: Any):
        self.ctx, self.store, self.cfg, self.clock = ctx, ctx.store, ctx.cfg, ctx.clock
        self.lock = threading.RLock()
        self.fails: dict[str, list[float]] = {}          # rate limits: "<bucket>:<key>" -> failure times
        self.refused: dict[str, float] = {}               # version -> when a publish was refused for its signature
        self.upgrades: dict[str, tuple] = {}              # S-R3: "<ip>|<ua sha>" -> (secret, sid, created)
        self.upgrade_times: list[float] = []

    # ---------------------------------------------------------------- config
    def conf(self, key: str) -> Any:
        v = self.cfg.get(key)
        return DEFAULTS[key] if v is None else v

    def grace(self) -> float:
        return float(self.conf("token_grace_s"))

    # ---------------------------------------------------------------- rate limits
    def _limited(self, bucket: str, key: str, limit: int, window: float, now: float) -> bool:
        with self.lock:
            k = f"{bucket}:{key}"
            ts = [t for t in self.fails.get(k, []) if now - t < window]
            self.fails[k] = ts
            return len(ts) >= limit

    def _fail(self, bucket: str, key: str, now: float) -> None:
        with self.lock:
            self.fails.setdefault(f"{bucket}:{key}", []).append(now)
            if len(self.fails) > 5000:                    # bound memory against address spraying
                self.fails = {k: v for k, v in self.fails.items() if v and now - v[-1] < 600}

    def client_ip(self, request: Request) -> str:
        """The address rate limits key on. X-Forwarded-For is honoured only on a hop proven to be a peer hub
        (Cluster.forward sends X-Forge-Proxy = the cluster token), and then only its LAST entry: the address that
        peer saw. Everything before it is client-supplied (S-R2)."""
        ip = request.client.host if request.client else ""
        if "x-forge-via" in request.headers and self.check_cluster_token(request.headers.get("x-forge-proxy", "")):
            hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
            if hops:
                ip = hops[-1]
        return ip or "?"

    # ---------------------------------------------------------------- secrets table
    def row(self, name: str) -> Optional[dict]:
        r = self.store.q("SELECT * FROM secrets WHERE name=?", (name,))
        return dict(r[0]) if r else None

    def value(self, name: str) -> Optional[str]:
        r = self.row(name)
        if r:
            return r["value"] or None                     # an existing row wins, also when cleared (S-R9 PIN reset)
        if name == "ui_token_hash":
            return self.cfg.get("ui_token_hash") or (token_hash(self.cfg["ui_token"]) if self.cfg.get("ui_token") else None)
        return self.cfg.get(name) or None

    def write_secret(self, name: str, value: Optional[str], prev: Optional[str] = None,
                     rotated: Optional[float] = None) -> None:
        now = self.clock()
        cur = self.row(name)
        upd = max(now, ((cur or {}).get("updated") or 0) + 0.001)
        self.store.x("INSERT OR REPLACE INTO secrets(name,value,prev_value,rotated,updated) VALUES(?,?,?,?,?)",
                     (name, value, prev, rotated, upd))
        self._apply(name, value, prev, rotated)
        self.persist(remove=(name,) if value is None else ())

    def _apply(self, name: str, value: Optional[str], prev: Optional[str], rotated: Optional[float]) -> bool:
        changed = self.cfg.get(name) != value
        if value is None:
            self.cfg.pop(name, None)
        else:
            self.cfg[name] = value
        if name == "cluster_token" and rotated:
            if self.cfg.get("cluster_token_prev") != prev or self.cfg.get("rotated") != rotated:
                changed = True
            self.cfg["cluster_token_prev"], self.cfg["rotated"] = prev, rotated
        return changed

    def persist(self, remove: tuple = ()) -> None:
        """Rewrite data/private/secrets.json and data/config.json.agent_token from cfg (watchdog and agents read files).
        `remove`: secret names that were cleared (dropped from the files, else load_config would bring them back)."""
        data_dir = Path(self.ctx.data_dir)
        priv = private_dir(data_dir)
        cp = data_dir / "config.json"
        try:
            with self.lock:
                file_cfg = _read_json(cp) if cp.exists() else {}
                if priv.is_dir():
                    sp = priv / "secrets.json"
                    sec = _read_json(sp) if sp.exists() else {}
                    for k in SECRET_FILE_KEYS:
                        if self.cfg.get(k) is not None:
                            sec[k] = self.cfg[k]
                        elif k in remove:
                            sec.pop(k, None)
                    _write_json(sp, sec)
                    keys = ("agent_token",)
                else:                                     # dev / test layout: one file
                    keys = ("agent_token",) + SECRET_FILE_KEYS
                dirty = False
                for k in keys:
                    if self.cfg.get(k) is not None and file_cfg.get(k) != self.cfg[k]:
                        file_cfg[k], dirty = self.cfg[k], True
                    elif self.cfg.get(k) is None and k in remove and k in file_cfg:
                        file_cfg.pop(k)
                        dirty = True
                if dirty:
                    _write_json(cp, file_cfg)
        except OSError as e:
            print(f"[forge_security] cannot persist secrets: {e}", file=sys.stderr)

    def adopt(self) -> bool:
        """Every hub: take newer `secrets` rows (replicated from the leader) into cfg + files."""
        changed, removed = False, []
        for r in self.store.q("SELECT * FROM secrets"):
            if r["name"] in ADOPT_NAMES and r["value"]:
                changed |= self._apply(r["name"], r["value"], r["prev_value"], r["rotated"])
            elif r["name"] == "admin_pin_hash" and not r["value"] and self.cfg.get("admin_pin_hash"):
                self._apply("admin_pin_hash", None, None, None)         # S-R9: the leader cleared the PIN
                changed = True
                removed.append("admin_pin_hash")
        if changed:
            self.persist(remove=tuple(removed))
        return changed

    def seed(self) -> None:
        """Settled leader: values only known from local files (installer) become fleet-wide rows."""
        for name in SEED_NAMES:
            if self.row(name) is None:
                v = self.value(name)
                if v:
                    self.store.x("INSERT INTO secrets(name,value,prev_value,rotated,updated) VALUES(?,?,?,?,?)",
                                 (name, v, None, None, self.clock()))

    # ---------------------------------------------------------------- tokens (S5)
    def _token_ok(self, name: str, tok: str) -> bool:
        if not tok:
            return False
        if ct_eq(tok, self.cfg.get(name)):
            return True
        r = self.row(name)
        if not r:
            return False
        if ct_eq(tok, r["value"]):
            return True
        return bool(r["prev_value"]) and self.clock() < (r["rotated"] or 0) + self.grace() and ct_eq(tok, r["prev_value"])

    def check_agent_token(self, tok: str) -> bool:
        return self._token_ok("agent_token", tok)

    def check_cluster_token(self, tok: str) -> bool:
        return self._token_ok("cluster_token", tok)

    def ui_token_ok(self, tok: str) -> bool:
        if not tok:
            return False
        h = token_hash(tok)
        cands = {self.value("ui_token_hash"), self.cfg.get("ui_token_hash")}
        if self.cfg.get("ui_token"):
            cands.add(token_hash(self.cfg["ui_token"]))
        return any(ct_eq(h, c) for c in cands if c)

    def old_agents(self, now: float) -> list[str]:
        out = []
        for r in self.store.q("SELECT id,last_seen,info,sys_last_seen,sys_version FROM nodes_live"):
            if now - (r["last_seen"] or 0) < RECENT_AGENT_S:
                v = jloads(r["info"], {}).get("agent_version")
                if not v or parse_version(v) < P3_AGENT:
                    out.append(r["id"])
                    continue
            if now - (r["sys_last_seen"] or 0) < RECENT_AGENT_S and (not r["sys_version"]
                                                                    or parse_version(r["sys_version"]) < P3_AGENT):
                out.append(r["id"])
        return sorted(set(out))

    def old_hubs(self) -> list[str]:
        return sorted(p["id"] for p in self.ctx.cluster.peers()
                      if p["id"] != self.ctx.node_id and p.get("version") and parse_version(p["version"]) < P3_AGENT)

    def rotate(self, which: list[str]) -> dict:
        now = self.clock()
        done = []
        for w in which:
            name = f"{w}_token"
            old = self.value(name) or self.cfg.get(name)
            self.write_secret(name, pysecrets.token_urlsafe(24), old, now)
            done.append(w)
        self.ctx.event(self.ctx.node_id, "warn", f"tokens rotated: {', '.join(done)} (old accepted for {int(self.grace())} s)")
        return {"ok": True, "rotated": done, "grace_until": now + self.grace()}

    def rotate_hint(self, resp: dict, now: float) -> None:
        """Report responses (both lanes) carry the new agent token while the previous one is still accepted."""
        r = self.row("agent_token")
        if r and r["prev_value"] and r["value"] and now < (r["rotated"] or 0) + self.grace():
            resp["rotate"] = {"agent_token": r["value"], "grace_until": (r["rotated"] or 0) + self.grace()}

    # ---------------------------------------------------------------- sessions (S4)
    def create_session(self, label: str, request: Request) -> tuple[str, str]:
        now = self.clock()
        secret = pysecrets.token_urlsafe(32)
        sid = sha256_hex(secret)
        ua = request.headers.get("user-agent", "")[:300]
        label = (label or "").strip()[:64] or ua_label(ua)
        self.store.x("INSERT INTO ui_sessions(id,label,created,last_seen,expires,revoked,stepup_until,user_agent,ip,updated)"
                     " VALUES(?,?,?,?,?,0,NULL,?,?,?)",
                     (sid, label, now, now, now + self.session_secs(), ua, self.client_ip(request), now))
        self.ctx.event(None, "info", f"device signed in: {label}")
        return secret, sid

    def session_secs(self) -> float:
        return float(self.conf("session_days")) * 86400

    def session_row(self, secret: str) -> Optional[dict]:
        if not secret:
            return None
        r = self.store.q("SELECT * FROM ui_sessions WHERE id=?", (sha256_hex(secret),))
        if not r or r[0]["revoked"] or (r[0]["expires"] or 0) <= self.clock():
            return None
        return dict(r[0])

    def ui_auth(self, request: Request) -> Optional[dict]:
        now = self.clock()
        row = self.session_row(request.cookies.get(SESSION_COOKIE, ""))
        if row:
            if self.ctx.is_leader and now - (row["last_seen"] or 0) >= SESSION_REFRESH_S:
                self.store.touch("ui_sessions", "id", row["id"], "last_seen=?, expires=?",
                                 (now, now + self.session_secs()))
            return {"session_id": row["id"], "label": row["label"], "stepup_until": row["stepup_until"]}
        legacy = request.cookies.get(LEGACY_COOKIE)
        tok = legacy or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if tok and self.ui_token_ok(tok):
            principal = {"session_id": None, "label": "access token", "legacy": True}
            if legacy and self.ctx.is_leader:              # upgrade a 2.2 browser cookie to a device session
                up = self.legacy_upgrade(request, now)
                if up:
                    request.state.forge_new_sid = up[0]
                    principal.update(session_id=up[1], label=self.row_label(up[1]))
            return principal
        return None

    def legacy_upgrade(self, request: Request, now: float) -> Optional[tuple[str, str]]:
        """(secret, sid) of the device session for a 2.2 `forge_token` cookie, or None (no upgrade; the request still
        authenticates as the legacy token). Browsers only; the same (ip, user agent) gets the SAME session for
        LEGACY_REUSE_S (the parallel fetches of one page load, or a client that drops Set-Cookie), and at most
        LEGACY_UPGRADES_PER_HOUR sessions are minted this way, so a script cannot flood ui_sessions (S-R3)."""
        ua = request.headers.get("user-agent", "")
        if "Mozilla/" not in ua:
            return None
        key = self.client_ip(request) + "|" + sha256_hex(ua)
        with self.lock:
            self.upgrades = {k: v for k, v in self.upgrades.items() if now - v[2] < LEGACY_REUSE_S}
            hit = self.upgrades.get(key)
        if hit and self.session_row(hit[0]):
            return hit[0], hit[1]
        with self.lock:
            self.upgrade_times = [t for t in self.upgrade_times if now - t < 3600]
            if len(self.upgrade_times) >= LEGACY_UPGRADES_PER_HOUR:
                return None
            self.upgrade_times.append(now)
        secret, sid = self.create_session("", request)
        with self.lock:
            self.upgrades[key] = (secret, sid, now)
        return secret, sid

    def row_label(self, sid: str) -> Optional[str]:
        r = self.store.q("SELECT label FROM ui_sessions WHERE id=?", (sid,))
        return r[0]["label"] if r else None

    def set_session_cookie(self, resp: Any, secret: str) -> None:
        resp.set_cookie(SESSION_COOKIE, secret, httponly=True, samesite="lax", path="/", max_age=int(self.session_secs()))
        resp.delete_cookie(LEGACY_COOKIE, path="/")

    def find_device(self, short: str) -> dict:
        short = str(short or "").lower()
        if not re.fullmatch(r"[0-9a-f]{6,64}", short):
            raise HTTPException(404, "no such device")
        rows = self.store.q("SELECT * FROM ui_sessions WHERE id LIKE ? AND revoked=0", (short + "%",))
        if len(rows) != 1:
            raise HTTPException(404, "no such device")
        return dict(rows[0])

    @staticmethod
    def device_view(r: dict, current: Optional[str]) -> dict:
        return {"id": r["id"][:12], "label": r["label"], "created": r["created"], "last_seen": r["last_seen"],
                "expires": r["expires"], "current": r["id"] == current, "ip": r["ip"], "user_agent": r["user_agent"]}

    # ---------------------------------------------------------------- step-up (S7)
    def _pin_keys(self, request: Request) -> dict:
        """Buckets a wrong PIN counts in (S-R3): the principal (a legacy-token caller is ONE principal however many
        sessions it mints), its IP, and the whole hub, so minting sessions or rotating addresses buys no guesses."""
        p = getattr(request.state, "principal", None) or {}
        ip = "ip:" + self.client_ip(request)
        who = "ui-token" if p.get("legacy") else (p.get("session_id") or ip)
        out: dict = {}
        for k, lim in ((who, PIN_LIMITS[0]), (ip, PIN_LIMITS[1]), ("*", PIN_LIMITS[2])):
            out[k] = min(lim, out.get(k, lim))
        return out

    def pin_locked(self, request: Request, now: float) -> bool:
        return any(self._limited("pin", k, lim, PIN_WINDOW_S, now) for k, lim in self._pin_keys(request).items())

    def pin_failed(self, request: Request, now: float) -> None:
        for k in self._pin_keys(request):
            self._fail("pin", k, now)

    def pin_reset(self) -> None:
        """S-R9 offline reset: tools/reset-admin-pin.ps1 (elevated) writes data/private/pin-reset.json; the leader
        clears the replicated PIN and every step-up window, then deletes the file. Writing into data/private needs a
        local administrator, which is the proof of ownership."""
        p = private_dir(Path(self.ctx.data_dir)) / PIN_RESET_FILE
        if not p.is_file() or not self.ctx.is_leader:
            return
        had = bool(self.value("admin_pin_hash"))
        if had:
            self.write_secret("admin_pin_hash", None)
            for r in self.store.q("SELECT id FROM ui_sessions WHERE stepup_until IS NOT NULL"):
                self.store.touch("ui_sessions", "id", r["id"], "stepup_until=NULL")
        try:
            p.unlink()
        except OSError:
            pass
        self.ctx.event(self.ctx.node_id, "warn", "admin PIN cleared by the local reset file; set a new one in Settings"
                       if had else "admin PIN reset file found, but no PIN was set")

    def require_step_up(self, request: Request) -> None:
        now = self.clock()
        pin_hash = self.value("admin_pin_hash")
        if not pin_hash:
            raise StepUpRequired("admin PIN not set", "setup")
        hdr = request.headers.get("x-admin-pin")
        principal = getattr(request.state, "principal", None) or {}
        if hdr is not None:                               # scripts / Bearer clients: one-shot PIN header
            if self.pin_locked(request, now):
                raise HTTPException(429, "too many wrong PINs; wait 10 minutes")
            if verify_pin(hdr.strip(), pin_hash):
                return
            self.pin_failed(request, now)
            raise StepUpRequired("wrong PIN", "required")
        sid = principal.get("session_id")
        if sid:
            r = self.store.q("SELECT stepup_until FROM ui_sessions WHERE id=? AND revoked=0", (sid,))
            if r and (r[0]["stepup_until"] or 0) > now:
                return
        raise StepUpRequired("admin PIN required", "required")

    def stepup_valid(self, request: Request) -> bool:
        try:
            self.require_step_up(request)
            return True
        except HTTPException:
            return False

    # ---------------------------------------------------------------- enrollment (S3)
    def hub_url(self, request: Request) -> str:
        pub = str(self.cfg.get("public_url") or "").strip().rstrip("/")
        if pub:
            return pub
        cluster = self.ctx.cluster
        lead = cluster.leader_id or self.ctx.node_id
        peers = cluster.peers()
        ordered = [p for p in peers if p["id"] == lead] + [p for p in peers if p["id"] != lead]
        for p in ordered:                                  # MagicDNS first (works from a phone on the tailnet)
            for u in p["urls"]:
                if (urlsplit(u).hostname or "").lower().endswith(".ts.net"):
                    return u.rstrip("/")
        for p in ordered:
            for u in p["urls"]:
                if not _is_loopback_url(u):
                    return u.rstrip("/")
        base = str(request.base_url).rstrip("/")
        if not _is_loopback_url(base):
            return base
        port = request.url.port or self.cfg.get("port") or 8787
        return f"http://{socket.gethostname().lower()}:{port}"

    def code_row(self, code: str, kinds: tuple) -> Optional[dict]:
        n = normalize_code(code)
        if not re.fullmatch(r"[0-9A-Z]{4,64}", n):
            return None
        r = self.store.q("SELECT * FROM enroll_codes WHERE id=?", (sha256_hex(n),))
        if not r or r[0]["kind"] not in kinds or r[0]["used"] or (r[0]["expires"] or 0) <= self.clock():
            return None
        return dict(r[0])

    def consume(self, row: dict, used_by: Optional[str]) -> bool:
        return bool(self.store.touch("enroll_codes", "id", row["id"], "used=?, used_by=?", (self.clock(), used_by),
                                     "used IS NULL"))

    def enroll_state(self, r: dict) -> dict:
        now = self.clock()
        out = {"id": r["id"], "kind": r["kind"], "label": r["label"], "expires": r["expires"], "state": "pending"}
        if r["used"]:
            out["state"] = "redeemed"
            nid = r["used_by"] if r["kind"] == "machine" else None
            if r["kind"] == "machine":
                if nid:
                    n = self.store.q("SELECT id FROM nodes_live WHERE id=? AND last_seen>=?", (nid, r["used"]))
                else:
                    n = self.store.q("SELECT id FROM nodes WHERE created>=? ORDER BY created LIMIT 1", (r["used"],))
                if n:
                    out["state"], out["node_id"] = "joined", n[0]["id"]
                elif nid:
                    out["node_id"] = nid
            else:
                out["state"] = "joined"
        elif (r["expires"] or 0) <= now:
            out["state"] = "expired"
        return out

    def import_firstrun(self) -> None:
        """F writes data/private/firstrun-claim.json; the leader imports it once as a device code and deletes it."""
        p = private_dir(Path(self.ctx.data_dir)) / "firstrun-claim.json"
        if not p.is_file() or not self.ctx.is_leader:
            return
        d = _read_json(p)
        h = str(d.get("code_sha256") or "").lower()
        if re.fullmatch(r"[0-9a-f]{64}", h) and not self.store.q("SELECT 1 FROM enroll_codes WHERE id=?", (h,)):
            now = self.clock()
            exp = float(d.get("expires") or now + float(self.conf("enroll_ttl_s")))
            self.store.x("INSERT INTO enroll_codes(id,kind,label,created,expires,used,used_by,created_by,updated)"
                         " VALUES(?,?,?,?,?,NULL,NULL,?,?)",
                         (h, "device", str(d.get("label") or "first device")[:64], now, exp, "firstrun", now))
            self.ctx.event(self.ctx.node_id, "info", "first-run claim code imported")
        try:
            p.unlink()
        except OSError:
            pass

    # ---------------------------------------------------------------- signing (S6)
    def verify_bundle(self, release_dir: Path) -> None:
        pub = self.value("signing_pubkey")
        if not pub:
            return
        try:
            verify_release(release_dir, pub)
        except BundleError:
            ver = ""
            try:
                ver = (Path(release_dir) / "VERSION").read_text(encoding="utf-8").strip()[:32]
            except OSError:
                pass
            with self.lock:
                self.refused[ver or "?"] = self.clock()
            self.ctx.event(self.ctx.node_id, "error", f"bundle {ver or '?'} refused: signature missing or invalid")
            raise

    def signing_view(self) -> dict:
        pub = self.value("signing_pubkey")
        kid = None
        if pub:
            try:
                kid = key_id(pub)
            except ValueError:
                kid = None
        return {"pinned": bool(pub), "key_id": kid}

    # ---------------------------------------------------------------- alerts (S1, S6)
    def alert_sources(self, now: float) -> list:
        out = []
        for r in self.store.q("SELECT id,last_seen,metrics,info FROM nodes_live ORDER BY id"):
            if now - (r["last_seen"] or 0) >= 30:
                continue
            m = jloads(r["metrics"], {})
            jt = m.get("job_token")
            if jt is None:
                out.append({"level": "warn", "kind": "job_token_unknown", "node_id": r["id"],
                            "msg": f"{r['id']} runs an agent older than 2.3: its jobs may run with a full admin token"})
            elif jt != "filtered":
                err = m.get("job_token_error")
                out.append({"level": "error", "kind": "job_token_full", "node_id": r["id"],
                            "msg": f"{r['id']} runs jobs with a full admin token ({jt}"
                                   + (f": {str(err)[:160]}" if err else "") + ")"})
            wd = m.get("watchdog") if isinstance(m.get("watchdog"), dict) else {}
            if wd.get("signature_refused"):
                out.append({"level": "error", "kind": "bundle_unsigned", "node_id": r["id"],
                            "msg": f"{r['id']} refused release {wd['signature_refused']}: signature missing or invalid"})
        with self.lock:
            recent = {v: t for v, t in self.refused.items() if now - t < 86400}
            self.refused = recent
        for v in sorted(recent):
            out.append({"level": "error", "kind": "bundle_unsigned", "node_id": self.ctx.node_id,
                        "msg": f"publish of {v} refused: bundle signature missing or invalid"})
        return out

    # ---------------------------------------------------------------- hooks
    def on_tick(self, now: float, settled: bool) -> None:
        self.adopt()
        if settled:
            self.seed()
        self.import_firstrun()
        self.pin_reset()

    def on_report(self, node_id: str, body: dict, resp: dict, now: float) -> None:
        self.rotate_hint(resp, now)


def get(ctx: Any) -> Security:
    return ctx.app.state.security


def rotate(ctx: Any, which: list[str]) -> dict:
    """For R6 (retire with rotate_tokens): rotate on the leader, no version gate."""
    return get(ctx).rotate([w for w in which if w in ("agent", "cluster")])


# ====================================================================== routes
def register(app: Any, ctx: Any) -> None:
    """Add S routes to `app` and S hooks/slots to `ctx` (forge_ctx.HubContext)."""
    sec = Security(ctx)
    app.state.security = sec
    store, cfg, clock = ctx.store, ctx.cfg, ctx.clock

    ctx.ui_auth = sec.ui_auth
    ctx.check_agent_token = sec.check_agent_token
    ctx.check_cluster_token = sec.check_cluster_token
    ctx.require_step_up = sec.require_step_up
    ctx.verify_bundle = sec.verify_bundle
    ctx.releases.verify = lambda d: ctx.verify_bundle(d) if ctx.verify_bundle else None
    ctx.on_tick.append(sec.on_tick)
    ctx.on_report.append(sec.on_report)
    ctx.on_system_report.append(sec.on_report)
    ctx.alert_sources.append(sec.alert_sources)
    ctx.status_extras.append(lambda: {"signing": sec.signing_view()})
    ctx.local_paths.discard("/api/enroll")                 # A-S2 (also in app.LOCAL_PATHS)
    ctx.local_paths.add("/api/claim")
    sec.import_firstrun()

    @app.exception_handler(StepUpRequired)
    async def _step_up_handler(request: Request, exc: StepUpRequired):
        return JSONResponse({"detail": exc.detail, "step_up": exc.step_up}, status_code=403)

    @app.middleware("http")
    async def _session_upgrade(request: Request, call_next: Callable):
        resp = await call_next(request)
        secret = request.scope.get("state", {}).get("forge_new_sid")
        if secret:
            sec.set_session_cookie(resp, secret)
        return resp

    ui = [Depends(ctx.require_ui)]

    def principal(request: Request) -> dict:
        return getattr(request.state, "principal", None) or {}

    def body_label(body: dict) -> str:
        lab = body.get("label")
        if lab is not None and (not isinstance(lab, str) or len(lab) > 64):
            raise HTTPException(400, "label must be a string of at most 64 characters")
        return (lab or "").strip()

    async def leader_or_local(request: Request):
        """Forward to the leader (sessions are replicated rows the leader writes). With no reachable leader the
        standby answers itself (degraded: the session then exists only on this hub)."""
        fwd, body = await ctx.leader_json(request)
        if fwd is not None and fwd.status_code == 503 and "x-forge-via" not in request.headers:
            try:
                body = json.loads(await request.body() or b"{}")
            except ValueError:
                raise HTTPException(422, "invalid json")
            return None, body if isinstance(body, dict) else {}
        return fwd, body

    # ---------------------------------------------------------------- S4 login / logout / claim / devices
    @app.post("/api/login")
    async def login(request: Request):
        fwd, body = await leader_or_local(request)
        if fwd is not None:
            return fwd
        now, ip = clock(), sec.client_ip(request)
        if sec._limited("login", ip, 10, 60, now):
            raise HTTPException(429, "too many attempts; wait a minute")
        tok = body.get("token")
        if not isinstance(tok, str) or not sec.ui_token_ok(tok.strip()):
            sec._fail("login", ip, now)
            raise HTTPException(401, "wrong token")
        secret, sid = sec.create_session(body_label(body), request)
        resp = JSONResponse({"ok": True, "session": {"id": sid[:12], "label": sec.row_label(sid)}})
        sec.set_session_cookie(resp, secret)
        return resp

    @app.post("/api/claim")
    async def claim(request: Request):
        fwd, body = await leader_or_local(request)
        if fwd is not None:
            return fwd
        now, ip = clock(), sec.client_ip(request)
        if sec._limited("code", ip, 10, 60, now):
            raise HTTPException(429, "too many attempts; wait a minute")
        row = sec.code_row(str(body.get("code") or ""), ("device",))
        if not row:
            sec._fail("code", ip, now)
            raise HTTPException(401, "bad, used or expired code")
        secret, sid = sec.create_session(body_label(body) or (row["label"] or ""), request)
        if not sec.consume(row, sid):
            store.touch("ui_sessions", "id", sid, "revoked=1")
            raise HTTPException(401, "bad, used or expired code")
        resp = JSONResponse({"ok": True, "session": {"id": sid[:12], "label": sec.row_label(sid)}})
        sec.set_session_cookie(resp, secret)
        return resp

    @app.post("/api/logout")
    async def logout(request: Request):
        fwd, _ = await leader_or_local(request)
        if fwd is not None:
            return fwd
        row = sec.session_row(request.cookies.get(SESSION_COOKIE, ""))
        if row:
            store.touch("ui_sessions", "id", row["id"], "revoked=1")
            ctx.event(None, "info", f"device signed out: {row['label']}")
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        resp.delete_cookie(LEGACY_COOKIE, path="/")
        return resp

    @app.get("/api/devices", dependencies=ui)
    def devices(request: Request) -> dict:
        cur = principal(request).get("session_id")
        rows = store.q("SELECT * FROM ui_sessions WHERE revoked=0 AND expires>? ORDER BY last_seen DESC", (clock(),))
        return {"devices": [sec.device_view(dict(r), cur) for r in rows]}

    @app.patch("/api/devices/{did}", dependencies=ui)
    def device_patch(did: str, body: dict, request: Request) -> dict:
        r = sec.find_device(did)
        lab = body_label(body)
        if not lab:
            raise HTTPException(400, "label required")
        store.touch("ui_sessions", "id", r["id"], "label=?", (lab,))
        return sec.device_view(dict(r, label=lab), principal(request).get("session_id"))

    @app.delete("/api/devices/{did}", dependencies=ui)
    def device_delete(did: str, request: Request):
        r = sec.find_device(did)
        store.touch("ui_sessions", "id", r["id"], "revoked=1")
        ctx.event(None, "warn", f"device revoked: {r['label']}")
        resp = JSONResponse({"ok": True})
        if r["id"] == principal(request).get("session_id"):
            resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    # ---------------------------------------------------------------- S3 enrollment
    def make_enroll(request: Request, kind: str, label: str) -> dict:
        if kind not in ("machine", "device"):
            raise HTTPException(400, "kind must be 'machine' or 'device'")
        now = clock()
        code = new_code()
        cid = code_id(code)
        exp = now + float(sec.conf("enroll_ttl_s"))
        by = principal(request).get("session_id") or "token"
        store.x("INSERT INTO enroll_codes(id,kind,label,created,expires,used,used_by,created_by,updated)"
                " VALUES(?,?,?,?,?,NULL,NULL,?,?)", (cid, kind, label or None, now, exp, by[:12], now))
        hub = sec.hub_url(request)
        out = {"id": cid, "code": code, "kind": kind, "expires": exp, "hub": hub}
        if kind == "machine":
            out["command"] = f"irm '{hub}/agent/install.ps1?code={code}' | iex"
        else:
            out["url"] = f"{hub}/?claim={code}"
        ctx.event(None, "info", f"enrollment code created ({kind}{', ' + label if label else ''})")
        return out

    @app.post("/api/enroll", dependencies=ui)
    def enroll_post(body: dict, request: Request) -> dict:
        return make_enroll(request, str(body.get("kind") or "machine"), body_label(body))

    @app.get("/api/enroll", dependencies=ui)
    def enroll_legacy(request: Request) -> dict:
        return make_enroll(request, "machine", "")

    @app.get("/api/enroll/{eid}", dependencies=ui)
    def enroll_get(eid: str) -> dict:
        r = store.q("SELECT * FROM enroll_codes WHERE id=?", (str(eid).lower(),))
        if not r:
            raise HTTPException(404, "no such enrollment")
        return sec.enroll_state(dict(r[0]))

    @app.delete("/api/enroll/{eid}", dependencies=ui)
    def enroll_delete(eid: str) -> dict:
        if not store.q("SELECT 1 FROM enroll_codes WHERE id=?", (str(eid).lower(),)):
            raise HTTPException(404, "no such enrollment")
        store.delete_row("enroll_codes", str(eid).lower())
        return {"ok": True}

    @app.get("/agent/install.ps1", response_class=PlainTextResponse)
    def installer(request: Request, t: str = "", code: str = "") -> str:
        now, ip = clock(), sec.client_ip(request)
        if sec._limited("code", ip, 10, 60, now):
            raise HTTPException(429, "too many attempts; wait a minute")
        if code:
            if not sec.code_row(code, ("machine",)):      # checked, NOT consumed (bootstrap redeems it)
                sec._fail("code", ip, now)
                raise HTTPException(401, "bad or expired code")
            the_code, the_tok = format_code(code), ""
        elif t and sec.check_agent_token(t):
            the_code, the_tok = "", t
        else:
            sec._fail("code", ip, now)
            raise HTTPException(401, "bad token")
        tpl = (Path(ctx.release_dir) / "agent" / "install.ps1").read_text(encoding="utf-8")
        return (tpl.replace("__HUB__", str(request.base_url).rstrip("/"))
                .replace("__CODE__", the_code).replace("__TOKEN__", the_tok))

    @app.get("/cluster/bootstrap")
    def bootstrap(request: Request, t: str = "", code: str = "", node: str = ""):
        now, ip = clock(), sec.client_ip(request)
        if code:
            fwd = ctx.leader_only_or_forward(request, b"")
            if fwd is not None:
                return fwd
        if sec._limited("code", ip, 10, 60, now):
            raise HTTPException(429, "too many attempts; wait a minute")
        hint = None
        if code:
            row = sec.code_row(code, ("machine",))
            nid = re.sub(r"[^a-z0-9._-]", "", str(node).strip().lower())[:64] or None
            if not row or not sec.consume(row, nid):
                sec._fail("code", ip, now)
                raise HTTPException(401, "bad, used or expired code")
            hint = row["label"] or nid
            ctx.event(nid, "ok", f"enrollment code redeemed{' by ' + nid if nid else ''}")
        elif not (t and sec.check_agent_token(t)):
            sec._fail("code", ip, now)
            raise HTTPException(401, "bad token")
        version, sha = ctx.releases.latest()
        out = {"agent_token": sec.value("agent_token"),
               "peers": [{"id": p["id"], "priority": p["priority"], "urls": p["urls"]} for p in ctx.cluster.peers()],
               "bundle_version": version, "bundle_sha256": sha, "node_hint": hint}
        # S-R1: the agent token is readable by filtered jobs (config.json), so `t=` alone never buys the cluster token
        # (which replicates every table). Only a redeemed one-time code does; `legacy_bootstrap_secrets: true` in
        # config.json restores the 2.2 behaviour for 2.2 installers joining a new machine with `?t=`.
        if code or cfg.get("legacy_bootstrap_secrets") is True:
            out.update(cluster_token=sec.value("cluster_token"), signing_pubkey=sec.value("signing_pubkey"),
                       ui_token_hash=sec.value("ui_token_hash"))
        return out

    # ---------------------------------------------------------------- S7 step-up
    @app.get("/api/stepup", dependencies=ui)
    def stepup_get(request: Request) -> dict:
        sid = principal(request).get("session_id")
        until = None
        if sid:
            r = store.q("SELECT stepup_until FROM ui_sessions WHERE id=?", (sid,))
            v = r[0]["stepup_until"] if r else None
            until = v if v and v > clock() else None
        return {"pin_set": bool(sec.value("admin_pin_hash")), "valid_until": until}

    @app.post("/api/stepup", dependencies=ui)
    def stepup_post(body: dict, request: Request) -> dict:
        now = clock()
        pin_hash = sec.value("admin_pin_hash")
        if not pin_hash:
            raise StepUpRequired("admin PIN not set", "setup")
        sid = principal(request).get("session_id")
        if not sid:
            raise HTTPException(400, "step-up needs a device session (sign in on this device) or the X-Admin-Pin header")
        if sec.pin_locked(request, now):
            raise HTTPException(429, "too many wrong PINs; wait 10 minutes")
        if not verify_pin(str(body.get("pin") or ""), pin_hash):
            sec.pin_failed(request, now)
            raise StepUpRequired("wrong PIN", "required")
        until = now + float(sec.conf("stepup_minutes")) * 60
        store.touch("ui_sessions", "id", sid, "stepup_until=?", (until,))
        return {"ok": True, "valid_until": until}

    @app.put("/api/stepup/pin", dependencies=ui)
    def stepup_pin(body: dict, request: Request) -> dict:
        now = clock()
        pin = str(body.get("pin") or "")
        if not PIN_RE.match(pin):
            raise HTTPException(400, "PIN must be exactly 6 digits")
        cur = sec.value("admin_pin_hash")
        p = principal(request)
        if cur:
            old = body.get("old_pin")
            if old is not None:
                if sec.pin_locked(request, now):
                    raise HTTPException(429, "too many wrong PINs; wait 10 minutes")
                if not verify_pin(str(old), cur):
                    sec.pin_failed(request, now)
                    raise StepUpRequired("wrong PIN", "required")
            else:
                sec.require_step_up(request)
        sec.write_secret("admin_pin_hash", hash_pin(pin))
        if p.get("session_id"):
            store.touch("ui_sessions", "id", p["session_id"], "stepup_until=?",
                        (now + float(sec.conf("stepup_minutes")) * 60,))
        ctx.event(None, "warn", "admin PIN " + ("changed" if cur else "set"))
        return {"ok": True}

    # ---------------------------------------------------------------- S5 rotation
    @app.post("/api/tokens/rotate", dependencies=ui)
    def tokens_rotate(body: dict, request: Request) -> dict:
        which = body.get("which") or ["agent", "cluster"]
        if not isinstance(which, list) or not which or any(w not in ("agent", "cluster") for w in which):
            raise HTTPException(400, "which must be a list of 'agent' / 'cluster'")
        if body.get("confirm") is not True:
            raise HTTPException(400, "confirmation required")
        sec.require_step_up(request)
        if not body.get("force"):
            old = sec.old_agents(clock()) if "agent" in which else []
            old += [h for h in (sec.old_hubs() if "cluster" in which else []) if h not in old]
            if old:
                raise HTTPException(409, f"agents older than 2.3 would be locked out: {', '.join(old)}")
        return sec.rotate(list(dict.fromkeys(which)))

    @app.get("/api/tokens", dependencies=ui)
    def tokens_view() -> dict:
        out = {}
        for w in ("agent", "cluster"):
            r = sec.row(f"{w}_token")
            rot = r["rotated"] if r else None
            out[w] = {"rotated": rot, "grace_until": (rot + sec.grace()) if rot else None}
        return out

    # ---------------------------------------------------------------- S6 signing key
    @app.get("/api/security/signing-key", dependencies=ui)
    def signing_get() -> dict:
        return sec.signing_view()

    @app.put("/api/security/signing-key", dependencies=ui)
    def signing_put(body: dict, request: Request) -> dict:
        xml = body.get("pubkey_xml")
        try:
            n, _ = parse_pubkey(xml if isinstance(xml, str) else "")
        except ValueError as e:
            raise HTTPException(400, str(e))
        if n.bit_length() < MIN_RSA_BITS:
            raise HTTPException(400, f"RSA key must be at least {MIN_RSA_BITS} bits (got {n.bit_length()})")
        sec.require_step_up(request)
        canon = canonical_pubkey(xml)
        cur = sec.value("signing_pubkey")
        if cur and canonical_pubkey(cur) != canon and body.get("replace") is not True:
            raise HTTPException(409, "a different signing key is already pinned; pass replace: true")
        sec.write_secret("signing_pubkey", canon)
        ctx.event(None, "warn", f"bundle signing key pinned ({key_id(canon)})")
        return {"ok": True, "key_id": key_id(canon)}
