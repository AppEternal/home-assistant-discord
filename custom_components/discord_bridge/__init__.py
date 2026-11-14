"""Discord Bridge: connects the Discord Bridge app (add-on) to automations.

The app opens a websocket to Home Assistant and subscribes with
``discord_bridge/connect``. Button clicks arrive as ``discord_bridge/interaction``
and are fired as ``discord_bridge_interaction`` events. The actions of this
integration are sent to the app through the subscription and answered with
``discord_bridge/result``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import OrderedDict
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import (
    Context,
    Event,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType

from .const import COMMAND_TIMEOUT, DOMAIN, EVENT_INTERACTION, SIGNAL_CONNECTION

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

RESPOND_TYPES = ["defer_update", "defer_reply", "reply", "update_message"]

# Remembered contexts of recent automation/script runs and Discord clicks, used to
# tell the app who triggered an action. Old entries are dropped first.
ORIGIN_CACHE_SIZE = 500
ORIGIN_EVENTS = {"automation_triggered": "automation", "script_started": "script"}

MESSAGE_FIELDS = {
    vol.Optional("set_id"): cv.string,
    vol.Optional("content"): cv.string,
    vol.Optional("embed"): vol.Any(None, dict),
    vol.Optional("buttons"): vol.Any(None, [dict]),
}

SERVICE_SCHEMAS = {
    "respond": vol.Schema(
        {
            vol.Required("interaction_id"): cv.string,
            vol.Required("type"): vol.In(RESPOND_TYPES),
            vol.Optional("ephemeral", default=False): cv.boolean,
            **MESSAGE_FIELDS,
        }
    ),
    "followup": vol.Schema(
        {
            vol.Required("interaction_id"): cv.string,
            vol.Optional("ephemeral", default=False): cv.boolean,
            **MESSAGE_FIELDS,
        }
    ),
    "send_message": vol.Schema({vol.Optional("channel_id"): cv.string, **MESSAGE_FIELDS}),
    "edit_message": vol.Schema(
        {
            vol.Required("message_id"): cv.string,
            vol.Optional("channel_id"): cv.string,
            **MESSAGE_FIELDS,
        }
    ),
}


class DiscordBridge:
    """Tracks the websocket connection of the app and pending requests."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.connection: websocket_api.ActiveConnection | None = None
        self.subscription_id: int | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self.origins: OrderedDict[str, list[tuple[str, str | None]]] = OrderedDict()

    @callback
    def remember_origin(self, context: Context, label: str) -> None:
        entries = self.origins.setdefault(context.id, [])
        entries.append((label, context.parent_id))
        self.origins.move_to_end(context.id)
        while len(self.origins) > ORIGIN_CACHE_SIZE:
            self.origins.popitem(last=False)

    @callback
    def remember_run(self, event: Event) -> None:
        kind = ORIGIN_EVENTS.get(event.event_type, event.event_type)
        name = event.data.get("name") or event.data.get("entity_id")
        self.remember_origin(event.context, f"{kind} '{name}' ({event.data.get('entity_id')})")

    async def async_describe_origin(self, context: Context) -> str:
        """Follow the context chain, e.g. automation <- Discord click, or script <- user."""
        labels: list[str] = []
        seen: set[str] = set()
        queue = [context.id, context.parent_id]
        while queue and len(seen) < 10:
            context_id = queue.pop(0)
            if not context_id or context_id in seen:
                continue
            seen.add(context_id)
            for label, parent_id in reversed(self.origins.get(context_id, [])):
                if label not in labels:
                    labels.append(label)
                queue.append(parent_id)
        if context.user_id:
            user = await self.hass.auth.async_get_user(context.user_id)
            labels.append(f"user '{user.name}'" if user else f"user {context.user_id}")
        return ", triggered by ".join(labels) or "unknown (not a user, automation or script)"

    @property
    def connected(self) -> bool:
        return self.connection is not None

    @callback
    def attach(self, connection: websocket_api.ActiveConnection, msg_id: int) -> None:
        self.connection = connection
        self.subscription_id = msg_id
        _LOGGER.info("Discord Bridge app connected")
        async_dispatcher_send(self.hass, SIGNAL_CONNECTION)

    @callback
    def detach(self, connection: websocket_api.ActiveConnection, msg_id: int) -> None:
        if self.connection is not connection or self.subscription_id != msg_id:
            return
        self.connection = None
        self.subscription_id = None
        for future in self.pending.values():
            if not future.done():
                future.set_exception(HomeAssistantError("The Discord Bridge app disconnected"))
        self.pending.clear()
        _LOGGER.info("Discord Bridge app disconnected")
        async_dispatcher_send(self.hass, SIGNAL_CONNECTION)

    @callback
    def resolve(self, request_id: str, success: bool, result: Any, error: str | None) -> bool:
        future = self.pending.pop(request_id, None)
        if future is None or future.done():
            return False
        if success:
            future.set_result(result or {})
        else:
            future.set_exception(HomeAssistantError(error or "The Discord Bridge app reported an error"))
        return True

    async def async_call(self, command: str, data: dict[str, Any], origin: str | None = None) -> dict[str, Any]:
        if self.connection is None or self.subscription_id is None:
            raise HomeAssistantError(
                "The Discord Bridge app is not connected. Is the app running?"
            )
        request_id = uuid.uuid4().hex
        future: asyncio.Future = self.hass.loop.create_future()
        self.pending[request_id] = future
        self.connection.send_message(
            websocket_api.event_message(
                self.subscription_id,
                {"type": "command", "request_id": request_id, "command": command, "data": data, "origin": origin},
            )
        )
        try:
            async with asyncio.timeout(COMMAND_TIMEOUT):
                return await future
        except TimeoutError as err:
            raise HomeAssistantError(f"The Discord Bridge app did not answer {command} in time") from err
        finally:
            self.pending.pop(request_id, None)


def _bridge(hass: HomeAssistant) -> DiscordBridge | None:
    return hass.data.get(DOMAIN)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): "discord_bridge/connect", vol.Optional("version"): int}
)
@callback
def ws_connect(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    bridge = _bridge(hass)
    if bridge is None:
        connection.send_error(msg["id"], "not_loaded", "Discord Bridge integration is not set up")
        return
    msg_id = msg["id"]
    bridge.attach(connection, msg_id)
    connection.subscriptions[msg_id] = lambda: bridge.detach(connection, msg_id)
    connection.send_result(msg_id)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "discord_bridge/result",
        vol.Required("request_id"): str,
        vol.Required("success"): bool,
        vol.Optional("result"): vol.Any(None, dict),
        vol.Optional("error"): vol.Any(None, str),
    }
)
@callback
def ws_result(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    bridge = _bridge(hass)
    if bridge is not None:
        bridge.resolve(msg["request_id"], msg["success"], msg.get("result"), msg.get("error"))
    connection.send_result(msg["id"])


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): "discord_bridge/interaction", vol.Required("data"): dict}
)
@callback
def ws_interaction(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    bridge = _bridge(hass)
    if bridge is None:
        connection.send_error(msg["id"], "not_loaded", "Discord Bridge integration is not set up")
        return
    data = msg["data"]
    context = Context()
    if data.get("user_id"):
        name = data.get("user_name") or "?"
        bridge.remember_origin(context, f"Discord user {name} ({data['user_id']})")
    hass.bus.async_fire(EVENT_INTERACTION, data, context=context)
    connection.send_result(msg["id"])


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    websocket_api.async_register_command(hass, ws_connect)
    websocket_api.async_register_command(hass, ws_result)
    websocket_api.async_register_command(hass, ws_interaction)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    bridge = DiscordBridge(hass)
    hass.data[DOMAIN] = bridge

    def make_handler(command: str):
        async def handle(call: ServiceCall) -> ServiceResponse:
            current = _bridge(hass)
            if current is None:
                raise HomeAssistantError("Discord Bridge integration is not set up")
            origin = await current.async_describe_origin(call.context)
            result = await current.async_call(command, dict(call.data), origin)
            return result if call.return_response else None

        return handle

    for event_type in ORIGIN_EVENTS:
        entry.async_on_unload(hass.bus.async_listen(event_type, bridge.remember_run))

    for command, schema in SERVICE_SCHEMAS.items():
        hass.services.async_register(
            DOMAIN, command, make_handler(command), schema=schema, supports_response=SupportsResponse.OPTIONAL
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        bridge = hass.data.pop(DOMAIN, None)
        if bridge is not None and bridge.connection is not None and bridge.subscription_id is not None:
            unsub = bridge.connection.subscriptions.pop(bridge.subscription_id, None)
            if unsub:
                unsub()
            # Ask the app to reconnect, so it gets a clear error until the entry is back.
            bridge.connection.send_error(bridge.subscription_id, "not_loaded", "Integration unloaded")
        for command in SERVICE_SCHEMAS:
            hass.services.async_remove(DOMAIN, command)
    return unloaded
