"""Tamper-evident audit log of every alarm and diagnosis.

Each record carries the hash of the previous record, so editing, deleting or
reordering any past entry breaks the chain from that point on. With a secret
key the chain uses HMAC-SHA256, so someone who can write the file but does
not hold the key cannot forge a consistent chain either. In a plant the file
would also be shipped to write-once storage or a syslog/SIEM collector.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


def _canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False, default=str).encode()


def _digest(prev: str, body: dict, key: bytes | None) -> str:
    msg = prev.encode() + _canonical(body)
    if key:
        return hmac.new(key, msg, hashlib.sha256).hexdigest()
    return hashlib.sha256(msg).hexdigest()


class AuditLog:
    def __init__(self, path: str | Path, key: bytes | None = None):
        self.path = Path(path)
        self.key = key
        self.prev, self.seq = GENESIS, 0
        if self.path.exists():
            ok, bad = verify(self.path, key)
            if not ok:
                raise ValueError(f"audit log {self.path} is corrupted at record {bad}")
            lines = self.path.read_text().splitlines()
            if lines:
                last = json.loads(lines[-1])
                self.prev, self.seq = last["hash"], last["seq"] + 1

    def append(self, event: dict, ts: datetime | None = None) -> dict:
        body = {
            "seq": self.seq,
            "ts": (ts or datetime.now(timezone.utc)).isoformat(),
            "event": event,
            "prev": self.prev,
        }
        rec = {**body, "hash": _digest(self.prev, body, self.key)}
        with self.path.open("a") as f:
            f.write(json.dumps(rec, sort_keys=True, allow_nan=False, default=str) + "\n")
        self.prev, self.seq = rec["hash"], self.seq + 1
        return rec


def verify(path: str | Path, key: bytes | None = None) -> tuple[bool, int | None]:
    """Check the whole chain. Returns (ok, index of the first bad record)."""
    prev = GENESIS
    for n, line in enumerate(Path(path).read_text().splitlines()):
        try:
            rec = json.loads(line)
            body = {k: rec[k] for k in ("seq", "ts", "event", "prev")}
        except (ValueError, KeyError, TypeError):
            return False, n
        if rec["seq"] != n or rec["prev"] != prev:
            return False, n
        if not hmac.compare_digest(rec["hash"], _digest(prev, body, key)):
            return False, n
        prev = rec["hash"]
    return True, None
