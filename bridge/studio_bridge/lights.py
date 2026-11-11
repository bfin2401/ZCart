"""Godox Bluetooth Mesh lights: one proxy connection, every light on it.

The lights do not reliably report their state (and never report power), so
the bridge keeps what it last asked for and treats that as the truth. Changes
made on a light's own panel are therefore not seen here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Protocol

from .config import GodoxConfig
from .models import Profile, profile_for
from .store import LightRecord, MeshStore, write_json_atomic

logger = logging.getLogger(__name__)

#: Frame byte 4 of a colour-temperature command names the dimming curve.
#: 0xFF names none, so the curve chosen on the light's panel is left alone.
KEEP_DIMMING_CURVE = 0xFF


class LightError(Exception):
    """A request that cannot be carried out; the message is for the user."""


@dataclass
class LightState:
    on: bool = True
    mode: str = "cct"  # cct | hsi | effect
    brightness: float = 50.0
    kelvin: int = 5600
    gm: int = 0
    hue: int = 0
    saturation: int = 100
    effect: int = 6
    speed: int | None = None


class Controller(Protocol):
    """The part of godox_mesh_bt.GodoxController the bridge uses."""

    @property
    def is_connected(self) -> bool: ...
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def power_on(self, *, dst: int | None = None) -> None: ...
    async def power_off(self, *, dst: int | None = None) -> None: ...
    async def set_params(self, **kwargs: Any) -> None: ...
    async def set_hsi(self, **kwargs: Any) -> None: ...
    async def set_effect(self, effect: int, **kwargs: Any) -> None: ...


ControllerFactory = Callable[[LightRecord, MeshStore, Callable[[int], None]], Controller]


def godox_controller_factory(
    light: LightRecord, store: MeshStore, on_sequence: Callable[[int], None]
) -> Controller:
    """Build the real controller. Imported late so tests need no Bluetooth."""

    from godox_mesh_bt import GodoxController, MeshState

    state = MeshState(
        network_key=store.network_key,
        app_key=store.app_key,
        provisioner_address=store.provisioner_address,
        node_address=light.node_address,
        sequence_number=store.sequence_number,
        iv_index=store.iv_index,
        device_key=light.device_key,
        device_address=light.ble_address,
        num_elements=light.num_elements,
    )
    return GodoxController(
        light.ble_address,
        state=state,
        state_writer=lambda new_state: on_sequence(new_state.sequence_number),
        proxy_config_ack_timeout=1.0,
    )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class _Light:
    def __init__(self, record: LightRecord, state: LightState) -> None:
        self.record = record
        self.profile: Profile = profile_for(record.model)
        self.desired = state
        #: What the light was last told. None means "unknown": send everything.
        self.sent: LightState | None = replace(state)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.record.id,
            "name": self.record.name,
            "model": self.record.model,
            "node_address": self.record.node_address,
            "profile": self.profile.to_dict(),
            "state": asdict(self.desired),
            "pending": self.sent != self.desired,
        }


class LightManager:
    def __init__(
        self,
        store: MeshStore,
        config: GodoxConfig,
        shadow_path: Path | None = None,
        controller_factory: ControllerFactory = godox_controller_factory,
    ) -> None:
        self.store = store
        self.config = config
        self.shadow_path = shadow_path
        self._factory = controller_factory
        self._lights: dict[str, _Light] = {}
        self._controller: Controller | None = None
        self._proxy_id: str | None = None
        self._last_error: str | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._save_handle: asyncio.TimerHandle | None = None
        self.on_change: Callable[[], None] = lambda: None

        saved: dict[str, Any] = {}
        if shadow_path is not None and shadow_path.exists():
            try:
                saved = json.loads(shadow_path.read_text())
            except (OSError, ValueError) as error:
                logger.warning("ignoring unreadable %s: %s", shadow_path, error)
        for record in store.lights:
            state = LightState()
            profile = profile_for(record.model)
            state.kelvin = int(_clamp(state.kelvin, profile.kelvin_min, profile.kelvin_max))
            known = {k: v for k, v in saved.get(record.id, {}).items() if hasattr(state, k)}
            self._lights[record.id] = _Light(record, replace(state, **known))

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._lights and self._task is None:
            self._task = asyncio.create_task(self._run(), name="godox-sender")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._drop_connection()
        self._save_shadow_now()

    @property
    def connected(self) -> bool:
        return self._controller is not None and self._controller.is_connected

    def snapshot(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "proxy": self._proxy_id,
            "error": self._last_error,
            "lights": [light.to_dict() for light in self._lights.values()],
        }

    # --------------------------------------------------------------- commands

    def resolve(self, targets: Any) -> list[_Light]:
        if targets in (None, "all", ["all"]):
            return list(self._lights.values())
        if isinstance(targets, str):
            targets = [targets]
        if not isinstance(targets, list) or not targets:
            raise LightError("targets must be 'all' or a list of light ids")
        found = []
        for target in targets:
            if target == "all":
                return list(self._lights.values())
            if target not in self._lights:
                raise LightError(f"unknown light {target!r}; known: {', '.join(self._lights) or 'none'}")
            found.append(self._lights[target])
        return found

    def apply(self, targets: Any, changes: dict[str, Any]) -> list[dict[str, Any]]:
        """Change what the targeted lights should be doing.

        Returns at once; the frames go out from the sender task. Rapid calls
        (a dial being turned) collapse into the latest value per light.
        """

        lights = self.resolve(targets)
        for light in lights:
            light.desired = self._merge(light, changes)
            if changes.get("resync"):
                light.sent = None
        self._wake.set()
        self._schedule_shadow_save()
        self.on_change()
        return [light.to_dict() for light in lights]

    def _merge(self, light: _Light, changes: dict[str, Any]) -> LightState:
        state = replace(light.desired)
        profile = light.profile

        def number(key: str) -> float | None:
            value = changes.get(key)
            if value is None:
                return None
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise LightError(f"{key} must be a number")
            return float(value)

        power = changes.get("power")
        if power is not None:
            if power not in ("on", "off", "toggle"):
                raise LightError("power must be on, off or toggle")
            state.on = (not state.on) if power == "toggle" else power == "on"

        if (value := number("brightness")) is not None:
            state.brightness = value
        if (value := number("brightness_delta")) is not None:
            state.brightness += value
        state.brightness = round(_clamp(state.brightness, 0.0, 100.0), 1)

        touched_cct = False
        if (value := number("kelvin")) is not None:
            state.kelvin = int(value)
            touched_cct = True
        if (value := number("kelvin_delta")) is not None:
            state.kelvin += int(value)
            touched_cct = True
        state.kelvin = int(_clamp(round(state.kelvin / 100) * 100, profile.kelvin_min, profile.kelvin_max))
        if (value := number("gm")) is not None:
            state.gm = int(_clamp(value, -100, 100)) if profile.tint else 0
            touched_cct = True

        touched_hsi = False
        if not profile.hsi:
            # A colour sent to several lights reaches a bi-colour one too;
            # it has nothing to store it in.
            changes = {k: v for k, v in changes.items() if not k.startswith(("hue", "saturation"))}
        if (value := number("hue")) is not None:
            state.hue = int(value) % 360
            touched_hsi = True
        if (value := number("hue_delta")) is not None:
            state.hue = int(state.hue + value) % 360
            touched_hsi = True
        if (value := number("saturation")) is not None:
            state.saturation = int(_clamp(value, 0, 100))
            touched_hsi = True
        if (value := number("saturation_delta")) is not None:
            state.saturation = int(_clamp(state.saturation + value, 0, 100))
            touched_hsi = True

        touched_effect = False
        if (value := number("effect")) is not None:
            if int(value) in profile.effects:
                state.effect = int(value)
                touched_effect = True
            elif changes.get("mode") == "effect":
                # This light lacks the effect (the request may be for several
                # lights): it keeps doing what it was doing.
                changes = {**changes, "mode": None}
        if "speed" in changes:
            speed = number("speed")
            state.speed = None if speed is None else int(_clamp(speed, 0, 100))

        mode = changes.get("mode")
        if mode is None:
            # A parameter of one mode, with no mode named, selects that mode.
            touched = [m for m, hit in (("cct", touched_cct), ("hsi", touched_hsi), ("effect", touched_effect)) if hit]
            if len(touched) == 1:
                mode = touched[0]
        if mode is not None:
            if mode not in ("cct", "hsi", "effect"):
                raise LightError("mode must be cct, hsi or effect")
            if mode in profile.modes:
                state.mode = mode
            else:
                # e.g. a colour sent to "all" reaches the bi-colour light too:
                # it keeps doing what it was doing.
                logger.debug("%s has no %s mode; keeping %s", light.record.id, mode, state.mode)
        if state.mode == "effect" and profile.effects and state.effect not in profile.effects:
            state.effect = profile.effects[0]
        return state

    # ----------------------------------------------------------------- sender

    async def _run(self) -> None:
        while True:
            try:
                if not self.connected:
                    await self._connect()
                await self._flush()
                self._wake.clear()
                if any(light.sent != light.desired for light in self._lights.values()):
                    continue
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass  # look at the connection again
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - keep the bridge alive
                self._set_error(f"{type(error).__name__}: {error}")
                await self._drop_connection()
                await asyncio.sleep(self.config.reconnect_interval)

    def _set_error(self, message: str | None) -> None:
        if message != self._last_error:
            if message:
                logger.warning("lights: %s", message)
            self._last_error = message
            self.on_change()

    async def _connect(self) -> None:
        errors = []
        # Any light can be the way in: it relays to the others. Start with the
        # one that worked last time.
        order = sorted(self._lights.values(), key=lambda light: light.record.id != self._proxy_id)
        for light in order:
            controller = self._factory(light.record, self.store, self.store.reserve_sequence)
            try:
                await controller.connect()
            except Exception as error:  # noqa: BLE001 - try the next light
                errors.append(f"{light.record.id}: {error}")
                continue
            self._controller = controller
            self._proxy_id = light.record.id
            logger.info("lights: connected through %s (%s)", light.record.id, light.record.ble_address)
            self._set_error(None)
            self.on_change()
            return
        raise ConnectionError("no light reachable over Bluetooth (" + "; ".join(errors) + ")")

    async def _drop_connection(self) -> None:
        controller, self._controller = self._controller, None
        if controller is not None:
            try:
                await controller.disconnect()
            except Exception as error:  # noqa: BLE001
                logger.debug("disconnect failed: %s", error)
            self.on_change()

    async def _flush(self) -> None:
        for light in self._lights.values():
            want = replace(light.desired)
            if light.sent == want:
                continue
            await self._send(light, light.sent, want)
            light.sent = want
            self.on_change()
            await asyncio.sleep(self.config.min_interval)

    async def _send(self, light: _Light, sent: LightState | None, want: LightState) -> None:
        controller = self._controller
        if controller is None:
            raise ConnectionError("not connected")
        dst = light.record.node_address
        profile = light.profile

        if sent is None or sent.on != want.on:
            for attempt in range(max(1, self.config.power_repeat)):
                if attempt:
                    await asyncio.sleep(self.config.min_interval)
                await (controller.power_on(dst=dst) if want.on else controller.power_off(dst=dst))
            if not want.on:
                return
            await asyncio.sleep(self.config.min_interval)
        elif not want.on:
            # Off: remember the change, send it when the light comes on.
            return

        if want.mode == "hsi":
            await controller.set_hsi(hue=want.hue, saturation=want.saturation, brightness=want.brightness, dst=dst)
        elif want.mode == "effect":
            await controller.set_effect(
                want.effect,
                brightness=want.brightness,
                speed=want.speed,
                effect_version=profile.effect_version,
                dst=dst,
            )
        else:
            await controller.set_params(
                brightness=want.brightness,
                cct=want.kelvin,
                dst=dst,
                min_kelvin=profile.kelvin_min,
                max_kelvin=profile.kelvin_max,
                gm=want.gm,
                supports_gm=profile.tint,
                circle=KEEP_DIMMING_CURVE,
            )

    # ----------------------------------------------------------- shadow state

    def _schedule_shadow_save(self) -> None:
        if self.shadow_path is None:
            return
        if self._save_handle is not None:
            self._save_handle.cancel()
        self._save_handle = asyncio.get_running_loop().call_later(5.0, self._save_shadow_now)

    def _save_shadow_now(self) -> None:
        if self._save_handle is not None:
            self._save_handle.cancel()
            self._save_handle = None
        if self.shadow_path is None:
            return
        try:
            write_json_atomic(
                self.shadow_path,
                {light_id: asdict(light.desired) for light_id, light in self._lights.items()},
                mode=0o644,
            )
        except OSError as error:
            logger.warning("could not save %s: %s", self.shadow_path, error)
