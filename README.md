# studio-control

Stream Deck buttons, through Bitfocus Companion on a Raspberry Pi 5, for:

- 4 × Godox LiteWafer UP150R and 1 × Godox Litemons LE200Bi, over Bluetooth
- an Audient EVO 8 plugged into the Pi: SmartGain, 48V, mute, preamp gain

```
Stream Deck ── Companion ──┬─ module "studio-godox" ─┐
                           └─ module "studio-evo"  ──┤ HTTP, 127.0.0.1:8765
                                                     ▼
                                        studio-bridge (service on the Pi)
                                          ├─ Bluetooth Mesh ──► 5 Godox lights
                                          └─ USB control ─────► EVO 8  (/dev/evo8)
```

Companion modules cannot hold a Bluetooth Mesh link or send USB class requests
past the Linux sound driver, so a small service on the Pi does both and the two
modules talk to it.

## What is and is not proven

Built from other people's reverse-engineering, tested against stand-ins, and so
far checked on one LE200Bi and one EVO 8. The UP150R is still untried.

| Part | Status |
|---|---|
| Bridge logic, HTTP API, capture decoder | 16 automated tests pass |
| Companion modules | Tests pass against the bridge in mock mode, and the built packages run under a stand-in for Companion's module host. Not yet loaded in Companion itself. |
| Kernel module `evo_ctl` | Compiles cleanly (Linux 6.8 headers). Not yet loaded on a Pi. |
| Godox protocol | Uses the `godox-mesh-bt` library, which its authors verified on Godox SL-series lights. |
| LE200Bi | Paired and controlled (brightness, colour temperature) on a real light. |
| UP150R | **Not in any published model list.** Assumed to use the same mesh protocol as every other Godox Light app fixture, with capabilities from its spec sheet. Step 2 below tells you within a minute whether that holds. |
| EVO 8 48V | Request sniffed from the EVO app on a real EVO 8 (evoctl project). |
| EVO 8 mute, gain | Read back correctly from a real EVO 8; writing them is confirmed on the EVO 4 only. A refused request shows as unsupported. |
| EVO 8 SmartGain | Works: the registers were found from USB captures and each step was tried by hand on an EVO 8. Built in; no setup. |

## Install

On the Pi (Raspberry Pi OS / CompanionPi, bookworm or newer), with this folder copied to it:

```bash
sudo ./install/install.sh
```

It installs the bridge in `/opt/studio-bridge`, a `studio-bridge` system user and
service, the `evo_ctl` kernel module (through DKMS, so it survives kernel
updates) and a udev rule. The Godox library is fetched from GitHub at a pinned commit.

Check it:

```bash
systemctl status studio-bridge
```

```bash
curl -s http://127.0.0.1:8765/api/state
```

## Add the lights

A light joins the bridge's own mesh network. **It then leaves the Godox phone
app** until you reset its Bluetooth and pair it there again.

1. Stop the service (it owns the Bluetooth link while running):

   ```bash
   sudo systemctl stop studio-bridge
   ```

2. Reset the Bluetooth of **one** light (on its menu: Bluetooth > Reset) and look for it:

   ```bash
   studio-bridge lights scan
   ```

   A line ending `PAIRING MODE (can be added)` means the light speaks the mesh
   protocol. If a UP150R never shows up here, stop and tell me what the scan
   printed: that would mean it uses something newer and needs its own work.

3. Add it, giving it a short id:

   ```bash
   studio-bridge lights add --id key --name "Key" --model UP150R
   ```

   Repeat steps 2–3 for each light, one at a time (four identical panels in
   pairing mode at once cannot be told apart). For the monolight use
   `--model LE200Bi`.

4. Start the service again:

   ```bash
   sudo systemctl start studio-bridge
   ```

Try one from the shell before touching Companion:

```bash
curl -s -X POST http://127.0.0.1:8765/api/lights/set -d '{"targets":["key"],"brightness":20,"kelvin":3200}'
```

## Companion

In Companion's web interface: **Modules → Import module package**, and import both files from `dist/`:

- `studio-godox-0.1.0.tgz`
- `studio-evo-0.1.2.tgz`

Then **Connections → Add connection**, search `studio`, add both, leave the
address `127.0.0.1` and port `8765`. Needs Companion 4.0 or newer.

Each module comes with presets (Buttons → Presets): per-light on/off, brighter/
dimmer, warmer/cooler, Stream Deck + dial presets for brightness, colour
temperature, hue and gain, all-lights buttons, SmartGain per input.

## SmartGain

Built in. The "SmartGain: start" action does what the green button on the unit
does, for the inputs chosen on the Companion button, and the button stays lit
until the EVO reports it has finished. A run started on the unit itself shows
up too. How it was found, and how to adapt it to another model, is in
`docs/SMARTGAIN.md`.

## Things to know

- **The lights do not report state.** Buttons show what was last sent. Changes on
  a light's own panel are not seen; the "Re-send state to lights" action puts a
  light back in line.
- **One Bluetooth link serves all five lights**; they relay to each other. The Pi
  needs to be in range of at least one. If the Pi is in a metal rack, a USB
  Bluetooth adapter on a short extension lead helps.
- **Do not run the Godox phone app and the bridge on the same light.**
- **The EVO kernel module only forwards audio class requests to the EVO's control
  interface.** It refuses anything else, firmware-update requests included.
- Without the kernel module, set `transport = "libusb"` under `[evo]`. That takes
  the EVO away from the Pi's sound driver while the bridge runs.
- The API has no password and listens on the Pi only.

## Layout

```
bridge/      the service (Python): lights, EVO, HTTP API, CLI
kmod/        evo_ctl kernel module
companion/   the two Companion modules (source and tests)
dist/        built module packages, ready to import
tools/       usbpcap_decode.py: finds SmartGain in a Windows USB capture
install/     installer, service unit, udev rule, example config
docs/        SMARTGAIN.md, PROTOCOLS.md
```

Run the tests: `cd bridge && pytest`, `npm test` in each module, and
`node --test companion/fake-host.test.js`.

## Credits

The protocol knowledge is other people's work: the Godox mesh protocol from
[binary-person/ha-godox-mesh](https://github.com/binary-person/ha-godox-mesh) and
[mattharrison/godox-ul60bi-bt](https://github.com/mattharrison/godox-ul60bi-bt) (MIT);
the EVO control layout from [soerenbnoergaard/evoctl](https://github.com/soerenbnoergaard/evoctl),
[anor4k/audient-evo-py](https://github.com/anor4k/audient-evo-py) and
[vijay-prema/audient-evo-linux-tools](https://github.com/vijay-prema/audient-evo-linux-tools).
Not affiliated with Godox, Audient or Bitfocus.
