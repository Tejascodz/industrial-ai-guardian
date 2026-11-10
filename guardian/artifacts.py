"""Saving and loading a fitted Guardian without pickle.

Pickle files execute code when loaded, so a swapped model file is a code
execution path into the monitoring host. A Guardian is stored instead as
  arrays.npz      numeric parameters, loaded with allow_pickle=False
  config.json     settings, channel names and calibrated alarm limits
  manifest.json   SHA-256 of both files, optionally signed with HMAC-SHA256
Loading verifies the manifest before reading anything else.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import numpy as np

from . import __version__
from .bank import VirtualSensorBank
from .features import Scaler
from .methods import Guardian
from .monitors import PCAMonitor


EXPECTED_FILES = {"arrays.npz", "config.json"}


class ArtifactIntegrityError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sign(manifest: dict, key: bytes) -> str:
    body = json.dumps(manifest["files"], sort_keys=True).encode()
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def save(g: Guardian, out_dir: str | Path, limits: np.ndarray | None = None,
         channels: list[str] | None = None, key: bytes | None = None) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    arrays = {
        "scaler_mean": g.scaler.mean, "scaler_std": g.scaler.std,
        "bank_coef": g.bank.coef, "bank_intercept": g.bank.intercept, "bank_sigma": g.bank.sigma,
        "pca_mu": g.pca.mu, "pca_P": g.pca.P, "pca_lam": g.pca.lam,
        "diff_std": g.diff_std, "sp_lo": g.sp_lo, "sp_hi": g.sp_hi,
    }
    if limits is not None:
        arrays["limits"] = np.asarray(limits, dtype=float)
    np.savez(out / "arrays.npz", **arrays)
    config = {
        "version": __version__,
        "cols": g.scaler.cols,
        "lags": g.lags,
        "bank_alpha": g.bank.alpha,
        "use_instrument": g.use_instrument,
        "use_setpoints": g.use_setpoints,
        "use_integrity": g.use_integrity,
        "name": g.name,
        "channels": channels,
    }
    (out / "config.json").write_text(json.dumps(config, indent=1))
    manifest = {"files": {n: _sha256(out / n) for n in ("arrays.npz", "config.json")}}
    if key:
        manifest["hmac_sha256"] = _sign(manifest, key)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return out


def load(in_dir: str | Path, key: bytes | None = None) -> tuple[Guardian, np.ndarray | None, list | None]:
    d = Path(in_dir)
    manifest = json.loads((d / "manifest.json").read_text())
    files = manifest.get("files", {})
    if set(files) != EXPECTED_FILES:  # every file that gets read must be covered, and nothing else
        raise ArtifactIntegrityError(f"manifest must list exactly {sorted(EXPECTED_FILES)}")
    for name, digest in files.items():
        if not hmac.compare_digest(_sha256(d / name), digest):
            raise ArtifactIntegrityError(f"{name} does not match its manifest hash")
    if key is not None:
        sig = manifest.get("hmac_sha256", "")
        if not hmac.compare_digest(sig, _sign(manifest, key)):
            raise ArtifactIntegrityError("manifest signature is missing or invalid")

    cfg = json.loads((d / "config.json").read_text())
    with np.load(d / "arrays.npz", allow_pickle=False) as z:
        a = {k: z[k] for k in z.files}

    g = Guardian(lags=cfg["lags"], use_instrument=cfg["use_instrument"],
                 use_setpoints=cfg["use_setpoints"], use_integrity=cfg["use_integrity"], name=cfg["name"])
    g.scaler = Scaler(cfg["cols"])
    g.scaler.mean, g.scaler.std = a["scaler_mean"], a["scaler_std"]
    g.bank = VirtualSensorBank(len(cfg["cols"]), cfg["lags"], cfg["bank_alpha"])
    g.bank.masks = g.bank._masks()
    g.bank.coef, g.bank.intercept, g.bank.sigma = a["bank_coef"], a["bank_intercept"], a["bank_sigma"]
    g.pca = PCAMonitor()
    g.pca.mu, g.pca.P, g.pca.lam = a["pca_mu"], a["pca_P"], a["pca_lam"]
    g.pca.k = g.pca.P.shape[1]
    g.pca.Ct = np.eye(len(g.pca.mu)) - g.pca.P @ g.pca.P.T
    g.diff_std, g.sp_lo, g.sp_hi = a["diff_std"], a["sp_lo"], a["sp_hi"]
    return g, a.get("limits"), cfg.get("channels")
