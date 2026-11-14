"""Web UI (shown through Home Assistant ingress) and its JSON API."""

from __future__ import annotations

import ipaddress
import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from .bot import Bridge
from .messages import MessageError

_LOGGER = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
INGRESS_IP = ipaddress.ip_address("172.30.32.2")


def create_app(bridge: Bridge, *, ingress_only: bool) -> web.Application:
    store = bridge.store

    @web.middleware
    async def guard(request: web.Request, handler: Any) -> web.StreamResponse:
        if ingress_only:
            try:
                remote = ipaddress.ip_address(request.remote or "")
            except ValueError:
                remote = None
            if remote != INGRESS_IP:
                raise web.HTTPForbidden(text="Only available through Home Assistant")
        try:
            return await handler(request)
        except MessageError as err:
            return web.json_response({"error": str(err)}, status=400)

    async def body(request: web.Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except ValueError as err:
            raise MessageError("Request body must be JSON") from err
        if not isinstance(data, dict):
            raise MessageError("Request body must be a JSON object")
        return data

    async def index(_: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    async def status(_: web.Request) -> web.Response:
        return web.json_response(bridge.status())

    async def list_sets(_: web.Request) -> web.Response:
        return web.json_response(store.list_sets())

    async def save_set(request: web.Request) -> web.Response:
        previous = request.match_info.get("set_id")
        data = await body(request)
        return web.json_response(await store.save_set(data, previous_id=previous))

    async def delete_set(request: web.Request) -> web.Response:
        await store.delete_set(request.match_info["set_id"])
        return web.json_response({"ok": True})

    async def send_set(request: web.Request) -> web.Response:
        data = await body(request)
        result = await bridge.send_message(
            {"set_id": request.match_info["set_id"], "channel_id": data.get("channel_id") or None},
            source="editor",
        )
        return web.json_response(result)

    async def history(_: web.Request) -> web.Response:
        return web.json_response(store.history)

    async def update_record(request: web.Request) -> web.Response:
        record = store.find_record(request.match_info["record_id"])
        data = await body(request)
        set_id = data.get("set_id") or record.get("set_id")
        if not set_id:
            raise MessageError("This message was not sent from a set; choose a set to update it with")
        result = await bridge.edit_message(
            {"message_id": record["message_id"], "channel_id": record["channel_id"], "set_id": set_id},
            source="editor",
        )
        return web.json_response(result)

    async def forget_record(request: web.Request) -> web.Response:
        await store.delete_record(request.match_info["record_id"])
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[guard], client_max_size=1024 * 1024)
    app.router.add_get("/", index)
    app.router.add_static("/static/", STATIC)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/sets", list_sets)
    app.router.add_post("/api/sets", save_set)
    app.router.add_put("/api/sets/{set_id}", save_set)
    app.router.add_delete("/api/sets/{set_id}", delete_set)
    app.router.add_post("/api/sets/{set_id}/send", send_set)
    app.router.add_get("/api/history", history)
    app.router.add_post("/api/history/{record_id}/update", update_record)
    app.router.add_delete("/api/history/{record_id}", forget_record)
    return app
