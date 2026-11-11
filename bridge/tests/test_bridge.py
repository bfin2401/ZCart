import asyncio
import struct
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from studio_bridge.config import EvoConfig, GodoxConfig
from studio_bridge.evo import Evo
from studio_bridge.evo_transport import MockTransport
from studio_bridge.lights import LightManager
from studio_bridge.mock import FakeController, demo_store
from studio_bridge.server import Bridge, build_app
from studio_bridge.smartgain import SmartGainConfigError, evaluate, parse_smartgain
from studio_bridge.store import MeshStore

FAST = GodoxConfig(min_interval=0.0, reconnect_interval=0.05)


def make_lights(tmp_path: Path, fail=lambda: False):
    frames: list = []
    store = demo_store(tmp_path / "mesh.json")
    manager = LightManager(store, FAST, tmp_path / "shadow.json", lambda r, s, seq: FakeController(frames, fail))
    return manager, frames


async def settle(manager: LightManager) -> None:
    for _ in range(200):
        await asyncio.sleep(0.01)
        if manager.connected and not any(l["pending"] for l in manager.snapshot()["lights"]):
            return
    raise AssertionError("lights never settled")


async def test_nothing_is_sent_at_startup(tmp_path):
    manager, frames = make_lights(tmp_path)
    await manager.start()
    await settle(manager)
    assert frames == []
    await manager.stop()


async def test_cct_and_power(tmp_path):
    manager, frames = make_lights(tmp_path)
    await manager.start()
    manager.apply(["key"], {"brightness": 80, "kelvin": 4321})
    await settle(manager)
    assert frames == [
        ("cct", 2, {"brightness": 80.0, "cct": 4300, "min_kelvin": 1800, "max_kelvin": 10000,
                    "gm": 0, "supports_gm": True, "circle": 0xFF})
    ]
    frames.clear()
    manager.apply(["key"], {"power": "off"})
    await settle(manager)
    assert frames == [("power", 2, False)] * 2
    frames.clear()
    # While off, changes are remembered, not sent ...
    manager.apply(["key"], {"brightness_delta": -30})
    await settle(manager)
    assert frames == []
    # ... and go out when it comes back on.
    manager.apply(["key"], {"power": "toggle"})
    await settle(manager)
    assert frames[:2] == [("power", 2, True)] * 2
    assert frames[2][0] == "cct" and frames[2][2]["brightness"] == 50.0
    await manager.stop()


async def test_ranges_follow_the_model(tmp_path):
    manager, frames = make_lights(tmp_path)
    await manager.start()
    manager.apply("all", {"kelvin": 9000, "gm": 20})
    await settle(manager)
    by_node = {f[1]: f[2] for f in frames}
    assert by_node[2]["cct"] == 9000 and by_node[2]["gm"] == 20
    # The LE200Bi stops at 6500 K and has no tint.
    assert by_node[10]["cct"] == 6500 and by_node[10]["gm"] == 0 and by_node[10]["supports_gm"] is False
    await manager.stop()


async def test_colour_to_all_leaves_the_bicolour_light_alone(tmp_path):
    manager, frames = make_lights(tmp_path)
    await manager.start()
    manager.apply("all", {"hue": 200, "saturation": 90})
    await settle(manager)
    assert sorted(f[1] for f in frames) == [2, 4, 6, 8]
    assert all(f[0] == "hsi" and f[2]["hue"] == 200 for f in frames)
    state = {l["id"]: l["state"] for l in manager.snapshot()["lights"]}
    assert state["spot"]["mode"] == "cct" and state["key"]["mode"] == "hsi"
    await manager.stop()


async def test_effect_and_unsupported_effect(tmp_path):
    manager, frames = make_lights(tmp_path)
    await manager.start()
    manager.apply("all", {"mode": "effect", "effect": 16})  # SOS: UP150R only
    await settle(manager)
    assert sorted(f[1] for f in frames) == [2, 4, 6, 8]
    assert frames[0][2]["effect"] == 16 and frames[0][2]["effect_version"] == 1
    await manager.stop()


async def test_dial_turns_collapse(tmp_path):
    manager, frames = make_lights(tmp_path)
    for _ in range(50):
        manager.apply(["key"], {"brightness_delta": 1})
    await manager.start()
    await settle(manager)
    assert len(frames) == 1 and frames[0][2]["brightness"] == 100.0
    await manager.stop()


async def test_reconnects_and_resends(tmp_path):
    broken = {"now": True}
    manager, frames = make_lights(tmp_path, lambda: broken["now"])
    await manager.start()
    manager.apply(["fill"], {"brightness": 10})
    await asyncio.sleep(0.1)
    assert frames == [] and not manager.connected and manager.snapshot()["error"]
    broken["now"] = False
    await settle(manager)
    assert frames and frames[-1][2]["brightness"] == 10.0 and manager.snapshot()["error"] is None
    await manager.stop()


async def test_state_survives_restart(tmp_path):
    manager, _ = make_lights(tmp_path)
    await manager.start()
    manager.apply(["key"], {"brightness": 33, "power": "off"})
    await settle(manager)
    await manager.stop()
    again, frames = make_lights(tmp_path)
    state = again.snapshot()["lights"][0]["state"]
    assert state["brightness"] == 33.0 and state["on"] is False


def test_store_addresses_and_sequence(tmp_path):
    store = demo_store(tmp_path / "mesh.json")
    assert store.next_node_address() == 12
    store.reserve_sequence(300)
    assert MeshStore.load(tmp_path / "mesh.json").sequence_number == 512
    store.reserve_sequence(301)  # inside the block: no change
    assert store.sequence_number == 512
    assert (tmp_path / "mesh.json").stat().st_mode & 0o777 == 0o600


# ------------------------------------------------------------------- evo

SMARTGAIN = {
    "assume_running_s": 0.2,
    "start": [
        {"value": "0x0300 + ch", "index": 0x3A00, "data": "{ch1_le32}", "per_channel": True},
        {"value": 0x0400, "index": 0x3A00, "data": "01{mask:02x}0000"},
    ],
    "cancel": [{"value": 0x0400, "index": 0x3A00, "data": "00000000"}],
}


def test_expressions():
    assert evaluate("0x0300 + ch", {"ch": 2}) == 0x0302
    assert evaluate("(1 << ch) | 0x10", {"ch": 3}) == 0x18
    for bad in ("__import__('os')", "ch.real", "1/0", "x"):
        with pytest.raises(SmartGainConfigError):
            evaluate(bad, {"ch": 1})
    with pytest.raises(SmartGainConfigError):
        parse_smartgain({"start": [{"value": 1, "index": 2, "data": "zz"}]})
    with pytest.raises(SmartGainConfigError):
        parse_smartgain({"start": [{"value": 1, "index": 2, "data": "00", "request_type": 0xA1}]})


async def test_smartgain_sequence():
    transport = MockTransport()
    evo = Evo(EvoConfig(smartgain=SMARTGAIN), transport)
    result = await evo.smartgain("start", [3, 1])
    assert result == {"configured": True, "can_cancel": True, "running": True, "channels": [1, 3]}
    assert transport.log == [
        (0x21, 1, 0x0300, 0x3A00, bytes.fromhex("01000000")),
        (0x21, 1, 0x0302, 0x3A00, bytes.fromhex("03000000")),
        (0x21, 1, 0x0400, 0x3A00, bytes.fromhex("01050000")),
    ]
    await asyncio.sleep(0.25)
    assert evo.smartgain_snapshot()["running"] is False
    await evo.smartgain("start")
    await evo.smartgain("cancel")
    assert evo.smartgain_snapshot()["running"] is False


async def test_evo_controls():
    transport = MockTransport()
    evo = Evo(EvoConfig(), transport)
    await evo.refresh()
    assert evo.available and evo.phantom == [False] * 4 and evo.gain_db == [20.0] * 4
    assert await evo.set_phantom(2, "toggle") is True
    assert transport.registers[(0x3A00, 0x0001)] == struct.pack("<I", 1)
    assert await evo.set_gain(4, delta=5) == 25.0
    assert transport.registers[(0x0B00, 0x0204)] == struct.pack("<h", 25 * 256)
    assert await evo.set_gain(1, db=99) == 58.0
    # A control the device refuses is dropped from polling, not retried.
    del transport.registers[(0x3A00, 0x0200)]
    await evo.refresh()
    assert "mute" in evo.snapshot()["unsupported"]


async def test_http_api(tmp_path):
    manager, frames = make_lights(tmp_path)
    evo = Evo(EvoConfig(), MockTransport())
    client = TestClient(TestServer(build_app(Bridge(manager, evo))))
    await client.start_server()
    await manager.start()
    try:
        state = await (await client.get("/api/state")).json()
        assert [l["id"] for l in state["lights"]["lights"]] == ["key", "fill", "back-l", "back-r", "spot"]
        assert state["evo"]["smartgain"]["configured"] is True

        reply = await client.post("/api/lights/set", json={"targets": ["spot"], "brightness": 70})
        assert reply.status == 200
        await settle(manager)
        assert frames[-1][1] == 10

        assert (await client.post("/api/lights/set", json={"targets": ["nope"], "power": "on"})).status == 409
        assert (await client.post("/api/lights/set", json={"brightness": "x"})).status == 409

        reply = await client.post("/api/evo/smartgain", json={"action": "start", "channels": [1]})
        assert (await reply.json())["smartgain"] == {
            "configured": True, "can_cancel": True, "running": True, "channels": [1]}
        assert (await client.post("/api/evo/smartgain", json={"action": "start", "channels": [9]})).status == 409

        reply = await client.post("/api/evo/phantom", json={"channel": 1, "state": "on"})
        assert (await reply.json()) == {"ok": True, "phantom": True}
        reply = await client.post("/api/evo/raw", json={"request_type": "0xa1", "value": "0x0000", "index": "0x3a00"})
        assert (await reply.json())["data"] == "01000000"
        # Requests to anything but the audio control interface are refused.
        assert (await client.post("/api/evo/raw", json={"value": 0, "index": 0x0003, "data": "00"})).status == 400

        revision = (await (await client.get("/api/state")).json())["revision"]
        waiting = asyncio.ensure_future(client.get(f"/api/state?since={revision}&wait=5"))
        await asyncio.sleep(0.05)
        assert not waiting.done()
        await client.post("/api/evo/phantom", json={"channel": 3, "state": "on"})
        changed = await (await asyncio.wait_for(waiting, 2)).json()
        assert changed["revision"] > revision and changed["evo"]["inputs"][2]["phantom"] is True
    finally:
        await manager.stop()
        await client.close()


def test_probe_sweep_and_diff():
    from studio_bridge import evo_probe

    transport = MockTransport()
    before = evo_probe.sweep(transport, pause=0)
    assert before["3a00:0000"] == "00000000" and "0b00:0201" in before
    transport.registers[(0x3A00, 0x0002)] = bytes.fromhex("01000000")
    lines = evo_probe.diff(before, evo_probe.sweep(transport, pause=0))
    assert len(lines) == 1 and "wValue=0x0002" in lines[0] and "phantom" in lines[0]
    assert all(entry[0] == 0xA1 for entry in transport.log)  # never writes


async def test_repeating_a_request_sends_it_again(tmp_path):
    # The light was changed on its own panel in between; the bridge cannot
    # know, so the same request must reach the light a second time.
    manager, frames = make_lights(tmp_path)
    await manager.start()
    manager.apply(["spot"], {"brightness": 20, "kelvin": 3200})
    await settle(manager)
    frames.clear()
    manager.apply(["spot"], {"brightness": 20, "kelvin": 3200})
    await settle(manager)
    assert [f[0] for f in frames] == ["power", "power", "cct"]
    assert frames[-1][2]["cct"] == 3200 and frames[-1][2]["brightness"] == 20.0
    await manager.stop()


async def test_smartgain_built_in_sequence():
    # What was tried by hand on an EVO 8: state 1, select the inputs, state 2.
    transport = MockTransport()
    evo = Evo(EvoConfig(), transport)
    await evo.smartgain("start", [2, 1])
    assert transport.log == [
        (0x21, 1, 0x0500, 0x3A00, bytes.fromhex("01000000")),
        (0x21, 1, 0x0400, 0x3A00, bytes.fromhex("01000000")),
        (0x21, 1, 0x0401, 0x3A00, bytes.fromhex("01000000")),
        (0x21, 1, 0x0500, 0x3A00, bytes.fromhex("02000000")),
    ]
    # "Running" follows the unit's own state register, not a timer.
    await evo.refresh()
    assert evo.smartgain_snapshot() == {"configured": True, "can_cancel": True, "running": True, "channels": [1, 2]}
    await asyncio.sleep(0.45)
    await evo.refresh()
    assert evo.smartgain_snapshot()["running"] is False

    await evo.smartgain("start", [4])
    await evo.smartgain("cancel")
    assert transport.registers[(0x3A00, 0x0500)] == bytes(4)
    await evo.refresh()
    assert evo.smartgain_snapshot()["running"] is False


async def test_smartgain_can_be_switched_off():
    evo = Evo(EvoConfig(smartgain={"start": []}), MockTransport())
    assert evo.smartgain_snapshot()["configured"] is False
    with pytest.raises(Exception, match="switched off"):
        await evo.smartgain("start", [1])


async def test_smartgain_started_on_the_unit_is_seen():
    transport = MockTransport()
    evo = Evo(EvoConfig(), transport)
    # as if the buttons on the EVO were pressed: state 1, input 3 chosen
    transport.registers[(0x3A00, 0x0500)] = bytes.fromhex("01000000")
    transport.registers[(0x3A00, 0x0402)] = bytes.fromhex("01000000")
    await evo.refresh()
    assert evo.smartgain_snapshot()["running"] is True and evo.smartgain_snapshot()["channels"] == [3]
