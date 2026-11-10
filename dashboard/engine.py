"""Live session: the closed-loop plant, Guardian running online, alarm
management and the audit trail.

The plant is the real simulator (sim/te_cli in interactive mode). Each tick
advances it by one historian sample (3 plant minutes), runs Guardian on the
recent window exactly as in the benchmark, updates alarms and records
everything in the HMAC-chained audit log.

Trainer actions (faults, disturbances, attacks, setpoint moves) exist only
because this is a simulator: Guardian itself never writes to a plant.
"""

from __future__ import annotations

import math
import subprocess  # nosec B404 - only runs the bundled simulator binary
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from guardian.audit import AuditLog
from guardian.evaluate import persisted
from guardian.features import BURN_IN_H, SP_NAMES
from guardian.methods import Guardian
from guardian.scenarios import fault_unit, stealth_direction
from guardian.sim import BASE_SP, BINARY, INTEGRITY, NORMAL, PROCESS, REGIME, SENSOR
from guardian.softsensor import TARGET, SoftSensor
from guardian.tags import ANALYZER_TIMING, FIELD_TAGS, MONITORED, SAMPLE_PERIOD_H, TAGS

from .plant import ATTACK_SPECS, DISTURBANCES, ISA, SETPOINTS

WINDOW = 200  # samples Guardian analyses each tick (10 plant hours)
HISTORY = 480  # samples kept for trends (24 plant hours)
PREROLL = 80  # samples simulated before the live view starts (4 h start-up)
DEBOUNCE = 3  # samples a changed diagnosis must hold before the alarm is updated
SP_COLS = [f"sp_{s}" for s in SP_NAMES]

PRIORITY = {INTEGRITY: 1, PROCESS: 2, SENSOR: 3, REGIME: 4}
PRIORITY_NAME = {1: "Critical", 2: "High", 3: "Medium", 4: "Advisory"}


class CommandError(ValueError):
    pass


@dataclass
class Alarm:
    id: int
    t_on: float
    cls: str
    tag: str | None
    priority: int
    message: str
    state: str = "active_unacked"  # active_unacked | active_acked | rtn_unacked | cleared
    t_off: float | None = None
    history: list = field(default_factory=list)

    def public(self) -> dict:
        return {"id": self.id, "tOn": self.t_on, "tOff": self.t_off, "cls": self.cls, "tag": self.tag,
                "isa": ISA.get(self.tag) if self.tag else None, "priority": self.priority,
                "priorityName": PRIORITY_NAME[self.priority], "message": self.message, "state": self.state}


def describe(cls: str, tag: str | None) -> tuple[str, str]:
    """Verdict and recommended action, in operator language."""
    if cls == SENSOR and tag:
        name = f"{ISA[tag]} {TAGS[tag].description.lower()}"
        loop = TAGS[tag].in_control_loop
        action = (f"Check {ISA[tag]} in the field. Its loop is acting on a wrong reading, so the real "
                  "process has moved: treat the product-quality estimate as untrusted." if loop else
                  f"Check {ISA[tag]} in the field. The soft sensor now uses Guardian's virtual "
                  f"value for {ISA[tag]} instead of the transmitter.")
        return f"Transmitter fault suspected: {name}", action
    if cls == PROCESS:
        return ("Real process change", "The transmitters agree with each other: something in the "
                "process changed. Investigate the unit; the product-quality estimate is untrusted.")
    if cls == REGIME:
        return ("Operating outside the validated range", "A setpoint was moved recently or is outside "
                "the range the models were validated on. Expect deviations; estimates are untrusted.")
    if cls == INTEGRITY:
        return ("Data integrity alarm", "Readings are repeating exactly, frozen together or broken. "
                "Do not act on these values; escalate to OT security and check the data path.")
    return "Normal operation", "No action needed."


EVIDENCE = {
    "R": "disagrees with the rest of the plant",
    "N": "shows excess noise",
    "F": "signal is flat",
    "P": "repeats an earlier recording exactly",
}
BINARY_CHANNELS = {"F", "P", "V", "ENV"}  # yes/no evidence: a ratio means nothing


def evidence(channel: str, ratio: float) -> str:
    fam, _, tag = channel.partition(":")
    if fam in EVIDENCE and tag:
        suffix = "" if fam in BINARY_CHANNELS else f" ({ratio:.1f}× its limit)"
        return f"{ISA.get(tag, tag)} {EVIDENCE[fam]}{suffix}"
    return {
        "Q": f"Many small deviations across the plant ({ratio:.1f}× limit)",
        "T2": f"Operating point is unusual for the normal history ({ratio:.1f}× limit)",
        "ENV": "A setpoint is outside the validated range",
        "V": "The data feed is broken (missing values or bad timestamps)",
    }.get(fam, channel)


class LiveSession:
    def __init__(self, guardian: Guardian, limits: np.ndarray, softsensor: SoftSensor,
                 audit: AuditLog, seed: int = 7):
        self.g, self.limits, self.ss, self.audit = guardian, limits, softsensor, audit
        self.lock = threading.RLock()
        self.seed = seed
        self.stealth = None
        self.proc: subprocess.Popen | None = None
        self.start(seed)

    # ----- simulator process --------------------------------------------------
    def _send(self, line: str) -> tuple[list[str], str]:
        if not (self.proc and self.proc.stdin and self.proc.stdout):
            raise RuntimeError("simulator is not running")
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        rows = []
        while True:
            out = self.proc.stdout.readline()
            if not out:
                raise RuntimeError("simulator stopped unexpectedly")
            out = out.strip()
            if out.startswith(("OK", "ERR", "TRIP")):
                return rows, out
            rows.append(out)

    def _command(self, line: str) -> None:
        if self.trip:
            raise CommandError("the plant has tripped; reset it first")
        _, status = self._send(line)
        if status.startswith("ERR"):
            raise CommandError(status[4:])
        if status.startswith("TRIP"):
            self._tripped(status)

    def start(self, seed: int) -> None:
        with self.lock:
            self.stop()
            self.seed = seed
            self.proc = subprocess.Popen(  # nosec B603 - bundled binary, fixed arguments
                [str(BINARY), "--interactive", "--seed", str(seed)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, text=True, bufsize=1)
            self.columns = self.proc.stdout.readline().strip().split(",")
            self.sp = dict(BASE_SP)
            self.rows: deque[dict] = deque(maxlen=HISTORY + WINDOW)
            self.series: deque[dict] = deque(maxlen=HISTORY)
            self.alarms: list[Alarm] = []
            self.next_alarm_id = 1
            self.events: list[dict] = []  # ground truth: what the trainer injected
            self.trip: str | None = None
            self.active_alarm: Alarm | None = None
            self._candidate, self._votes = None, 0
            self.state: dict = {}
            self.live = False
            rows, status = self._send(f"step {PREROLL}")
            for r in rows:
                self._append(r)
            for _ in range(WINDOW // 4):  # let the filters settle before going live
                self._tick_locked()
            self.alarms, self.active_alarm, self.live = [], None, True
            self.audit.append({"action": "session_start", "seed": seed})
            self._publish(self.state.get("guardian", {}))

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write("quit\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=2)
            except Exception:
                self.proc.kill()

    def _append(self, line: str) -> dict:
        vals = np.array(line.split(","), dtype=float)
        row = dict(zip(self.columns, vals))
        for s in SP_NAMES:
            row[f"sp_{s}"] = self.sp[s]
        self.rows.append(row)
        return row

    def _tripped(self, status: str) -> None:
        parts = status.split(" ", 2)
        self.trip = parts[2] if len(parts) > 2 else "plant trip"
        t = float(parts[1]) if len(parts) > 1 else self.now
        alarm = Alarm(self.next_alarm_id, t, "trip", None, 1, f"Plant trip: {self.trip}")
        self.next_alarm_id += 1
        self.alarms.insert(0, alarm)
        self.audit.append({"alarm": "raised", **alarm.public()})

    @property
    def now(self) -> float:
        return float(self.rows[-1]["time"]) if self.rows else 0.0

    # ----- one live step ------------------------------------------------------
    def tick(self) -> dict:
        with self.lock:
            if self.trip:
                return self.state
            return self._tick_locked()

    def _tick_locked(self) -> dict:
        rows, status = self._send("step 1")
        if status.startswith("TRIP"):
            self._tripped(status)
            return self._publish(self.state.get("guardian", {}))
        for r in rows:
            self._append(r)
        return self._analyse()

    def _analyse(self) -> dict:
        df = pd.DataFrame(list(self.rows)[-WINDOW:])
        df = df[df["time"] >= BURN_IN_H].reset_index(drop=True)  # plant start-up is not monitored
        a = self.g.analyze(df)
        hot = a.S > self.limits
        alarm = persisted(hot.any(axis=1))
        i = len(df) - 1
        raw_cls, raw_tag = (self.g.diagnose(a, i, self.limits) if alarm[i] else (NORMAL, None))
        self._update_alarms(bool(alarm[i]), raw_cls, raw_tag)
        # What the operator sees is the alarm's (debounced) diagnosis.
        act = self.active_alarm if self.live else None
        cls, tag = (act.cls, act.tag) if act else (raw_cls, raw_tag)

        x = self.g.scaler.transform(df)
        virtual = self.g.bank.predict(x)[-1] * self.g.scaler.std + self.g.scaler.mean
        raw = float(self.ss.predict_x(x)[-1])
        protected, ss_status = raw, "ok"
        if cls == SENSOR and tag:
            xk, _ = self.g.bank.reconstruct(x, MONITORED.index(tag))
            protected = float(self.ss.predict_x(xk)[-1])
            ss_status = "untrusted" if TAGS[tag].in_control_loop else "reconstructed"
        elif cls != NORMAL:
            ss_status = "untrusted"

        ratios = a.S[i] / self.limits
        order = np.argsort(-ratios)
        ev = [evidence(a.channels[c], float(ratios[c])) for c in order[:6] if ratios[c] > 1.0]
        idx = a.extra["idx"]
        health = {}
        for n, t in enumerate(MONITORED):
            r = float(ratios[idx["R"]][n])
            if t in FIELD_TAGS:
                f = FIELD_TAGS.index(t)
                for fam in ("N", "F", "P"):
                    if fam in idx:
                        r = max(r, float(ratios[idx[fam]][f]))
            health[t] = round(r, 3)
        verdict, action = describe(cls, tag)
        row = self.rows[-1]
        lag = ANALYZER_TIMING[TARGET][1]
        point = {
            "t": round(float(row["time"]), 4),
            "meas": {t: float(row[t]) for t in MONITORED},
            "virtual": {t: float(virtual[n]) for n, t in enumerate(MONITORED)},
            "true": {t: float(row[t.replace("xmeas_", "true_")]) for t in FIELD_TAGS},
            "ss": {"raw": raw, "protected": protected, "status": ss_status,
                   "analyzer": float(row[TARGET]), "analyzerT": round(float(row["time"]) - lag, 4)},
            "alarm": bool(alarm[i]),
            "cls": cls,
        }
        self.series.append(point)
        guardian = {"cls": cls, "tag": tag, "isa": ISA.get(tag) if tag else None, "verdict": verdict,
                    "action": action, "evidence": ev, "health": health, "alarm": bool(alarm[i]),
                    "since": self.active_alarm.t_on if self.active_alarm else None}
        return self._publish(guardian)

    def _publish(self, guardian: dict) -> dict:
        self.state = {
            "t": self.now, "trip": self.trip, "sp": dict(self.sp), "guardian": guardian,
            "point": self.series[-1] if self.series else None,
            "alarms": [x.public() for x in self.alarms if x.state != "cleared"][:50],
            "journal": [x.public() for x in self.alarms][:100],
            "truth": [e for e in self.events if e.get("active")],
            "seed": self.seed,
        }
        return self.state

    # ----- alarm management (ISA-18.2 states) ---------------------------------
    def _update_alarms(self, on: bool, cls: str, tag: str | None) -> None:
        """Raise, re-diagnose and clear alarms. A new diagnosis must hold for
        DEBOUNCE consecutive samples before it replaces the current one, so the
        operator is not shown a flickering verdict."""
        if not self.live:
            return
        a = self.active_alarm
        verdict = describe(cls, tag)[0]
        if on and a is None:
            a = Alarm(self.next_alarm_id, self.now, cls, tag, PRIORITY.get(cls, 2), verdict)
            self.next_alarm_id += 1
            self.alarms.insert(0, a)
            self.active_alarm = a
            self._candidate, self._votes = None, 0
            self.audit.append({"alarm": "raised", **a.public()})
        elif on and a is not None and (cls, tag) != (a.cls, a.tag):
            self._votes = self._votes + 1 if self._candidate == (cls, tag) else 1
            self._candidate = (cls, tag)
            if self._votes >= DEBOUNCE:
                a.history.append((self.now, a.cls, a.tag))
                a.cls, a.tag, a.message = cls, tag, verdict
                a.priority = min(a.priority, PRIORITY.get(cls, 2))
                self._candidate, self._votes = None, 0
                self.audit.append({"alarm": "diagnosis_changed", **a.public()})
        elif on:
            self._candidate, self._votes = None, 0
        elif a is not None:
            a.t_off = self.now
            a.state = "cleared" if a.state == "active_acked" else "rtn_unacked"
            self.audit.append({"alarm": "returned_to_normal", **a.public()})
            self.active_alarm = None

    def acknowledge(self, alarm_id: int | None) -> None:
        with self.lock:
            for a in self.alarms:
                if alarm_id is not None and a.id != alarm_id:
                    continue
                if a.state == "active_unacked":
                    a.state = "active_acked"
                elif a.state == "rtn_unacked":
                    a.state = "cleared"
                else:
                    continue
                self.audit.append({"alarm": "acknowledged", **a.public()})
            self._publish(self.state.get("guardian", {}))

    # ----- trainer actions ----------------------------------------------------
    def _event(self, kind: str, label: str, **extra) -> None:
        self.events.append({"kind": kind, "label": label, "t": self.now, "active": True, **extra})
        self.audit.append({"action": "trainer", "kind": kind, "label": label, **extra})

    def _end_events(self, kind: str, match=lambda e: True) -> None:
        for e in self.events:
            if e["active"] and e["kind"] == kind and match(e):
                e["active"] = False
                e["tEnd"] = self.now

    def set_setpoint(self, name: str, value: float) -> None:
        if name not in SETPOINTS:
            raise CommandError(f"unknown setpoint {name}")
        if not math.isfinite(value):
            raise CommandError("setpoint must be a finite number")
        lo, hi = SETPOINTS[name][2], SETPOINTS[name][3]
        if not lo <= value <= hi:
            raise CommandError(f"{SETPOINTS[name][0]} must be between {lo:g} and {hi:g}")
        with self.lock:
            self._command(f"sp {name} {value:.6g}")
            self.sp[name] = value
            self._event("setpoint", f"{SETPOINTS[name][0]} → {value:g} {SETPOINTS[name][1]}",
                        name=name, value=value)

    def inject_fault(self, tag: str, kind: str, magnitude: float) -> None:
        if tag not in FIELD_TAGS:
            raise CommandError("faults can be injected on the 22 field transmitters only")
        if kind not in ("bias", "drift", "stuck", "noise"):
            raise CommandError(f"unknown fault type {kind}")
        if not math.isfinite(magnitude):
            raise CommandError("fault size must be a finite number")
        u = fault_unit(tag)
        if abs(magnitude) > 20 * u:
            raise CommandError(f"magnitude is limited to ±{20 * u:.4g} {TAGS[tag].unit} for {ISA[tag]}")
        if kind == "noise" and magnitude <= 0:
            raise CommandError("noise magnitude must be positive")
        with self.lock:
            idx = int(tag.split("_")[1])
            self._command(f"sf {idx} {kind} {magnitude:.6g}")
            unit = TAGS[tag].unit + ("/h" if kind == "drift" else "")
            label = f"{kind} on {ISA[tag]}" + ("" if kind == "stuck" else f" ({magnitude:+g} {unit})")
            self._event("sensor_fault", label, tag=tag, faultKind=kind, magnitude=magnitude)

    def clear_faults(self) -> None:
        with self.lock:
            self._command("sfclear all")
            self._end_events("sensor_fault")
            self.audit.append({"action": "trainer", "kind": "clear_faults"})

    def disturbance(self, idv: int, on: bool) -> None:
        if idv not in DISTURBANCES:
            raise CommandError("disturbance must be 1..20")
        with self.lock:
            self._command(f"idv {idv} {1 if on else 0}")
            if on:
                self._event("disturbance", f"IDV({idv}) {DISTURBANCES[idv]}", idv=idv)
            else:
                self._end_events("disturbance", lambda e: e.get("idv") == idv)
                self.audit.append({"action": "trainer", "kind": "disturbance_off", "idv": idv})

    def attack(self, preset: str, jitter: bool) -> None:
        with self.lock:
            if preset in ATTACK_SPECS:
                tags, xmv, delta = ATTACK_SPECS[preset]
                ids = ",".join(t.split("_")[1] for t in tags)
                self._command(f"replay {ids} 2 {1e-4 if jitter else 0}")
                if xmv:
                    self._command(f"act {xmv.split('_')[1]} {delta:g}")
                label = f"replay of {len(tags)} transmitter(s)" + (f", {ISA[xmv]} {delta:+g} %" if xmv else "")
                self._event("attack", label + (" with noise" if jitter else ""), preset=preset)
            elif preset == "multi_freeze":
                rng = np.random.default_rng()
                tags = [str(t) for t in rng.choice(FIELD_TAGS, size=3, replace=False)]
                for t in tags:
                    self._command(f"sf {t.split('_')[1]} stuck 0")
                self._event("attack", "freeze of " + ", ".join(ISA[t] for t in tags), preset=preset)
            elif preset == "stealthy_bias":
                if self.stealth is None:
                    self.stealth = stealth_direction()
                for t, v in self.stealth.items():
                    if abs(v) > 1e-9:
                        self._command(f"sf {t.split('_')[1]} drift {3.0 * v / 2.0:.6g}")
                self._event("attack", "coordinated slow bias on many transmitters", preset=preset)
            else:
                raise CommandError(f"unknown attack {preset}")

    def stop_attacks(self) -> None:
        with self.lock:
            self._command("replayclear")
            self._command("actclear")
            self._command("sfclear all")
            self._end_events("attack")
            self._end_events("sensor_fault")
            self.audit.append({"action": "trainer", "kind": "stop_attacks"})

    # ----- snapshot for a new client -----------------------------------------
    def snapshot(self) -> dict:
        with self.lock:
            return {"series": list(self.series), "state": self.state}


def sample_period_s() -> float:
    return SAMPLE_PERIOD_H * 3600.0


__all__ = ["LiveSession", "CommandError", "Path"]
