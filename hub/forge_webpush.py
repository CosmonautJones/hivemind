"""Web Push (RFC 8030) with VAPID (RFC 8292) and aes128gcm payload encryption (RFC 8291 / RFC 8188).

Owner: workstream N (CONTRACT-phase3.md §5 N2). Pure functions on top of `cryptography`; no HTTP here (the caller
POSTs the returned body + headers, so tests can use a fake transport). `cryptography` is imported lazily: a hub whose
venv lacks it still starts, and Web Push reports itself unavailable.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import time
from typing import Optional
from urllib.parse import urlsplit

RECORD_SIZE = 4096


class WebPushError(Exception):
    pass


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_dec(s: str) -> bytes:
    s = (s or "").strip().replace("+", "-").replace("/", "_").rstrip("=")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def available() -> bool:
    try:
        import cryptography.hazmat.primitives.asymmetric.ec  # noqa: F401
        return True
    except ImportError:
        return False


def _ec():
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError as e:  # pragma: no cover - depends on the venv
        raise WebPushError("Web Push needs the 'cryptography' package on the hub") from e
    return ec


def generate_vapid_keys() -> tuple[str, str]:
    """-> (private key: base64url of the 32-byte scalar, public key: base64url of the 65-byte uncompressed point)."""
    ec = _ec()
    key = ec.generate_private_key(ec.SECP256R1())
    d = key.private_numbers().private_value.to_bytes(32, "big")
    return b64u(d), b64u(_public_bytes(key.public_key()))


def _public_bytes(pub) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return pub.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def _private_from_b64(priv_b64: str):
    ec = _ec()
    return ec.derive_private_key(int.from_bytes(b64u_dec(priv_b64), "big"), ec.SECP256R1())


def public_from_private(priv_b64: str) -> str:
    return b64u(_public_bytes(_private_from_b64(priv_b64).public_key()))


def valid_subscription_keys(p256dh: str, auth: str) -> bool:
    try:
        pk, au = b64u_dec(p256dh), b64u_dec(auth)
    except (ValueError, TypeError):
        return False
    if len(pk) != 65 or pk[0] != 4 or len(au) != 16:
        return False
    try:
        ec = _ec()
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), pk)
    except (ValueError, WebPushError):
        return False
    return True


def vapid_header(endpoint: str, priv_b64: str, subject: str, now: Optional[float] = None,
                 ttl_s: int = 12 * 3600) -> str:
    """`Authorization: vapid t=<ES256 JWT>, k=<public key>` for the push service that owns `endpoint`."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    ec = _ec()
    parts = urlsplit(endpoint)
    aud = f"{parts.scheme}://{parts.netloc}"
    now = time.time() if now is None else now
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64u(json.dumps({"aud": aud, "exp": int(now) + ttl_s, "sub": subject}, separators=(",", ":")).encode())
    signing_input = f"{head}.{claims}".encode("ascii")
    key = _private_from_b64(priv_b64)
    r, s = decode_dss_signature(key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    jwt = f"{head}.{claims}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    return f"vapid t={jwt}, k={b64u(_public_bytes(key.public_key()))}"


def _hkdf_expand_1(prk: bytes, info: bytes, length: int) -> bytes:
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def encrypt(payload: bytes, p256dh: str, auth: str, *, salt: Optional[bytes] = None, as_private=None) -> bytes:
    """aes128gcm content coding (single record). `salt` / `as_private` are injectable for test vectors."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    ec = _ec()
    ua_pub_bytes, auth_secret = b64u_dec(p256dh), b64u_dec(auth)
    ua_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub_bytes)
    as_key = as_private or ec.generate_private_key(ec.SECP256R1())
    as_pub_bytes = _public_bytes(as_key.public_key())
    ecdh = as_key.exchange(ec.ECDH(), ua_pub)
    prk_key = hmac.new(auth_secret, ecdh, hashlib.sha256).digest()
    ikm = _hkdf_expand_1(prk_key, b"WebPush: info\x00" + ua_pub_bytes + as_pub_bytes, 32)
    salt = salt or os.urandom(16)
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    cek = _hkdf_expand_1(prk, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf_expand_1(prk, b"Content-Encoding: nonce\x00", 12)
    if len(payload) + 1 + 16 > RECORD_SIZE:
        raise WebPushError("push payload too large")
    ct = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack("!IB", RECORD_SIZE, len(as_pub_bytes)) + as_pub_bytes + ct


def decrypt(body: bytes, ua_private, auth: str) -> bytes:
    """Inverse of `encrypt`, as a browser does it (used by the tests to check real payloads)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    ec = _ec()
    salt, (rs, idlen) = body[:16], struct.unpack("!IB", body[16:21])
    as_pub_bytes, ct = body[21:21 + idlen], body[21 + idlen:]
    ua_pub_bytes = _public_bytes(ua_private.public_key())
    ecdh = ua_private.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub_bytes))
    prk_key = hmac.new(b64u_dec(auth), ecdh, hashlib.sha256).digest()
    ikm = _hkdf_expand_1(prk_key, b"WebPush: info\x00" + ua_pub_bytes + as_pub_bytes, 32)
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    pt = AESGCM(_hkdf_expand_1(prk, b"Content-Encoding: aes128gcm\x00", 16)).decrypt(
        _hkdf_expand_1(prk, b"Content-Encoding: nonce\x00", 12), ct, None)
    return pt.rstrip(b"\x00")[:-1]          # strip padding + the 0x02 last-record delimiter


def build_request(sub: dict, payload: dict, priv_b64: str, subject: str, *, ttl: int = 86400,
                  urgency: str = "normal", topic: Optional[str] = None, now: Optional[float] = None
                  ) -> tuple[str, dict, bytes]:
    """-> (endpoint, headers, body) for one subscription {endpoint, p256dh, auth}."""
    body = encrypt(json.dumps(payload, separators=(",", ":")).encode("utf-8"), sub["p256dh"], sub["auth"])
    headers = {"Authorization": vapid_header(sub["endpoint"], priv_b64, subject, now), "TTL": str(int(ttl)),
               "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "Urgency": urgency}
    if topic:     # replaces an undelivered message with the same topic (max 32 base64url chars)
        headers["Topic"] = b64u(hashlib.sha256(topic.encode("utf-8")).digest())[:32]
    return sub["endpoint"], headers, body
