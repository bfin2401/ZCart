"""SmartGain: a sequence of control transfers.

The EVO app has no SmartGain button, so nothing in it sends a request to
copy. What it does do is read two registers of extension unit 58 while
SmartGain runs from the unit's own button: a state (selector 5) and a
per-input "selected" flag (selector 4). Writing those same registers was
tried on an EVO 8 and does what the button does:

    state = 1                 the unit enters SmartGain, waiting for inputs
    selected[input] = 1       for each input to set
    state = 2                 it starts listening
    state = 0                 cancels

The state reads 2 and 3 while listening, 4 when done, then 0.

That sequence is built in (DEFAULT_*). The ``[evo.smartgain]`` table of the
config file can replace any part of it, for another model or firmware:

    [evo.smartgain]
    assume_running_s = 20          # how long "running" is shown without a status read

    [[evo.smartgain.start]]        # one table per transfer, in order
    value = "0x0400 + ch"          # wValue: a number, or an expression
    index = 0x3A00                 # wIndex
    data = "01000000"              # hex; may hold {placeholders}
    per_channel = true             # repeat once per selected channel
    delay_ms = 20                  # pause after this transfer

    [[evo.smartgain.cancel]]
    ...

    [evo.smartgain.status]         # a register that tells "running"
    value = 0x0500
    index = 0x3A00
    length = 4
    idle = "00000000"              # anything else reads as "running"

``start = []`` switches SmartGain off.

Names usable in ``value``/``index`` expressions and in ``data`` placeholders:

    ch     channel, counting from 0        (per_channel transfers only)
    ch1    channel, counting from 1        (per_channel transfers only)
    mask   bit mask of the selected channels, bit 0 = channel 1
    count  how many channels are selected

``data`` placeholders take Python format specs and three endian helpers:
``{mask:02x}``, ``{ch:02x}``, ``{mask_le16}``, ``{mask_le32}``, ``{ch_le32}``,
``{ch1_le32}``.
"""

from __future__ import annotations

import ast
import operator
from dataclasses import dataclass, field
from typing import Any

from .evo_transport import CUR, SET

_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.BitOr: operator.or_,
    ast.BitAnd: operator.and_,
    ast.BitXor: operator.xor,
    ast.LShift: operator.lshift,
    ast.RShift: operator.rshift,
}


class SmartGainConfigError(ValueError):
    pass


def evaluate(expression: Any, names: dict[str, int]) -> int:
    """An integer, or integer arithmetic over the placeholder names."""

    if isinstance(expression, bool):
        raise SmartGainConfigError(f"not a number: {expression!r}")
    if isinstance(expression, int):
        return expression
    if not isinstance(expression, str):
        raise SmartGainConfigError(f"not a number or expression: {expression!r}")

    def walk(node: ast.AST) -> int:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in names:
                raise SmartGainConfigError(f"unknown name {node.id!r} in {expression!r}")
            return names[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            return _BINARY[type(node.op)](walk(node.left), walk(node.right))
        raise SmartGainConfigError(f"unsupported expression {expression!r}")

    try:
        return walk(ast.parse(expression.strip(), mode="eval"))
    except SyntaxError as error:
        raise SmartGainConfigError(f"cannot parse {expression!r}") from error


def _names(channels: list[int], channel: int | None) -> dict[str, int]:
    names = {"mask": sum(1 << (c - 1) for c in channels), "count": len(channels)}
    if channel is not None:
        names["ch"] = channel - 1
        names["ch1"] = channel
    return names


def _render_data(template: str, names: dict[str, int]) -> bytes:
    values: dict[str, Any] = dict(names)
    values["mask_le16"] = names["mask"].to_bytes(2, "little").hex()
    values["mask_le32"] = names["mask"].to_bytes(4, "little").hex()
    if "ch" in names:
        values["ch_le32"] = names["ch"].to_bytes(4, "little").hex()
        values["ch1_le32"] = names["ch1"].to_bytes(4, "little").hex()
    try:
        text = template.format(**values)
        return bytes.fromhex(text.replace(" ", ""))
    except KeyError as error:
        raise SmartGainConfigError(f"unknown placeholder {error} in data {template!r}") from error
    except ValueError as error:
        raise SmartGainConfigError(f"data {template!r} is not hex: {error}") from error


@dataclass(frozen=True)
class Step:
    value: Any
    index: Any
    data: str
    request_type: int = SET
    request: int = CUR
    per_channel: bool = False
    delay_ms: int = 0

    def render(self, channels: list[int]) -> list[tuple[int, int, int, int, bytes]]:
        out = []
        for channel in channels if self.per_channel else [None]:
            names = _names(channels, channel)
            out.append(
                (
                    self.request_type,
                    self.request,
                    evaluate(self.value, names) & 0xFFFF,
                    evaluate(self.index, names) & 0xFFFF,
                    _render_data(self.data, names),
                )
            )
        return out


@dataclass(frozen=True)
class StatusRead:
    value: int
    index: int
    length: int = 4
    idle: bytes = b""

    def running(self, reply: bytes) -> bool:
        return reply != self.idle if self.idle else any(reply)


@dataclass(frozen=True)
class SmartGainConfig:
    start: tuple[Step, ...] = ()
    cancel: tuple[Step, ...] = ()
    status: StatusRead | None = None
    assume_running_s: float = 20.0
    #: The sequence found on the EVO 8 is in use, so its registers can be read.
    builtin: bool = False
    channels: tuple[int, ...] = field(default=(1, 2, 3, 4))

    @property
    def configured(self) -> bool:
        return bool(self.start)


def _steps(raw: Any, name: str) -> tuple[Step, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SmartGainConfigError(f"[evo.smartgain] {name} must be a list of tables ([[evo.smartgain.{name}]])")
    steps = []
    for number, item in enumerate(raw, 1):
        try:
            step = Step(**item)
        except TypeError as error:
            raise SmartGainConfigError(f"[[evo.smartgain.{name}]] #{number}: {error}") from error
        if step.request_type & 0x80:
            raise SmartGainConfigError(f"[[evo.smartgain.{name}]] #{number}: request_type must be an OUT transfer")
        # Fail at start-up, not on the button press.
        step.render([1])
        steps.append(step)
    return tuple(steps)


#: Extension unit 58 of the EVO 8: wIndex, and the two SmartGain selectors.
_EU58 = 0x3A00
_STATE = 0x0500
_SELECTED = 0x0400

DEFAULT_START: list[dict[str, Any]] = [
    {"value": _STATE, "index": _EU58, "data": "01000000", "delay_ms": 100},
    {"value": f"{_SELECTED} + ch", "index": _EU58, "data": "01000000", "per_channel": True, "delay_ms": 50},
    {"value": _STATE, "index": _EU58, "data": "02000000"},
]
DEFAULT_CANCEL: list[dict[str, Any]] = [{"value": _STATE, "index": _EU58, "data": "00000000"}]
DEFAULT_STATUS: dict[str, Any] = {"value": _STATE, "index": _EU58, "length": 4, "idle": "00000000"}


def parse_smartgain(raw: dict[str, Any]) -> SmartGainConfig:
    raw = dict(raw)
    builtin = "start" not in raw
    if builtin:
        # Nothing of their own: the sequence found on the EVO 8.
        raw["start"] = DEFAULT_START
        raw.setdefault("cancel", DEFAULT_CANCEL)
        raw.setdefault("status", DEFAULT_STATUS)
    status = None
    if (status_raw := raw.pop("status", None)) is not None:
        try:
            status = StatusRead(
                value=int(status_raw["value"]),
                index=int(status_raw["index"]),
                length=int(status_raw.get("length", 4)),
                idle=bytes.fromhex(status_raw.get("idle", "")),
            )
        except (KeyError, ValueError, TypeError) as error:
            raise SmartGainConfigError(f"[evo.smartgain.status]: {error!r}") from error
    config = SmartGainConfig(
        start=_steps(raw.pop("start", None), "start"),
        cancel=_steps(raw.pop("cancel", None), "cancel"),
        status=status,
        assume_running_s=float(raw.pop("assume_running_s", 20.0)),
        builtin=builtin,
    )
    if raw:
        raise SmartGainConfigError(f"unknown [evo.smartgain] option(s): {', '.join(sorted(raw))}")
    return config
