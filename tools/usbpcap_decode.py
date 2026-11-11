#!/usr/bin/env python3
"""Decode a USBPcap capture of the Audient EVO app and find the SmartGain request.

Usage:
    python3 usbpcap_decode.py capture.pcapng            # summary + every write
    python3 usbpcap_decode.py capture.pcapng --from 12 --to 20
    python3 usbpcap_decode.py capture.pcapng --toml     # config for the bridge

Reads the .pcap/.pcapng files Wireshark saves from USBPcap on Windows. Needs
only Python 3; nothing to install. It shows the class requests the EVO app
sends to the Audio Control interface, labels the ones already understood and
marks the rest NEW. See docs/SMARTGAIN.md for how to make the capture.
"""

from __future__ import annotations

import argparse
import struct
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Iterator

LINKTYPE_USBPCAP = 249

KNOWN = {
    (0x3A, 0): "phantom 48V",
    (0x3A, 1): "preamp gain",
    (0x3A, 2): "input mute",
    (0x3A, 4): "SmartGain: input selected",
    (0x3A, 5): "SmartGain: state",
    (0x3E, 6): "change notification queue",
    (0x3C, 0): "meters",
    (0x3B, 0): "output volume",
    (0x3B, 1): "output mute",
    (0x3C, 1): "monitor mixer crosspoint",
    (0x0A, 1): "output mute (feature unit)",
    (0x0A, 2): "output volume (feature unit)",
    (0x0B, 1): "preamp mute (feature unit)",
    (0x0B, 2): "preamp gain (feature unit)",
    (0x29, 1): "sample rate",
    (0x28, 1): "clock selector",
}


@dataclass
class Transfer:
    time: float
    device: int
    request_type: int
    request: int
    value: int
    index: int
    length: int
    data: bytes = b""
    status: int = 0

    @property
    def is_get(self) -> bool:
        return bool(self.request_type & 0x80)

    @property
    def label(self) -> str:
        return KNOWN.get((self.index >> 8, self.value >> 8), "NEW")

    def line(self) -> str:
        kind = {1: "CUR", 2: "RANGE"}.get(self.request, f"req{self.request}")
        direction = "GET" if self.is_get else "SET"
        failed = "" if self.status == 0 else f"  [failed 0x{self.status:08x}]"
        return (
            f"{self.time:9.3f}s  {direction} {kind:<5} wValue=0x{self.value:04x} wIndex=0x{self.index:04x} "
            f"len={self.length:<3} data={self.data.hex() or '-':<16} {self.label}{failed}"
        )


def _packets(blob: bytes) -> Iterator[tuple[float, bytes]]:
    """(seconds, packet bytes) for each USBPcap packet in a pcap or pcapng file."""

    magic = blob[:4]
    if magic in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4", b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d"):
        endian = "<" if magic[0] in (0xD4, 0x4D) else ">"
        nano = magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
        (linktype,) = struct.unpack_from(endian + "I", blob, 20)
        if linktype != LINKTYPE_USBPCAP:
            raise SystemExit(f"this capture is link type {linktype}, not USBPcap (249)")
        offset = 24
        while offset + 16 <= len(blob):
            sec, frac, caplen, _ = struct.unpack_from(endian + "IIII", blob, offset)
            offset += 16
            yield sec + frac / (1e9 if nano else 1e6), blob[offset : offset + caplen]
            offset += caplen
        return
    if magic != b"\x0a\x0d\x0d\x0a":
        raise SystemExit("not a pcap or pcapng file (save the capture from Wireshark as .pcapng)")

    endian = "<"
    interfaces: list[tuple[int, float]] = []
    offset = 0
    while offset + 12 <= len(blob):
        block_type = struct.unpack_from(endian + "I", blob, offset)[0]
        if block_type == 0x0A0D0D0A:
            endian = "<" if blob[offset + 8 : offset + 12] == b"\x4d\x3c\x2b\x1a" else ">"
            interfaces = []
        block_len = struct.unpack_from(endian + "I", blob, offset + 4)[0]
        if block_len < 12:
            break
        body = blob[offset + 8 : offset + block_len - 4]
        if block_type == 1:  # interface description
            linktype = struct.unpack_from(endian + "H", body, 0)[0]
            resolution, pos = 1e-6, 8
            while pos + 4 <= len(body):
                code, size = struct.unpack_from(endian + "HH", body, pos)
                if code == 0:
                    break
                if code == 9 and size >= 1:  # if_tsresol
                    raw = body[pos + 4]
                    resolution = 2.0 ** -(raw & 0x7F) if raw & 0x80 else 10.0 ** -raw
                pos += 4 + (size + 3) // 4 * 4
            interfaces.append((linktype, resolution))
        elif block_type == 6:  # enhanced packet
            iface, high, low, caplen, _ = struct.unpack_from(endian + "IIIII", body, 0)
            if iface < len(interfaces) and interfaces[iface][0] == LINKTYPE_USBPCAP:
                yield ((high << 32) | low) * interfaces[iface][1], body[20 : 20 + caplen]
        offset += block_len


def transfers(blob: bytes) -> list[Transfer]:
    """Control transfers, put back together from USBPcap's per-stage packets."""

    pending: "OrderedDict[int, Transfer]" = OrderedDict()
    done: list[Transfer] = []
    start: float | None = None
    for timestamp, packet in _packets(blob):
        if len(packet) < 27:
            continue
        header_len, irp, status, _function, info, _bus, device, _endpoint, kind, _length = struct.unpack_from(
            "<HQIHBHHBBI", packet, 0
        )
        if kind != 2 or header_len < 28 or len(packet) < header_len:
            continue
        start = timestamp if start is None else start
        stage = packet[27]
        payload = packet[header_len:]
        if stage == 0 and len(payload) >= 8:
            request_type, request, value, index, length = struct.unpack_from("<BBHHH", payload, 0)
            transfer = Transfer(timestamp - start, device, request_type, request, value, index, length)
            if not transfer.is_get:
                transfer.data = payload[8 : 8 + length]  # newer USBPcap: data follows the setup
            pending[irp] = transfer
            continue
        transfer = pending.get(irp)
        if transfer is None:
            continue
        from_device = bool(info & 1)
        if payload and (stage == 1 or stage == 3) and from_device == transfer.is_get:
            transfer.data = payload[: transfer.length]
        if stage in (2, 3):
            transfer.status = status
            done.append(pending.pop(irp))
    done.extend(pending.values())
    done.sort(key=lambda t: t.time)
    return done


def audio_control(all_transfers: list[Transfer], device: int | None) -> list[Transfer]:
    """Class requests to interface 0 of an entity: what the EVO app controls."""

    picked = [
        t
        for t in all_transfers
        if (t.request_type in (0x21, 0xA1) and (t.index & 0xFF) == 0 and (t.index >> 8) != 0)
        # Vendor requests are shown too, in case the app uses any (labelled NEW).
        or (t.request_type & 0x60) == 0x40
    ]
    if device is None and picked:
        # The capture holds every device on the hub; keep the one that gets
        # requests to the EVO's own extension units.
        evo = [t.device for t in picked if (t.index >> 8) in (0x3A, 0x3B, 0x3C)]
        if evo:
            device = max(set(evo), key=evo.count)
    return [t for t in picked if device is None or t.device == device]


def toml(transfer: Transfer) -> str:
    return (
        "[[evo.smartgain.start]]\n"
        f"value = 0x{transfer.value:04x}\n"
        f"index = 0x{transfer.index:04x}\n"
        f'data = "{transfer.data.hex()}"\n'
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("capture")
    parser.add_argument("--device", type=int, help="USB device address (default: guessed)")
    parser.add_argument("--from", dest="start", type=float, default=0.0, help="only from this second")
    parser.add_argument("--to", dest="end", type=float, default=float("inf"), help="only up to this second")
    parser.add_argument("--gets", action="store_true", help="list every read too, not just the summary")
    parser.add_argument("--toml", action="store_true", help="print the NEW writes as bridge config")
    args = parser.parse_args()

    with open(args.capture, "rb") as handle:
        found = audio_control(transfers(handle.read()), args.device)
    found = [t for t in found if args.start <= t.time <= args.end]
    if not found:
        print("No audio control requests in this capture. Was the EVO's USB root hub selected in USBPcap?")
        return 1
    print(f"USB device address {found[0].device}: {len(found)} audio control requests\n")

    @dataclass
    class Group:
        count: int = 0
        first: float = 0.0
        values: list[str] = field(default_factory=list)
        sample: Transfer | None = None

    groups: "OrderedDict[tuple[bool, int, int, int], Group]" = OrderedDict()
    for t in found:
        group = groups.setdefault((t.is_get, t.request, t.value, t.index), Group(first=t.time, sample=t))
        group.count += 1
        if t.data.hex() not in group.values:
            group.values.append(t.data.hex())

    print("Summary (one line per distinct request):")
    for (is_get, _request, value, index), group in groups.items():
        assert group.sample is not None
        shown = ", ".join(group.values[:6]) + (" ..." if len(group.values) > 6 else "")
        print(
            f"  {'GET' if is_get else 'SET'} wValue=0x{value:04x} wIndex=0x{index:04x} x{group.count:<5} "
            f"first {group.first:8.3f}s  {group.sample.label:<28} data: {shown or '-'}"
        )

    writes = [t for t in found if not t.is_get]
    print(f"\nWrites in order ({len(writes)}):" if not args.gets else "\nEvery request in order:")
    for t in found if args.gets else writes:
        print("  " + t.line())

    new = [t for t in writes if t.label == "NEW"]
    new_reads = sorted({(t.value, t.index) for t in found if t.is_get and t.label == "NEW"})
    print(f"\n{len(new)} write(s) to controls not yet understood.")
    if new_reads:
        print("Reads of controls not yet understood (candidates for the 'SmartGain running' status):")
        for value, index in new_reads:
            print(f"  wValue=0x{value:04x} wIndex=0x{index:04x}")
    if args.toml and new:
        print("\n# Paste into /etc/studio-bridge/config.toml, keeping only the SmartGain transfers.")
        print("# Replace a channel number with a placeholder to make it follow the button")
        print("# (see the top of bridge/studio_bridge/smartgain.py).\n")
        seen = set()
        for t in new:
            if (t.value, t.index, t.data) not in seen:
                seen.add((t.value, t.index, t.data))
                print(toml(t))
    return 0


if __name__ == "__main__":
    sys.exit(main())
