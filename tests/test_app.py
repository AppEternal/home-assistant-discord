"""Tests for the Discord Bridge app. Run from the repo root: pytest tests"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "discord_bridge"))

from app.bot import Bridge  # noqa: E402
from app import __main__ as app_main  # noqa: E402
from app.ha_link import HALink  # noqa: E402
from app.messages import MessageError, normalize_set, render  # noqa: E402
from app.store import Store  # noqa: E402
from app.web import create_app  # noqa: E402

SET = {
    "id": "alarm_panel",
    "name": "Alarm panel",
    "channel_id": "123456789012345678",
    "content": "Alarm controls",
    "embed": {"title": "Alarms", "description": "", "color": "#da373c"},
    "buttons": [
        {"label": "Alarm 1", "custom_id": "alarm_1", "style": "danger"},
        {"label": "Docs", "style": "link", "url": "https://example.com"},
    ],
}


# ------------------------------------------------------------------ messages
def test_normalize_set_validates_buttons():
    item = normalize_set(SET)
    assert item["embed"]["color"] == "#DA373C"
    assert item["buttons"][1] == {"label": "Docs", "style": "link", "emoji": "", "disabled": False, "url": "https://example.com"}

    with pytest.raises(MessageError, match="Duplicate"):
        normalize_set({**SET, "buttons": [SET["buttons"][0], SET["buttons"][0]]})
    with pytest.raises(MessageError, match="custom ID"):
        normalize_set({**SET, "buttons": [{"label": "x", "style": "primary"}]})
    with pytest.raises(MessageError, match="Set ID"):
        normalize_set({**SET, "id": "Bad ID"})


def test_render_overrides_and_partial_edits():
    item = normalize_set(SET)
    spec = render(item, content="Override")
    assert spec["content"] == "Override" and len(spec["buttons"]) == 2
    assert render(None, content="only text", partial=True) == {"content": "only text"}
    assert render(None, buttons=[], partial=True) == {"buttons": []}
    with pytest.raises(MessageError):
        render(None, partial=True)
    with pytest.raises(MessageError):
        render(None, content="  ")


# -------------------------------------------------------------- interactions
class FakeHA:
    connected = True
    error = None

    def __init__(self):
        self.events = []

    async def send_interaction(self, data):
        self.events.append(data)


def make_interaction(user_id=42, message_id=999):
    calls = []

    async def record(name, *args, **kwargs):
        calls.append((name, kwargs))
        return SimpleNamespace(id=555)

    def rec(name):
        return lambda *a, **k: record(name, *a, **k)

    button = SimpleNamespace(custom_id="alarm_1", label="Alarm 1")
    interaction = SimpleNamespace(
        id=1001,
        type=discord.InteractionType.component,
        data={"custom_id": "alarm_1", "component_type": 2},
        user=SimpleNamespace(id=user_id, name="bob", display_name="Bob"),
        message=SimpleNamespace(id=message_id, components=[SimpleNamespace(children=[button])], edit=rec("message.edit")),
        channel_id=123456789012345678,
        guild_id=7,
        response=SimpleNamespace(defer=rec("defer"), send_message=rec("send_message"), edit_message=rec("edit_message")),
        followup=SimpleNamespace(send=rec("followup.send")),
        edit_original_response=rec("edit_original_response"),
    )
    return interaction, calls


@pytest.fixture
async def bridge(tmp_path):
    store = Store(tmp_path)
    store.load()
    await store.save_set(SET)
    await store.add_record(set_id="alarm_panel", channel_id=SET["channel_id"], message_id="999", payload={})
    bridge = Bridge({"auto_ack_seconds": 0.05, "auto_ack_type": "defer_update", "allowed_user_ids": []}, store)
    bridge.ha = FakeHA()
    return bridge


async def test_click_fires_event_and_custom_ack_wins(bridge):
    interaction, calls = make_interaction()
    await bridge.handle_interaction(interaction)
    event = bridge.ha.events[0]
    assert event["custom_id"] == "alarm_1"
    assert event["button_label"] == "Alarm 1"
    assert event["set_id"] == "alarm_panel"
    assert event["user_display_name"] == "Bob"

    result = await bridge.respond({"interaction_id": event["interaction_id"], "type": "reply", "content": "Armed", "ephemeral": True})
    assert result["acknowledged"] == "reply"
    await asyncio.sleep(0.1)
    assert [c[0] for c in calls] == ["send_message"]
    assert calls[0][1]["ephemeral"] is True and calls[0][1]["content"] == "Armed"

    # Further replies become follow-ups.
    await bridge.followup({"interaction_id": "1001", "content": "Done"})
    assert calls[-1][0] == "followup.send"


async def test_auto_ack_then_late_responses(bridge):
    interaction, calls = make_interaction()
    await bridge.handle_interaction(interaction)
    await asyncio.sleep(0.1)
    assert calls == [("defer", {})]

    await bridge.respond({"interaction_id": "1001", "type": "update_message", "set_id": "alarm_panel"})
    assert calls[-1][0] == "edit_original_response"
    assert "view" in calls[-1][1] and calls[-1][1]["content"] == "Alarm controls"

    await bridge.respond({"interaction_id": "1001", "type": "reply", "content": "Late"})
    assert calls[-1][0] == "followup.send"
    record = bridge.store.find_by_message("999")
    assert record["payload"]["content"] == "Alarm controls" and "edited_at" in record


async def test_defer_reply_is_replaced_by_first_followup(bridge):
    interaction, calls = make_interaction()
    await bridge.handle_interaction(interaction)
    await bridge.respond({"interaction_id": "1001", "type": "defer_reply", "ephemeral": True})
    assert calls[0] == ("defer", {"thinking": True, "ephemeral": True})
    await bridge.followup({"interaction_id": "1001", "content": "Done"})
    await bridge.followup({"interaction_id": "1001", "content": "Again"})
    assert [c[0] for c in calls] == ["defer", "edit_original_response", "followup.send"]


async def test_not_allowed_user_gets_no_event(bridge):
    bridge.allowed_users = {"1"}
    interaction, calls = make_interaction(user_id=42)
    await bridge.handle_interaction(interaction)
    assert bridge.ha.events == []
    assert calls[0][0] == "send_message" and calls[0][1]["ephemeral"] is True


async def test_unknown_interaction_errors(bridge):
    with pytest.raises(MessageError, match="expired"):
        await bridge.respond({"interaction_id": "nope", "type": "defer_update"})


# ----------------------------------------------------------------- editor
@pytest.mark.parametrize(
    ("supervisor_token", "allow_any_ip", "expected_host"),
    [
        (None, None, "127.0.0.1"),
        ("supervisor-token", None, "0.0.0.0"),
        (None, "1", "0.0.0.0"),
        ("supervisor-token", "1", "0.0.0.0"),
    ],
)
async def test_editor_bind_address(monkeypatch, tmp_path, supervisor_token, allow_any_ip, expected_host):
    for name in ("SUPERVISOR_TOKEN", "ALLOW_ANY_IP", "HA_TOKEN", "DISCORD_BOT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_FOLDER", str(tmp_path))
    monkeypatch.setenv("OPTIONS_FILE", str(tmp_path / "options.json"))
    if supervisor_token:
        monkeypatch.setenv("SUPERVISOR_TOKEN", supervisor_token)
    if allow_any_ip:
        monkeypatch.setenv("ALLOW_ANY_IP", allow_any_ip)

    started = asyncio.Event()
    hosts = []

    class FakeSite:
        def __init__(self, runner, host, port):
            hosts.append(host)

        async def start(self):
            started.set()

    async def idle_link(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(app_main.web, "TCPSite", FakeSite)
    monkeypatch.setattr(HALink, "run", idle_link)
    task = asyncio.create_task(app_main.main())
    try:
        await asyncio.wait_for(started.wait(), 5)
        assert hosts == [expected_host]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.parametrize(("ingress_only", "expected_status"), [(True, 403), (False, 200)])
async def test_editor_ingress_guard(bridge, ingress_only, expected_status):
    async with TestClient(TestServer(create_app(bridge, ingress_only=ingress_only))) as client:
        response = await client.get("/api/status")
        assert response.status == expected_status


# -------------------------------------------------------------- HA protocol
async def fake_home_assistant(known_commands=True):
    """A minimal Home Assistant websocket that behaves like the integration."""
    state = {"interactions": [], "results": asyncio.Queue(), "ws": None, "sub": None, "connected": asyncio.Event()}

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required"})
        auth = await ws.receive_json()
        if auth["access_token"] != "token":
            await ws.send_json({"type": "auth_invalid", "message": "bad"})
            return ws
        await ws.send_json({"type": "auth_ok"})
        async for msg in ws:
            data = msg.json()
            if not known_commands:
                await ws.send_json({"id": data["id"], "type": "result", "success": False,
                                    "error": {"code": "unknown_command", "message": "Unknown command."}})
                continue
            if data["type"] == "discord_bridge/connect":
                state["ws"], state["sub"] = ws, data["id"]
                state["connected"].set()
            elif data["type"] == "discord_bridge/interaction":
                state["interactions"].append(data["data"])
            elif data["type"] == "discord_bridge/result":
                await state["results"].put(data)
            await ws.send_json({"id": data["id"], "type": "result", "success": True, "result": None})
        return ws

    app = web.Application()
    app.router.add_get("/api/websocket", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"ws://127.0.0.1:{port}/api/websocket", state


async def test_ha_link_round_trip():
    runner, url, state = await fake_home_assistant()
    seen = []

    async def handler(command, data):
        seen.append((command, data))
        if command == "fail":
            raise MessageError("nope")
        return {"ok": True}

    link = HALink(url, "token", handler, retry_delay=0.1)
    task = asyncio.create_task(link.run())
    try:
        await asyncio.wait_for(state["connected"].wait(), 5)
        for _ in range(50):
            if link.connected:
                break
            await asyncio.sleep(0.02)
        assert link.connected

        await link.send_interaction({"custom_id": "alarm_1"})
        await state["ws"].send_json({"id": state["sub"], "type": "event",
                                     "event": {"type": "command", "request_id": "r1", "command": "respond", "data": {"x": 1}}})
        result = await asyncio.wait_for(state["results"].get(), 5)
        assert result["request_id"] == "r1" and result["success"] is True and result["result"] == {"ok": True}
        assert seen == [("respond", {"x": 1})]
        assert state["interactions"] == [{"custom_id": "alarm_1"}]

        await state["ws"].send_json({"id": state["sub"], "type": "event",
                                     "event": {"type": "command", "request_id": "r2", "command": "fail", "data": {}}})
        result = await asyncio.wait_for(state["results"].get(), 5)
        assert result["success"] is False and result["error"] == "nope"
    finally:
        task.cancel()
        await runner.cleanup()


async def test_ha_link_reports_missing_integration():
    runner, url, _ = await fake_home_assistant(known_commands=False)
    link = HALink(url, "token", lambda c, d: None, retry_delay=0.1)
    task = asyncio.create_task(link.run())
    try:
        for _ in range(100):
            if link.error and "not installed" in link.error:
                break
            await asyncio.sleep(0.02)
        assert "not installed" in link.error
        assert not link.connected
    finally:
        task.cancel()
        await runner.cleanup()
