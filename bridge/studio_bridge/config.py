"""Configuration file (TOML) for the bridge."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("/etc/studio-bridge/config.toml")
DEFAULT_STATE_DIR = Path("/var/lib/studio-bridge")


@dataclass
class HttpConfig:
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass
class GodoxConfig:
    enabled: bool = True
    #: How often a power command is sent. The vendor app sends it twice.
    power_repeat: int = 2
    #: Minimum gap between two frames to the same light, in seconds. Dial
    #: turns arriving faster than this collapse into the latest value.
    min_interval: float = 0.06
    #: Seconds between reconnect attempts while no light can be reached.
    reconnect_interval: float = 5.0


@dataclass
class EvoConfig:
    enabled: bool = True
    #: "auto" (kmod if /dev/evo8 exists, else libusb), "kmod", "libusb", "mock"
    transport: str = "auto"
    device: str = "/dev/evo8"
    #: Seconds between read-backs of phantom/gain/mute (0: every 30 s).
    poll_interval: float = 2.0
    #: Raw [evo.smartgain] table; parsed by studio_bridge.smartgain.
    smartgain: dict[str, Any] = field(default_factory=dict)


@dataclass
class Config:
    state_dir: Path = DEFAULT_STATE_DIR
    http: HttpConfig = field(default_factory=HttpConfig)
    godox: GodoxConfig = field(default_factory=GodoxConfig)
    evo: EvoConfig = field(default_factory=EvoConfig)

    @property
    def mesh_path(self) -> Path:
        return self.state_dir / "mesh.json"

    @property
    def shadow_path(self) -> Path:
        return self.state_dir / "lights-state.json"


def _apply(target: Any, values: dict[str, Any], section: str) -> None:
    for key, value in values.items():
        if not hasattr(target, key):
            raise ValueError(f"unknown option [{section}] {key}")
        setattr(target, key, value)


def load_config(path: Path | str | None = None) -> Config:
    """Load the config file; a missing default file means all defaults."""

    config = Config()
    explicit = path is not None
    file = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not file.exists():
        if explicit:
            raise FileNotFoundError(file)
        return config
    with file.open("rb") as handle:
        raw = tomllib.load(handle)

    if "state_dir" in raw:
        config.state_dir = Path(raw.pop("state_dir"))
    _apply(config.http, raw.pop("http", {}), "http")
    _apply(config.godox, raw.pop("godox", {}), "godox")
    _apply(config.evo, raw.pop("evo", {}), "evo")
    if raw:
        raise ValueError(f"unknown config section(s): {', '.join(sorted(raw))}")
    return config
