"""Monitoring methods compared in the benchmark.

Every method turns one run of historian data into
  * a matrix of alarm channels S (samples x channels); a channel alarms when
    it exceeds its limit, and limits are calibrated on validation normal data
  * a diagnosis at any alarmed sample: (class, suspected tag)

All methods see the same standardised, EWMA-filtered measurements, so the
differences come from the models and the diagnostic logic, not filtering.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .bank import VirtualSensorBank
from .features import SP_NAMES, Scaler, ewma
from .monitors import PCAMonitor
from .scenarios import ENVELOPE
from .sim import INTEGRITY, NORMAL, PROCESS, REGIME, SENSOR
from .tags import FIELD_TAGS, MONITORED

LAMBDA = 0.2  # EWMA weight, shared by all methods
FIELD_IDX = [MONITORED.index(t) for t in FIELD_TAGS]


@dataclass
class Analysis:
    """Per-run results a method needs for alarms and diagnosis."""
    S: np.ndarray
    channels: list[str]
    extra: dict = field(default_factory=dict)


class Method:
    name = "method"

    def fit(self, train_runs: list[pd.DataFrame], val_normal_runs: list[pd.DataFrame]) -> "Method":
        self.scaler = Scaler(MONITORED).fit(pd.concat(train_runs))
        return self

    def analyze(self, run: pd.DataFrame) -> Analysis:
        raise NotImplementedError

    def diagnose(self, a: Analysis, i: int, limits: np.ndarray) -> tuple[str, str | None]:
        raise NotImplementedError


# --------------------------------------------------------------------------
# Baseline 1: per-tag limit checks (DCS-style alarms)
# --------------------------------------------------------------------------
class LimitChecks(Method):
    name = "Limit checks"

    def analyze(self, run):
        z = ewma(self.scaler.transform(run), LAMBDA)
        return Analysis(np.abs(z), [f"L:{t}" for t in MONITORED])

    def diagnose(self, a, i, limits):
        hot = np.flatnonzero(a.S[i] > limits)
        hot_field = [h for h in hot if h in FIELD_IDX]
        if len(hot) == 1 and hot_field:
            return SENSOR, MONITORED[hot_field[0]]
        return PROCESS, None


# --------------------------------------------------------------------------
# Baselines 2 and 3: PCA with contribution plots / reconstruction (RBC)
# --------------------------------------------------------------------------
class PCAContrib(Method):
    name = "PCA + contributions"
    share = 0.5  # a single variable must dominate the SPE to blame a sensor

    def fit(self, train_runs, val_normal_runs):
        super().fit(train_runs, val_normal_runs)
        X = np.vstack([ewma(self.scaler.transform(r), LAMBDA) for r in train_runs])
        self.pca = PCAMonitor().fit(X)
        return self

    def analyze(self, run):
        st = self.pca.stats(ewma(self.scaler.transform(run), LAMBDA))
        return Analysis(np.column_stack([st["t2"], st["spe"]]), ["T2", "SPE"], st)

    def diagnose(self, a, i, limits):
        t2_hot, spe_hot = a.S[i] > limits
        if spe_hot:
            c = a.extra["contrib"][i]
            j = int(np.argmax(c))
            if j in FIELD_IDX and c[j] / c.sum() > self.share:
                return SENSOR, MONITORED[j]
            return PROCESS, None
        return (REGIME, None) if t2_hot else (NORMAL, None)


class PCARBC(PCAContrib):
    name = "PCA + RBC"

    def diagnose(self, a, i, limits):
        t2_hot, spe_hot = a.S[i] > limits
        if spe_hot:
            rbc = a.extra["rbc"][i]
            j = FIELD_IDX[int(np.argmax(rbc[FIELD_IDX]))]
            # Reconstruction test: does removing sensor j explain the SPE?
            if a.extra["spe"][i] - rbc[j] <= limits[1]:
                return SENSOR, MONITORED[j]
            return PROCESS, None
        return (REGIME, None) if t2_hot else (NORMAL, None)


# --------------------------------------------------------------------------
# Guardian
# --------------------------------------------------------------------------
class Guardian(Method):
    """Data validation + integrity checks + instrument checks + virtual sensor
    bank + hypothesis test + DCS context.

    Channel families (each calibrated as one family, see evaluate.limits_at)
      V           data pipeline failure (NaN / infinite value, bad timestamps)
      R:<tag>     smoothed bank residual of each tag (analytical redundancy)
      Q           sum of squared smoothed residuals (many small deviations)
      N:<tag>     high-frequency content of the residual (noisy transmitter)
      F:<tag>     flatline score (stuck transmitter)
      P:<tag>     replayed data: a window of readings repeats exactly
      T2          operating point vs. the validated envelope
      ENV         setpoints outside the range seen in normal history

    Diagnosis order
      1. broken data, replayed data, or three or more
         transmitters frozen at the same moment          -> integrity
      2. flatline on one field tag                       -> sensor fault
      3. setpoint outside the validated envelope, or
         moved recently (DCS context)                    -> regime change
      4. noise confined to one field tag                 -> sensor fault
      5. reconstructing one field tag explains all other
         residuals and that tag is itself anomalous      -> sensor fault
      6. otherwise                                       -> process fault
    A sensor diagnosis made at the start of an alarm is kept while that
    sensor stays anomalous (see diagnose()).
    """

    name = "Guardian"
    recent_sp_h = 2.0
    onset_window = 6  # samples (18 min) in which the root cause is identified
    onset_majority = 0.9  # share of onset samples that must agree on one sensor
    explain_tol = 1.0  # residuals left after reconstruction, relative to their limits
    noise_win = 20
    flat_win = 5
    replay_win = 5  # samples (15 min) compared for exact repetition
    freeze_min_tags = 3  # simultaneous flatlines that indicate a link failure / DoS

    def __init__(self, lags: int = 2, use_instrument: bool = True,
                 use_setpoints: bool = True, use_integrity: bool = True, name: str | None = None):
        self.lags = lags
        self.use_instrument = use_instrument
        self.use_setpoints = use_setpoints
        self.use_integrity = use_integrity
        if name:
            self.name = name

    def fit(self, train_runs, val_normal_runs):
        super().fit(train_runs, val_normal_runs)
        xs = [self.scaler.transform(r) for r in train_runs]
        self.bank = VirtualSensorBank(len(MONITORED), self.lags).fit(xs)
        self.bank.calibrate([self.scaler.transform(r) for r in val_normal_runs])
        self.pca = PCAMonitor().fit(np.vstack([ewma(x, LAMBDA) for x in xs]))
        d = np.vstack([np.diff(x[:, FIELD_IDX], axis=0) for x in xs])
        self.diff_std = d.std(axis=0) + 1e-12
        # Setpoint envelope seen in the normal history (the validated range).
        sp = pd.concat(train_runs)[[f"sp_{n}" for n in SP_NAMES]]
        self.sp_lo, self.sp_hi = sp.min().to_numpy(), sp.max().to_numpy()
        return self

    # ----- per-run analysis -------------------------------------------------
    @staticmethod
    def _rolling_std(a: np.ndarray, w: int) -> np.ndarray:
        # A full window is required: a handful of samples at the start of the
        # data would otherwise look flat (or noisy) on every tag at once.
        s = pd.DataFrame(a).rolling(w, min_periods=w).std().to_numpy()
        return np.nan_to_num(s, nan=1.0)

    def _replay_score(self, raw: np.ndarray) -> np.ndarray:
        """1 where the last replay_win readings of a tag occurred before, exactly.

        Real transmitter noise never repeats; recorded-and-replayed data does.
        Flat windows are left to the flatline check.
        """
        w = self.replay_win
        out = np.zeros(raw.shape)
        q = np.round(raw, 6)
        for j in range(raw.shape[1]):
            seen: set[bytes] = set()
            for i in range(w - 1, len(q)):
                win = q[i - w + 1: i + 1, j]
                if np.ptp(win) == 0:
                    continue
                key = win.tobytes()
                if key in seen:
                    out[i, j] = 1.0
                else:
                    seen.add(key)
        return out

    def analyze(self, run):
        from .validation import sanitize, validate

        blocks: list[tuple[str, list[str], np.ndarray]] = []
        report = validate(run)
        run = sanitize(run, report, fallback=dict(zip(self.scaler.cols, self.scaler.mean)))
        if self.use_integrity:
            blocks.append(("V", ["V"], report.pipeline_bad[:, None].astype(float)))

        x = self.scaler.transform(run)
        z = self.bank.residuals(x)
        R = ewma(z, LAMBDA) ** 2
        blocks.append(("R", [f"R:{t}" for t in MONITORED], R))
        blocks.append(("Q", ["Q"], R.sum(axis=1, keepdims=True)))
        if self.use_instrument:
            # High-frequency content of the residual: steps and slow drifts in
            # the process do not raise it, a noisy transmitter does.
            dz = np.diff(z[:, FIELD_IDX], axis=0, prepend=z[:1, FIELD_IDX])
            blocks.append(("N", [f"N:{t}" for t in FIELD_TAGS], self._rolling_std(dz, self.noise_win)))
            dx = np.diff(x[:, FIELD_IDX], axis=0, prepend=x[:1, FIELD_IDX])
            dstd = self._rolling_std(dx, self.flat_win)
            blocks.append(("F", [f"F:{t}" for t in FIELD_TAGS],
                           self.diff_std / (dstd + 1e-3 * self.diff_std)))
        if self.use_integrity:
            blocks.append(("P", [f"P:{t}" for t in FIELD_TAGS], self._replay_score(run[FIELD_TAGS].to_numpy())))
        blocks.append(("T2", ["T2"], self.pca.stats(ewma(x, LAMBDA))["t2"][:, None]))

        extra = {"time": run["time"].to_numpy(), "R": R, "validation": report}
        if self.use_setpoints:
            sp = run[[f"sp_{s}" for s in SP_NAMES]].to_numpy()
            changed = np.r_[False, np.any(np.diff(sp, axis=0) != 0, axis=1)]
            extra["sp_change_times"] = extra["time"][changed]
            tol = 0.05 * (self.sp_hi - self.sp_lo) + 0.002 * np.abs(self.sp_hi)
            outside = np.any((sp < self.sp_lo - tol) | (sp > self.sp_hi + tol), axis=1)
            extra["sp_outside"] = outside
            blocks.append(("ENV", ["ENV"], outside[:, None].astype(float)))

        # Reconstruction hypotheses, one per field tag: smoothed residuals of
        # every tag after replacing tag k by its virtual sensor.
        recon = np.empty((len(FIELD_IDX),) + R.shape)
        for n, k in enumerate(FIELD_IDX):
            _, zk = self.bank.reconstruct(x, k)
            recon[n] = ewma(zk, LAMBDA) ** 2
        extra["recon"] = recon

        idx, start = {}, 0
        for fam, names, M in blocks:
            idx[fam] = slice(start, start + len(names))
            start += len(names)
        extra["idx"] = idx
        S = np.hstack([M for _, _, M in blocks])
        names = [n for _, ns, _ in blocks for n in ns]
        return Analysis(S, names, extra)

    # ----- diagnosis ---------------------------------------------------------
    @staticmethod
    def _ratio(a, i, hot, lim, sl):
        return np.where(hot[i, sl], a.S[i, sl] / lim[sl], 0.0)

    def _integrity(self, a, i, hot, lim) -> bool:
        if not self.use_integrity:
            return False
        ix = a.extra["idx"]
        if hot[i, ix["V"]].any() or hot[i, ix["P"]].any():
            return True  # broken or replayed data
        if "F" not in ix:
            return False
        # Several transmitters froze at the same moment: a lost link or a
        # denial of service, not a process (saturating loops freeze one by one).
        F = hot[: i + 1, ix["F"]]
        onsets = []
        for j in np.flatnonzero(F[i]):
            s = i
            while s > 0 and F[s - 1, j]:
                s -= 1
            onsets.append(s)
        onsets.sort()
        k = self.freeze_min_tags
        return any(onsets[n + k - 1] - onsets[n] <= 1 for n in range(len(onsets) - k + 1))

    def _instant(self, a, i, hot, lim):
        """Diagnosis from the evidence at sample i alone."""
        ix = a.extra["idx"]
        if self._integrity(a, i, hot, lim):
            return INTEGRITY, None

        if "F" in ix:
            flat = self._ratio(a, i, hot, lim, ix["F"])
            if flat.max() > 0:  # a live process never produces a flat signal
                return SENSOR, FIELD_TAGS[int(np.argmax(flat))]

        if self.use_setpoints:
            t = a.extra["time"][i]
            recent = a.extra["sp_change_times"]
            if a.extra["sp_outside"][i] or np.any((recent <= t) & (recent > t - self.recent_sp_h)):
                return REGIME, None  # deviations are expected after an operator move

        if "N" in ix:
            noise = self._ratio(a, i, hot, lim, ix["N"])
            order = np.sort(noise)[::-1]
            if order[0] > 0 and order[0] >= 2.0 * order[1]:  # noise confined to one tag
                return SENSOR, FIELD_TAGS[int(np.argmax(noise))]

        r_lim, q_lim = lim[ix["R"]], lim[ix["Q"]][0]
        bank_hot = hot[i, ix["R"]].any() or hot[i, ix["Q"]].any()
        if bank_hot:
            nM = len(MONITORED)
            best, best_score = None, np.inf
            for n, k in enumerate(FIELD_IDX):
                if a.extra["R"][i, k] <= r_lim[k]:
                    continue  # tag k is not itself anomalous
                rk = a.extra["recon"][n, i]
                others = np.delete(np.arange(nM), k)
                rest = max(np.max(rk[others] / r_lim[others]), rk[others].sum() / q_lim)
                if rest <= self.explain_tol and rest < best_score:
                    best, best_score = k, rest
            if best is not None:
                return SENSOR, MONITORED[best]

        if self.use_setpoints:
            return PROCESS, None
        # Without DCS context: fall back to the classical reading of T2-only alarms.
        others = np.ones(hot.shape[1], dtype=bool)
        others[ix["T2"]] = False
        only_t2 = hot[i, ix["T2"]].any() and not hot[i, others].any()
        return (REGIME, None) if only_t2 else (PROCESS, None)

    def diagnose(self, a, i, limits):
        """Episode-level diagnosis: the root cause precedes its propagation.

        In closed loop a transmitter fault makes the controller move the real
        process, so after a while other tags deviate too. If one sensor
        consistently explained the alarm during its first minutes, and that
        sensor is still anomalous, the later spread is treated as propagation
        of that fault. Integrity evidence always takes precedence.
        """
        hot = a.S > limits
        any_hot = hot.any(axis=1)
        start = i
        while start > 0 and any_hot[start - 1]:
            start -= 1
        if any(self._integrity(a, s, hot, limits) for s in range(start, i + 1)):
            return INTEGRITY, None
        now = self._instant(a, i, hot, limits)
        window = range(start, min(i, start + self.onset_window) + 1)
        votes = [self._instant(a, s, hot, limits) for s in window]
        sensor_votes = [tag for cls, tag in votes if cls == SENSOR]
        if not sensor_votes:
            return now
        tag = max(set(sensor_votes), key=sensor_votes.count)
        if sensor_votes.count(tag) < self.onset_majority * len(votes):
            return now  # no consistent single-sensor explanation at onset
        ix = a.extra["idx"]
        k = MONITORED.index(tag)
        f = FIELD_TAGS.index(tag)
        still = hot[i, ix["R"]][k] or ("N" in ix and hot[i, ix["N"]][f]) or ("F" in ix and hot[i, ix["F"]][f])
        return (SENSOR, tag) if still else now


def all_methods() -> list[Method]:
    return [
        LimitChecks(),
        PCAContrib(),
        PCARBC(),
        Guardian(),
        Guardian(use_instrument=False, name="Guardian w/o instrument checks"),
        Guardian(use_setpoints=False, name="Guardian w/o setpoint context"),
        Guardian(lags=0, name="Guardian w/o dynamics (static bank)"),
        Guardian(use_integrity=False, name="Guardian w/o integrity layer"),
    ]


__all__ = ["all_methods", "Guardian", "LimitChecks", "PCAContrib", "PCARBC", "ENVELOPE"]
