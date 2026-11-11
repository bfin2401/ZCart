## Godox Bluetooth Mesh lights (studio-bridge)

Controls Godox lights that use the Godox Light app's Bluetooth Mesh, such as the
LiteWafer UP150R and the Litemons LE200Bi.

This module does not talk to the lights itself. It talks to the **studio-bridge**
service, which holds the Bluetooth link. Install the bridge first and add your
lights to it with `studio-bridge lights add`; they then appear here by name.

**Configuration**: the address and port of the bridge. Leave the defaults
(`127.0.0.1`, `8765`) when the bridge runs on the same machine as Companion.

**Actions**: power, brightness, colour temperature and tint, colour (hue and
saturation), effects, and relative adjustments meant for Stream Deck + dials.
Every action takes one or more lights, or "All lights".

**Feedbacks**: light is on, any light is on, light is in a mode, lights not reachable.

**Good to know**

- The lights do not report their state. Buttons show what the bridge last sent;
  a change made on a light's own panel is not seen. "Re-send state to lights"
  puts a light back to what the buttons show.
- A light added to the bridge leaves the Godox phone app until its Bluetooth is reset.
