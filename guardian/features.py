"""Loading historian data and building model features."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .sim import BASE_SP
from .tags import MONITORED

DATA = Path(__file__).resolve().parents[1] / "data"
BURN_IN_H = 2.0  # discard plant start-up
SP_NAMES = list(BASE_SP)


def load_split(split: str) -> tuple[pd.DataFrame, dict[str, dict]]:
    df = pd.read_parquet(DATA / f"{split}.parquet")
    meta = {m["name"]: m for m in json.loads((DATA / f"{split}_scenarios.json").read_text())}
    df = df[df["time"] >= BURN_IN_H].reset_index(drop=True)
    return add_setpoints(df, meta), meta


def add_setpoints(df: pd.DataFrame, meta: dict[str, dict]) -> pd.DataFrame:
    """Add the controller setpoints (sp_*) as the DCS would report them.

    Setpoints are not process measurements; they are operator actions, read
    from the control system alongside the measurements.
    """
    out = []
    for name, g in df.groupby("scenario", sort=False):
        g = g.copy()
        moves = meta[name]["moves"] + meta[name]["regime_moves"]
        for sp in SP_NAMES:
            v = np.full(len(g), BASE_SP[sp])
            for m in sorted((m for m in moves if m["name"] == sp), key=lambda m: m["t"]):
                v[g["time"].to_numpy() >= m["t"]] = m["value"]
            g[f"sp_{sp}"] = v
        out.append(g)
    return pd.concat(out, ignore_index=True)


class Scaler:
    """Standardise columns with statistics from normal training data."""

    def __init__(self, cols: list[str] = MONITORED):
        self.cols = cols

    def fit(self, df: pd.DataFrame) -> "Scaler":
        self.mean = df[self.cols].mean().to_numpy()
        self.std = df[self.cols].std().to_numpy() + 1e-12
        return self

    def transform(self, df: pd.DataFrame) -> np.ndarray:
        return (df[self.cols].to_numpy() - self.mean) / self.std


def lag_stack(x: np.ndarray, lags: int) -> np.ndarray:
    """[x(t), x(t-1), ..., x(t-lags)] for one run; the start is edge-padded."""
    if lags == 0:
        return x
    pad = np.vstack([np.repeat(x[:1], lags, axis=0), x])
    n = len(x)
    return np.hstack([pad[lags - k: lags - k + n] for k in range(lags + 1)])


def ewma(z: np.ndarray, lam: float) -> np.ndarray:
    """Exponentially weighted moving average along axis 0 (one run)."""
    out = np.empty_like(z, dtype=float)
    acc = np.zeros(z.shape[1:])
    for i in range(len(z)):
        acc = lam * z[i] + (1.0 - lam) * acc
        out[i] = acc
    return out


def runs(df: pd.DataFrame):
    """Iterate over (scenario name, run DataFrame) preserving order."""
    for name, g in df.groupby("scenario", sort=False):
        yield name, g.reset_index(drop=True)
