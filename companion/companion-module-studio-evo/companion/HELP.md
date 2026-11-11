## Audient EVO 8 (studio-bridge)

Starts SmartGain and controls 48V phantom power, input mute and preamp gain on
an Audient EVO 8.

This module talks to the **studio-bridge** service on the computer the EVO 8 is
plugged into, which sends the USB control requests.

**Configuration**: the address and port of the bridge (`127.0.0.1`, `8765` when
it runs on the same machine as Companion).

**Actions**: SmartGain start (for one or more inputs) and cancel, 48V, input
mute, set and adjust preamp gain.

**Feedbacks**: SmartGain is running, SmartGain not set up, 48V is on, input is
muted, EVO not connected.

SmartGain works as on the unit: it listens on the chosen inputs and sets their gain.
The button stays lit until the EVO reports it has finished.
