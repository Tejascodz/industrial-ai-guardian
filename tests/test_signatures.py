"""Closed-loop fault signatures (Guardian rule 6)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from guardian import artifacts
from guardian.evaluate import calibrate, persisted, prepare
from guardian.methods import Guardian
from guardian.plant import Plant
from guardian.sim import NORMAL, PROCESS, SENSOR

TOY = Plant("toy", ("a", "b", "c", "d", "e"), ("a", "b", "c", "d", "e"), (), 1 / 60)
LOOP_RESPONSE = np.array([0.0, 1.2, -0.9, 0.0, 0.7])  # what a fault on "a" does to the plant, in closed loop
OTHER = np.array([0.0, 0.9, 0.9, 0.0, -1.1])  # a process fault along a different direction


def _runs(n_runs: int, seed: int, shift: np.ndarray | None = None, size: float = 1.0) -> list[pd.DataFrame]:
    rng = np.random.default_rng(seed)
    out = []
    for r in range(n_runs):
        n = 900
        u = np.cumsum(rng.normal(0, 0.05, n))
        x = np.column_stack([u, 2 * u, -u, u + 0.5 * np.roll(u, 1), 3 * u]) + rng.normal(0, 0.05, (n, 5))
        if shift is not None:
            ramp = np.clip((np.arange(n) - 600) / 30, 0, 1)[:, None]
            x += size * ramp * shift[None, :]
        df = pd.DataFrame(x, columns=list("abcde"))
        df["time"] = np.arange(n) / 60
        df["scenario"] = f"r{seed}_{r}"
        out.append(df)
    return out


def _fitted():
    g = Guardian(plant=TOY).fit(_runs(6, 1), _runs(4, 2))
    val = _runs(4, 3)
    meta = {r["scenario"][0]: {"event_class": NORMAL} for r in val}
    limits, _ = calibrate(prepare(g, [(r["scenario"][0], r) for r in val], meta), 1.0, TOY.sample_period_h)
    return g, limits


def _verdict(g, limits, run):
    a = g.analyze(run)
    alarm = persisted((a.S > limits).any(axis=1))
    i = int(np.argmax(alarm & (np.arange(len(run)) >= 600)))
    assert alarm[i]
    return g.diagnose(a, min(i + 60, len(run) - 1), limits)


def test_without_signatures_a_loop_response_is_a_process_fault():
    g, limits = _fitted()
    g.fit_signatures([])
    assert g.signatures == {}
    assert _verdict(g, limits, _runs(1, 7, LOOP_RESPONSE)[0]) == (PROCESS, None)


def test_signature_names_the_in_loop_transmitter_and_nothing_else():
    g, limits = _fitted()
    g.fit_signatures([(r, "a", 10.0) for r in _runs(4, 5, LOOP_RESPONSE)] +
                     [(r, "d", 10.0) for r in _runs(4, 6, np.array([0.0, 0.0, 1.0, 0.0, 1.0]))])
    assert set(g.signatures) == {"a", "d"}
    assert _verdict(g, limits, _runs(1, 7, LOOP_RESPONSE, size=0.8)[0]) == (SENSOR, "a")
    assert _verdict(g, limits, _runs(1, 8, OTHER)[0]) == (PROCESS, None)
    g.use_signatures = False
    assert _verdict(g, limits, _runs(1, 7, LOOP_RESPONSE, size=0.8)[0]) == (PROCESS, None)


def test_signatures_survive_a_signed_artifact_roundtrip(tmp_path):
    g, _ = _fitted()
    g.fit_signatures([(r, "a", 10.0) for r in _runs(4, 5, LOOP_RESPONSE)])
    g.sp_lo = g.sp_hi = np.zeros(0)
    artifacts.save(g, tmp_path, key=b"k")
    g2, _, _ = artifacts.load(tmp_path, key=b"k")
    assert set(g2.signatures) == {"a"}
    np.testing.assert_array_equal(g.signatures["a"], g2.signatures["a"])
