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

Invalid values are reported and repaired per sensor, using past data only;
they never reach the models raw, and one bad transmitter never hides the
readings of the others.
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


def sanitize(df: pd.DataFrame, report: ValidationReport, tags: list[str] = MONITORED,
             fallback: dict[str, float] | None = None) -> pd.DataFrame:
    """Repair invalid values so the models get finite input, one sensor at a time.

    * A value outside the instrument span is clipped to the span limit, the way
      a saturated transmitter reads. The deviation stays visible to the models,
      so a sensor driven off scale still raises an alarm.
    * A missing or non-finite value holds that sensor's last valid reading.
      Only past data is used: nothing is filled backwards from the future.
    * A sensor with no valid reading yet takes ``fallback[tag]`` (the training
      mean when called from Guardian), else the middle of its span.
    * Timestamp problems leave the values alone; they are an integrity alarm
      (``report.pipeline_bad``), not a reason to discard good measurements.

    The samples stay flagged in the report, so repair never hides them.
    """
    out = df.copy()
    if report.ok:
        return out
    for t in tags:
        v = out[t].astype(float).to_numpy().copy()
        lo, hi = SPANS.get(t, (-np.inf, np.inf))
        v[~np.isfinite(v)] = np.nan
        v = np.clip(v, lo, hi)  # NaN stays NaN
        col = pd.Series(v, index=out.index).ffill()
        if col.isna().any():
            if fallback is not None and t in fallback:
                first = float(fallback[t])
            elif np.isfinite(lo) and np.isfinite(hi):
                first = (lo + hi) / 2.0
            else:
                first = 0.0
            col = col.fillna(first)
        out[t] = col
    return out
