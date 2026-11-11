"""Audient EVO 8: SmartGain, phantom power, input mute and preamp gain.

Where each control's layout comes from, and how sure it is:

==============  =======================  ====================================
Control         Request                  Evidence
==============  =======================  ====================================
Phantom 48V     EU58, CS 0, CN 0-3, 4 B  sniffed from the EVO app on an EVO 8
                                         (evoctl, doc/usb_control_messages)
Input mute      EU58, CS 2, CN 0-3, 4 B  confirmed on EVO 4; EVO 8 by analogy
Preamp gain     FU11, CS 2, CN 1-4, 2 B  standard UAC2 feature unit; confirmed
                                         on EVO 4, EVO 8 by analogy
SmartGain       unknown                  see smartgain.py
==============  =======================  ====================================

A control the device refuses (USB STALL) is reported as unsupported and left
alone, so a wrong guess for the EVO 8 cannot do more than fail.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from typing import Any, Callable

from .config import EvoConfig
from .evo_transport import CUR, GET, SET, EvoStall, EvoUnavailable, Transport, open_transport
from .smartgain import SmartGainConfig, parse_smartgain

logger = logging.getLogger(__name__)

INPUTS = 4
EU58 = 0x3A00  # wIndex: entity 58, interface 0
FU11 = 0x0B00  # wIndex: feature unit 11 (preamps), interface 0
CS_PHANTOM = 0x0000
CS_GAIN_FU = 0x0200  # UAC2 FU volume control
CS_MUTE = 0x0200
GAIN_DB_MIN = 0.0
GAIN_DB_MAX = 58.0


class EvoError(Exception):
    """A request that cannot be carried out; the message is for the user."""


class Evo:
    def __init__(self, config: EvoConfig, transport: Transport | None = None) -> None:
        self.config = config
        self.smartgain_config: SmartGainConfig = parse_smartgain(config.smartgain)
        self._transport = transport
        self._io_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self.on_change: Callable[[], None] = lambda: None

        self.available = False
        self.error: str | None = None
        self.phantom: list[bool | None] = [None] * INPUTS
        self.mute: list[bool | None] = [None] * INPUTS
        self.gain_db: list[float | None] = [None] * INPUTS
        self._unsupported: set[str] = set()
        self._smartgain_channels: list[int] = []
        self._smartgain_until = 0.0
        self._smartgain_status: bool | None = None

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._poll_loop(), name="evo-poll")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._transport is not None:
            self._transport.close()

    # -------------------------------------------------------------- transfers

    async def transfer(self, request_type: int, request: int, value: int, index: int, data: bytes | int) -> bytes:
        async with self._io_lock:
            if self._transport is None:
                self._transport = open_transport(self.config.transport, self.config.device)
            try:
                reply = await asyncio.to_thread(self._transport.transfer, request_type, request, value, index, data)
            except EvoUnavailable as error:
                self._set_available(False, str(error))
                raise EvoError(str(error)) from error
            except EvoStall:
                self._set_available(True, None)
                raise
            self._set_available(True, None)
            return reply

    def _set_available(self, available: bool, error: str | None) -> None:
        if (available, error) != (self.available, self.error):
            if error:
                logger.warning("evo: %s", error)
            elif available:
                logger.info("evo: connected (%s)", self._transport.name if self._transport else "?")
            self.available, self.error = available, error
            self.on_change()

    async def _get(self, index: int, value: int, length: int) -> bytes:
        return await self.transfer(GET, CUR, value, index, length)

    async def _set(self, index: int, value: int, data: bytes) -> None:
        await self.transfer(SET, CUR, value, index, data)

    # --------------------------------------------------------------- controls

    @staticmethod
    def _channel(channel: Any) -> int:
        if isinstance(channel, bool) or not isinstance(channel, int) or not 1 <= channel <= INPUTS:
            raise EvoError(f"channel must be 1-{INPUTS}")
        return channel

    @staticmethod
    def _resolve_switch(state: Any, current: bool | None) -> bool:
        if state == "toggle":
            if current is None:
                raise EvoError("cannot toggle: the current state has not been read from the EVO")
            return not current
        if state in ("on", "off"):
            return state == "on"
        raise EvoError("state must be on, off or toggle")

    async def _switch(self, name: str, values: list[bool | None], cs: int, channel: Any, state: Any) -> bool:
        channel = self._channel(channel)
        current = values[channel - 1]
        if current is None:
            current = await self._read_switch(name, values, cs, channel)
        wanted = self._resolve_switch(state, current)
        try:
            await self._set(EU58, cs | (channel - 1), struct.pack("<I", int(wanted)))
        except EvoStall as error:
            raise EvoError(f"the EVO refused the {name} request; this control's layout is not confirmed") from error
        values[channel - 1] = wanted
        self.on_change()
        return wanted

    async def _read_switch(self, name: str, values: list[bool | None], cs: int, channel: int) -> bool | None:
        try:
            reply = await self._get(EU58, cs | (channel - 1), 4)
        except EvoStall:
            self._unsupported.add(name)
            return None
        values[channel - 1] = bool(reply[:1] != b"\x00") if reply else None
        return values[channel - 1]

    async def set_phantom(self, channel: Any, state: Any) -> bool:
        return await self._switch("phantom", self.phantom, CS_PHANTOM, channel, state)

    async def set_mute(self, channel: Any, state: Any) -> bool:
        return await self._switch("mute", self.mute, CS_MUTE, channel, state)

    async def _read_gain(self, channel: int) -> float | None:
        try:
            reply = await self._get(FU11, CS_GAIN_FU | channel, 2)
        except EvoStall:
            self._unsupported.add("gain")
            return None
        if len(reply) < 2:
            return None
        self.gain_db[channel - 1] = struct.unpack("<h", reply[:2])[0] / 256.0
        return self.gain_db[channel - 1]

    async def set_gain(self, channel: Any, *, db: Any = None, delta: Any = None) -> float:
        channel = self._channel(channel)
        if db is None:
            if delta is None:
                raise EvoError("give db or delta")
            current = self.gain_db[channel - 1]
            if current is None:
                current = await self._read_gain(channel)
            if current is None:
                raise EvoError("cannot read the current gain from the EVO")
            db = current + float(delta)
        wanted = float(round(max(GAIN_DB_MIN, min(GAIN_DB_MAX, float(db)))))
        try:
            await self._set(FU11, CS_GAIN_FU | channel, struct.pack("<h", int(wanted * 256)))
        except EvoStall as error:
            raise EvoError("the EVO refused the gain request; this control's layout is not confirmed") from error
        self.gain_db[channel - 1] = wanted
        self.on_change()
        return wanted

    # -------------------------------------------------------------- smartgain

    def _channels(self, channels: Any) -> list[int]:
        if channels in (None, [], "all"):
            return [1, 2, 3, 4]
        if not isinstance(channels, list):
            raise EvoError("channels must be a list of input numbers, e.g. [1, 2]")
        return sorted({self._channel(c) for c in channels})

    async def smartgain(self, action: str, channels: Any = None) -> dict[str, Any]:
        config = self.smartgain_config
        if action not in ("start", "cancel"):
            raise EvoError("action must be start or cancel")
        steps = config.start if action == "start" else config.cancel
        if not steps:
            if action == "start":
                raise EvoError(
                    "SmartGain is not configured yet: its USB request has to be captured from the "
                    "EVO app once and put in [evo.smartgain] of the config (see docs/SMARTGAIN.md)"
                )
            raise EvoError("no [[evo.smartgain.cancel]] transfers are configured")
        selected = self._channels(channels)
        for step in steps:
            for request_type, request, value, index, data in step.render(selected):
                try:
                    await self.transfer(request_type, request, value, index, data)
                except EvoStall as error:
                    raise EvoError(
                        f"the EVO refused the SmartGain transfer wValue=0x{value:04x} wIndex=0x{index:04x}"
                    ) from error
                if step.delay_ms:
                    await asyncio.sleep(step.delay_ms / 1000)
        if action == "start":
            self._smartgain_channels = selected
            self._smartgain_until = time.monotonic() + config.assume_running_s
        else:
            self._smartgain_until = 0.0
        self._smartgain_status = None
        self.on_change()
        return self.smartgain_snapshot()

    def smartgain_running(self) -> bool:
        if self._smartgain_status is not None:
            return self._smartgain_status
        return time.monotonic() < self._smartgain_until

    def smartgain_snapshot(self) -> dict[str, Any]:
        running = self.smartgain_running()
        return {
            "configured": self.smartgain_config.configured,
            "can_cancel": bool(self.smartgain_config.cancel),
            "running": running,
            "channels": self._smartgain_channels if running else [],
        }

    # ---------------------------------------------------------------- polling

    async def refresh(self) -> None:
        for channel in range(1, INPUTS + 1):
            if "phantom" not in self._unsupported:
                await self._read_switch("phantom", self.phantom, CS_PHANTOM, channel)
            if "mute" not in self._unsupported:
                await self._read_switch("mute", self.mute, CS_MUTE, channel)
            if "gain" not in self._unsupported:
                await self._read_gain(channel)
        status = self.smartgain_config.status
        if status is not None:
            try:
                reply = await self._get(status.index, status.value, status.length)
                self._smartgain_status = status.running(reply)
            except EvoStall:
                self._smartgain_status = None

    async def _poll_loop(self) -> None:
        was_running = False
        while True:
            before = self.snapshot()
            try:
                await self.refresh()
            except EvoError:
                pass  # already recorded by transfer()
            except Exception as error:  # noqa: BLE001 - keep the bridge alive
                self._set_available(False, f"{type(error).__name__}: {error}")
            running = self.smartgain_running()
            if before != self.snapshot() or running != was_running:
                self.on_change()
            was_running = running
            interval = self.config.poll_interval or 30.0
            if not self.available:
                interval = max(interval, 5.0)
            elif running:
                interval = min(interval, 0.5)
            await asyncio.sleep(interval)

    def snapshot(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "transport": self._transport.name if self._transport else self.config.transport,
            "error": self.error,
            "inputs": [
                {
                    "channel": channel + 1,
                    "phantom": self.phantom[channel],
                    "mute": self.mute[channel],
                    "gain_db": self.gain_db[channel],
                }
                for channel in range(INPUTS)
            ],
            "unsupported": sorted(self._unsupported),
            "smartgain": self.smartgain_snapshot(),
        }
