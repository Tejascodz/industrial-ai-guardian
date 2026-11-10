"""Input validation for historian / OPC UA data before any model sees it.

Bad data is the cheapest way to make a monitor lie: a NaN, a frozen
timestamp or a physically impossible value can raise false alarms or hide
real ones. Every sample is checked against

  * schema       required tags present and numeric
  * finiteness   no NaN or infinity
  * physics      value inside the instrument's span (with margin); a real
                 upset can saturate an instrument, so this is reported but
                 not treated as an integrity failure
  * time         strictly increasing timestamps, no gaps or duplicates

Invalid samples are reported and masked; they never reach the models raw.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .tags import MONITORED, SAMPLE_PERIOD_H


class SchemaError(ValueError):
    pass


def _span(tag: str) -> tuple[float, float]:
    """Instrument span in engineering units (generous: catch impossible values only)."""
    n = int(tag.split("_")[1])
    if tag.startswith("xmv_"):
        return (-1.0, 101.0)  # valve position, %
    if n in (7, 13, 16):
        return (0.0, 4000.0)  # kPa gauge
    if n in (8, 12, 15):
        return (-5.0, 105.0)  # % level
    if n in (9, 11, 18, 21, 22):
        return (0.0, 250.0)  # degC
    if n == 19:
        return (-50.0, 500.0)  # steam flow, near zero in this strategy
    if n in (2, 3):
        return (0.0, 10000.0)  # kg/h
    if n == 20:
        return (0.0, 1000.0)  # kW
    return (0.0, 200.0)  # flows in kscmh or m3/h


SPANS = {t: _span(t) for t in MONITORED}


PIPELINE_CODES = {"not_finite", "bad_timestamp", "not_increasing", "gap"}


@dataclass
class ValidationReport:
    bad: np.ndarray  # True for samples that failed any check
    pipeline_bad: np.ndarray  # failures that a real process cannot cause (NaN, time)
    issues: list[tuple[float, str, str]] = field(default_factory=list)  # (time, tag, code)

    @property
    def ok(self) -> bool:
        return not self.bad.any()

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for _, _, code in self.issues:
            out[code] = out.get(code, 0) + 1
        return out


def validate(df: pd.DataFrame, tags: list[str] = MONITORED,
             period_h: float = SAMPLE_PERIOD_H, max_issues: int = 1000) -> ValidationReport:
    missing = [t for t in ["time", *tags] if t not in df.columns]
    if missing:
        raise SchemaError(f"missing columns: {missing[:5]}{'...' if len(missing) > 5 else ''}")
    for t in ["time", *tags]:
        if not pd.api.types.is_numeric_dtype(df[t]):
            raise SchemaError(f"column {t} is not numeric")

    n = len(df)
    bad = np.zeros(n, dtype=bool)
    pipeline_bad = np.zeros(n, dtype=bool)
    issues: list[tuple[float, str, str]] = []
    time = df["time"].to_numpy(dtype=float)

    def flag(mask: np.ndarray, tag: str, code: str) -> None:
        idx = np.flatnonzero(mask)
        bad[idx] = True
        if code in PIPELINE_CODES:
            pipeline_bad[idx] = True
        for i in idx[: max(0, max_issues - len(issues))]:
            issues.append((float(time[i]) if np.isfinite(time[i]) else float("nan"), tag, code))

    X = df[tags].to_numpy(dtype=float)
    for j, t in enumerate(tags):
        v = X[:, j]
        flag(~np.isfinite(v), t, "not_finite")
        lo, hi = SPANS.get(t, (-np.inf, np.inf))
        flag(np.isfinite(v) & ((v < lo) | (v > hi)), t, "out_of_span")

    flag(~np.isfinite(time), "time", "bad_timestamp")
    dt = np.diff(time, prepend=np.nan)
    flag(np.r_[False, dt[1:] <= 0], "time", "not_increasing")
    flag(np.r_[False, dt[1:] > 1.5 * period_h], "time", "gap")
    return ValidationReport(bad, pipeline_bad, issues)


def sanitize(df: pd.DataFrame, report: ValidationReport, tags: list[str] = MONITORED) -> pd.DataFrame:
    """Hold the last valid value for invalid samples so models get finite input.

    The samples stay flagged in the report; holding values only keeps the
    numerical pipeline running while the alarm is raised.
    """
    out = df.copy()
    if report.ok:
        return out
    vals = out[tags].astype(float)
    vals[report.bad] = np.nan
    for t in tags:
        lo, hi = SPANS.get(t, (-np.inf, np.inf))
        vals.loc[(vals[t] < lo) | (vals[t] > hi), t] = np.nan
    out[tags] = vals.ffill().bfill().fillna(0.0)
    return out
