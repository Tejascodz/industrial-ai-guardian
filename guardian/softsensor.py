"""Product-quality soft sensor and its Guardian-protected version.

Target: product analysis component E (XMEAS 38, mol%). The product analyzer
samples every 15 min and reports 15 min later, so the true value of "now" is
only known 15-30 min afterwards. The soft sensor estimates it every 3 min from
the continuous measurements and valve positions.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

from .features import Scaler, lag_stack
from .tags import ANALYZER_TIMING, MONITORED, SAMPLE_PERIOD_H

TARGET = "xmeas_38"


def aligned_truth(run: pd.DataFrame, target: str = TARGET) -> np.ndarray:
    """Analyzer value moved back by its dead time, i.e. the composition at t."""
    shift = int(round(ANALYZER_TIMING[target][1] / SAMPLE_PERIOD_H))
    return run[target].shift(-shift).to_numpy()


class SoftSensor:
    def __init__(self, lags: int = 2, alpha: float = 1.0, target: str = TARGET):
        self.lags, self.alpha, self.target = lags, alpha, target

    def fit(self, train_runs: list[pd.DataFrame], scaler: Scaler | None = None) -> "SoftSensor":
        self.scaler = scaler or Scaler(MONITORED).fit(pd.concat(train_runs))
        Z, y = [], []
        for r in train_runs:
            Z.append(lag_stack(self.scaler.transform(r), self.lags))
            y.append(aligned_truth(r, self.target))
        Z, y = np.vstack(Z), np.concatenate(y)
        ok = ~np.isnan(y)
        self.model = Ridge(alpha=self.alpha).fit(Z[ok], y[ok])
        return self

    def predict_x(self, x: np.ndarray) -> np.ndarray:
        """Predict from an already standardised measurement matrix."""
        return self.model.predict(lag_stack(x, self.lags))

    def predict(self, run: pd.DataFrame) -> np.ndarray:
        return self.predict_x(self.scaler.transform(run))
