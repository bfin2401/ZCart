"""Audient EVO 8: SmartGain, phantom power, input mute and preamp gain.

Where each control's layout comes from, and how sure it is:

==============  =======================  ====================================
Control         Request                  Evidence
==============  =======================  ====================================
Phantom 48V     EU58, CS 0, CN 0-3, 4 B  sniffed from the EVO app on an EVO 8
Input mute      EU58, CS 2, CN 0-3, 4 B  confirmed on EVO 4; reads on an EVO 8
Preamp gain     FU11, CS 2, CN 1-4, 2 B  standard UAC2 feature unit; reads on
                                         an EVO 8
SmartGain       EU58, CS 4 and 5         tried by hand on an EVO 8
                                         (see smartgain.py)
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
from .evo_transport import CUR, GET, RANGE, SET, EvoStall, EvoUnavailable, Transport, open_transport
from .smartgain import SmartGainConfig, parse_smartgain

logger = logging.getLogger(__name__)

INPUTS = 4
EU58 = 0x3A00  # wIndex: entity 58, interface 0
FU11 = 0x0B00  # wIndex: feature unit 11 (preamps), interface 0
CS_PHANTOM = 0x0000
CS_GAIN_FU = 0x0200  # UAC2 FU volume control
CS_MUTE = 0x0200
CS_SMARTGAIN_SELECTED = 0x0400
SMARTGAIN_POLL_S = 0.1
#: Used until the device has told its own range (a UAC2 RANGE request).
GAIN_DB_MIN = -8.0
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
        self.gain_range: tuple[float, float] | None = None
        self._unsupported: set[str] = set()
        self._smartgain_channels: list[int] = []
        self._smartgain_until = 0.0
        self._smartgain_status: bool | None = None
        #: Outcome of the last run per input: "ok" or "failed".
        self._smartgain_results: dict[int, str] = {}
        self._run_active = False
        self._run_succeeded = False
        self._run_cancelled = False
        #: The distinct values the state register showed during the last run.
        self._run_states: list[str] = []

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

    async def _read_gain_range(self) -> tuple[float, float]:
        if self.gain_range is None:
            low, high = GAIN_DB_MIN, GAIN_DB_MAX
            try:
                # wNumSubRanges, then min / max / resolution, each dB x 256
                reply = await self.transfer(GET, RANGE, CS_GAIN_FU | 1, FU11, 8)
                if len(reply) >= 6:
                    _, raw_low, raw_high = struct.unpack("<Hhh", reply[:6])
                    if raw_low < raw_high:
                        low, high = raw_low / 256.0, raw_high / 256.0
            except EvoStall:
                pass
            self.gain_range = (low, high)
        return self.gain_range

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
        low, high = await self._read_gain_range()
        wanted = float(round(max(low, min(high, float(db)))))
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
        if action == "clear":
            for channel in self._channels(channels):
                self._smartgain_results.pop(channel, None)
            self.on_change()
            return self.smartgain_snapshot()
        if action not in ("start", "cancel"):
            raise EvoError("action must be start, cancel or clear")
        steps = config.start if action == "start" else config.cancel
        if not steps:
            if action == "start":
                raise EvoError("SmartGain is switched off in the bridge config ([evo.smartgain] start = [])")
            raise EvoError("no [[evo.smartgain.cancel]] transfers are configured")
        selected = self._channels(channels)
        if action == "start":
            # Before the first transfer: the unit reports "running" as soon
            # as it enters SmartGain, and a poll may land mid-sequence.
            self._smartgain_channels = selected
            for channel in selected:
                self._smartgain_results.pop(channel, None)
            self._run_succeeded = self._run_cancelled = False
            self._run_states = []
        else:
            # Ending a run on purpose is neither a success nor a failure.
            self._run_cancelled = True
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
            # The unit was just told to listen. Should the run be over before
            # the next read, that read must still count as its end.
            self._run_active = True
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
            "results": {str(channel): result for channel, result in sorted(self._smartgain_results.items())},
            "last_states": list(self._run_states),
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
        await self._poll_smartgain(read_inputs=True)

    async def _poll_smartgain(self, *, read_inputs: bool = False) -> None:
        """Read the SmartGain state register and follow a run to its outcome."""

        status = self.smartgain_config.status
        if status is None:
            return
        try:
            reply = await self._get(status.index, status.value, status.length)
        except EvoStall:
            self._smartgain_status = None
            return
        running = status.running(reply)
        self._smartgain_status = running

        if running:
            if not self._run_active:
                # A run the bridge did not start: the unit's own button.
                self._run_active = True
                self._run_succeeded = self._run_cancelled = False
                self._run_states = []
                read_inputs = True
            if reply.hex() not in self._run_states:
                self._run_states.append(reply.hex())
            if read_inputs and self.smartgain_config.builtin:
                chosen = []
                for channel in range(1, INPUTS + 1):
                    try:
                        flag = await self._get(EU58, CS_SMARTGAIN_SELECTED | (channel - 1), 4)
                    except EvoStall:
                        break
                    if flag[:1] not in (b"", b"\x00"):
                        chosen.append(channel)
                if chosen:
                    self._smartgain_channels = chosen
            if status.succeeded(reply) and not self._run_succeeded:
                self._run_succeeded = True
                for channel in self._smartgain_channels:
                    self._smartgain_results[channel] = "ok"
        elif self._run_active:
            self._run_active = False
            if self._run_cancelled:
                for channel in self._smartgain_channels:
                    self._smartgain_results.pop(channel, None)
            elif status.done and not self._run_succeeded:
                # Back to idle without ever reading "done".
                for channel in self._smartgain_channels:
                    self._smartgain_results[channel] = "failed"

    async def _poll_loop(self) -> None:
        was_running = False
        next_full = 0.0
        while True:
            before = self.snapshot()
            now = time.monotonic()
            try:
                if now >= next_full:
                    await self.refresh()
                    interval = self.config.poll_interval or 30.0
                    if not self.available:
                        interval = max(interval, 5.0)
                    next_full = now + interval
                else:
                    await self._poll_smartgain()
            except EvoError:
                pass  # already recorded by transfer()
            except Exception as error:  # noqa: BLE001 - keep the bridge alive
                self._set_available(False, f"{type(error).__name__}: {error}")
            running = self.smartgain_running()
            if before != self.snapshot() or running != was_running:
                self.on_change()
            was_running = running
            if running and self.available:
                # The "done" value shows only briefly before the unit goes
                # idle; watch the state register closely while a run lasts.
                await asyncio.sleep(SMARTGAIN_POLL_S)
            else:
                await asyncio.sleep(max(0.05, next_full - time.monotonic()))

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
            "gain_range_db": list(self.gain_range) if self.gain_range else None,
            "unsupported": sorted(self._unsupported),
            "smartgain": self.smartgain_snapshot(),
        }
