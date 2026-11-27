"""Plant abstraction and the HAI loader. No downloaded data is needed."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from guardian import hai
from guardian.evaluate import calibrate, persisted, prepare
from guardian.methods import Guardian
from guardian.plant import TE, Plant
from guardian.sim import NORMAL, SENSOR

ROOT = Path(__file__).resolve().parents[1]


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ----- plant descriptions -------------------------------------------------------
def test_plant_rejects_inconsistent_descriptions():
    with pytest.raises(ValueError):
        Plant("x", ("a", "b"), ("c",), (), 0.1)
    with pytest.raises(ValueError):
        Plant("x", ("a", "a"), ("a",), (), 0.1)
    with pytest.raises(ValueError):
        Plant("x", ("a",), ("a",), (), 0.0)


def test_te_plant_keeps_the_original_windows():
    # Windows are counted in samples; on TE the setpoint window is still 2 h.
    assert Guardian.recent_sp_samples * TE.sample_period_h == 2.0


def test_hai_plant_roles_are_disjoint_and_complete():
    p = hai.HAI
    assert set(p.field) <= set(p.monitored)
    assert not set(p.setpoints) & set(p.monitored)
    assert set(hai.LOOP_TRANSMITTERS) <= set(p.field)
    inject = _script("run_hai").INJECT_TAGS
    assert set(inject) <= set(p.field) and not set(inject) & set(hai.LOOP_TRANSMITTERS)


# ----- attack timetable ---------------------------------------------------------
def test_timetable_has_the_50_attacks_of_hai_21_03():
    ids = [a.id for a in hai.ATTACKS]
    assert len(ids) == 50 == len(set(ids))
    per_file = {f: sum(a.file == f for a in hai.ATTACKS) for f in ("test1", "test2", "test3", "test4", "test5")}
    assert per_file == {"test1": 5, "test2": 20, "test3": 8, "test4": 5, "test5": 12}
    assert all(a.duration_s > 0 and a.targets for a in hai.ATTACKS)
    assert all(set(hai.hidden_transmitters(a)) <= set(hai.FIELD) for a in hai.ATTACKS)


def _write_raw(path: Path, start: str, seconds: int, attack_at: tuple[int, int] | None = None) -> None:
    t = pd.date_range(start, periods=seconds, freq="1s")
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"time": t.strftime("%Y-%m-%d %H:%M:%S")})
    for c in list(hai.HAI.monitored) + list(hai.SETPOINTS):
        df[c] = rng.normal(size=seconds)
    df["attack"] = 0
    if attack_at:
        df.loc[attack_at[0]:attack_at[1], "attack"] = 1
    df.to_csv(path, index=False, compression="gzip")


def test_loader_averages_to_10_s_and_keeps_attack_labels(tmp_path):
    _write_raw(tmp_path / "test9.csv.gz", "2020-07-07 15:00:00", 120, attack_at=(35, 44))
    df = hai.load("test9", tmp_path)
    assert len(df) == 12
    assert np.allclose(np.diff(df["time"]), 10 / 3600)
    assert df["attack"].tolist() == [0, 0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0]
    assert hai.attack_episodes(df) == [(df["time"][3], df["time"][4])]


def test_timetable_mismatch_is_refused(tmp_path):
    _write_raw(tmp_path / "test1.csv.gz", "2020-07-07 15:00:00", 600, attack_at=(100, 200))
    with pytest.raises(ValueError):
        hai.match_timetable(hai.load("test1", tmp_path))  # one labelled attack, timetable lists five


# ----- the method on a plant it has never seen --------------------------------------
def _toy_runs(n_runs: int, seed: int) -> list[pd.DataFrame]:
    """Five correlated tags driven by one latent process variable."""
    rng = np.random.default_rng(seed)
    out = []
    for r in range(n_runs):
        n = 600
        u = np.cumsum(rng.normal(0, 0.05, n))
        x = np.column_stack([u, 2 * u, -u, u + 0.5 * np.roll(u, 1), 3 * u]) + rng.normal(0, 0.05, (n, 5))
        df = pd.DataFrame(x, columns=["a", "b", "c", "d", "e"])
        df["time"] = np.arange(n) / 60
        df["scenario"] = f"r{r}"
        out.append(df)
    return out


def test_guardian_runs_on_any_plant_description():
    toy = Plant("toy", ("a", "b", "c", "d", "e"), ("a", "b", "c", "d", "e"), (), 1 / 60)
    train, val = _toy_runs(6, 1), _toy_runs(4, 2)
    g = Guardian(plant=toy).fit(train, val)
    meta = {r["scenario"][0]: {"event_class": NORMAL} for r in val}
    limits, _ = calibrate(prepare(g, [(r["scenario"][0], r) for r in val], meta), 1.0, toy.sample_period_h)
    run = _toy_runs(1, 3)[0]
    run.loc[300:, "c"] += 1.0  # bias on transmitter c
    a = g.analyze(run)
    alarm = persisted((a.S > limits).any(axis=1))
    i = int(np.argmax(alarm & (np.arange(len(run)) >= 300)))
    assert alarm[i]
    assert g.diagnose(a, min(i + 20, len(run) - 1), limits) == (SENSOR, "c")
