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
import threading
from collections import deque
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
        # Writers come from more than one thread (the plant clock and the web
        # server), so sequence number, previous hash and the file write must
        # change together.
        self.lock = threading.Lock()
        self.prev, self.seq = GENESIS, 0
        if self.path.exists():
            ok, bad = verify(self.path, key)
            if not ok:
                raise ValueError(f"audit log {self.path} is corrupted at record {bad}")
            tail = self.tail(1)
            if tail:
                self.prev, self.seq = tail[0]["hash"], tail[0]["seq"] + 1

    def append(self, event: dict, ts: datetime | None = None) -> dict:
        with self.lock:
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

    def tail(self, n: int) -> list[dict]:
        """The last n records, oldest first; unreadable lines are skipped."""
        if not self.path.exists():
            return []
        with self.path.open() as f:
            lines = deque(f, maxlen=n)
        out = []
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out

    def check(self, n: int = 200) -> tuple[bool, int | None, list[dict]]:
        """Verify the whole chain and return the last n records, as one consistent snapshot."""
        with self.lock:
            ok, bad = verify(self.path, self.key)
            return ok, bad, self.tail(n)


def verify(path: str | Path, key: bytes | None = None) -> tuple[bool, int | None]:
    """Check the whole chain. Returns (ok, index of the first bad record)."""
    prev = GENESIS
    with Path(path).open() as f:  # streamed: memory stays flat however long the log grows
        for n, line in enumerate(f):
            try:
                rec = json.loads(line)
                body = {k: rec[k] for k in ("seq", "ts", "event", "prev")}
                if rec["seq"] != n or rec["prev"] != prev:
                    return False, n
                if not hmac.compare_digest(str(rec["hash"]), _digest(prev, body, key)):
                    return False, n
            except (ValueError, KeyError, TypeError):
                return False, n
            prev = rec["hash"]
    return True, None
