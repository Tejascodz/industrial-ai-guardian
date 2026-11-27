"""HAI 21.03: a hardware-in-the-loop industrial control testbed with labelled attacks.

HAI (HIL-based Augmented ICS security dataset) was recorded by the National
Security Research Institute, Korea, from a testbed in which real boiler,
turbine and water-treatment control hardware (Emerson Ovation, GE Mark VIe,
Siemens S7) is coupled to a dSPACE hardware-in-the-loop simulation of power
generation. It is not a real plant. Version 21.03 has 78 data points at 1 s,
256 h of normal operation and 50 attacks in 112 h of test data. Licence:
CC BY-SA 4.0. Source: https://github.com/icsdataset/hai

The data is not redistributed with Guardian; ``scripts/fetch_hai.py`` downloads
it and checks every file against the SHA-256 hashes below.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .plant import Plant

ROOT = Path(__file__).resolve().parents[1]
HAI_DIR = ROOT / "data" / "hai" / "hai-21.03"
RESAMPLE_S = 10  # 1 s data averaged to 10 s samples

# SHA-256 of each file at the pinned commit (hashes, not secrets).
SHA256 = {
    "train1.csv.gz": "fdd0162315b32817ba665e2cc8990eb09c47c4edc29683a86b11dc9df062b3a9",  # pragma: allowlist secret
    "train2.csv.gz": "c330af06a24f5fe3df864dddc0e9cbbbfd27db2e005bd32dc8ab3eb2ab356337",  # pragma: allowlist secret
    "train3.csv.gz": "de09120c617bad5f4f3cab86705137fe7ced4b1123fb67ffdf67fd645f3d1b8f",  # pragma: allowlist secret
    "test1.csv.gz": "532de41bed372206941609d05b763958f59abecfd63c8c123649f823452e445e",  # pragma: allowlist secret
    "test2.csv.gz": "c5572e0ed19906107ceb56c4b4f40e165153fc7ba87b3506230d53d2478c29bb",  # pragma: allowlist secret
    "test3.csv.gz": "bff835894baff0c73871bc01ece4f5cf759d455a049e808b012128767420caaf",  # pragma: allowlist secret
    "test4.csv.gz": "51380fd28fd985b94a10cae3d8b80926becb3af9f233f2d3a38ab8be9482a482",  # pragma: allowlist secret
    "test5.csv.gz": "f0d59308b167ed95d0e81fb3e27c906682c9b21d3dba0ab4c4d696565df35179",  # pragma: allowlist secret
}

# Tag roles, from the data-point table of the HAI technical details document.
# Field transmitters: measured process values and valve position feedback.
FIELD = (
    "P1_FCV01Z", "P1_FCV02Z", "P1_FCV03Z", "P1_LCV01Z", "P1_PCV01Z", "P1_PCV02Z",
    "P1_FT01", "P1_FT02", "P1_FT03", "P1_LIT01", "P1_PIT01", "P1_PIT02", "P1_TIT01", "P1_TIT02",
    "P2_24Vdc", "P2_SIT01", "P2_SIT02", "P2_VT01", "P2_VXT02", "P2_VXT03", "P2_VYT02", "P2_VYT03",
    "P3_FIT01", "P3_LIT01", "P3_PIT01",
    "P4_HT_FD", "P4_HT_PO", "P4_ST_FD", "P4_ST_PO", "P4_ST_PT01", "P4_ST_TT01",
)
# Controller outputs and computed (cascade or load-following) demands: modelled,
# never blamed as a faulty transmitter.
CONTROL = (
    "P1_B2016", "P1_B4022", "P1_B400B", "P1_B4005",
    "P1_FCV01D", "P1_FCV02D", "P1_FCV03D", "P1_LCV01D", "P1_PCV01D",
    "P2_CO_rpm", "P2_HILout", "P3_LCP01D", "P3_LCV01D",
    "P4_LD", "P4_HT_LD", "P4_ST_LD", "P4_ST_GOV",
)
# Operator setpoints, changed in steps by the HMI scheduler during normal operation.
SETPOINTS = ("P1_B2004", "P1_B3004", "P1_B3005", "P1_B4002", "P4_HT_PS", "P4_ST_PS")
# Not used: 30 tags that never change in normal operation (pump states, switches,
# fixed limits), and P1_FT01Z/FT02Z/FT03Z, which are unit conversions of
# P1_FT01/FT02/FT03 and would let a transmitter vouch for itself.

# Feedback loops (controller, controlled transmitter): a fault injected into one
# of these transmitters in recorded data would not reproduce the loop's reaction,
# so the semi-synthetic benchmark never injects into them.
LOOP_TRANSMITTERS = ("P1_PIT01", "P1_LIT01", "P1_FT03", "P1_TIT01", "P1_FT02", "P2_SIT01", "P3_LIT01")

HAI = Plant(
    name="HAI 21.03 (hardware-in-the-loop boiler, turbine, water treatment and power testbed)",
    monitored=FIELD + CONTROL,
    field=FIELD,
    setpoints=SETPOINTS,
    sample_period_h=RESAMPLE_S / 3600,
    spans={},  # the data-point table gives ranges, but not for every point; no span checks
)


@dataclass(frozen=True)
class Attack:
    id: str
    file: str
    start: str  # local time of day from the timetable, HH:MM
    duration_s: int
    targets: tuple[str, ...]  # attacked points (setpoints, controller outputs, hidden PVs)


# The 50 attacks of HAI 21.03, from the attack timetable of the technical details
# document (combined attacks list both primitives). The loader checks every entry
# against the attack labels in the data. "P2_LCV01D" in the timetable (A220 and
# the AP25 combinations) is read as P3_LCV01D, the only valve LCV01D of P3-LC.
ATTACKS = [
    Attack("A101", "test1", "15:35", 192, ("P1_B2016",)),
    Attack("A102", "test1", "17:28", 98, ("P1_B3005",)),
    Attack("A103", "test1", "18:59", 190, ("P1_LCV01D",)),
    Attack("A104", "test1", "20:21", 60, ("P2_SCO",)),
    Attack("A105", "test1", "21:03", 89, ("P2_AutoSD",)),
    Attack("A201", "test2", "15:47", 83, ("P2_VTR02",)),
    Attack("A202", "test2", "17:38", 422, ("P1_B2016", "P1_PIT01")),
    Attack("A203", "test2", "18:59", 17, ("P1_LCV01D",)),
    Attack("A204", "test2", "20:10", 259, ("P1_B3005", "P1_FT03")),
    Attack("A205", "test2", "21:15", 123, ("P1_B2016",)),
    Attack("A206", "test2", "23:02", 256, ("P1_FCV03D", "P1_FT03")),
    Attack("A207", "test2", "01:08", 68, ("P2_VTR01",)),
    Attack("A208", "test2", "01:33", 261, ("P1_B3004", "P1_LIT01")),
    Attack("A209", "test2", "03:03", 159, ("P1_B3004",)),
    Attack("A210", "test2", "05:29", 421, ("P1_PCV01D", "P1_PIT01")),
    Attack("A211", "test2", "07:51", 45, ("P2_AutoSD",)),
    Attack("A212", "test2", "09:13", 152, ("P2_AutoSD", "P2_SIT01")),
    Attack("A213", "test2", "10:49", 254, ("P1_LCV01D", "P1_LIT01")),
    Attack("A214", "test2", "12:51", 152, ("P1_PCV01D",)),
    Attack("A215", "test2", "15:11", 151, ("P2_SCO", "P2_SIT01")),
    Attack("A216", "test2", "15:40", 65, ("P1_FCV03D",)),
    Attack("A217", "test2", "16:22", 184, ("P2_RTR",)),
    Attack("A218", "test2", "18:21", 99, ("P1_FCV03D",)),
    Attack("A219", "test2", "21:25", 119, ("P3_LCP01D",)),
    Attack("A220", "test2", "22:56", 119, ("P3_LCV01D",)),
    Attack("A301", "test3", "13:51", 132, ("P1_LCV01D", "P1_B3005")),
    Attack("A302", "test3", "15:21", 421, ("P1_B2016", "P1_PIT01", "P1_B3005")),
    Attack("A303", "test3", "18:11", 189, ("P1_PCV01D", "P1_LCV01D")),
    Attack("A304", "test3", "20:53", 106, ("P2_AutoSD", "P2_VTR01")),
    Attack("A305", "test3", "21:23", 84, ("P2_SCO", "P2_VTR02")),
    Attack("A306", "test3", "23:55", 238, ("P1_B2016", "P2_AutoSD")),
    Attack("A307", "test3", "01:51", 110, ("P1_FCV03D", "P2_VTR01")),
    Attack("A308", "test3", "03:53", 255, ("P1_LCV01D", "P1_LIT01", "P2_AutoSD")),
    Attack("A401", "test4", "12:43", 254, ("P1_PCV01D", "P1_B3004", "P1_LIT01")),
    Attack("A402", "test4", "13:45", 262, ("P1_B3005", "P1_FT03", "P3_LCV01D")),
    Attack("A403", "test4", "15:57", 263, ("P1_B3004", "P1_LIT01", "P3_LCV01D")),
    Attack("A404", "test4", "17:45", 258, ("P2_SCO", "P2_SIT01", "P1_LCV01D", "P1_LIT01")),
    Attack("A405", "test4", "20:47", 120, ("P2_AutoSD", "P3_LCV01D")),
    Attack("A501", "test5", "11:16", 172, ("P1_PCV01D", "P2_VTR02")),
    Attack("A502", "test5", "13:30", 258, ("P1_FCV03D", "P1_FT03", "P2_SCO")),
    Attack("A503", "test5", "16:05", 256, ("P1_B3004", "P1_LIT01", "P2_SCO")),
    Attack("A504", "test5", "17:45", 120, ("P1_FCV03D", "P3_LCV01D")),
    Attack("A505", "test5", "18:38", 203, ("P1_B3004", "P2_AutoSD")),
    Attack("A506", "test5", "20:42", 153, ("P2_SCO", "P2_SIT01", "P3_LCV01D")),
    Attack("A507", "test5", "23:13", 79, ("P2_AutoSD", "P2_VTR01")),
    Attack("A508", "test5", "01:15", 51, ("P1_FCV03D", "P1_LCV01D")),
    Attack("A509", "test5", "02:01", 241, ("P1_B2016", "P1_PCV01D")),
    Attack("A510", "test5", "09:54", 262, ("P1_B3004", "P1_LCV01D", "P1_LIT01")),
    Attack("A511", "test5", "10:40", 120, ("P2_RTR", "P3_LCV01D")),
    Attack("A512", "test5", "11:21", 262, ("P1_B3005", "P1_FCV03D", "P1_FT03")),
]


def hidden_transmitters(a: Attack) -> list[str]:
    """Field transmitters the attack freezes or replays to hide its effect."""
    return [t for t in a.targets if t in FIELD]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_files(directory: Path = HAI_DIR) -> None:
    for name, digest in SHA256.items():
        p = directory / name
        if not p.exists():
            raise FileNotFoundError(f"{p} missing: run `python scripts/fetch_hai.py`")
        if sha256(p) != digest:
            raise ValueError(f"{p} does not match the expected SHA-256")


def load(name: str, directory: Path = HAI_DIR) -> pd.DataFrame:
    """One HAI file averaged to 10 s samples.

    Columns: ``time`` in hours since the start of the file (Guardian's time
    axis), ``timestamp`` (local time of the testbed), every used tag, and
    ``attack`` (1 if any 1 s sample in the 10 s window was under attack).
    """
    raw = pd.read_csv(directory / f"{name}.csv.gz", parse_dates=["time"])
    raw = raw.set_index("time")
    tags = list(HAI.monitored) + list(SETPOINTS)
    agg = raw[tags].resample(f"{RESAMPLE_S}s").mean()
    if "attack" in raw:
        agg["attack"] = raw["attack"].resample(f"{RESAMPLE_S}s").max()
    else:
        agg["attack"] = 0
    agg = agg.dropna(subset=tags).reset_index().rename(columns={"time": "timestamp"})
    agg["time"] = (agg["timestamp"] - agg["timestamp"].iloc[0]).dt.total_seconds() / 3600
    agg["scenario"] = name
    return agg


def attack_episodes(df: pd.DataFrame) -> list[tuple[float, float]]:
    """(start, end) in hours of each contiguous labelled attack."""
    a = df["attack"].to_numpy() > 0
    t = df["time"].to_numpy()
    starts = np.flatnonzero(a & ~np.r_[False, a[:-1]])
    ends = np.flatnonzero(a & ~np.r_[a[1:], False])
    return [(float(t[s]), float(t[e])) for s, e in zip(starts, ends, strict=True)]


def match_timetable(df: pd.DataFrame) -> list[tuple[Attack, float, float]]:
    """Pair each labelled episode in a test file with its timetable entry.

    Raises if the number of episodes or any start time disagrees with the
    timetable by more than a minute, so a transcription error cannot pass silently.
    """
    name = df["scenario"].iloc[0]
    table = [a for a in ATTACKS if a.file == name]
    eps = attack_episodes(df)
    if len(eps) != len(table):
        raise ValueError(f"{name}: {len(eps)} labelled attacks, timetable lists {len(table)}")
    ts = df.set_index("time")["timestamp"]
    out = []
    for a, (s, e) in zip(table, eps, strict=True):
        clock = ts.loc[s]
        hh, mm = map(int, a.start.split(":"))
        minutes = (clock.hour * 60 + clock.minute - (hh * 60 + mm)) % (24 * 60)
        if min(minutes, 24 * 60 - minutes) > 1:
            raise ValueError(f"{a.id}: labelled start {clock:%H:%M} vs timetable {a.start}")
        out.append((a, s, e))
    return out
