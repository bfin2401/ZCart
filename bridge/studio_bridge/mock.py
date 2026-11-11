"""Stand-ins for the hardware: ``studio-bridge run --mock`` and the tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .config import Config
from .evo import Evo
from .evo_transport import MockTransport
from .lights import LightManager
from .store import LightRecord, MeshStore


class FakeController:
    """Records what would have gone to the lights."""

    def __init__(self, frames: list[tuple[Any, ...]], fail: Callable[[], bool] = lambda: False) -> None:
        self.frames = frames
        self._connected = False
        self._fail = fail

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        if self._fail():
            raise ConnectionError("light not in range")
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    def _record(self, *frame: Any) -> None:
        if self._fail():
            self._connected = False
            raise ConnectionError("link lost")
        self.frames.append(frame)

    async def power_on(self, *, dst: int | None = None) -> None:
        self._record("power", dst, True)

    async def power_off(self, *, dst: int | None = None) -> None:
        self._record("power", dst, False)

    async def set_params(self, **kwargs: Any) -> None:
        self._record("cct", kwargs.pop("dst"), kwargs)

    async def set_hsi(self, **kwargs: Any) -> None:
        self._record("hsi", kwargs.pop("dst"), kwargs)

    async def set_effect(self, effect: int, **kwargs: Any) -> None:
        self._record("effect", kwargs.pop("dst"), {"effect": effect, **kwargs})


DEMO_LIGHTS = (
    ("key", "Key (UP150R)", "UP150R"),
    ("fill", "Fill (UP150R)", "UP150R"),
    ("back-l", "Back left (UP150R)", "UP150R"),
    ("back-r", "Back right (UP150R)", "UP150R"),
    ("spot", "Spot (LE200Bi)", "LE200Bi"),
)


def demo_store(path: Path) -> MeshStore:
    store = MeshStore(path=path, network_key="00" * 16, app_key="11" * 16)
    for number, (light_id, name, model) in enumerate(DEMO_LIGHTS):
        store.lights.append(
            LightRecord(
                id=light_id,
                name=name,
                model=model,
                ble_address=f"AA:BB:CC:00:00:{number:02X}",
                node_address=2 + number * 2,
            )
        )
    return store


def mock_bridge_parts(config: Config) -> tuple[LightManager, Evo]:
    frames: list[tuple[Any, ...]] = []
    store = demo_store(Path("/nonexistent/mesh.json"))
    store.reserve_sequence = lambda used: None  # type: ignore[method-assign]
    lights = LightManager(store, config.godox, None, lambda record, store, on_seq: FakeController(frames))
    lights.frames = frames  # type: ignore[attr-defined]
    return lights, Evo(config.evo, MockTransport())
