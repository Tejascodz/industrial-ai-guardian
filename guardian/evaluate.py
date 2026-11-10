"""Threshold calibration and benchmark scoring.

Alarm rule (same for every method): an alarm is raised when any channel is
above its limit for PERSIST consecutive samples (9 minutes).

Calibration: limits come from the validation set's normal samples (see
limits_at); a common quantile q is chosen per method so that all methods run
at the same false-alarm rate on validation data. Reported numbers come
from the separate test set.
"""

from __future__ import annotations

from dataclasses import dataclass

import re

import numpy as np
import pandas as pd

from .methods import Analysis, Method
from .sim import NORMAL

PERSIST = 3
WARMUP_H = 4.0  # first hours after burn-in are excluded from scoring
DIAG_LATER = 20  # samples after detection for the settled diagnosis (1 h)
SAMPLE_H = 0.05


@dataclass
class Prepared:
    name: str
    meta: dict
    a: Analysis
    time: np.ndarray
    normal_mask: np.ndarray  # samples that are normal operation and scored


def prepare(method: Method, runs: list[tuple[str, pd.DataFrame]], meta: dict) -> list[Prepared]:
    out = []
    for name, run in runs:
        t = run["time"].to_numpy()
        m = meta[name]
        ev = m["event_t"] if m["event_class"] != NORMAL else np.inf
        out.append(Prepared(name, m, method.analyze(run), t, (t >= WARMUP_H) & (t < ev)))
    return out


def persisted(hot: np.ndarray) -> np.ndarray:
    """True where hot has held for PERSIST consecutive samples."""
    run_len = np.zeros(len(hot), dtype=int)
    c = 0
    for i, h in enumerate(hot):
        c = c + 1 if h else 0
        run_len[i] = c
    return run_len >= PERSIST


def onsets(alarm: np.ndarray, mask: np.ndarray) -> int:
    rising = alarm & ~np.r_[False, alarm[:-1]]
    return int(np.sum(rising & mask))


def _families(channels: list[str]) -> np.ndarray:
    """Channel family = name prefix before ':' (e.g. all 'R:<tag>' channels)."""
    names = [c.split(":")[0] if ":" in c else c for c in channels]
    uniq = sorted(set(names), key=names.index)
    return np.array([uniq.index(n) for n in names])


def limits_at(q: float, pool: np.ndarray, channels: list[str]) -> np.ndarray:
    """Per-channel limits from a common quantile q.

    Each channel is first scaled by its own 99th percentile on normal data (a
    stable estimate). Within a family of similar channels the maximum scaled
    value is the family statistic, and its q-quantile sets one limit for the
    whole family. A few family limits generalise much better than one extreme
    quantile per channel.
    """
    scale = np.quantile(pool, 0.99, axis=0) + 1e-12
    fam = _families(channels)
    lim = np.empty(pool.shape[1])
    for f in np.unique(fam):
        cols = fam == f
        stat = (pool[:, cols] / scale[cols]).max(axis=1)
        lim[cols] = np.quantile(stat, q) * scale[cols]
    return lim


def false_alarms_per_day(prep: list[Prepared], limits: np.ndarray) -> float:
    n, hours = 0, 0.0
    for p in prep:
        alarm = persisted((p.a.S > limits).any(axis=1))
        n += onsets(alarm, p.normal_mask)
        hours += p.normal_mask.sum() * SAMPLE_H
    return 24.0 * n / max(hours, 1e-9)


def calibrate(val: list[Prepared], target_fa_per_day: float) -> tuple[np.ndarray, float]:
    pool = np.vstack([p.a.S[p.normal_mask] for p in val])
    channels = val[0].a.channels
    lo, hi = 0.99, 1.0
    for _ in range(30):  # bisection on the common quantile
        mid = 0.5 * (lo + hi)
        if false_alarms_per_day(val, limits_at(mid, pool, channels)) > target_fa_per_day:
            lo = mid
        else:
            hi = mid
    q = hi
    return limits_at(q, pool, channels) + 1e-12, q


def score(method: Method, test: list[Prepared], limits: np.ndarray) -> pd.DataFrame:
    rows = []
    for p in test:
        alarm = persisted((p.a.S > limits).any(axis=1))
        m = p.meta
        row = {
            "scenario": p.name,
            "true_class": m["event_class"],
            "true_tag": (m["sensor_fault"] or {}).get("tag"),
            "fault_kind": (m["sensor_fault"] or {}).get("kind"),
            "idv": m["idv"],
            "attack": m.get("attack"),
            "event_t": m["event_t"],
            "false_alarms": onsets(alarm, p.normal_mask),
            "normal_hours": p.normal_mask.sum() * SAMPLE_H,
            "detected": False,
            "delay_h": np.nan,
            "trip_t": _trip_time(m.get("shutdown", "")),
        }
        if m["event_class"] != NORMAL:
            # Only an alarm that starts at or after the event counts. An alarm
            # already standing when the event begins is a false alarm, and
            # crediting it would report a detection with zero delay.
            rising = alarm & ~np.r_[False, alarm[:-1]]
            after = np.flatnonzero(rising & (p.time >= m["event_t"]))
            if len(after):
                i = int(after[0])
                j = min(i + DIAG_LATER, len(p.time) - 1)
                row.update(detected=True, delay_h=p.time[i] - m["event_t"])
                row["diag_class"], row["diag_tag"] = method.diagnose(p.a, i, limits)
                row["diag_class_1h"], row["diag_tag_1h"] = method.diagnose(p.a, j, limits)
        rows.append(row)
    return pd.DataFrame(rows)


def _trip_time(msg: str) -> float:
    """Time of a plant interlock trip, parsed from the simulator message."""
    hit = re.search(r"t=([0-9.]+) h", msg or "")
    return float(hit.group(1)) if hit else np.nan


def _event_t(ev: pd.DataFrame) -> pd.Series:
    return ev["event_t"] if "event_t" in ev else pd.Series(12.0, index=ev.index)


def summarize(df: pd.DataFrame, suffix: str = "_1h") -> dict:
    ev = df[df.true_class != NORMAL]
    det = ev[ev.detected]
    cls = det[f"diag_class{suffix}"]
    tag = det[f"diag_tag{suffix}"]
    sens = det[det.true_class == "sensor_fault"]
    proc = det[det.true_class == "process_fault"]
    reg = det[det.true_class == "regime_change"]
    s_cls = sens[f"diag_class{suffix}"]
    s_tag = sens[f"diag_tag{suffix}"]
    return {
        "false_alarms_per_day": 24 * df.false_alarms.sum() / df.normal_hours.sum(),
        "detection_rate": ev.detected.mean(),
        "sensor_detection_rate": ev[ev.true_class == "sensor_fault"].detected.mean(),
        "process_detection_rate": ev[ev.true_class == "process_fault"].detected.mean(),
        "regime_detection_rate": ev[ev.true_class == "regime_change"].detected.mean(),
        "median_delay_h": det.delay_h.median(),
        "class_accuracy": (cls == det.true_class).mean(),
        "sensor_correct_tag": ((s_cls == "sensor_fault") & (s_tag == sens.true_tag)).mean(),
        "sensor_called_sensor": (s_cls == "sensor_fault").mean(),
        "process_blamed_on_sensor": (proc[f"diag_class{suffix}"] == "sensor_fault").mean(),
        "regime_correct": (reg[f"diag_class{suffix}"] == "regime_change").mean(),
        "trips": int(ev.trip_t.notna().sum()),
        "trips_warned": int((ev.detected & (ev.trip_t > ev.delay_h + _event_t(ev))).sum()),
        "median_trip_warning_h": float(np.nanmedian(ev.trip_t - ev.delay_h - _event_t(ev)))
        if ev.trip_t.notna().any() else np.nan,
        "n_events": len(ev),
        "n_detected": len(det),
    }


def summarize_attacks(df: pd.DataFrame) -> pd.DataFrame:
    """Per attack family: detected, delay, recognised as an integrity attack."""
    rows = []
    for fam, g in df.groupby("attack"):
        det = g[g.detected]
        rows.append({
            "attack": fam,
            "runs": len(g),
            "detected": g.detected.mean(),
            "median_delay_min": 60 * det.delay_h.median() if len(det) else np.nan,
            "recognised_as_attack": (g.get("diag_class_1h") == "integrity_attack").mean(),
            "trips": int(g.trip_t.notna().sum()),
            "trips_warned": int((g.detected & (g.trip_t > g.delay_h + g.event_t)).sum()),
        })
    return pd.DataFrame(rows)
