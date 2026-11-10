"""Tag dictionary for the Tennessee Eastman process (Downs & Vogel, 1993).

Each measured variable is described the way it would appear in a plant
historian: tag, description, unit, and whether a control loop uses it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tag:
    name: str
    description: str
    unit: str
    in_control_loop: bool = False
    analyzer: bool = False


XMEAS = [
    Tag("xmeas_1", "A feed (stream 1)", "kscmh", True),
    Tag("xmeas_2", "D feed (stream 2)", "kg/h", True),
    Tag("xmeas_3", "E feed (stream 3)", "kg/h", True),
    Tag("xmeas_4", "A and C feed (stream 4)", "kscmh", True),
    Tag("xmeas_5", "Recycle flow (stream 8)", "kscmh"),
    Tag("xmeas_6", "Reactor feed rate (stream 6)", "kscmh"),
    Tag("xmeas_7", "Reactor pressure", "kPa g", True),
    Tag("xmeas_8", "Reactor level", "%", True),
    Tag("xmeas_9", "Reactor temperature", "degC", True),
    Tag("xmeas_10", "Purge rate (stream 9)", "kscmh", True),
    Tag("xmeas_11", "Separator temperature", "degC", True),
    Tag("xmeas_12", "Separator level", "%", True),
    Tag("xmeas_13", "Separator pressure", "kPa g"),
    Tag("xmeas_14", "Separator underflow (stream 10)", "m3/h", True),
    Tag("xmeas_15", "Stripper level", "%", True),
    Tag("xmeas_16", "Stripper pressure", "kPa g"),
    Tag("xmeas_17", "Stripper underflow (stream 11)", "m3/h", True),
    Tag("xmeas_18", "Stripper temperature", "degC"),
    Tag("xmeas_19", "Stripper steam flow", "kg/h"),
    Tag("xmeas_20", "Compressor work", "kW"),
    Tag("xmeas_21", "Reactor cooling water outlet temperature", "degC"),
    Tag("xmeas_22", "Separator cooling water outlet temperature", "degC"),
]
_feed = "ABCDEF"
_purge = "ABCDEFGH"
_prod = "DEFGH"
XMEAS += [Tag(f"xmeas_{23 + i}", f"Reactor feed analysis, component {c}", "mol%",
              in_control_loop=(c in "AC"), analyzer=True) for i, c in enumerate(_feed)]
XMEAS += [Tag(f"xmeas_{29 + i}", f"Purge gas analysis, component {c}", "mol%", analyzer=True)
          for i, c in enumerate(_purge)]
XMEAS += [Tag(f"xmeas_{37 + i}", f"Product analysis, component {c}", "mol%",
              in_control_loop=(c == "G"), analyzer=True) for i, c in enumerate(_prod)]

XMV = [
    Tag("xmv_1", "D feed flow (valve)", "%"),
    Tag("xmv_2", "E feed flow (valve)", "%"),
    Tag("xmv_3", "A feed flow (valve)", "%"),
    Tag("xmv_4", "A and C feed flow (valve)", "%"),
    Tag("xmv_5", "Compressor recycle valve", "%"),
    Tag("xmv_6", "Purge valve", "%"),
    Tag("xmv_7", "Separator liquid flow (valve)", "%"),
    Tag("xmv_8", "Stripper liquid product flow (valve)", "%"),
    Tag("xmv_9", "Stripper steam valve", "%"),
    Tag("xmv_10", "Reactor cooling water flow (valve)", "%"),
    Tag("xmv_11", "Condenser cooling water flow (valve)", "%"),
    Tag("xmv_12", "Agitator speed", "%"),
]

TAGS = {t.name: t for t in XMEAS + XMV}

# Continuous field instruments (transmitters) - the tags we monitor for faults.
FIELD_TAGS = [t.name for t in XMEAS[:22]]
# Controller outputs available from the DCS. The recycle valve, steam valve and
# agitator are held constant by this control strategy, so they carry no signal.
VALVE_TAGS = [f"xmv_{i}" for i in (1, 2, 3, 4, 6, 7, 8, 10, 11)]
MONITORED = FIELD_TAGS + VALVE_TAGS

# Analyzer sampling: (sample interval h, dead time h)
ANALYZER_TIMING = {**{f"xmeas_{i}": (0.1, 0.1) for i in range(23, 37)},
                   **{f"xmeas_{i}": (0.25, 0.25) for i in range(37, 42)}}

SAMPLE_PERIOD_H = 0.05  # historian logging interval (3 min)


def label(tag: str) -> str:
    t = TAGS[tag]
    return f"{tag.upper().replace('_', ' ')} {t.description} [{t.unit}]"
