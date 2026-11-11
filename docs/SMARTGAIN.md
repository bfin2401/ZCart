# Finding the SmartGain request

The EVO app starts SmartGain by sending the EVO 8 a USB control request. Nobody
has published which one. This finds it on your own unit in about fifteen minutes.

## 1. Capture on the Windows PC

1. Install the Audient EVO driver and app if they are not there, and
   [Wireshark](https://www.wireshark.org/). In Wireshark's installer, tick **USBPcap**. Reboot.
2. Plug the EVO 8 into the Windows PC. Open the EVO app and check it sees the unit.
3. Start Wireshark **as administrator**. In the interface list there is one
   `USBPcap` entry per USB root hub. Click the gear next to each to see which one
   lists the EVO 8; tick the EVO 8 there, and start capturing on it.
4. With a watch in view, do this slowly:
   - wait 5 seconds;
   - in the EVO app, click **SmartGain**, select **input 1 only**, click the green button to start;
   - talk or play into input 1 until it finishes;
   - wait 5 seconds, stop the capture.
5. **File → Save As**, type pcapng, name it `sg-in1.pcapng`.
6. Make three more the same way. Each one separates something the first cannot:

   | File | What you do | What it shows |
   |---|---|---|
   | `sg-in2.pcapng` | input 2 only | where the channel number sits |
   | `sg-in12.pcapng` | inputs 1 and 2 together | one request per channel, or a bit mask |
   | `sg-cancel.pcapng` | start on input 1, cancel before it finishes | the cancel request |

## 2. Decode

Copy the files to the Pi (or anywhere with Python 3) and run:

```bash
python3 tools/usbpcap_decode.py sg-in1.pcapng --toml
```

The app reads meters many times a second, so the summary has a few lines with
large counts; ignore those. Look at **Writes in order**: the lines marked `NEW`
at the moments you clicked are SmartGain. Compare the three "start" files:
whatever differs between input 1 and input 2 is the channel.

If you paste me the output of all four, I will write the config for you.

## 3. Configure

Put the transfers in `/etc/studio-bridge/config.toml`. The decoder's `--toml`
output is the literal version for the channels you captured:

```toml
[evo.smartgain]
assume_running_s = 20

[[evo.smartgain.start]]
value = 0x0000        # from the capture
index = 0x3a00
data = "00000000"
```

To make it follow the inputs chosen on the button, replace the part that
changed between captures with a placeholder:

| If the captures differ in… | Write |
|---|---|
| the low byte of `wValue` (0 for input 1, 1 for input 2) | `value = "0x0300 + ch"` and `per_channel = true` |
| a data byte holding the input number from 0 | `data = "{ch:02x}000000"` and `per_channel = true` |
| a data byte holding a bit mask (1, 2, 3 for in 1, in 2, both) | `data = "{mask:02x}000000"` |

The full list of placeholders is at the top of `bridge/studio_bridge/smartgain.py`.
Add `[[evo.smartgain.cancel]]` the same way. If the decoder lists a `NEW` read
whose value changes while SmartGain runs, that is the status register:

```toml
[evo.smartgain.status]
value = 0x0000
index = 0x3a00
length = 4
idle = "00000000"
```

With it the Stream Deck button stays green exactly as long as SmartGain listens;
without it the button stays green for `assume_running_s` seconds.

Then:

```bash
sudo systemctl restart studio-bridge
```

```bash
curl -s -X POST http://127.0.0.1:8765/api/evo/smartgain -d '{"action":"start","channels":[1]}'
```

The EVO's SmartGain button should light as if you had pressed it.

## Trying a single request by hand

```bash
studio-bridge evo raw --value 0x0000 --index 0x3a00
```

reads a register (that one is 48V on input 1), and adding `--data 01000000` writes it.

## Without a Windows capture

The Pi can watch the EVO's registers while you press the SmartGain button **on the unit**:

```bash
studio-bridge evo watch
```

It reads every register that answers, then prints each one that changes. This
only ever reads. It can show the status register and possibly the register the
button drives, but a register that changes is not necessarily one that can be
written to start SmartGain, so treat this as a supplement to the capture.

One caution from the EVO 4, which locks up its control pipe if certain
non-existent controls are read: if `watch` or `dump` reports that the EVO stopped
answering, unplug and replug it. Nothing was written to it.
