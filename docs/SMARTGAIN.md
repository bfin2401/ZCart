# SmartGain

## What the bridge sends

Three registers of the EVO 8's extension unit 58 (`wIndex 0x3A00`), each 4 bytes little-endian:

| Register | wValue | Meaning |
|---|---|---|
| State | `0x0500` | 0 idle, 1 choosing inputs, 2 and 3 listening, 4 done |
| Input n selected | `0x0400 + (n-1)` | 0 / 1 |

To start: write state 1, write 1 to "selected" for each input, write state 2.
To cancel: write state 0. While it runs the bridge reads the state register;
anything but 0 counts as running.

By hand:

```bash
studio-bridge evo raw --value 0x0500 --index 0x3a00 --data 01000000
```

```bash
studio-bridge evo raw --value 0x0400 --index 0x3a00 --data 01000000
```

```bash
studio-bridge evo raw --value 0x0500 --index 0x3a00 --data 02000000
```

Through the bridge:

```bash
curl -s -X POST http://127.0.0.1:8765/api/evo/smartgain -d '{"action":"start","channels":[1,2]}'
```

## How it was found

The Windows EVO app has no SmartGain button, so there was no request to copy.
USB captures (Wireshark with USBPcap) were made while pressing the button on
the unit. They showed the app polling a notification queue (`wIndex 0x3E00`,
`wValue 0x0600`) and then reading the two registers above as they changed.
Writing the same values back was then tried on an EVO 8, one step at a time,
and each did what the button does.

`tools/usbpcap_decode.py capture.pcapng` prints such a capture in readable form.

## Changing the sequence

For another model or firmware, `[evo.smartgain]` in
`/etc/studio-bridge/config.toml` replaces the built-in sequence. The format and
the placeholders are described at the top of `bridge/studio_bridge/smartgain.py`.
`start = []` switches SmartGain off.

## Looking for other registers

```bash
studio-bridge evo watch
```

reads every register that answers, then prints each one that changes while you
use the unit's own controls. It only reads. If it reports that the EVO stopped
answering, unplug and replug it.
