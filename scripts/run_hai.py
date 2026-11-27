"""Guardian on HAI 21.03, a hardware-in-the-loop testbed, with the method frozen.

    python scripts/fetch_hai.py      # once: download and verify the data
    python scripts/run_hai.py

Nothing in Guardian was tuned for HAI. Every setting is the one used on
Tennessee Eastman; only the plant description (guardian/hai.py) is new. Window
lengths are counted in samples, so at 10 s sampling they are 18x shorter in time.

Protocol
  fit          train1 + train2 (normal operation, 176 h)
  calibrate    train3 (normal, 80 h): alarm limits for 0.25 false alarms per day
  attacks      test1-5 (112 h, 50 labelled attacks). An attack is detected by an
               alarm that starts between its first labelled sample and 10 min
               after its last; alarms starting elsewhere are false alarms.
               HAI's attacks manipulate setpoints and controller outputs. Where an
               attack also "hides" a sensor, it does so on the HMI path: in the
               recorded data that sensor keeps reporting the real process. So no
               transmitter is faulty in the data, and naming one is a false blame.
  injected     transmitter faults added to normal stretches of test1-5, on
               transmitters outside feedback loops only (injecting into a loop's
               own transmitter in recorded data would skip the loop's reaction).

Writes results/hai_attack_runs.csv, results/hai_injected_runs.csv and
results/hai_summary.csv (95 % intervals: Wilson, exact Poisson, bootstrap).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from guardian import hai  # noqa: E402
from guardian.evaluate import Prepared, calibrate, persisted  # noqa: E402
from guardian.methods import PCARBC, Guardian, LimitChecks, PCAContrib  # noqa: E402
from guardian.sim import NORMAL, SENSOR  # noqa: E402
from guardian.stats import boot_median, poisson_rate, wilson  # noqa: E402

RES = ROOT / "results"
TARGET_FA = 0.25
CHUNK_H = 6.0  # long files are analysed in chunks ...
WARM_H = 0.5  # ... each with a discarded warm-up, so chunk edges never alarm
GRACE_H = 10 / 60  # alarms up to 10 min after an attack ends still count as detections
DIAG_LATER = 20  # samples after detection for the settled diagnosis (same count as on TE)
SAMPLE_H = hai.HAI.sample_period_h

# Semi-synthetic faults: transmitters outside every feedback loop, with enough
# variation in normal operation to model.
INJECT_TAGS = ["P1_FT01", "P1_PIT02", "P1_TIT02", "P1_FCV01Z", "P1_FCV03Z", "P1_LCV01Z", "P1_PCV01Z",
               "P2_SIT02", "P2_VT01", "P3_FIT01", "P3_PIT01", "P4_ST_PT01", "P4_ST_TT01"]
KINDS = ["bias", "drift", "stuck", "noise"]
REPS = 2
SLICE_H, INJECT_AT_H = 3.0, 1.5  # 0.5 h warm-up, 1 h normal, 1.5 h faulty
SLICE_STEP_H = 0.5  # candidate slices start every 30 min (they may overlap)


def methods() -> list:
    p = hai.HAI
    return [LimitChecks(plant=p), PCAContrib(plant=p), PCARBC(plant=p), Guardian(plant=p)]


def chunks(df: pd.DataFrame):
    """(sub-frame, mask of samples to score) covering df once, each with a warm-up."""
    t = df["time"].to_numpy()
    start = 0.0
    while start < t[-1]:
        sel = (t >= start - WARM_H) & (t < start + CHUNK_H)
        sub = df[sel].reset_index(drop=True)
        yield sub, sub["time"].to_numpy() >= start
        start += CHUNK_H


class Scored:
    """Alarms of one method over one long file, stitched from chunks."""

    def __init__(self, m, df: pd.DataFrame, limits: np.ndarray):
        self.parts = []  # (analysis, local indices kept)
        alarm, times = [], []
        for sub, keep in chunks(df):
            a = m.analyze(sub)
            al = persisted((a.S > limits).any(axis=1))
            self.parts.append((a, np.flatnonzero(keep)))
            alarm.append(al[keep])
            times.append(sub["time"].to_numpy()[keep])
        self.alarm = np.concatenate(alarm)
        self.time = np.concatenate(times)
        self.m, self.limits = m, limits

    def locate(self, i: int):
        """Global sample i -> (analysis, local index)."""
        off = 0
        for a, kept in self.parts:
            if i < off + len(kept):
                return a, int(kept[i - off])
            off += len(kept)
        raise IndexError(i)

    def diagnose(self, i: int) -> tuple[str, str | None]:
        a, j = self.locate(i)
        return self.m.diagnose(a, j, self.limits)


def calibrated(m, cal: pd.DataFrame) -> np.ndarray:
    prep = []
    for sub, keep in chunks(cal):
        prep.append(Prepared(str(len(prep)), {"event_class": NORMAL}, m.analyze(sub), sub["time"].to_numpy(), keep))
    limits, _ = calibrate(prep, TARGET_FA, SAMPLE_H)
    return limits


def score_attacks(m, limits, tests: dict[str, pd.DataFrame]) -> tuple[list[dict], dict]:
    rows, fa, normal_h = [], 0, 0.0
    for name, df in tests.items():
        s = Scored(m, df, limits)
        eps = hai.match_timetable(df)
        rising = s.alarm & ~np.r_[False, s.alarm[:-1]]
        covered = np.zeros(len(s.time), dtype=bool)
        for _, t0, t1 in eps:
            covered |= (s.time >= t0) & (s.time <= t1 + GRACE_H)
        fa += int(np.sum(rising & ~covered))
        normal_h += float(np.sum(~covered)) * SAMPLE_H
        for atk, t0, t1 in eps:
            hit = np.flatnonzero(rising & (s.time >= t0) & (s.time <= t1 + GRACE_H))
            row = {"method": m.name, "attack": atk.id, "file": name, "targets": " ".join(atk.targets),
                   "hidden_transmitters": " ".join(hai.hidden_transmitters(atk)),
                   "duration_s": atk.duration_s, "detected": bool(len(hit))}
            if len(hit):
                i = int(hit[0])
                j = min(i + DIAG_LATER, len(s.time) - 1)
                c0, g0 = s.diagnose(i)
                c1, g1 = s.diagnose(j)
                row.update(delay_s=round((s.time[i] - t0) * 3600), class_at_detection=c0, tag_at_detection=g0,
                           class_settled=c1, tag_settled=g1)
            rows.append(row)
    return rows, {"false_alarms": fa, "normal_hours": normal_h}


def window_sigma(train: list[pd.DataFrame], tags: list[str]) -> dict[str, float]:
    """Typical short-term variation of each tag: median std over 1 h windows of normal data."""
    w = int(round(1.0 / SAMPLE_H))
    out = {}
    for t in tags:
        stds = [g[t].iloc[k:k + w].std() for g in train for k in range(0, len(g) - w, w)]
        out[t] = float(np.median(stds))
    return out


def normal_slices(tests: dict[str, pd.DataFrame]) -> list[pd.DataFrame]:
    """3 h stretches of test data at least 30 min away from any attack."""
    out = []
    for df in tests.values():
        eps = hai.attack_episodes(df)
        t = df["time"].to_numpy()
        s = 0.0
        while s + SLICE_H <= t[-1]:
            if all(e + 0.5 < s or b - 0.5 > s + SLICE_H for b, e in eps):
                sub = df[(t >= s) & (t < s + SLICE_H)].copy()
                sub["time"] = sub["time"] - s
                out.append(sub.reset_index(drop=True))
            s += SLICE_STEP_H
    return out


def inject(sub: pd.DataFrame, tag: str, kind: str, size: float, rng: np.random.Generator) -> pd.DataFrame:
    out = sub.copy()
    t = out["time"].to_numpy()
    on = t >= INJECT_AT_H
    v = out[tag].to_numpy().copy()
    if kind == "bias":
        v[on] += size
    elif kind == "drift":
        v[on] += size * (t[on] - INJECT_AT_H) / (SLICE_H - INJECT_AT_H)
    elif kind == "stuck":
        v[on] = v[np.flatnonzero(on)[0] - 1]
    elif kind == "noise":
        v[on] += rng.normal(0.0, size, on.sum())
    out[tag] = v
    return out


def score_injected(m, limits, cases) -> list[dict]:
    rows = []
    for case, run in cases:
        a = m.analyze(run)
        t = run["time"].to_numpy()
        alarm = persisted((a.S > limits).any(axis=1))
        rising = alarm & ~np.r_[False, alarm[:-1]]
        hit = np.flatnonzero(rising & (t >= INJECT_AT_H))
        row = {"method": m.name, **case, "detected": bool(len(hit))}
        if len(hit):
            i = int(hit[0])
            j = min(i + DIAG_LATER, len(t) - 1)
            cls, tag = m.diagnose(a, j, limits)
            row.update(delay_s=round((t[i] - INJECT_AT_H) * 3600), class_settled=cls, tag_settled=tag)
        rows.append(row)
    return rows


def summarize(att: pd.DataFrame, fa: dict, inj: pd.DataFrame) -> pd.DataFrame:
    rows = []

    def add(m, metric, est, k=None, n=None):
        rows.append({"method": m, "metric": metric, "value": est[0], "ci_low": est[1], "ci_high": est[2],
                     "k": k, "n": n})

    for m, g in att.groupby("method", sort=False):
        f = fa[m]
        add(m, "attack_false_alarms_per_day", poisson_rate(f["false_alarms"], f["normal_hours"] / 24),
            f["false_alarms"], round(f["normal_hours"] / 24, 2))
        det = g[g.detected]
        add(m, "attacks_detected", wilson(len(det), len(g)), len(det), len(g))
        add(m, "attack_median_delay_s", boot_median(det.delay_s.to_numpy()), None, len(det))
        # Attacks last 1 to 7 min, so the class is read when the alarm starts.
        blamed = int((det.class_at_detection == SENSOR).sum())
        add(m, "attacks_blamed_on_a_transmitter", wilson(blamed, len(det)), blamed, len(det))
        for cls in ["integrity_attack", "sensor_fault", "process_fault", "regime_change", "normal"]:
            k = int((det.class_at_detection == cls).sum())
            add(m, f"attack_class_{cls}", wilson(k, len(det)), k, len(det))
        ig = inj[inj.method == m]
        idet = ig[ig.detected]
        add(m, "injected_detected", wilson(len(idet), len(ig)), len(idet), len(ig))
        if len(idet):
            add(m, "injected_median_delay_s", boot_median(idet.delay_s.to_numpy()), None, len(idet))
        named = int(((idet.class_settled == SENSOR) & (idet.tag_settled == idet.tag)).sum())
        wrong = int(((idet.class_settled == SENSOR) & (idet.tag_settled != idet.tag)).sum())
        add(m, "injected_transmitter_named", wilson(named, len(idet)), named, len(idet))
        add(m, "injected_wrong_transmitter_blamed", wilson(wrong, len(idet)), wrong, len(idet))
    return pd.DataFrame(rows)


def main() -> None:
    t0 = time.time()
    hai.verify_files()
    train = [hai.load("train1"), hai.load("train2")]
    cal = hai.load("train3")
    tests = {n: hai.load(n) for n in ["test1", "test2", "test3", "test4", "test5"]}
    print(f"data loaded ({time.time() - t0:.0f} s)")

    sigma = window_sigma(train, INJECT_TAGS)
    slices = normal_slices(tests)
    rng = np.random.default_rng(2103)
    cases = []
    for tag in INJECT_TAGS:
        for kind in KINDS:
            for rep in range(REPS):
                sub = slices[int(rng.integers(len(slices)))]
                sign = 1.0 if rep % 2 == 0 else -1.0
                size = {"bias": 4.0 * sign, "drift": 6.0 * sign, "stuck": 0.0, "noise": 2.0}[kind] * sigma[tag]
                case = {"tag": tag, "kind": kind, "rep": rep, "size": size, "size_sigma": size / sigma[tag]}
                cases.append((case, inject(sub, tag, kind, abs(size) if kind == "noise" else size, rng)))
    print(f"{len(slices)} normal 3 h slices, {len(cases)} injected faults")

    att_rows, inj_rows, fa = [], [], {}
    for m in methods():
        t1 = time.time()
        m.fit(train, [cal])
        limits = calibrated(m, cal)
        rows, f = score_attacks(m, limits, tests)
        att_rows += rows
        fa[m.name] = f
        inj_rows += score_injected(m, limits, cases)
        d = sum(r["detected"] for r in rows)
        print(f"{m.name:22s} attacks {d}/50, false alarms {f['false_alarms']} in {f['normal_hours']:.1f} h "
              f"({time.time() - t1:.0f} s)")

    att, inj = pd.DataFrame(att_rows), pd.DataFrame(inj_rows)
    att.to_csv(RES / "hai_attack_runs.csv", index=False)
    inj.to_csv(RES / "hai_injected_runs.csv", index=False, float_format="%.6g")
    summ = summarize(att, fa, inj)
    by_kind = []
    for (m, kind), g in inj.groupby(["method", "kind"], sort=False):
        d = g[g.detected]
        named = int(((d.class_settled == SENSOR) & (d.tag_settled == d.tag)).sum())
        by_kind.append({"method": m, "kind": kind, "runs": len(g), "detected": len(d), "named": named})
    pd.DataFrame(by_kind).to_csv(RES / "hai_injected_by_kind.csv", index=False)
    summ.to_csv(RES / "hai_summary.csv", index=False, float_format="%.6g")
    pd.set_option("display.width", 160)
    print(summ.to_string(index=False))
    print(f"done in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
