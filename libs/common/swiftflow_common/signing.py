"""Webhook signatures: ``SwiftFlow-Signature: t=<unix>,v1=<hex hmac-sha256("<t>.<body>")>``."""

from __future__ import annotations

import hashlib
import hmac
import time

TOLERANCE_SECONDS = 300


def compute_signature(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode() + body, hashlib.sha256)
    return mac.hexdigest()


def sign(secret: str, body: bytes, timestamp: int | None = None) -> str:
    t = int(time.time()) if timestamp is None else timestamp
    return f"t={t},v1={compute_signature(secret, t, body)}"


def parse_header(header: str) -> tuple[int | None, list[str]]:
    ts: int | None = None
    sigs: list[str] = []
    for part in (header or "").split(","):
        k, _, v = part.strip().partition("=")
        if k == "t":
            try:
                ts = int(v)
            except ValueError:
                return None, []
        elif k == "v1" and v:
            sigs.append(v)
    return ts, sigs


def verify(secret: str, header: str, body: bytes, now: int | None = None, tolerance: int = TOLERANCE_SECONDS) -> tuple[bool, str]:
    """Returns ``(valid, reason)``. Constant-time comparison; rejects stale timestamps."""
    ts, sigs = parse_header(header)
    if ts is None or not sigs:
        return False, "malformed_signature"
    current = int(time.time()) if now is None else now
    if abs(current - ts) > tolerance:
        return False, "stale_timestamp"
    expected = compute_signature(secret, ts, body)
    if any(hmac.compare_digest(expected, s) for s in sigs):
        return True, "ok"
    return False, "signature_mismatch"
