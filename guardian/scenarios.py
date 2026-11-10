"""Scenario library with ground truth.

Splits use disjoint random seeds:
  train  - normal operating history only (what a plant historian would hold)
  val    - normal runs + labelled events, used to set thresholds
  test   - normal runs + labelled events, used only for the reported results

Normal operation is not a single steady state: operators move production
rate, product quality and reactor level setpoints inside a normal envelope.
Events are one of
  sensor_fault   - a transmitter fault (bias, drift, stuck, noise)
  process_fault  - a real process disturbance, IDV(k) of Downs & Vogel
  regime_change  - setpoint moves to an operating point outside the envelope
"""

from __future__ import annotations

import numpy as np

from .sim import (INTEGRITY, PROCESS, REGIME, SENSOR, BASE_SP, ActuatorAttack, Replay, Scenario,
                  SensorFault, SetpointMove)
from .tags import FIELD_TAGS

# Normal operating envelope (operators may move setpoints within these ranges).
ENVELOPE = {
    "prod_rate": (21.5, 24.2),
    "pctg": (51.5, 56.0),
    "reactor_level": (62.0, 68.0),
}

# Out-of-envelope operating points for regime changes.
REGIME_TARGETS = [
    ("prod_rate", 25.6), ("prod_rate", 20.2),
    ("pctg", 58.5), ("pctg", 49.0),
    ("reactor_pressure", 2730.0), ("reactor_pressure", 2860.0),
    ("reactor_temp", 124.0), ("reactor_temp", 121.6),
]

# Process disturbances that the Ricker control strategy rides through.
# IDV(3), (9) and (15) are omitted: they are known to have no observable effect.
# IDV(6) (loss of A feed) trips the plant under this control strategy.
PROCESS_IDVS = [1, 2, 4, 5, 7, 8, 10, 11, 12, 13, 14, 16, 17, 18, 19, 20]

FAULT_KINDS = ["bias", "drift", "stuck", "noise"]

NOMINAL = {
    "xmeas_1": 0.2666, "xmeas_2": 3652.0, "xmeas_3": 4446.0, "xmeas_4": 9.237,
    "xmeas_5": 32.41, "xmeas_6": 47.58, "xmeas_7": 2800.0, "xmeas_8": 65.0,
    "xmeas_9": 122.9, "xmeas_10": 0.2119, "xmeas_11": 92.0, "xmeas_12": 50.0,
    "xmeas_13": 2704.0, "xmeas_14": 25.27, "xmeas_15": 50.0, "xmeas_16": 3332.0,
    "xmeas_17": 22.89, "xmeas_18": 66.62, "xmeas_19": 0.0, "xmeas_20": 272.9,
    "xmeas_21": 102.5, "xmeas_22": 92.34,
}
_TEMPS = {"xmeas_9", "xmeas_11", "xmeas_18", "xmeas_21", "xmeas_22"}
_LEVELS = {"xmeas_8", "xmeas_12", "xmeas_15"}
_PRESSURES = {"xmeas_7", "xmeas_13", "xmeas_16"}


def fault_unit(tag: str) -> float:
    """A realistic 'one unit' of instrument error for a tag, in engineering units."""
    if tag in _TEMPS:
        return 0.5  # degC - RTD / thermocouple calibration error
    if tag in _LEVELS:
        return 2.0  # % of level span
    if tag in _PRESSURES:
        return 0.005 * NOMINAL[tag]
    if tag == "xmeas_19":
        return 2.0  # steam flow is near zero under this strategy
    return 0.01 * NOMINAL[tag]  # 1 % of reading for flows and compressor work


def _background(rng: np.random.Generator, hours: float, n_moves: int,
                t_range: tuple[float, float]) -> list[SetpointMove]:
    moves = []
    for t in np.sort(rng.uniform(*t_range, size=n_moves)):
        name = rng.choice(list(ENVELOPE))
        lo, hi = ENVELOPE[name]
        moves.append(SetpointMove(str(name), round(float(t), 2), round(float(rng.uniform(lo, hi)), 3)))
    return moves


def normal_history(seed: int, hours: float = 48.0) -> Scenario:
    rng = np.random.default_rng(seed)
    moves = _background(rng, hours, int(rng.integers(3, 6)), (1.0, hours - 4.0))
    return Scenario(f"train_normal_{seed}", seed, hours, moves)


def _base(rng, name, seed, hours, event_t):
    # Background: one or two in-envelope moves before the event window, and
    # sometimes one shortly after the event time (a legitimate move that the
    # monitor must not confuse with a fault).
    moves = _background(rng, hours, int(rng.integers(1, 3)), (1.0, event_t - 3.0))
    return Scenario(name, seed, hours, moves, event_t=event_t)


def make_split(split: str, seed0: int, n_normal: int, sensor_reps: int,
               process_reps: int, regime_reps: int, hours: float = 40.0,
               event_t: float = 12.0) -> list[Scenario]:
    rng = np.random.default_rng(seed0)
    seed = seed0
    out: list[Scenario] = []

    def next_seed():
        nonlocal seed
        seed += 1
        return seed

    for i in range(n_normal):
        s = _base(rng, f"{split}_normal_{i}", next_seed(), hours, event_t)
        s.event_t = None
        if rng.random() < 0.7:  # in-envelope move during the evaluation window
            s.moves += _background(rng, hours, 1, (event_t, event_t + 6.0))
        out.append(s)

    for rep in range(sensor_reps):
        for tag in FIELD_TAGS:
            for kind in FAULT_KINDS:
                s = _base(rng, f"{split}_sf_{tag}_{kind}_{rep}", next_seed(), hours, event_t)
                u = fault_unit(tag)
                sign = rng.choice([-1.0, 1.0])
                if kind == "bias":
                    p = sign * rng.uniform(1.0, 3.0) * u
                elif kind == "drift":
                    p = sign * rng.uniform(2.0, 4.0) * u / 8.0  # per hour
                elif kind == "noise":
                    p = rng.uniform(1.0, 3.0) * u
                else:
                    p = 0.0
                s.event_class = SENSOR
                s.sensor_fault = SensorFault(tag, kind, event_t, round(float(p), 6))
                out.append(s)

    for rep in range(process_reps):
        for k in PROCESS_IDVS:
            s = _base(rng, f"{split}_idv{k}_{rep}", next_seed(), hours, event_t)
            s.event_class = PROCESS
            s.idv = k
            out.append(s)

    for rep in range(regime_reps):
        for name, value in REGIME_TARGETS:
            s = _base(rng, f"{split}_regime_{name}_{value:g}_{rep}", next_seed(), hours, event_t)
            s.event_class = REGIME
            s.regime_moves = [SetpointMove(name, event_t, value)]
            out.append(s)
    return out


# --------------------------------------------------------------------------
# Integrity attacks (cyber): the attacker can rewrite transmitter readings on
# the way to the DCS and can move a real valve while the DCS logs the command.
# --------------------------------------------------------------------------
# Stuxnet-style: replay the transmitters around the attacked valve so the DCS
# sees normal readings while the plant is pushed somewhere else.
REPLAY_ATTACKS = {
    "cooling_small": (["xmeas_9", "xmeas_21"], "xmv_10", -1.5),
    "cooling_large": (["xmeas_9", "xmeas_21"], "xmv_10", -3.0),
    "a_feed": (["xmeas_1"], "xmv_3", 15.0),
    "purge": (["xmeas_7", "xmeas_10"], "xmv_6", -3.0),
    "stripper_product": (["xmeas_17"], "xmv_8", 5.0),
    "all_transmitters": (FIELD_TAGS, None, 0.0),
}
JITTER = 1e-4  # relative noise an attacker adds so replayed values never repeat exactly


def stealth_direction() -> dict[str, float]:
    """A coordinated bias along the main correlation of normal data.

    An informed attacker biases several transmitters together so the change
    looks like a normal operating move to a correlation-based monitor.
    Returns the bias per field tag in engineering units for 'one unit' of attack.
    """
    import pandas as pd

    from .features import DATA, BURN_IN_H
    from .tags import MONITORED

    df = pd.read_parquet(DATA / "train.parquet", columns=["time", *MONITORED])
    df = df[df.time >= BURN_IN_H]
    X = df[MONITORED].to_numpy()
    sd = X.std(axis=0)
    w, V = np.linalg.eigh(np.cov((X - X.mean(0)) / sd, rowvar=False))
    p = V[:, -1]
    out = {t: float(p[MONITORED.index(t)] * sd[MONITORED.index(t)]) for t in FIELD_TAGS}
    norm = np.sqrt(sum((out[t] / sd[MONITORED.index(t)]) ** 2 for t in FIELD_TAGS))
    return {t: v / norm for t, v in out.items()}


def attack_split(split: str, seed0: int, reps: int, hours: float = 40.0,
                 event_t: float = 12.0) -> list[Scenario]:
    rng = np.random.default_rng(seed0)
    seed = seed0
    direction = stealth_direction()
    out: list[Scenario] = []
    for rep in range(reps):
        for name, (tags, xmv, delta) in REPLAY_ATTACKS.items():
            for jitter in (0.0, JITTER):
                seed += 1
                s = _base(rng, f"{split}_atk_{name}{'_jit' if jitter else ''}_{rep}", seed, hours, event_t)
                s.event_class, s.attack = INTEGRITY, f"replay_{name}" + ("_jitter" if jitter else "")
                s.replay = Replay(list(tags), event_t, 2.0, jitter)
                if xmv:
                    s.actuator_attacks = [ActuatorAttack(xmv, event_t, delta)]
                out.append(s)
        # Several transmitters freeze at once (lost communication or a DoS).
        seed += 1
        s = _base(rng, f"{split}_atk_freeze_{rep}", seed, hours, event_t)
        s.event_class, s.attack = INTEGRITY, "multi_freeze"
        tags = rng.choice(FIELD_TAGS, size=int(rng.integers(3, 6)), replace=False)
        s.injected = [SensorFault(str(t), "stuck", event_t, 0.0) for t in tags]
        out.append(s)
        # Coordinated slow bias along the main normal correlation, reaching
        # 3 standard deviations of normal variation after 8 hours.
        seed += 1
        s = _base(rng, f"{split}_atk_stealth_{rep}", seed, hours, event_t)
        s.event_class, s.attack = INTEGRITY, "stealthy_coordinated_bias"
        sign = rng.choice([-1.0, 1.0])
        s.injected = [SensorFault(t, "drift", event_t, round(sign * 3.0 * v / 8.0, 8))
                      for t, v in direction.items() if abs(v) > 1e-9]
        out.append(s)
    return out


def all_splits() -> dict[str, list[Scenario]]:
    return {
        "train": [normal_history(1000 + i) for i in range(30)],
        "val": make_split("val", 20000, n_normal=12, sensor_reps=1, process_reps=2, regime_reps=1),
        # Seed bases 50000 (test) and 80000 (test_attack) are retired: that split
        # was seen during development (see docs/benchmark.md).
        "test": make_split("test", 60000, n_normal=24, sensor_reps=2, process_reps=4, regime_reps=3),
    }


def attack_splits() -> dict[str, list[Scenario]]:
    return {
        "val_attack": attack_split("val", 70000, reps=1),
        "test_attack": attack_split("test", 90000, reps=3),
    }


__all__ = ["all_splits", "fault_unit", "ENVELOPE", "BASE_SP", "PROCESS_IDVS"]
