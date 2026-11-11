## Audient EVO 8 (studio-bridge)

Starts SmartGain and controls 48V phantom power, input mute and preamp gain on
an Audient EVO 8.

This module talks to the **studio-bridge** service on the computer the EVO 8 is
plugged into, which sends the USB control requests.

**Configuration**: the address and port of the bridge (`127.0.0.1`, `8765` when
it runs on the same machine as Companion).

**Actions**: SmartGain start (for one or more inputs) and cancel, 48V, input
mute, set and adjust preamp gain.

**Feedbacks**: SmartGain is running, SmartGain last result (succeeded / failed),
SmartGain switched off, 48V is on, input is muted, EVO not connected.

SmartGain works as on the unit: it listens on the chosen inputs and sets their gain.
The preset buttons are red while the EVO listens, then green if it set the gain
or yellow if it gave up, until the next run on that input or "clear result".
