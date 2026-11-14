"""WebSocket link from the app to the Discord Bridge integration in Home Assistant."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from .messages import MessageError

_LOGGER = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
CommandHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class HALink:
    """Keeps a websocket open to Home Assistant.

    The app subscribes with ``discord_bridge/connect``; the integration pushes
    action requests through that subscription and the app answers them with
    ``discord_bridge/result``. Button clicks are sent with
    ``discord_bridge/interaction`` and fired as Home Assistant events.
    """

    def __init__(self, url: str, token: str, handler: CommandHandler, retry_delay: float = 5.0) -> None:
        self.url = url
        self.token = token
        self.handler = handler
        self.base_retry_delay = retry_delay
        self.retry_delay = retry_delay
        self.connected = False
        self.error: str | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ids = itertools.count(1)
        self._send_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    await self._session(session)
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001
                    if self.error is None or str(err) not in self.error:
                        _LOGGER.warning("Home Assistant connection problem: %s", err)
                    self.error = self.error or f"Connection problem: {err}"
                finally:
                    self.connected = False
                    self._ws = None
                await asyncio.sleep(self.retry_delay)

    async def _session(self, session: aiohttp.ClientSession) -> None:
        async with session.ws_connect(self.url, heartbeat=30, max_msg_size=0) as ws:
            first = await ws.receive_json()
            if first.get("type") != "auth_required":
                raise ConnectionError(f"Unexpected first message: {first}")
            await ws.send_json({"type": "auth", "access_token": self.token})
            auth = await ws.receive_json()
            if auth.get("type") != "auth_ok":
                raise ConnectionError(f"Authentication failed: {auth.get('message', auth)}")

            self._ids = itertools.count(1)
            connect_id = next(self._ids)
            await ws.send_json({"id": connect_id, "type": "discord_bridge/connect", "version": PROTOCOL_VERSION})
            self._ws = ws

            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                        break
                    continue
                payload = json.loads(msg.data)
                for item in payload if isinstance(payload, list) else [payload]:
                    await self._on_message(connect_id, item)
            raise ConnectionError("Home Assistant closed the connection")

    async def _on_message(self, connect_id: int, msg: dict[str, Any]) -> None:
        if msg.get("id") == connect_id and msg.get("type") == "result":
            if msg.get("success"):
                self.connected = True
                self.error = None
                self.retry_delay = self.base_retry_delay
                _LOGGER.info("Connected to the Discord Bridge integration in Home Assistant")
                return
            error = msg.get("error") or {}
            if error.get("code") == "unknown_command":
                self.error = (
                    "The Discord Bridge integration is not installed or not set up in Home Assistant"
                )
            else:
                self.error = f"Home Assistant refused the connection: {error.get('message', error)}"
            _LOGGER.warning(self.error)
            if self._ws is not None:
                await self._ws.close()
            self.retry_delay = max(self.retry_delay, 30)
            return
        if msg.get("id") == connect_id and msg.get("type") == "event":
            event = msg.get("event") or {}
            if event.get("type") == "command":
                task = asyncio.create_task(self._run_command(event))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            return
        if msg.get("type") == "result" and not msg.get("success"):
            _LOGGER.warning("Home Assistant rejected a message: %s", msg.get("error"))

    async def _run_command(self, event: dict[str, Any]) -> None:
        request_id = event.get("request_id")
        command = event.get("command", "")
        reply: dict[str, Any] = {"type": "discord_bridge/result", "request_id": request_id}
        try:
            reply["result"] = await self.handler(command, event.get("data") or {})
            reply["success"] = True
        except MessageError as err:
            reply.update(success=False, error=str(err))
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Command %s failed", command)
            reply.update(success=False, error=f"Unexpected error: {err}")
        await self._send(reply)

    async def _send(self, message: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            raise ConnectionError("Not connected to Home Assistant")
        async with self._send_lock:
            await ws.send_json({"id": next(self._ids), **message})

    async def send_interaction(self, data: dict[str, Any]) -> None:
        await self._send({"type": "discord_bridge/interaction", "data": data})
