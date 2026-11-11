"""USB control transfers to the Audient EVO 8.

Everything the EVO does over USB besides audio is a class request on endpoint
0, addressed to an entity of the Audio Control interface (interface 0). Linux
will not let a user-space program send those while its own sound driver holds
that interface, so there are two ways in:

``kmod``
    The small ``evo_ctl`` kernel module (kmod/evo_ctl in this repository)
    binds the EVO's otherwise unused DFU interface and forwards control
    transfers through ``/dev/evo8``. Audio keeps working. The older
    ``evo_raw`` module from audient-evo-py uses the same device node and
    ioctl, so either will do.

``libusb``
    No kernel module: the sound driver is detached while the bridge runs, so
    the Pi itself cannot use the EVO for audio. The EVO's own monitoring and
    its settings carry on.
"""

from __future__ import annotations

import errno
import fcntl
import os
import struct
import threading
import time
from typing import Protocol

VENDOR_ID = 0x2708
PRODUCT_ID_EVO8 = 0x0007

#: bmRequestType: class request, recipient interface.
SET = 0x21
GET = 0xA1
#: bRequest (USB Audio Class 2.0)
CUR = 0x01
RANGE = 0x02

_XFER = struct.Struct("<BBHHH256s")
# _IOWR('E', 0, struct evo_ctrl_xfer): dir=3 (read|write), size=264, type='E'
_IOCTL_CTRL_TRANSFER = (3 << 30) | (_XFER.size << 16) | (0x45 << 8) | 0


class EvoStall(OSError):
    """The device refused the request (USB STALL): no such control."""


class EvoUnavailable(OSError):
    """The device is not there, or cannot be opened."""


class Transport(Protocol):
    name: str

    def transfer(self, request_type: int, request: int, value: int, index: int, data: bytes | int) -> bytes:
        """One control transfer. ``data`` is bytes to send, or a length to read."""
        ...

    def close(self) -> None: ...


def _direction_in(request_type: int) -> bool:
    return bool(request_type & 0x80)


class KmodTransport:
    name = "kmod"

    def __init__(self, device: str = "/dev/evo8") -> None:
        self.device = device
        self._fd: int | None = None
        self._lock = threading.Lock()

    def _open(self) -> int:
        if self._fd is None:
            try:
                self._fd = os.open(self.device, os.O_RDWR)
            except OSError as error:
                raise EvoUnavailable(error.errno, f"cannot open {self.device}: {error.strerror}") from error
        return self._fd

    def transfer(self, request_type: int, request: int, value: int, index: int, data: bytes | int) -> bytes:
        read = _direction_in(request_type)
        payload = b"" if read else bytes(data)  # type: ignore[arg-type]
        length = int(data) if read else len(payload)  # type: ignore[arg-type]
        if length > 256:
            raise ValueError("at most 256 bytes per transfer")
        buffer = bytearray(_XFER.pack(request_type, request, value, index, length, payload))
        with self._lock:
            fd = self._open()
            try:
                got = fcntl.ioctl(fd, _IOCTL_CTRL_TRANSFER, buffer)
            except OSError as error:
                if error.errno == errno.EPIPE:
                    raise EvoStall(error.errno, "request refused by the device (STALL)") from error
                if error.errno in (errno.ENODEV, errno.ESHUTDOWN, errno.EPROTO):
                    self.close_locked()
                    raise EvoUnavailable(error.errno, f"EVO not reachable: {error.strerror}") from error
                raise
        if not read:
            return b""
        return bytes(_XFER.unpack(buffer)[5][:got])

    def close_locked(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None

    def close(self) -> None:
        with self._lock:
            self.close_locked()


class LibusbTransport:
    name = "libusb"

    def __init__(self, product_id: int = PRODUCT_ID_EVO8) -> None:
        self.product_id = product_id
        self._dev = None
        self._lock = threading.Lock()

    def _open(self):  # noqa: ANN202 - pyusb is optional
        if self._dev is not None:
            return self._dev
        try:
            import usb.core
        except ImportError as error:
            raise EvoUnavailable(errno.ENOENT, "pyusb is not installed (pip install pyusb)") from error
        dev = usb.core.find(idVendor=VENDOR_ID, idProduct=self.product_id)
        if dev is None:
            raise EvoUnavailable(errno.ENODEV, "EVO 8 not found on USB")
        try:
            # Interface 0 is Audio Control; the sound driver holds it.
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError) as error:
            raise EvoUnavailable(errno.EACCES, f"cannot take the EVO from the sound driver: {error}") from error
        self._dev = dev
        return dev

    def transfer(self, request_type: int, request: int, value: int, index: int, data: bytes | int) -> bytes:
        import usb.core

        with self._lock:
            dev = self._open()
            try:
                result = dev.ctrl_transfer(request_type, request, value, index, data, timeout=1000)
            except usb.core.USBError as error:
                if error.errno == errno.EPIPE:
                    raise EvoStall(error.errno, "request refused by the device (STALL)") from error
                if error.errno in (errno.ENODEV, errno.EIO):
                    self._dev = None
                    raise EvoUnavailable(error.errno or errno.EIO, f"EVO not reachable: {error}") from error
                raise
        return bytes(result) if _direction_in(request_type) else b""

    def close(self) -> None:
        with self._lock:
            if self._dev is not None:
                try:
                    import usb.util

                    usb.util.dispose_resources(self._dev)
                    self._dev.attach_kernel_driver(0)
                except Exception:  # noqa: BLE001 - best effort on the way out
                    pass
                self._dev = None


class MockTransport:
    """An EVO 8 that exists only in memory, for tests and ``--mock``.

    Holds the controls whose layout is known. It also records every transfer,
    so a test can check what a SmartGain sequence put on the wire.
    """

    name = "mock"

    def __init__(self) -> None:
        self.log: list[tuple[int, int, int, int, bytes | int]] = []
        self.registers: dict[tuple[int, int], bytes] = {}
        self.listen_seconds = 0.4
        self.done_seconds = 0.3
        #: "ok": a run ends through state 4. "fail": straight back to idle.
        self.outcome = "ok"
        self._listening_until = 0.0
        for channel in range(4):
            self.registers[(0x3A00, 0x0000 | channel)] = bytes(4)  # phantom
            self.registers[(0x3A00, 0x0200 | channel)] = bytes(4)  # input mute
            self.registers[(0x3A00, 0x0400 | channel)] = bytes(4)  # SmartGain: selected
            self.registers[(0x0B00, 0x0200 | (channel + 1))] = (20 * 256).to_bytes(2, "little", signed=True)
        self.registers[(0x3A00, 0x0500)] = bytes(4)  # SmartGain: state

    def transfer(self, request_type: int, request: int, value: int, index: int, data: bytes | int) -> bytes:
        self.log.append((request_type, request, value, index, data))
        self._finish_smartgain()
        key = (index, value)
        if _direction_in(request_type):
            if request == RANGE and index == 0x0B00:
                return struct.pack("<Hhhh", 1, 0, 58 * 256, 256)
            if key not in self.registers:
                raise EvoStall(errno.EPIPE, "request refused by the device (STALL)")
            return self.registers[key][: int(data)]  # type: ignore[arg-type]
        self.registers[key] = bytes(data)  # type: ignore[arg-type]
        if key == (0x3A00, 0x0500):
            started = bytes(data)[:1] == b"\x02"
            self._listening_until = time.monotonic() + self.listen_seconds if started else 0.0
        return b""

    def _finish_smartgain(self) -> None:
        # Like the unit: listening ends by itself; a success shows state 4
        # for a moment, then idle, and the inputs deselect.
        if not self._listening_until or time.monotonic() < self._listening_until:
            return
        if self.outcome == "ok" and time.monotonic() < self._listening_until + self.done_seconds:
            self.registers[(0x3A00, 0x0500)] = bytes.fromhex("04000000")
            return
        self._listening_until = 0.0
        self.registers[(0x3A00, 0x0500)] = bytes(4)
        for channel in range(4):
            self.registers[(0x3A00, 0x0400 | channel)] = bytes(4)

    def close(self) -> None:
        pass


def open_transport(kind: str, device: str = "/dev/evo8") -> Transport:
    if kind == "mock":
        return MockTransport()
    if kind == "kmod":
        return KmodTransport(device)
    if kind == "libusb":
        return LibusbTransport()
    if kind == "auto":
        # Never fall back to libusb unasked: it takes the EVO away from the
        # sound driver, which would cut the Pi's audio without warning.
        return KmodTransport(device)
    raise ValueError(f"unknown evo transport {kind!r} (auto, kmod, libusb, mock)")
