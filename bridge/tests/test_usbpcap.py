import importlib.util
import struct
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "usbpcap_decode", Path(__file__).resolve().parents[2] / "tools" / "usbpcap_decode.py"
)
decode = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = decode
spec.loader.exec_module(decode)


def usbpcap(irp, stage, info, payload, device=5, status=0, kind=2):
    header = struct.pack("<HQIHBHHBBI", 28, irp, status, 8, info, 1, device, 0x80 if info else 0, kind, len(payload))
    return header + bytes([stage]) + payload


def setup(request_type, value, index, length):
    return struct.pack("<BBHHH", request_type, 1, value, index, length)


def pcapng(packets):
    def block(kind, body):
        body += b"\0" * (-len(body) % 4)
        size = len(body) + 12
        return struct.pack("<II", kind, size) + body + struct.pack("<I", size)

    out = block(0x0A0D0D0A, struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1))
    out += block(1, struct.pack("<HHI", 249, 0, 65535))
    for micros, data in packets:
        out += block(6, struct.pack("<IIIII", 0, micros >> 32, micros & 0xFFFFFFFF, len(data), len(data)) + data)
    return out


def pcap(packets):
    out = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 249)
    for micros, data in packets:
        out += struct.pack("<IIII", micros // 10**6, micros % 10**6, len(data), len(data)) + data
    return out


PACKETS = [
    # new-style USBPcap: OUT data rides with the setup packet
    (1_000_000, usbpcap(1, 0, 0, setup(0x21, 0x0001, 0x3A00, 4) + bytes.fromhex("01000000"))),
    (1_001_000, usbpcap(1, 3, 1, b"")),
    # a read answered in the "complete" packet
    (2_000_000, usbpcap(2, 0, 0, setup(0xA1, 0x0900, 0x3A00, 4))),
    (2_001_000, usbpcap(2, 3, 1, bytes.fromhex("02000000"))),
    # old-style: separate data and status stages; an unknown write
    (3_000_000, usbpcap(3, 0, 0, setup(0x21, 0x0800, 0x3A00, 4))),
    (3_000_500, usbpcap(3, 1, 0, bytes.fromhex("0f000000"))),
    (3_001_000, usbpcap(3, 2, 1, b"")),
    # noise: another device, a standard request, a bulk packet
    (3_500_000, usbpcap(4, 0, 0, setup(0x80, 0x0100, 0x0000, 18), device=2)),
    (3_600_000, usbpcap(5, 0, 0, b"\0" * 16, kind=3)),
]


def check(blob):
    found = decode.audio_control(decode.transfers(blob), None)
    assert [(t.value, t.index, t.data.hex(), t.label) for t in found] == [
        (0x0001, 0x3A00, "01000000", "phantom 48V"),
        (0x0900, 0x3A00, "02000000", "NEW"),
        (0x0800, 0x3A00, "0f000000", "NEW"),
    ]
    assert found[0].time == 0.0 and abs(found[2].time - 2.0) < 1e-6 and found[0].device == 5
    assert "value = 0x0800" in decode.toml(found[2]) and 'data = "0f000000"' in decode.toml(found[2])


def test_pcapng():
    check(pcapng(PACKETS))


def test_pcap():
    check(pcap(PACKETS))
