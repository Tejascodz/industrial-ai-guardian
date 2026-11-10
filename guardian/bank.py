"""Virtual sensor bank: analytical redundancy learned from normal history.

For every monitored tag j, a ridge model predicts x_j(t) from all *other* tags
at the current and previous samples (lags capture loop dynamics). Tag j never
appears in its own model, so a faulty transmitter cannot vouch for itself.

Residuals z_j = (x_j - x_hat_j) / sigma_j are scaled with held-out normal data.
Reconstructing tag k (replacing it by x_hat_k) and recomputing everyone else's
residuals tells us whether a fault on k alone explains what we see.
"""

from __future__ import annotations

import numpy as np
from sklearn.linear_model import Ridge

from .features import lag_stack


class VirtualSensorBank:
    def __init__(self, n_tags: int, lags: int = 2, alpha: float = 1.0):
        self.n, self.lags, self.alpha = n_tags, lags, alpha

    def _masks(self) -> list[np.ndarray]:
        cols = np.tile(np.arange(self.n), self.lags + 1)  # tag index of each lagged column
        return [cols != j for j in range(self.n)]

    def fit(self, runs_x: list[np.ndarray]) -> "VirtualSensorBank":
        Z = np.vstack([lag_stack(x, self.lags) for x in runs_x])
        X = np.vstack(runs_x)
        self.masks = self._masks()
        self.coef = np.zeros((self.n, Z.shape[1]))
        self.intercept = np.zeros(self.n)
        for j in range(self.n):
            m = Ridge(alpha=self.alpha).fit(Z[:, self.masks[j]], X[:, j])
            self.coef[j, self.masks[j]] = m.coef_
            self.intercept[j] = m.intercept_
        self.sigma = np.ones(self.n)
        return self

    def predict(self, x: np.ndarray) -> np.ndarray:
        return lag_stack(x, self.lags) @ self.coef.T + self.intercept

    def calibrate(self, runs_x: list[np.ndarray]) -> "VirtualSensorBank":
        """Residual scale from held-out normal runs (not the training data)."""
        r = np.vstack([x - self.predict(x) for x in runs_x])
        self.sigma = r.std(axis=0) + 1e-9
        return self

    def residuals(self, x: np.ndarray) -> np.ndarray:
        return (x - self.predict(x)) / self.sigma

    def reconstruct(self, x: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
        """Replace tag k by its virtual sensor; return (x_tilde, residuals)."""
        xt = x.copy()
        xt[:, k] = self.predict(x)[:, k]
        return xt, self.residuals(xt)
