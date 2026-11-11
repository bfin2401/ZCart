"""Read-only exploration of the EVO's control registers.

Used to find what the EVO app does not document: sweep every GET_CUR the
device answers, then watch which registers move while something is done on
the unit itself (pressing its SmartGain button, say). Nothing here writes.
"""

from __future__ import annotations

import json
import time
from typing import Callable, Iterator

from .evo_transport import CUR, GET, EvoStall, Transport

#: (wIndex, control selectors, channel numbers, read length)
SWEEP: tuple[tuple[int, range, range, int], ...] = (
    (0x0A00, range(1, 3), range(0, 5), 2),   # FU10 outputs: mute, volume
    (0x0B00, range(1, 3), range(0, 5), 2),   # FU11 preamps: mute, gain
    (0x3200, range(0, 16), range(0, 8), 4),  # EU50
    (0x3300, range(0, 16), range(0, 8), 4),  # EU51
    (0x3900, range(0, 16), range(0, 8), 4),  # EU57
    (0x3A00, range(0, 16), range(0, 8), 4),  # EU58 inputs
    (0x3B00, range(0, 16), range(0, 8), 4),  # EU59 outputs
)

#: A request every EVO 8 answers (phantom power, input 1). If this fails after
#: a refused probe, the unit has locked up its control pipe and needs a replug.
CANARY = (0x3A00, 0x0000, 4)

KNOWN: dict[tuple[int, int], str] = {
    (0x3A00, 0x00): "phantom 48V",
    (0x3A00, 0x01): "preamp gain (mirror)",
    (0x3A00, 0x02): "input mute",
    (0x3A00, 0x04): "SmartGain: input selected",
    (0x3A00, 0x05): "SmartGain: state",
    (0x3B00, 0x00): "output volume (mirror)",
    (0x3B00, 0x01): "output mute",
    (0x0A00, 0x02): "output volume",
    (0x0B00, 0x02): "preamp gain",
}


class ProbeAborted(RuntimeError):
    pass


def key(index: int, value: int) -> str:
    return f"{index:04x}:{value:04x}"


def describe(index: int, value: int) -> str:
    entity, cs, cn = index >> 8, value >> 8, value & 0xFF
    kind = "FU" if entity < 32 else "EU"
    label = KNOWN.get((index, cs), "unknown")
    return f"{kind}{entity} CS={cs} CN={cn} ({label})"


def _read(transport: Transport, index: int, value: int, length: int) -> bytes | None:
    try:
        return transport.transfer(GET, CUR, value, index, length)
    except EvoStall:
        return None


def sweep(transport: Transport, *, pause: float = 0.005, progress: Callable[[str], None] | None = None) -> dict[str, str]:
    """Every register that answers a GET_CUR, as ``{"3a00:0000": "00000000"}``."""

    found: dict[str, str] = {}
    for index, selectors, channels, length in SWEEP:
        for cs in selectors:
            misses = 0
            for cn in channels:
                value = (cs << 8) | cn
                reply = _read(transport, index, value, length)
                time.sleep(pause)
                if reply is None:
                    misses += 1
                    if _read(transport, *CANARY) is None:
                        raise ProbeAborted(
                            f"the EVO stopped answering after probing {describe(index, value)}. "
                            "Unplug and replug its USB cable; nothing was written to it."
                        )
                    # Two refusals in a row at the start: no such control.
                    if misses == 2 and cn == channels.start + 1:
                        break
                    continue
                found[key(index, value)] = reply.hex()
            if progress:
                progress(f"wIndex=0x{index:04x} CS={cs}")
    return found


def diff(before: dict[str, str], after: dict[str, str]) -> list[str]:
    lines = []
    for register in sorted(set(before) | set(after)):
        old, new = before.get(register), after.get(register)
        if old != new:
            index, value = (int(part, 16) for part in register.split(":"))
            lines.append(
                f"wIndex=0x{index:04x} wValue=0x{value:04x}  {old or '--'} -> {new or '--'}   {describe(index, value)}"
            )
    return lines


def watch(transport: Transport, baseline: dict[str, str], *, interval: float = 0.25) -> Iterator[str]:
    """Poll the registers of ``baseline`` forever, yielding each change."""

    current = dict(baseline)
    started = time.monotonic()
    while True:
        for register in list(current):
            index, value = (int(part, 16) for part in register.split(":"))
            reply = _read(transport, index, value, len(current[register]) // 2 or 4)
            new = reply.hex() if reply is not None else "--"
            if new != current[register]:
                yield (
                    f"[{time.monotonic() - started:7.2f}s] wIndex=0x{index:04x} wValue=0x{value:04x}  "
                    f"{current[register]} -> {new}   {describe(index, value)}"
                )
                current[register] = new
            time.sleep(0.003)
        time.sleep(interval)


def load(path: str) -> dict[str, str]:
    with open(path) as handle:
        return json.load(handle)
