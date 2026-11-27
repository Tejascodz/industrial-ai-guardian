"""Confidence intervals used in every results table."""

from __future__ import annotations

import numpy as np
from scipy import stats

Z = stats.norm.ppf(0.975)


def wilson(k: int, n: int) -> tuple[float, float, float]:
    if n == 0:
        return np.nan, np.nan, np.nan
    p = k / n
    d = 1 + Z**2 / n
    c = (p + Z**2 / (2 * n)) / d
    h = Z * np.sqrt(p * (1 - p) / n + Z**2 / (4 * n**2)) / d
    return p, max(0.0, c - h), min(1.0, c + h)


def poisson_rate(k: int, exposure_days: float) -> tuple[float, float, float]:
    lo = stats.chi2.ppf(0.025, 2 * k) / 2 if k > 0 else 0.0
    hi = stats.chi2.ppf(0.975, 2 * k + 2) / 2
    return k / exposure_days, lo / exposure_days, hi / exposure_days


def boot_median(x: np.ndarray, n: int = 2000, seed: int = 0) -> tuple[float, float, float]:
    x = np.asarray(x, dtype=float)
    rng = np.random.default_rng(seed)
    meds = np.median(rng.choice(x, size=(n, len(x)), replace=True), axis=1)
    return float(np.median(x)), float(np.quantile(meds, 0.025)), float(np.quantile(meds, 0.975))
