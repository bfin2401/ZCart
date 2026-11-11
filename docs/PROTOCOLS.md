# Protocol notes

## Godox lights

Standard Bluetooth SIG Mesh over the GATT Proxy bearer (service `0x1828`); only
the access-layer opcode is Godox's. The bridge provisions each light onto its own
network (PB-GATT, service `0x1827`), binds one application key to the vendor
model `0x0211:0000`, and then sends vendor frames on opcode `0x0211F0`.

| Frame | Bytes | Used for |
|---|---|---|
| `FE` | `FE 00/01 FF FF FF FF FF crc` | power on / off |
| `F0` | `F0 bri K/100 gm+50 curve gm tenths crc` | colour temperature |
| `F1` | `F1 bri hueLo hueHi sat mode tenths crc` | hue / saturation |
| `F7` | length-prefixed | effects |

CRC is Dallas/Maxim CRC-8. Full detail, with the evidence for each statement, is
in the upstream project's `docs/consolidated/protocol.md`.

The bridge reaches all lights through one proxy connection, addressing each by
its unicast address. Mesh sequence numbers must never repeat, so the bridge
stores the counter a block (256) ahead of the one in use, and starts at 300000.

### Model profiles (`bridge/studio_bridge/models.py`)

- **LE200Bi**: Godox catalogue entry, radio id `00AE`: 2800–6500 K, no tint,
  parameterised effects 6–15 and 17.
- **UP150R**: absent from the catalogue snapshot (September 2026). Profile from
  the spec sheet and the UP-C150: 1800–10000 K, ±tint, HSI, effects. If a scan
  shows a UP150R in pairing mode, its manufacturer data holds its radio id;
  worth reporting upstream.

## Audient EVO 8

USB Audio Class 2.0 device, vendor `2708`, product `0007`. Controls are class
requests (`bmRequestType` `0x21` set / `0xA1` get, `bRequest` 1 = CUR) to entities
of interface 0. `wIndex = entity << 8`, `wValue = selector << 8 | channel`.

| Control | wIndex | wValue | Data | Source |
|---|---|---|---|---|
| 48V, input n | `0x3A00` | `0x0000 + (n-1)` | 4 bytes LE, 0/1 | sniffed on EVO 8 |
| Input mute | `0x3A00` | `0x0200 + (n-1)` | 4 bytes LE, 0/1 | EVO 4, by analogy |
| Preamp gain | `0x0B00` | `0x0200 + n` | 2 bytes, dB × 256 | UAC2 feature unit |
| Output volume | `0x3B00` | `0x0000` / `0x0002` | 4 bytes | sniffed on EVO 8 (not used) |
| Monitor mixer | `0x3C00` | `0x0100 + crosspoint` | 2 bytes | sniffed on EVO 8 (not used) |
| SmartGain | ? | ? | ? | to be captured |

Extension units 50, 51 and 57 are in the descriptors with no known controls;
SmartGain is most likely an unlisted selector on unit 58.

### Why a kernel module

Linux lets user space send class requests to an interface only if user space
holds that interface, and `snd-usb-audio` holds interface 0. `evo_ctl` binds the
unused DFU interface (3) to get a handle on the device and forwards control
transfers from `/dev/evo8`. It accepts only class requests to interface 0.

The ioctl (`_IOWR('E', 0, 264 bytes)`) carries
`{u8 bRequestType, u8 bRequest, u16 wValue, u16 wIndex, u16 wLength, u8 data[256]}`.

## Bridge HTTP API

| | |
|---|---|
| `GET /api/state` | everything; `?since=<revision>&wait=20` answers when something changes |
| `POST /api/lights/set` | `{"targets": "all" or [ids], ...}` with any of `power` (on/off/toggle), `brightness`, `brightness_delta`, `kelvin`, `kelvin_delta`, `gm`, `hue`, `hue_delta`, `saturation`, `saturation_delta`, `mode` (cct/hsi/effect), `effect`, `speed`, `resync` |
| `POST /api/evo/smartgain` | `{"action": "start" or "cancel", "channels": [1, 2]}` |
| `POST /api/evo/phantom`, `/api/evo/mute` | `{"channel": 1, "state": "on" / "off" / "toggle"}` |
| `POST /api/evo/gain` | `{"channel": 1, "db": 30}` or `{"channel": 1, "delta": -1}` |
| `POST /api/evo/raw` | `{"request_type": "0xa1", "value": "0x0000", "index": "0x3a00", "length": 4}` or with `"data": "hex"` to write |
