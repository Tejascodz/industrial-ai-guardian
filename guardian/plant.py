"""What Guardian needs to know about a plant.

Guardian's method is plant-independent: it needs the list of tags to model,
which of them are field transmitters (the only tags it can blame), which
columns carry DCS setpoints, the instrument spans, and the sampling period.
Window lengths inside the methods are counted in samples, so they scale with
the sampling period of the plant.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .features import SP_NAMES
from .tags import FIELD_TAGS, MONITORED, SAMPLE_PERIOD_H
from .validation import SPANS


@dataclass(frozen=True)
class Plant:
    name: str
    monitored: tuple[str, ...]  # every tag in the models: transmitters and controller outputs
    field: tuple[str, ...]  # field transmitters, a subset of monitored
    setpoints: tuple[str, ...]  # columns that hold DCS setpoints (may be empty)
    sample_period_h: float
    spans: dict[str, tuple[float, float]] = field(default_factory=dict, compare=False, hash=False)

    def __post_init__(self) -> None:
        missing = [t for t in self.field if t not in self.monitored]
        if missing:
            raise ValueError(f"field tags not in monitored: {missing}")
        if len(set(self.monitored)) != len(self.monitored):
            raise ValueError("duplicate monitored tags")
        if not self.sample_period_h > 0:
            raise ValueError("sample_period_h must be positive")

    @property
    def field_idx(self) -> list[int]:
        return [self.monitored.index(t) for t in self.field]


TE = Plant(
    name="Tennessee Eastman (closed loop, Ricker control)",
    monitored=tuple(MONITORED),
    field=tuple(FIELD_TAGS),
    setpoints=tuple(f"sp_{n}" for n in SP_NAMES),
    sample_period_h=SAMPLE_PERIOD_H,
    spans=SPANS,
)
