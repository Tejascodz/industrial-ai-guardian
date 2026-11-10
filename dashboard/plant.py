"""Plant metadata for the live dashboard: ISA-5.1 style tags, units, loops,
setpoint limits, disturbances and attack presets."""

from __future__ import annotations

from guardian.scenarios import ENVELOPE, REPLAY_ATTACKS, fault_unit
from guardian.sim import BASE_SP
from guardian.tags import FIELD_TAGS, TAGS, VALVE_TAGS

# Instrument tag numbers in the style of a P&ID (function letters + loop number).
ISA = {
    "xmeas_1": "FIC-101", "xmeas_2": "FIC-102", "xmeas_3": "FIC-103", "xmeas_4": "FIC-104",
    "xmeas_5": "FI-105", "xmeas_6": "FI-106", "xmeas_7": "PIC-107", "xmeas_8": "LIC-108",
    "xmeas_9": "TIC-109", "xmeas_10": "FIC-110", "xmeas_11": "TIC-111", "xmeas_12": "LIC-112",
    "xmeas_13": "PI-113", "xmeas_14": "FIC-114", "xmeas_15": "LIC-115", "xmeas_16": "PI-116",
    "xmeas_17": "FIC-117", "xmeas_18": "TI-118", "xmeas_19": "FI-119", "xmeas_20": "JI-120",
    "xmeas_21": "TI-121", "xmeas_22": "TI-122",
    "xmv_1": "FV-102", "xmv_2": "FV-103", "xmv_3": "FV-101", "xmv_4": "FV-104", "xmv_6": "FV-110",
    "xmv_7": "FV-114", "xmv_8": "FV-117", "xmv_10": "TV-109", "xmv_11": "TV-111",
    "xmeas_38": "AI-138",
}

SETPOINTS = {
    # name: (label, unit, hard min, hard max) - hard limits keep the trainer sane
    "prod_rate": ("Production rate", "m³/h", 18.0, 27.0),
    "pctg": ("Product G content", "mol%", 45.0, 62.0),
    "reactor_pressure": ("Reactor pressure", "kPa g", 2650.0, 2900.0),
    "reactor_level": ("Reactor level", "%", 50.0, 80.0),
    "reactor_temp": ("Reactor temperature", "°C", 120.0, 126.5),
    "sep_level": ("Separator level", "%", 30.0, 70.0),
    "strip_level": ("Stripper level", "%", 30.0, 70.0),
}

DISTURBANCES = {
    1: "A/C feed ratio step (stream 4)",
    2: "B composition step (stream 4)",
    3: "D feed temperature step",
    4: "Reactor cooling-water inlet temperature step",
    5: "Condenser cooling-water inlet temperature step",
    6: "Loss of A feed (trips the plant under this control scheme)",
    7: "C header pressure loss (stream 4)",
    8: "A, B, C feed composition random variation",
    9: "D feed temperature random variation",
    10: "C feed temperature random variation",
    11: "Reactor cooling-water inlet temperature random variation",
    12: "Condenser cooling-water inlet temperature random variation",
    13: "Reaction kinetics slow drift",
    14: "Reactor cooling-water valve sticking",
    15: "Condenser cooling-water valve sticking",
    16: "Unknown disturbance 16",
    17: "Unknown disturbance 17",
    18: "Unknown disturbance 18",
    19: "Unknown disturbance 19",
    20: "Unknown disturbance 20",
}

ATTACKS = {
    "replay_cooling_small": "Replay TIC-109 and TI-121, cooling-water valve −1.5 %",
    "replay_cooling_large": "Replay TIC-109 and TI-121, cooling-water valve −3 %",
    "replay_a_feed": "Replay FIC-101, A feed valve +15 %",
    "replay_purge": "Replay PIC-107 and FIC-110, purge valve −3 %",
    "replay_stripper_product": "Replay FIC-117, stripper product valve +5 %",
    "replay_all_transmitters": "Replay all 22 transmitters",
    "multi_freeze": "Freeze three transmitters at once",
    "stealthy_bias": "Coordinated slow bias on many transmitters",
}
ATTACK_SPECS = {f"replay_{k}": v for k, v in REPLAY_ATTACKS.items()}


def tag_meta() -> list[dict]:
    out = []
    for t in FIELD_TAGS + VALVE_TAGS + ["xmeas_38"]:
        meta = TAGS[t]
        out.append({
            "id": t, "isa": ISA[t], "name": meta.description, "unit": meta.unit,
            "loop": meta.in_control_loop, "kind": "valve" if t.startswith("xmv") else
            ("analyzer" if meta.analyzer else "transmitter"),
            "faultUnit": fault_unit(t) if t in FIELD_TAGS else None,
        })
    return out


def meta() -> dict:
    return {
        "tags": tag_meta(),
        "setpoints": [{"id": k, "label": v[0], "unit": v[1], "min": v[2], "max": v[3],
                       "base": BASE_SP[k], "envelope": ENVELOPE.get(k)} for k, v in SETPOINTS.items()],
        "disturbances": [{"id": k, "label": v} for k, v in DISTURBANCES.items()],
        "attacks": [{"id": k, "label": v} for k, v in ATTACKS.items()],
        "faultKinds": ["bias", "drift", "stuck", "noise"],
    }
