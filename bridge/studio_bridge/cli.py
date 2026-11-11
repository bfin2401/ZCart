"""Command line: run the bridge, add lights, explore the EVO."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import re
import socket
import sys
from pathlib import Path

from .config import Config, load_config
from .models import PROFILES, profile_for
from .store import LightRecord, MeshStore

# A fresh node has seen nothing from this provisioner, but a light that was
# reset and re-added may remember old numbers; starting high clears both.
SEQUENCE_FLOOR = 300_000


def _int(text: str) -> int:
    return int(text, 0)


def _daemon_running(config: Config) -> bool:
    try:
        with socket.create_connection((config.http.host, config.http.port), timeout=0.5):
            return True
    except OSError:
        return False


def _need_daemon_stopped(config: Config) -> None:
    if _daemon_running(config):
        raise SystemExit(
            "The bridge service is running and holds the Bluetooth link and the mesh sequence counter.\n"
            "Stop it first:  sudo systemctl stop studio-bridge"
        )


# ------------------------------------------------------------------- run


def cmd_run(args: argparse.Namespace, config: Config) -> int:
    from .evo import Evo
    from .evo_transport import MockTransport
    from .lights import LightManager
    from .server import Bridge, serve

    lights = evo = None
    if args.mock:
        from .mock import mock_bridge_parts

        lights, evo = mock_bridge_parts(config)
    else:
        if config.godox.enabled:
            store = MeshStore.load(config.mesh_path)
            if store.lights:
                lights = LightManager(store, config.godox, config.shadow_path)
            else:
                logging.warning("no lights yet: add them with 'studio-bridge lights add'")
        if config.evo.enabled:
            evo = Evo(config.evo, MockTransport() if config.evo.transport == "mock" else None)
    try:
        asyncio.run(serve(Bridge(lights, evo), config.http.host, config.http.port))
    except KeyboardInterrupt:
        pass
    return 0


# ---------------------------------------------------------------- lights


def cmd_lights_list(args: argparse.Namespace, config: Config) -> int:
    store = MeshStore.load(config.mesh_path)
    if not store.lights:
        print("No lights yet. Add one with: studio-bridge lights add --id key --model UP150R")
    for light in store.lights:
        print(f"{light.id:<12} {light.model:<10} {light.ble_address}  node 0x{light.node_address:04x}  {light.name}")
    return 0


async def _scan(timeout: float) -> int:
    from godox_mesh_bt.scanner import is_mesh_node, scan, scan_unprovisioned

    print(f"Scanning for {timeout:.0f} s ...")
    pairing = {device.address for device in await scan_unprovisioned(timeout=timeout)}
    devices = [device for device in await scan(timeout=timeout) if is_mesh_node(device) or device.address in pairing]
    if not devices:
        print("No Godox mesh lights heard. Is the light on, in range, and is Bluetooth enabled on it?")
        return 1
    for device in devices:
        state = "PAIRING MODE (can be added)" if device.address in pairing else "already on a mesh (reset it to add it)"
        data = " ".join(f"{k:04x}:{v}" for k, v in device.advertisement.manufacturer_data.items())
        print(f"{device.address}  rssi {device.rssi}  {device.name or '?':<10} {state}")
        if data:
            print(f"    manufacturer data {data}")
    return 0


def cmd_lights_scan(args: argparse.Namespace, config: Config) -> int:
    _need_daemon_stopped(config)
    return asyncio.run(_scan(args.timeout))


async def _bind(store: MeshStore, record: LightRecord) -> None:
    from godox_mesh_bt import MeshState
    from godox_mesh_bt.config_session import ConfigSession

    last: Exception | None = None
    for attempt in range(1, 5):
        # The light restarts its radio after provisioning; give it a moment.
        await asyncio.sleep(3.0)
        state = MeshState(
            network_key=store.network_key,
            app_key=store.app_key,
            provisioner_address=store.provisioner_address,
            node_address=record.node_address,
            sequence_number=max(store.sequence_number, SEQUENCE_FLOOR),
            iv_index=store.iv_index,
            device_key=record.device_key,
            device_address=record.ble_address,
            num_elements=record.num_elements,
        )
        try:
            final = await ConfigSession(record.ble_address, state).run()
        except Exception as error:  # noqa: BLE001 - retried, then reported
            last = error
            print(f"  binding attempt {attempt} failed: {error}")
            # The numbers that attempt used are spent either way.
            store.reserve_sequence(state.sequence_number + 64)
            continue
        store.reserve_sequence(final.sequence_number)
        return
    raise SystemExit(
        f"The light joined the network but did not take the application key ({last}).\n"
        f"Its keys are saved. Retry with:  studio-bridge lights rebind {record.id}"
    )


async def _add(args: argparse.Namespace, config: Config) -> int:
    from godox_mesh_bt.provisioning import ProvisioningSession
    from godox_mesh_bt.scanner import scan_unprovisioned

    store = MeshStore.load(config.mesh_path)
    if store.get(args.id):
        raise SystemExit(f"A light with id {args.id!r} already exists (see 'studio-bridge lights list').")
    profile = profile_for(args.model)
    address = args.address
    if not address:
        print("Looking for a light in pairing mode ...")
        found = await scan_unprovisioned(timeout=args.timeout)
        if not found:
            raise SystemExit(
                "No light in pairing mode found. Reset the light's Bluetooth (see its manual: usually\n"
                "MENU > Bluetooth > Reset, or holding the Bluetooth button) and try again."
            )
        if len(found) > 1:
            print("More than one light is in pairing mode:")
            for device in found:
                print(f"  {device.address}  {device.name}")
            raise SystemExit("Put only one light in pairing mode at a time, or pass --address.")
        address = found[0].address
        print(f"Found {found[0].name or 'light'} at {address}")

    store.ensure_keys()
    store.sequence_number = max(store.sequence_number, SEQUENCE_FLOOR)
    store.save()
    node_address = store.next_node_address()
    print(f"Provisioning {address} as node 0x{node_address:04x} ...")
    state = await ProvisioningSession(
        address=address,
        net_key=bytes.fromhex(store.network_key),
        key_index=0,
        iv_index=store.iv_index,
        unicast_address=node_address,
        provisioner_address=store.provisioner_address,
    ).run()
    record = LightRecord(
        id=args.id,
        name=args.name or args.id,
        model=profile.model,
        ble_address=address,
        node_address=node_address,
        num_elements=state.num_elements,
        device_key=state.device_key,
    )
    # Saved before binding: the device key cannot be asked for again.
    store.lights.append(record)
    store.save()
    print("Joined. Sending the application key ...")
    await _bind(store, record)
    print(f"Done: {record.id} ({record.model}) is ready.")
    if profile.assumed:
        print(f"Note: the {profile.model} profile has not been checked on a real light yet (docs/PROTOCOLS.md).")
    return 0


def cmd_lights_add(args: argparse.Namespace, config: Config) -> int:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,23}", args.id):
        raise SystemExit("--id: lower-case letters, digits, '-' and '_' only (e.g. key, fill-left)")
    _need_daemon_stopped(config)
    return asyncio.run(_add(args, config))


def cmd_lights_rebind(args: argparse.Namespace, config: Config) -> int:
    _need_daemon_stopped(config)
    store = MeshStore.load(config.mesh_path)
    record = store.get(args.id)
    if record is None:
        raise SystemExit(f"No light {args.id!r}.")
    asyncio.run(_bind(store, record))
    print("Done.")
    return 0


def cmd_lights_remove(args: argparse.Namespace, config: Config) -> int:
    _need_daemon_stopped(config)
    store = MeshStore.load(config.mesh_path)
    record = store.get(args.id)
    if record is None:
        raise SystemExit(f"No light {args.id!r}.")
    store.lights.remove(record)
    store.save()
    print(f"Removed {args.id} from the bridge. Reset the light's Bluetooth to use it with the Godox app again.")
    return 0


# ------------------------------------------------------------------- evo


def _transport(args: argparse.Namespace, config: Config):  # noqa: ANN202
    from .evo_transport import open_transport

    return open_transport(args.transport or config.evo.transport, config.evo.device)


def cmd_evo_raw(args: argparse.Namespace, config: Config) -> int:
    from .evo_transport import CUR, GET, SET

    transport = _transport(args, config)
    try:
        if args.data is None:
            reply = transport.transfer(GET, args.request or CUR, args.value, args.index, args.length)
            print(reply.hex())
        else:
            transport.transfer(SET, args.request or CUR, args.value, args.index, bytes.fromhex(args.data))
            print("sent")
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        transport.close()
    return 0


def cmd_evo_dump(args: argparse.Namespace, config: Config) -> int:
    from . import evo_probe

    transport = _transport(args, config)
    try:
        found = evo_probe.sweep(transport, progress=lambda text: print(f"  {text}", file=sys.stderr))
    except (evo_probe.ProbeAborted, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        transport.close()
    text = json.dumps(found, indent=1, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text + "\n")
        print(f"{len(found)} registers written to {args.output}")
    else:
        print(text)
    return 0


def cmd_evo_diff(args: argparse.Namespace, config: Config) -> int:
    from . import evo_probe

    lines = evo_probe.diff(evo_probe.load(args.before), evo_probe.load(args.after))
    print("\n".join(lines) if lines else "no differences")
    return 0


def cmd_evo_watch(args: argparse.Namespace, config: Config) -> int:
    from . import evo_probe

    transport = _transport(args, config)
    try:
        if args.baseline:
            baseline = evo_probe.load(args.baseline)
        else:
            print("Sweeping the registers once ...", file=sys.stderr)
            baseline = evo_probe.sweep(transport)
        print(f"Watching {len(baseline)} registers. Use the EVO's own buttons now; Ctrl-C to stop.", file=sys.stderr)
        for line in evo_probe.watch(transport, baseline):
            print(line, flush=True)
    except KeyboardInterrupt:
        pass
    except (evo_probe.ProbeAborted, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        transport.close()
    return 0


# ------------------------------------------------------------------ main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="studio-bridge", description=__doc__)
    parser.add_argument("-c", "--config", help="config file (default /etc/studio-bridge/config.toml)")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the bridge (what the service does)")
    run.add_argument("--mock", action="store_true", help="pretend lights and EVO, no hardware")
    run.set_defaults(func=cmd_run)

    lights = sub.add_parser("lights", help="manage the Godox lights").add_subparsers(dest="sub", required=True)
    lights.add_parser("list", help="lights the bridge knows").set_defaults(func=cmd_lights_list)
    scan = lights.add_parser("scan", help="look for Godox mesh lights over Bluetooth")
    scan.add_argument("--timeout", type=float, default=8.0)
    scan.set_defaults(func=cmd_lights_scan)
    add = lights.add_parser("add", help="join a light in pairing mode to the bridge's mesh")
    add.add_argument("--id", required=True, help="short name used by Companion, e.g. key")
    add.add_argument("--name", help="display name, e.g. 'Key light'")
    add.add_argument("--model", required=True, choices=sorted(PROFILES), help="light model")
    add.add_argument("--address", help="Bluetooth address, if several lights are in pairing mode")
    add.add_argument("--timeout", type=float, default=10.0)
    add.set_defaults(func=cmd_lights_add)
    rebind = lights.add_parser("rebind", help="re-send the application key to a light")
    rebind.add_argument("id")
    rebind.set_defaults(func=cmd_lights_rebind)
    remove = lights.add_parser("remove", help="forget a light")
    remove.add_argument("id")
    remove.set_defaults(func=cmd_lights_remove)

    evo = sub.add_parser("evo", help="explore the Audient EVO 8").add_subparsers(dest="sub", required=True)
    for name, func, text in (
        ("raw", cmd_evo_raw, "one control transfer (read, or write with --data)"),
        ("dump", cmd_evo_dump, "read every register that answers (read-only)"),
        ("watch", cmd_evo_watch, "print registers as they change (read-only)"),
        ("diff", cmd_evo_diff, "compare two dumps"),
    ):
        p = evo.add_parser(name, help=text)
        p.add_argument("--transport", choices=["kmod", "libusb", "mock"], help="override the config")
        p.set_defaults(func=func)
        if name == "raw":
            p.add_argument("--value", type=_int, required=True, help="wValue, e.g. 0x0000")
            p.add_argument("--index", type=_int, required=True, help="wIndex, e.g. 0x3a00")
            p.add_argument("--request", type=_int, help="bRequest (default 1 = CUR)")
            p.add_argument("--length", type=int, default=4, help="bytes to read")
            p.add_argument("--data", help="hex bytes to write; omit to read")
        elif name == "dump":
            p.add_argument("-o", "--output", help="write JSON here")
        elif name == "watch":
            p.add_argument("--baseline", help="a dump to start from, instead of sweeping")
        elif name == "diff":
            p.add_argument("before")
            p.add_argument("after")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if args.verbose < 2:
        logging.getLogger("bleak").setLevel(logging.WARNING)
    try:
        config = load_config(args.config)
    except (OSError, ValueError) as error:
        print(f"config error: {error}", file=sys.stderr)
        return 2
    return args.func(args, config)
