"""Python interface to the closed-loop Tennessee Eastman simulator (sim/te_cli).

A Scenario describes one plant run: background operating moves, plus at most
one *event* with a ground-truth class. Running it returns a historian-style
DataFrame with the measured values (what the DCS sees), the true process
values, and per-sample ground-truth labels.
"""

from __future__ import annotations

import subprocess  # nosec B404 - only runs the bundled simulator binary
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "sim" / "te_cli"

NORMAL = "normal"
SENSOR = "sensor_fault"
PROCESS = "process_fault"
REGIME = "regime_change"
INTEGRITY = "integrity_attack"  # data integrity / cyber attack on the measurements
CLASSES = [NORMAL, SENSOR, PROCESS, REGIME, INTEGRITY]

# Nominal setpoints of the Ricker control strategy.
BASE_SP = {
    "prod_rate": 22.89,
    "reactor_pressure": 2800.0,
    "reactor_level": 65.0,
    "reactor_temp": 122.9,
    "pctg": 53.8,
    "sep_level": 50.0,
    "strip_level": 50.0,
}


@dataclass
class SetpointMove:
    name: str
    t: float
    value: float

    def arg(self) -> list[str]:
        return ["--sp", f"{self.name}@{self.t:g}={self.value:g}"]


@dataclass
class SensorFault:
    tag: str  # e.g. "xmeas_9"
    kind: str  # bias | drift | stuck | noise | gain
    t: float
    p: float = 0.0

    def arg(self) -> list[str]:
        idx = int(self.tag.split("_")[1])
        return ["--sf", f"{idx}:{self.kind}@{self.t:g}={self.p:g}"]


@dataclass
class Replay:
    """Replay attack: the tags replay their own readings of the previous rec_h hours."""
    tags: list[str]
    t: float
    rec_h: float = 2.0
    jitter: float = 0.0  # relative noise the attacker adds to hide repetition

    def arg(self) -> list[str]:
        idx = ",".join(t.split("_")[1] for t in self.tags)
        return ["--replay", f"{idx}@{self.t:g}:{self.rec_h:g}:{self.jitter:g}"]


@dataclass
class ActuatorAttack:
    """The real valve receives the controller output + delta; the DCS logs the command."""
    xmv: str  # e.g. "xmv_10"
    t: float
    delta: float

    def arg(self) -> list[str]:
        return ["--act", f"{self.xmv.split('_')[1]}@{self.t:g}={self.delta:g}"]


@dataclass
class Scenario:
    name: str
    seed: int
    hours: float = 40.0
    moves: list[SetpointMove] = field(default_factory=list)  # background operation
    event_class: str = NORMAL
    event_t: float | None = None
    idv: int | None = None  # process disturbance
    sensor_fault: SensorFault | None = None
    regime_moves: list[SetpointMove] = field(default_factory=list)
    # Integrity attacks
    attack: str | None = None  # attack family, for reporting
    replay: Replay | None = None
    actuator_attacks: list[ActuatorAttack] = field(default_factory=list)
    injected: list[SensorFault] = field(default_factory=list)  # attacker-written readings

    def args(self) -> list[str]:
        a = ["--hours", f"{self.hours:g}", "--seed", str(self.seed)]
        for m in self.moves + self.regime_moves:
            a += m.arg()
        if self.idv is not None:
            a += ["--idv", f"{self.idv}@{self.event_t:g}"]
        if self.sensor_fault is not None:
            a += self.sensor_fault.arg()
        if self.replay is not None:
            a += self.replay.arg()
        for x in self.actuator_attacks:
            a += x.arg()
        for f in self.injected:
            a += f.arg()
        return a

    @property
    def attacked_tags(self) -> list[str]:
        tags = list(self.replay.tags) if self.replay else []
        return tags + [f.tag for f in self.injected]

    @property
    def fault_tag(self) -> str | None:
        return self.sensor_fault.tag if self.sensor_fault else None

    def describe(self) -> str:
        if self.event_class == SENSOR:
            f = self.sensor_fault
            return f"{f.kind} on {f.tag} (p={f.p:g}) at {f.t:g} h"
        if self.event_class == PROCESS:
            return f"IDV({self.idv}) at {self.event_t:g} h"
        if self.event_class == INTEGRITY:
            return f"attack '{self.attack}' on {', '.join(self.attacked_tags)}" + (
                f" + {', '.join(f'{a.xmv}{a.delta:+g}%' for a in self.actuator_attacks)}"
                if self.actuator_attacks else "")
        if self.event_class == REGIME:
            return ", ".join(f"{m.name}->{m.value:g} at {m.t:g} h" for m in self.regime_moves)
        return "normal operation"


def run(s: Scenario) -> pd.DataFrame:
    """Simulate a scenario and return the historian DataFrame with labels."""
    if not BINARY.exists():
        raise FileNotFoundError(f"{BINARY} not built - run `make -C sim`")
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "run.csv"
        # Fixed binary, no shell, arguments built from typed scenario fields.
        p = subprocess.run([str(BINARY), "--out", str(out), *s.args()],  # nosec B603
                           capture_output=True, text=True, timeout=600)
        if p.returncode not in (0, 3):
            raise RuntimeError(p.stderr.strip())
        df = pd.read_csv(out)
        # Exit code 3: the plant's own interlocks shut it down. The data up to
        # the trip is still valid history, so keep it and record the trip.
        df.attrs["shutdown"] = p.stderr.strip() if p.returncode == 3 else ""
    df["time"] = df["time"].round(4)
    active = np.zeros(len(df), dtype=bool)
    if s.event_t is not None:
        active = df["time"].to_numpy() >= s.event_t
    df["label"] = np.where(active, s.event_class, NORMAL)
    df["scenario"] = s.name
    return df
