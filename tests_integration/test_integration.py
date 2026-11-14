"""Tests for the Discord Bridge integration.

Needs pytest-homeassistant-custom-component (Linux/WSL); run from the repo root:
    pytest tests_integration
"""

from __future__ import annotations

import asyncio

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

# Import before the hass fixture puts its own testing_config/custom_components on sys.path.
import custom_components.discord_bridge  # noqa: F401, E402

DOMAIN = "discord_bridge"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
async def entry(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_config_flow_creates_single_entry(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "abort"


async def test_app_round_trip(hass: HomeAssistant, entry, hass_ws_client):
    sensor = "binary_sensor.discord_bridge_app_connected"
    assert hass.states.get(sensor).state == "off"
    with pytest.raises(HomeAssistantError, match="not connected"):
        await hass.services.async_call(
            DOMAIN, "respond", {"interaction_id": "1", "type": "defer_update"}, blocking=True
        )

    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "discord_bridge/connect", "version": 1})
    assert (await ws.receive_json())["success"]
    await hass.async_block_till_done()
    assert hass.states.get(sensor).state == "on"

    events = async_capture_events(hass, "discord_bridge_interaction")
    await ws.send_json({"id": 2, "type": "discord_bridge/interaction", "data": {"custom_id": "alarm_1", "interaction_id": "77"}})
    assert (await ws.receive_json())["success"]
    await hass.async_block_till_done()
    assert events[0].data == {"custom_id": "alarm_1", "interaction_id": "77"}

    call = asyncio.create_task(
        hass.services.async_call(
            DOMAIN, "respond",
            {"interaction_id": 77, "type": "reply", "content": "Hi", "ephemeral": True},
            blocking=True, return_response=True,
        )
    )
    msg = await ws.receive_json()
    assert msg["id"] == 1 and msg["type"] == "event"
    command = msg["event"]
    assert command["command"] == "respond"
    assert command["data"] == {"interaction_id": "77", "type": "reply", "content": "Hi", "ephemeral": True}
    await ws.send_json({"id": 3, "type": "discord_bridge/result", "request_id": command["request_id"],
                        "success": True, "result": {"acknowledged": "reply"}})
    assert (await ws.receive_json())["success"]
    assert await call == {"acknowledged": "reply"}

    call = asyncio.create_task(
        hass.services.async_call(DOMAIN, "send_message", {"set_id": "nope"}, blocking=True)
    )
    command = (await ws.receive_json())["event"]
    await ws.send_json({"id": 4, "type": "discord_bridge/result", "request_id": command["request_id"],
                        "success": False, "error": "Unknown message set: nope"})
    await ws.receive_json()
    with pytest.raises(HomeAssistantError, match="Unknown message set"):
        await call

    await ws.close()
    await hass.async_block_till_done()
    assert hass.states.get(sensor).state == "off"


async def test_unload_removes_services(hass: HomeAssistant, entry):
    assert hass.services.has_service(DOMAIN, "send_message")
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not hass.services.has_service(DOMAIN, "send_message")
