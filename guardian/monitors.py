"""Classical process-monitoring baselines.

* Univariate limit checks - what a DCS alarm system does per tag.
* PCA with Hotelling T2 and SPE (Q) statistics, contribution plots for
  isolation (MacGregor & Kourti, 1995).
* Reconstruction-based contributions, RBC (Alcala & Qin, 2009), with the
  reconstruction test of Dunia, Qin, Edgar & McAvoy (1996): if removing one
  sensor's direction brings SPE back under its limit, that sensor is faulty.
"""

from __future__ import annotations

import numpy as np


class PCAMonitor:
    def __init__(self, var_explained: float = 0.90):
        self.var_explained = var_explained

    def fit(self, X: np.ndarray) -> "PCAMonitor":
        self.mu = X.mean(axis=0)
        C = np.cov(X - self.mu, rowvar=False)
        w, V = np.linalg.eigh(C)
        w, V = w[::-1], V[:, ::-1]
        self.k = int(np.searchsorted(np.cumsum(w) / w.sum(), self.var_explained) + 1)
        self.P = V[:, : self.k]
        self.lam = w[: self.k]
        self.Ct = np.eye(X.shape[1]) - self.P @ self.P.T  # residual-space projector
        return self

    def stats(self, X: np.ndarray) -> dict[str, np.ndarray]:
        Xc = X - self.mu
        t = Xc @ self.P
        e = Xc @ self.Ct
        return {
            "t2": np.sum(t**2 / self.lam, axis=1),
            "spe": np.sum(e**2, axis=1),
            "contrib": e**2,  # SPE contribution of each variable
            "rbc": e**2 / np.diag(self.Ct),  # reconstruction-based contribution
        }
