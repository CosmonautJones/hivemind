"""Release bundles: build/serve the running release, validate and store published ones."""
from __future__ import annotations

import hashlib
import io
import re
import shutil
import threading
import zipfile
from pathlib import Path
from typing import Callable, Optional

BUNDLE_ITEMS = ("hub", "agent", "watchdog", "requirements.txt", "VERSION")
SIG_FILE = "bundle.sig"            # S6: signature over the file manifest (forge_security.bundle_manifest), zip root
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
MAX_BUNDLE_BYTES = 200 * 1024 * 1024
MAX_UNPACKED_BYTES = 600 * 1024 * 1024
_FIXED_TIME = (2020, 1, 1, 0, 0, 0)


class BundleError(ValueError):
    """Raised for an invalid published bundle. `status` is the HTTP status to return."""

    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def parse_version(v: Optional[str]) -> tuple:
    try:
        return tuple(int(p) for p in str(v).strip().split("."))
    except ValueError:
        return (0,)


def read_version(root: Path) -> str:
    try:
        return (root / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def _skip(p: Path) -> bool:
    return "__pycache__" in p.parts or p.suffix == ".pyc"


def build_zip(root: Path) -> bytes:
    """Deterministic zip (sorted, fixed timestamps) so every node computes the same sha256."""
    files: list[Path] = []
    for item in BUNDLE_ITEMS + (SIG_FILE,):      # bundle.sig travels with the re-zip so it still verifies
        p = root / item
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            files.extend(f for f in p.rglob("*") if f.is_file() and not _skip(f.relative_to(root)))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(files, key=lambda f: f.relative_to(root).as_posix()):
            zi = zipfile.ZipInfo(f.relative_to(root).as_posix(), _FIXED_TIME)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            z.writestr(zi, f.read_bytes())
    return buf.getvalue()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_bundle(data: bytes) -> str:
    """Return the bundle's version or raise BundleError. Does not compare against current versions."""
    if len(data) > MAX_BUNDLE_BYTES:
        raise BundleError("bundle too large", 413)
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise BundleError("not a zip file")
    with z:
        names = z.namelist()
        total = 0
        for zi in z.infolist():
            n = zi.filename
            if n.startswith(("/", "\\")) or ".." in Path(n).parts or re.match(r"^[A-Za-z]:", n):
                raise BundleError(f"unsafe path in bundle: {n}")
            total += zi.file_size
        if total > MAX_UNPACKED_BYTES:
            raise BundleError("bundle too large when unpacked", 413)
        if "VERSION" not in names or "hub/app.py" not in names:
            raise BundleError("bundle must contain VERSION and hub/app.py")
        version = z.read("VERSION").decode("utf-8", "replace").strip()
        if not VERSION_RE.match(version):
            raise BundleError(f"bad VERSION {version!r}; expected MAJOR.MINOR.PATCH")
        try:
            compile(z.read("hub/app.py").decode("utf-8"), "hub/app.py", "exec")
        except (SyntaxError, ValueError, UnicodeDecodeError) as e:
            raise BundleError(f"hub/app.py does not compile: {e}")
    return version


class Releases:
    """The running release (read-only, may be zipped on demand) plus stored published releases.

    Stored layout: <releases_dir>/<ver>/{bundle.zip, bundle.sha256, <extracted bundle files>}
    """

    def __init__(self, release_dir: Path, releases_dir: Path):
        self.release_dir, self.releases_dir = Path(release_dir), Path(releases_dir)
        self.running_version = read_version(self.release_dir)
        self._lock = threading.Lock()
        self._running_zip: Optional[tuple[bytes, str]] = None
        # S6: verify(extracted_dir) raises BundleError for a missing/bad signature (set by forge_security.register)
        self.verify: Optional[Callable[[Path], None]] = None

    def running_bundle(self) -> tuple[bytes, str]:
        with self._lock:
            if self._running_zip is None:
                data = build_zip(self.release_dir)
                self._running_zip = (data, sha256(data))
            return self._running_zip

    def stored_versions(self) -> list[str]:
        if not self.releases_dir.is_dir():
            return []
        out = [d.name for d in self.releases_dir.iterdir()
               if d.is_dir() and VERSION_RE.match(d.name) and (d / "bundle.zip").is_file()]
        return sorted(out, key=parse_version)

    def latest_version(self) -> str:
        cands = [self.running_version, *self.stored_versions()]
        return max(cands, key=parse_version)

    def get(self, version: Optional[str] = None) -> Optional[tuple[str, bytes, str]]:
        """(version, zip bytes, sha256) for `version` (default: latest available) or None."""
        version = version or self.latest_version()
        stored = self.releases_dir / version / "bundle.zip"
        if VERSION_RE.match(version) and stored.is_file():
            data = stored.read_bytes()
            return version, data, sha256(data)
        if version == self.running_version:
            data, sha = self.running_bundle()
            return version, data, sha
        return None

    def latest(self) -> tuple[str, str]:
        """(version, sha256) of the latest available bundle."""
        ver = self.latest_version()
        f = self.releases_dir / ver / "bundle.sha256"
        if f.is_file():
            return ver, f.read_text(encoding="ascii").strip()
        got = self.get(ver)
        return (ver, got[2]) if got else (ver, "")

    def publish(self, data: bytes) -> tuple[str, str]:
        version = validate_bundle(data)
        if parse_version(version) <= parse_version(self.latest_version()):
            raise BundleError(f"version {version} is not newer than {self.latest_version()}", 409)
        dest = self.releases_dir / version
        tmp = self.releases_dir / f".{version}.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                z.extractall(tmp)
            if self.verify is not None:
                self.verify(tmp)                     # pinned signing key: refuse unsigned / badly signed bundles
            digest = sha256(data)
            (tmp / "bundle.zip").write_bytes(data)
            (tmp / "bundle.sha256").write_text(digest, encoding="ascii")
            shutil.rmtree(dest, ignore_errors=True)
            tmp.rename(dest)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return version, digest
