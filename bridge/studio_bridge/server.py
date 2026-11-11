"""The HTTP API Companion talks to. Local, unauthenticated, JSON."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from aiohttp import web

from . import __version__
from .evo import Evo, EvoError
from .evo_transport import GET
from .lights import LightError, LightManager

logger = logging.getLogger(__name__)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class Bridge:
    def __init__(self, lights: LightManager | None, evo: Evo | None) -> None:
        self.lights = lights
        self.evo = evo
        #: Goes up whenever anything in /api/state changes, so a client that
        #: polls can tell cheaply that nothing did.
        self.revision = 0
        self._changed = asyncio.Event()
        for part in (lights, evo):
            if part is not None:
                part.on_change = self._bump

    def _bump(self) -> None:
        self.revision += 1
        self._changed.set()

    def state(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "revision": self.revision,
            "lights": self.lights.snapshot() if self.lights else None,
            "evo": self.evo.snapshot() if self.evo else None,
        }

    async def wait_change(self, since: int, timeout: float) -> None:
        if self.revision != since:
            return
        self._changed.clear()
        try:
            await asyncio.wait_for(self._changed.wait(), timeout)
        except asyncio.TimeoutError:
            pass


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


@web.middleware
async def _errors(request: web.Request, handler: Handler) -> web.StreamResponse:
    try:
        return await handler(request)
    except (LightError, EvoError) as error:
        return _error(409, str(error))
    except (ValueError, KeyError, TypeError) as error:
        return _error(400, f"bad request: {error}")


async def _body(request: web.Request) -> dict[str, Any]:
    if not request.can_read_body:
        return {}
    data = await request.json()
    if not isinstance(data, dict):
        raise ValueError("the request body must be a JSON object")
    return data


def _int(value: Any, name: str) -> int:
    if isinstance(value, str):
        return int(value, 0)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def build_app(bridge: Bridge) -> web.Application:
    routes = web.RouteTableDef()

    def lights() -> LightManager:
        if bridge.lights is None:
            raise LightError("lights are disabled in the bridge config")
        return bridge.lights

    def evo() -> Evo:
        if bridge.evo is None:
            raise EvoError("the EVO is disabled in the bridge config")
        return bridge.evo

    @routes.get("/api/state")
    async def get_state(request: web.Request) -> web.Response:
        # ?since=<revision>&wait=<seconds>: answer when something changed, or
        # after the wait. Lets Companion follow changes without hammering.
        since = request.query.get("since")
        if since is not None:
            wait = min(30.0, float(request.query.get("wait", "20")))
            await bridge.wait_change(int(since), wait)
        return web.json_response(bridge.state())

    @routes.post("/api/lights/set")
    async def lights_set(request: web.Request) -> web.Response:
        body = await _body(request)
        targets = body.pop("targets", "all")
        return web.json_response({"ok": True, "lights": lights().apply(targets, body)})

    @routes.post("/api/evo/smartgain")
    async def evo_smartgain(request: web.Request) -> web.Response:
        body = await _body(request)
        result = await evo().smartgain(body.get("action", "start"), body.get("channels"))
        return web.json_response({"ok": True, "smartgain": result})

    @routes.post("/api/evo/phantom")
    async def evo_phantom(request: web.Request) -> web.Response:
        body = await _body(request)
        state = await evo().set_phantom(body.get("channel"), body.get("state"))
        return web.json_response({"ok": True, "phantom": state})

    @routes.post("/api/evo/mute")
    async def evo_mute(request: web.Request) -> web.Response:
        body = await _body(request)
        state = await evo().set_mute(body.get("channel"), body.get("state"))
        return web.json_response({"ok": True, "mute": state})

    @routes.post("/api/evo/gain")
    async def evo_gain(request: web.Request) -> web.Response:
        body = await _body(request)
        gain = await evo().set_gain(body.get("channel"), db=body.get("db"), delta=body.get("delta"))
        return web.json_response({"ok": True, "gain_db": gain})

    @routes.post("/api/evo/raw")
    async def evo_raw(request: web.Request) -> web.Response:
        # One arbitrary class request, for trying out what a capture showed.
        body = await _body(request)
        request_type = _int(body.get("request_type", 0x21), "request_type")
        if request_type not in (0x21, 0xA1):
            raise ValueError("request_type must be 0x21 (set) or 0xA1 (get)")
        index = _int(body["index"], "index")
        if index & 0xFF:
            raise ValueError("index must address interface 0 (low byte 00)")
        if request_type == GET:
            data: bytes | int = _int(body.get("length", 4), "length")
        else:
            data = bytes.fromhex(body["data"])
        try:
            reply = await evo().transfer(
                request_type, _int(body.get("request", 1), "request"), _int(body["value"], "value"), index, data
            )
        except OSError as error:
            return _error(409, str(error))
        return web.json_response({"ok": True, "data": reply.hex()})

    app = web.Application(middlewares=[_errors])
    app.add_routes(routes)
    return app


async def serve(bridge: Bridge, host: str, port: int) -> None:
    runner = web.AppRunner(build_app(bridge), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("listening on http://%s:%d", host, port)
    try:
        if bridge.lights:
            await bridge.lights.start()
        if bridge.evo:
            await bridge.evo.start()
        await asyncio.Event().wait()
    finally:
        if bridge.lights:
            await bridge.lights.stop()
        if bridge.evo:
            await bridge.evo.stop()
        await runner.cleanup()
