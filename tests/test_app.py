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

from app.bot import Bridge, build_view, button_custom_id  # noqa: E402
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
FORM = {"custom_id": "alarm_reason", "title": "Confirm action", "label": "Reason", "placeholder": "Enter your message"}
FORM_BUTTON = {**SET["buttons"][0], "form": FORM}


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


def test_form_validation_and_rendering():
    item = normalize_set({**SET, "buttons": [FORM_BUTTON]})
    assert item["buttons"][0]["form"] == FORM
    spec = render(item)
    spec["buttons"][0]["form"]["title"] = "Changed"
    assert item["buttons"][0]["form"]["title"] == FORM["title"]
    assert render(None, buttons=[FORM_BUTTON])["buttons"][0]["form"] == FORM


@pytest.mark.parametrize("form", [
    "not an object", {}, {"title": "x", "label": ""},
    {**FORM, "title": "x" * 46}, {**FORM, "label": "x" * 46},
    {**FORM, "placeholder": "x" * 101},
    {**FORM, "custom_id": ""}, {**FORM, "custom_id": "x" * 101},
])
def test_invalid_form_rejected(form):
    with pytest.raises(MessageError, match="Form"):
        normalize_set({**SET, "buttons": [{**FORM_BUTTON, "form": form}]})


def test_link_forms_and_reserved_ids_rejected():
    with pytest.raises(MessageError, match="link buttons"):
        normalize_set({**SET, "buttons": [{**SET["buttons"][1], "form": FORM}]})
    with pytest.raises(MessageError, match="reserved"):
        normalize_set({**SET, "buttons": [{**FORM_BUTTON, "custom_id": "db_form:test"}]})


async def test_form_wire_id_supports_full_length_custom_ids():
    button = {**FORM_BUTTON, "custom_id": "x" * 100}
    item = normalize_set({**SET, "buttons": [button]})
    view = build_view(item["buttons"])
    assert view.children[0].custom_id == button_custom_id(button)
    assert len(view.children[0].custom_id) <= 100
    assert build_view(normalize_set(SET)["buttons"]).children[0].custom_id == "alarm_1"


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
        if args and name in ("send_message", "send_modal"):
            kwargs["content" if name == "send_message" else "modal"] = args[0]
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
        response=SimpleNamespace(defer=rec("defer"), send_message=rec("send_message"),
                                 edit_message=rec("edit_message"), send_modal=rec("send_modal")),
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


async def open_form(bridge):
    record = bridge.store.find_by_message("999")
    await bridge.store.update_record(record, payload=render(None, buttons=[FORM_BUTTON]))
    interaction, calls = make_interaction()
    interaction.data["custom_id"] = button_custom_id(FORM_BUTTON)
    interaction.message.components[0].children[0].custom_id = interaction.data["custom_id"]
    await bridge.handle_interaction(interaction)
    return interaction, calls


def make_submission(custom_id, text="Please turn on the lights", user_id=42):
    interaction, calls = make_interaction(user_id=user_id)
    interaction.id = 1002
    interaction.type = discord.InteractionType.modal_submit
    interaction.data = {
        "custom_id": custom_id,
        "components": [{"type": 18, "component": {"type": 4, "custom_id": "message", "value": text}}],
    }
    return interaction, calls


async def test_form_opens_then_submission_fires_event_and_can_reply(bridge):
    _, calls = await open_form(bridge)
    assert len(bridge.ha.events) == 1
    assert bridge.ha.events[0]["custom_id"] == "alarm_1"
    assert bridge.ha.events[0]["interaction_type"] == "button"
    assert bridge.pending["1001"].ack == "modal"
    assert [name for name, _ in calls] == ["send_modal"]
    modal = calls[0][1]["modal"]
    assert modal.title == FORM["title"]
    text_input = modal.children[0].component
    assert text_input.custom_id == "message" and modal.children[0].text == FORM["label"]
    assert text_input.style == discord.TextStyle.paragraph
    assert text_input.required and text_input.min_length == 1 and text_input.max_length == 4000
    custom_id = next(iter(bridge.forms))
    assert bridge.forms[custom_id].event["button_label"] == "Alarm 1"
    # Opening the form must not race with auto-acknowledgement.
    await asyncio.sleep(0.1)
    assert [name for name, _ in calls] == ["send_modal"]

    text = "Please turn on the lights\nAfter sunset"
    submission, replies = make_submission(custom_id, text)
    await bridge.handle_interaction(submission)
    event = bridge.ha.events[1]
    assert event["interaction_type"] == "modal_submit"
    assert event["custom_id"] == FORM["custom_id"]
    assert event["button_custom_id"] == "alarm_1"
    assert event["values"] == {"message": text}
    assert event["interaction_id"] == "1002"
    assert event["message_id"] == "999" and event["set_id"] == "alarm_panel"
    assert event["user_id"] == "42" and event["button_label"] == "Alarm 1"
    await bridge.respond({"interaction_id": "1002", "type": "reply", "content": "Received", "ephemeral": True})
    assert replies[0] == ("send_message", {"content": "Received", "ephemeral": True})
    await bridge.handle_interaction(submission)
    assert len(bridge.ha.events) == 2
    assert "expired" in replies[-1][1]["content"]


async def test_form_button_click_can_still_trigger_a_reply(bridge):
    _, calls = await open_form(bridge)
    result = await bridge.respond({
        "interaction_id": bridge.ha.events[0]["interaction_id"],
        "type": "reply", "content": "Please complete the form", "ephemeral": True,
    })
    assert result["acknowledged"] == "modal"
    assert [name for name, _ in calls] == ["send_modal", "followup.send"]
    assert len(bridge.forms) == 1


@pytest.mark.parametrize(("text", "legacy_rows"), [("x", False), ("x" * 4000, False), ("Legacy", True)])
async def test_form_accepts_length_boundaries_and_action_rows(bridge, text, legacy_rows):
    await open_form(bridge)
    submission, _ = make_submission(next(iter(bridge.forms)), text)
    if legacy_rows:
        submission.data["components"] = [{
            "type": 1, "components": [submission.data["components"][0]["component"]],
        }]
    await bridge.handle_interaction(submission)
    assert bridge.ha.events[1]["values"]["message"] == text


@pytest.mark.parametrize("failure", ["foreign_user", "disallowed", "expired", "restart", "empty", "too_long", "missing_input", "offline"])
async def test_form_rejects_invalid_submissions(bridge, failure):
    await open_form(bridge)
    custom_id = next(iter(bridge.forms))
    submission, replies = make_submission(custom_id, user_id=43 if failure == "foreign_user" else 42)
    if failure == "disallowed":
        bridge.allowed_users = {"1"}
    elif failure == "expired":
        bridge.forms[custom_id].received -= 901
    elif failure == "restart":
        bridge.forms.clear()
    elif failure in ("empty", "too_long"):
        submission.data["components"][0]["component"]["value"] = "  " if failure == "empty" else "x" * 4001
    elif failure == "missing_input":
        submission.data["components"] = []
    elif failure == "offline":
        bridge.ha.connected = False
    await bridge.handle_interaction(submission)
    assert len(bridge.ha.events) == 1 and "1002" not in bridge.pending
    assert replies[0][0] == "send_message" and replies[0][1]["ephemeral"] is True


async def test_form_config_survives_reload_but_fails_closed_when_forgotten(bridge):
    await open_form(bridge)
    bridge.store.load()
    interaction, calls = make_interaction()
    interaction.data["custom_id"] = button_custom_id(FORM_BUTTON)
    await bridge.handle_interaction(interaction)
    assert calls[0][0] == "send_modal"
    await bridge.store.delete_record(bridge.store.find_by_message("999")["id"])
    interaction, calls = make_interaction()
    interaction.data["custom_id"] = button_custom_id(FORM_BUTTON)
    await bridge.handle_interaction(interaction)
    assert calls[0][0] == "send_message"
    assert "no longer available" in calls[0][1]["content"]
    assert len(bridge.ha.events) == 2


async def test_form_configuration_saved_via_editor_api(bridge):
    async with TestClient(TestServer(create_app(bridge, ingress_only=False))) as client:
        response = await client.put("/api/sets/alarm_panel", json={**SET, "buttons": [FORM_BUTTON]})
        assert response.status == 200
        assert (await response.json())["buttons"][0]["form"] == FORM
        bridge.store.load()
        assert bridge.store.get_set("alarm_panel")["buttons"][0]["form"] == FORM


@pytest.mark.parametrize("kind", ["reply", "followup", "update_message"])
async def test_form_buttons_in_responses_are_remembered(bridge, kind):
    interaction, _ = make_interaction()
    message = SimpleNamespace(
        id=555, channel=SimpleNamespace(id=interaction.channel_id),
        guild=None, jump_url="https://discord.com/channels/@me/123/555",
    )

    async def sent_message(*args, **kwargs):
        return message

    interaction.original_response = sent_message
    interaction.followup.send = sent_message
    await bridge.handle_interaction(interaction)
    data = {"interaction_id": "1001", "buttons": [FORM_BUTTON]}
    if kind == "followup":
        await bridge.followup(data)
    else:
        await bridge.respond({**data, "type": kind})
    record = bridge.store.find_by_message("999" if kind == "update_message" else "555")
    assert record["payload"]["buttons"][0]["form"] == FORM


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


async def test_ha_link_round_trip(caplog):
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
                                     "event": {"type": "command", "request_id": "r2", "command": "fail", "data": {},
                                               "origin": "automation 'Doorbell' (automation.doorbell)"}})
        result = await asyncio.wait_for(state["results"].get(), 5)
        assert result["success"] is False and result["error"] == "nope"
        assert "fail requested by automation 'Doorbell' (automation.doorbell) failed: nope" in caplog.text
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
