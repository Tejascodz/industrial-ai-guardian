"""The whole secured pipeline, end to end, on one machine.

    GUARDIAN_KEY=<secret> python scripts/opcua_demo.py [--scenario NAME] [--split SPLIT]

  plant (simulated run) -> OPC UA server, SignAndEncrypt, read-only ruleset
        -> Guardian client (certificate-authenticated, read-only)
        -> input validation -> Guardian (signed model, verified on load)
        -> tamper-evident audit log (HMAC hash chain)

Run scripts/export_model.py first. The demo plays a recorded run through the
OPC UA server quickly (one sample per tick) and prints alarms as they happen.
"""

from __future__ import annotations

import argparse
import contextlib
import asyncio
import logging
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from guardian import artifacts  # noqa: E402
from guardian.audit import AuditLog, verify  # noqa: E402
from guardian.evaluate import persisted  # noqa: E402
from guardian.features import SP_NAMES, load_split  # noqa: E402
from guardian.opcua import PlantServer, SecureReadOnlyClient, make_identity  # noqa: E402
from guardian.tags import MONITORED  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))
from export_model import signing_key  # noqa: E402

SP_TAGS = [f"sp_{s}" for s in SP_NAMES]


async def main(scenario: str, split: str, start_h: float, end_h: float) -> None:
    logging.getLogger("asyncua").setLevel(logging.ERROR)
    key = signing_key()
    g, limits, _ = artifacts.load(ROOT / "models" / "guardian", key=key)  # refuses tampered models
    df, meta = load_split(split)
    run = df[df.scenario == scenario].reset_index(drop=True)
    run = run[run.time <= end_h]
    print(f"scenario: {meta[scenario]['description']}")

    # Demo certificates and private keys live only for this run.
    with tempfile.TemporaryDirectory(prefix="guardian_pki_") as tmp:
        await _serve(Path(tmp), g, limits, key, run, start_h)


async def _serve(pki: Path, g, limits, key: bytes, run: pd.DataFrame, start_h: float) -> None:
    server_id = make_identity(pki, "plant", server=True)
    client_id = make_identity(pki, "guardian", server=False)
    tags = MONITORED + SP_TAGS
    endpoint = "opc.tcp://127.0.0.1:48410/guardian/"
    server = PlantServer(server_id, [client_id.cert], tags, endpoint)

    audit_path = ROOT / "results" / "audit_demo.jsonl"
    audit_path.unlink(missing_ok=True)
    audit = AuditLog(audit_path, key=key)
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    history: list[dict] = []
    last = None
    try:
        await server.start()  # inside try: a failed start still stops the server
        async with SecureReadOnlyClient(endpoint, client_id, server_id.cert, tags) as client:
            for _, row in run.iterrows():
                await server.publish(row)  # the plant updates its tags
                history.append(await client.read_snapshot())  # Guardian reads them
                if row.time < start_h:
                    continue
                h = pd.DataFrame(history)
                a = g.analyze(h)
                alarm = persisted((a.S > limits).any(axis=1))
                i = len(h) - 1
                state = g.diagnose(a, i, limits) if alarm[i] else ("normal", None)
                if state != last:
                    rep = a.extra["validation"]
                    event = {"time_h": round(float(h.time.iloc[i]), 2), "status": state[0], "tag": state[1],
                             "hot_channels": [a.channels[c] for c in np.flatnonzero(a.S[i] > limits)][:8],
                             "data_issues": rep.summary()}
                    audit.append(event, ts=t0 + timedelta(hours=float(h.time.iloc[i])))
                    print(f"t={event['time_h']:6.2f} h  {state[0]:<17} {state[1] or '':<9} "
                          f"{', '.join(event['hot_channels'][:4])}")
                    last = state
    finally:
        with contextlib.suppress(Exception):  # a server that never started has nothing to stop
            await server.stop()
    ok, bad = verify(audit_path, key)
    print(f"audit log: {audit_path.relative_to(ROOT)} ({audit.seq} records, chain {'verified' if ok else f'BROKEN at {bad}'})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="test_attack")
    ap.add_argument("--scenario", default="test_atk_cooling_small_0")
    ap.add_argument("--start", type=float, default=11.0, help="hour at which live monitoring starts")
    ap.add_argument("--end", type=float, default=15.0)
    a = ap.parse_args()
    asyncio.run(main(a.scenario, a.split, a.start, a.end))
