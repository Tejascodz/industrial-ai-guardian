"""Start the live dashboard.

    python -m dashboard [--port 8050] [--seed 7] [--no-browser]

Needs the simulator (make -C sim) and the generated data (scripts/generate_data.py).
If GUARDIAN_KEY is set and a signed model exists in models/guardian, that model
is loaded and its signature verified; otherwise Guardian is fitted at start-up.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from guardian import artifacts  # noqa: E402
from guardian.audit import AuditLog  # noqa: E402
from guardian.evaluate import calibrate, prepare  # noqa: E402
from guardian.features import DATA, load_split, runs  # noqa: E402
from guardian.methods import Guardian  # noqa: E402
from guardian.sim import BINARY, NORMAL  # noqa: E402
from guardian.softsensor import SoftSensor  # noqa: E402

from .engine import LiveSession  # noqa: E402
from .server import create_app, new_token  # noqa: E402


def load_models(key: bytes | None):
    train, _ = load_split("train")
    train_runs = [r for _, r in runs(train)]
    model_dir = ROOT / "models" / "guardian"
    if key and (model_dir / "manifest.json").exists():
        g, limits, _ = artifacts.load(model_dir, key=key)  # refuses tampered or unsigned models
        print("loaded signed Guardian model from models/guardian (signature verified)")
    else:
        print("fitting Guardian on the normal history (about half a minute)...")
        val, vm = load_split("val")
        vr = list(runs(val))
        g = Guardian().fit(train_runs, [r for n, r in vr if vm[n]["event_class"] == NORMAL])
        limits, _ = calibrate(prepare(g, vr, vm), 0.25)
    ss = SoftSensor().fit(train_runs, scaler=g.scaler)
    return g, limits, ss


def main() -> None:
    ap = argparse.ArgumentParser(description="Industrial AI Guardian live dashboard")
    ap.add_argument("--port", type=int, default=8050)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    if not BINARY.exists():
        sys.exit("simulator not built: run `make -C sim` first")
    if not (DATA / "train.parquet").exists():
        sys.exit("no data yet: run `python scripts/generate_data.py` first")

    env_key = os.environ.get("GUARDIAN_KEY")
    key = env_key.encode() if env_key else None
    g, limits, ss = load_models(key)
    audit_path = ROOT / "results" / "audit_live.jsonl"
    audit_path.parent.mkdir(exist_ok=True)
    if key:
        audit_key = key
    else:
        # No key configured: sign this session's log with a random key that is
        # never stored, and start a fresh log (an old one cannot be verified).
        audit_key = secrets.token_bytes(32)
        audit_path = ROOT / "results" / "audit_live_session.jsonl"
        audit_path.unlink(missing_ok=True)
        print("note: GUARDIAN_KEY not set; this session's audit log uses a temporary key")
    try:
        audit = AuditLog(audit_path, key=audit_key)
    except ValueError:
        broken = audit_path.with_suffix(".broken.jsonl")
        audit_path.rename(broken)
        print(f"existing audit log failed verification (wrong key or tampered); moved to {broken.name}")
        audit = AuditLog(audit_path, key=audit_key)

    session = LiveSession(g, limits, ss, audit, seed=args.seed)
    token = new_token()
    host = "127.0.0.1"
    app = create_app(session, token, audit_path, audit_key, host=host, port=args.port)
    url = f"http://{host}:{args.port}/?token={token}"
    print(f"\nGuardian control room: {url}\n(keep this link private: the token controls the trainer)\n")
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=args.port, log_level="warning", ws_max_size=65536)


if __name__ == "__main__":
    main()
