"""What each light model can do.

The Godox mesh lights share one vendor protocol; a model only decides which
modes exist and their ranges. Values for the LE200Bi are Godox's own catalogue
entry (radio id 00AE). The UP150R was not in the catalogue this was built
from, so its entry is taken from the published specification (1800-10000 K,
HSI, RGBW, gels) and from its nearest catalogued relatives, the UP-C150/C300;
``assumed`` marks it until it has been checked on a real light.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

EFFECT_NAMES: dict[int, str] = {
    3: "RGB Cycle",
    4: "Party",
    5: "Flash",
    6: "Lightning",
    7: "Cloudy",
    8: "Broken Bulb",
    9: "TV",
    10: "Candle",
    11: "Fire",
    12: "Firework",
    13: "Explosion",
    14: "Welding",
    15: "Police Car",
    16: "SOS",
    17: "Music",
}


@dataclass(frozen=True)
class Profile:
    model: str
    kelvin_min: int
    kelvin_max: int
    tint: bool = False
    hsi: bool = False
    #: 0 = eight-byte F3 effect frame, 1 = parameterised F7 frame.
    effect_version: int = 1
    effects: tuple[int, ...] = ()
    assumed: bool = False
    modes: tuple[str, ...] = field(default=("cct",))

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["effects"] = [{"id": e, "name": EFFECT_NAMES.get(e, f"Effect {e}")} for e in self.effects]
        return data


PROFILES: dict[str, Profile] = {
    "LE200Bi": Profile(
        model="LE200Bi",
        kelvin_min=2800,
        kelvin_max=6500,
        effects=(6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 17),
        modes=("cct", "effect"),
    ),
    "UP150R": Profile(
        model="UP150R",
        kelvin_min=1800,
        kelvin_max=10000,
        tint=True,
        hsi=True,
        effects=(6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 4, 5),
        assumed=True,
        modes=("cct", "hsi", "effect"),
    ),
    # Anything else: full ranges, nothing refused. The light ignores what it
    # cannot do.
    "generic-rgb": Profile(
        model="generic-rgb",
        kelvin_min=1800,
        kelvin_max=10000,
        tint=True,
        hsi=True,
        effects=tuple(range(3, 18)),
        assumed=True,
        modes=("cct", "hsi", "effect"),
    ),
    "generic-bicolor": Profile(
        model="generic-bicolor",
        kelvin_min=2800,
        kelvin_max=6500,
        effects=(6, 7, 8, 9, 10, 11, 12, 13, 14, 15),
        assumed=True,
        modes=("cct", "effect"),
    ),
}


def profile_for(model: str) -> Profile:
    for name, profile in PROFILES.items():
        if name.lower() == model.lower():
            return profile
    raise KeyError(f"unknown model {model!r}; known: {', '.join(PROFILES)}")
