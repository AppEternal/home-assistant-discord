"""Discord bot: sends message sets and turns button clicks into Home Assistant events."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import discord

from .messages import FORM_PREFIX, MessageError, render, validate_channel_id
from .store import Store

if TYPE_CHECKING:
    from .ha_link import HALink

_LOGGER = logging.getLogger(__name__)

INTERACTION_LIFETIME = 15 * 60
RESPOND_TYPES = ("defer_update", "defer_reply", "reply", "update_message")
AUTO_ACK = {
    "defer_update": ("defer_update", False),
    "defer_reply": ("defer_reply", False),
    "defer_reply_ephemeral": ("defer_reply", True),
}
BUTTON_STYLES = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
    "link": discord.ButtonStyle.link,
}


def button_custom_id(button: dict[str, Any]) -> str | None:
    custom_id = button.get("custom_id")
    if custom_id and button.get("form"):
        return FORM_PREFIX + hashlib.sha256(custom_id.encode()).hexdigest()
    return custom_id


def build_view(buttons: list[dict[str, Any]]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for button in buttons:
        view.add_item(
            discord.ui.Button(
                style=BUTTON_STYLES[button["style"]],
                label=button.get("label") or None,
                custom_id=button_custom_id(button),
                url=button.get("url"),
                emoji=button.get("emoji") or None,
                disabled=button.get("disabled", False),
            )
        )
    # A stopped view is still serialized, but discord.py will not dispatch clicks
    # to it; all clicks are handled in on_interaction instead.
    view.stop()
    return view


def build_embed(embed: dict[str, Any] | None) -> discord.Embed | None:
    if not embed:
        return None
    color = embed.get("color")
    return discord.Embed(
        title=embed.get("title") or None,
        description=embed.get("description") or None,
        color=int(color.lstrip("#"), 16) if color else None,
    )


def send_kwargs(spec: dict[str, Any]) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if (spec.get("content") or "").strip():
        kwargs["content"] = spec["content"]
    if spec.get("embed"):
        kwargs["embed"] = build_embed(spec["embed"])
    if spec.get("buttons"):
        kwargs["view"] = build_view(spec["buttons"])
    return kwargs


def edit_kwargs(spec: dict[str, Any]) -> dict[str, Any]:
    """Only keys present in the spec are changed; empty values clear that part."""
    kwargs: dict[str, Any] = {}
    if "content" in spec:
        kwargs["content"] = spec["content"] if (spec["content"] or "").strip() else None
    if "embed" in spec:
        kwargs["embed"] = build_embed(spec["embed"])
    if "buttons" in spec:
        kwargs["view"] = build_view(spec["buttons"]) if spec["buttons"] else None
    return kwargs


def discord_error(err: discord.HTTPException) -> MessageError:
    if isinstance(err, discord.Forbidden):
        return MessageError(f"Discord refused the request (missing permission?): {err.text}")
    if isinstance(err, discord.NotFound):
        return MessageError(f"Discord could not find it (wrong ID, or the bot can't see it): {err.text}")
    return MessageError(f"Discord error {err.status}: {err.text}")


@dataclass
class PendingInteraction:
    interaction: Any
    received: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    ack: str | None = None
    original_filled: bool = False
    auto_task: asyncio.Task | None = None

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.received > INTERACTION_LIFETIME - 15


@dataclass
class PendingForm:
    event: dict[str, Any]
    custom_id: str
    received: float = field(default_factory=time.monotonic)

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.received > INTERACTION_LIFETIME


class Bridge:
    def __init__(self, options: dict[str, Any], store: Store) -> None:
        self.options = options
        self.store = store
        self.ha: HALink | None = None
        self.client: discord.Client | None = None
        self.pending: dict[str, PendingInteraction] = {}
        self.forms: dict[str, PendingForm] = {}
        self.bot_error: str | None = None
        self.allowed_users = {str(u).strip() for u in options.get("allowed_user_ids") or [] if str(u).strip()}

    # ----------------------------------------------------------------- client
    def create_client(self) -> discord.Client:
        # Interactions are delivered without any gateway intents, so the bot
        # never receives regular chat messages.
        client = discord.Client(intents=discord.Intents.none())

        @client.event
        async def on_ready() -> None:
            self.bot_error = None
            _LOGGER.info("Discord bot connected as %s (%s)", client.user, client.user.id)

        @client.event
        async def on_interaction(interaction: discord.Interaction) -> None:
            await self.handle_interaction(interaction)

        self.client = client
        return client

    def status(self) -> dict[str, Any]:
        client = self.client
        ready = bool(client and client.is_ready())
        return {
            "discord_connected": ready,
            "bot_user": str(client.user) if ready and client.user else None,
            "bot_id": str(client.user.id) if ready and client.user else None,
            "bot_error": self.bot_error,
            "ha_connected": bool(self.ha and self.ha.connected),
            "ha_error": self.ha.error if self.ha else None,
            "default_channel_id": self.options.get("default_channel_id") or "",
        }

    def _require_client(self) -> discord.Client:
        if not self.client or not self.client.is_ready():
            raise MessageError(self.bot_error or "The Discord bot is not connected yet")
        return self.client

    # ----------------------------------------------------------- interactions
    async def handle_interaction(self, interaction: Any) -> None:
        is_form = interaction.type == discord.InteractionType.modal_submit
        if interaction.type != discord.InteractionType.component and not is_form:
            return
        data = interaction.data or {}
        if not is_form and data.get("component_type") != discord.ComponentType.button.value:
            return
        if is_form and not str(data.get("custom_id", "")).startswith(FORM_PREFIX):
            return
        user_id = str(interaction.user.id)
        if self.allowed_users and user_id not in self.allowed_users:
            _LOGGER.info("Ignoring click on %s by non-allowed Discord user %s (%s)", data.get("custom_id"), interaction.user.name, user_id)
            await interaction.response.send_message("You are not allowed to use these buttons.", ephemeral=True)
            return
        if not self.ha or not self.ha.connected:
            await interaction.response.send_message(
                "Home Assistant is not connected to the Discord Bridge right now.", ephemeral=True
            )
            return

        self._forget_expired()
        ack = None
        if is_form:
            form = self.forms.get(data.get("custom_id"))
            if form is None or form.event["user_id"] != user_id:
                await interaction.response.send_message(
                    "This form has expired or belongs to another user. Press the button again.", ephemeral=True
                )
                return
            inputs = [
                child
                for row in data.get("components", [])
                for child in ([row["component"]] if "component" in row else row.get("components", []))
                if child.get("custom_id") == "message"
            ]
            text = inputs[0].get("value") if len(inputs) == 1 else None
            if not isinstance(text, str) or not text.strip() or len(text) > 4000:
                await interaction.response.send_message(
                    "Enter a message between 1 and 4000 characters, then try again.", ephemeral=True
                )
                return
            self.forms.pop(data["custom_id"])
            event = {
                **form.event,
                **self.event_data(interaction),
                "custom_id": form.custom_id,
                "button_custom_id": form.event["custom_id"],
                "button_label": form.event["button_label"],
                "set_id": form.event["set_id"],
                "message_id": form.event["message_id"],
                "interaction_type": "modal_submit",
                "values": {"message": text},
            }
        else:
            event = self.event_data(interaction)
            if str(data.get("custom_id", "")).startswith(FORM_PREFIX):
                record = self.store.find_by_message(event["message_id"]) if event["message_id"] else None
                button = next(
                    (b for b in (record or {}).get("payload", {}).get("buttons", [])
                     if button_custom_id(b) == data["custom_id"] and b.get("form")),
                    None,
                )
                if button is None:
                    await interaction.response.send_message(
                        "This form's configuration is no longer available. Ask the owner to send or update the message.",
                        ephemeral=True,
                    )
                    return
                event["custom_id"] = button["custom_id"]
                await self._open_form(interaction, event, button["form"])
                ack = "modal"
        pending = PendingInteraction(interaction, ack=ack)
        self.pending[str(interaction.id)] = pending
        if ack is None:
            pending.auto_task = asyncio.create_task(self._auto_ack(pending))
        _LOGGER.info(
            "%s for button %s by Discord user %s (%s, display name %s) in channel %s",
            event["interaction_type"], event["custom_id"], event["user_name"], event["user_id"],
            event["user_display_name"], event["channel_id"],
        )
        try:
            await self.ha.send_interaction(event)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Could not forward the interaction to Home Assistant")

    async def _open_form(self, interaction: Any, event: dict[str, Any], form: dict[str, str]) -> None:
        custom_id = FORM_PREFIX + uuid.uuid4().hex
        modal = discord.ui.Modal(title=form["title"], custom_id=custom_id, timeout=None)
        modal.add_item(discord.ui.Label(
            text=form["label"],
            component=discord.ui.TextInput(
                custom_id="message", style=discord.TextStyle.paragraph,
                placeholder=form["placeholder"] or None, required=True, min_length=1, max_length=4000,
            ),
        ))
        # Like button views, forms are dispatched only through on_interaction.
        modal.stop()
        await interaction.response.send_modal(modal)
        self.forms[custom_id] = PendingForm(event, form["custom_id"])

    def event_data(self, interaction: Any) -> dict[str, Any]:
        data = interaction.data or {}
        custom_id = data.get("custom_id")
        message = interaction.message
        message_id = str(message.id) if message else None
        record = self.store.find_by_message(message_id) if message_id else None
        label = None
        for row in getattr(message, "components", None) or []:
            for child in getattr(row, "children", None) or []:
                if getattr(child, "custom_id", None) == custom_id:
                    label = getattr(child, "label", None)
        user = interaction.user
        return {
            "interaction_type": "button",
            "interaction_id": str(interaction.id),
            "custom_id": custom_id,
            "button_label": label,
            "set_id": record.get("set_id") if record else None,
            "message_id": message_id,
            "channel_id": str(interaction.channel_id) if interaction.channel_id else None,
            "guild_id": str(interaction.guild_id) if interaction.guild_id else None,
            "user_id": str(user.id),
            "user_name": user.name,
            "user_display_name": getattr(user, "display_name", user.name),
            "expires_at": (datetime.now(UTC) + timedelta(seconds=INTERACTION_LIFETIME)).isoformat(timespec="seconds"),
        }

    def _forget_expired(self) -> None:
        for key in [key for key, item in self.pending.items() if item.expired]:
            self.pending.pop(key, None)
        for key in [key for key, item in self.forms.items() if item.expired]:
            self.forms.pop(key, None)

    def _get_pending(self, interaction_id: Any) -> PendingInteraction:
        pending = self.pending.get(str(interaction_id))
        if pending is None or pending.expired:
            raise MessageError(
                f"Unknown or expired interaction {interaction_id} (Discord allows replies for 15 minutes)"
            )
        return pending

    async def _auto_ack(self, pending: PendingInteraction) -> None:
        await asyncio.sleep(float(self.options.get("auto_ack_seconds", 2.0)))
        kind, ephemeral = AUTO_ACK.get(self.options.get("auto_ack_type", "defer_update"), AUTO_ACK["defer_update"])
        async with pending.lock:
            if pending.ack is not None:
                return
            try:
                await self._first_response(pending, kind, ephemeral, {})
                _LOGGER.debug("Auto-acknowledged interaction %s with %s", pending.interaction.id, kind)
            except discord.HTTPException as err:
                _LOGGER.warning("Auto-acknowledge failed: %s", err)

    async def _first_response(
        self, pending: PendingInteraction, kind: str, ephemeral: bool, spec: dict[str, Any]
    ) -> dict[str, Any]:
        response = pending.interaction.response
        result: dict[str, Any] = {}
        if kind == "defer_update":
            await response.defer()
        elif kind == "defer_reply":
            await response.defer(thinking=True, ephemeral=ephemeral)
        elif kind == "reply":
            await response.send_message(ephemeral=ephemeral, **send_kwargs(spec))
            pending.original_filled = True
            if any(button.get("form") for button in spec.get("buttons", [])):
                await self._remember_form_message(await pending.interaction.original_response(), spec)
        elif kind == "update_message":
            await response.edit_message(**edit_kwargs(spec))
            await self._remember_edit(pending.interaction.message, spec)
        pending.ack = kind
        result["acknowledged"] = kind
        return result

    async def _after_ack(
        self, pending: PendingInteraction, kind: str, ephemeral: bool, spec: dict[str, Any]
    ) -> dict[str, Any]:
        interaction = pending.interaction
        if kind in ("defer_update", "defer_reply"):
            return {"acknowledged": pending.ack, "note": "already acknowledged"}
        if kind == "update_message":
            if pending.ack == "defer_update":
                await interaction.edit_original_response(**edit_kwargs(spec))
            else:
                await interaction.message.edit(**edit_kwargs(spec))
            await self._remember_edit(interaction.message, spec)
            return {"acknowledged": pending.ack, "updated_message_id": str(interaction.message.id)}
        # kind == "reply"
        if pending.ack == "defer_reply" and not pending.original_filled:
            message = await interaction.edit_original_response(**edit_kwargs(spec))
            pending.original_filled = True
        else:
            message = await interaction.followup.send(wait=True, ephemeral=ephemeral, **send_kwargs(spec))
        await self._remember_form_message(message, spec)
        return {"acknowledged": pending.ack, "message_id": str(message.id) if message else None}

    async def _remember_form_message(self, message: Any, spec: dict[str, Any]) -> None:
        if message is None or not any(button.get("form") for button in spec.get("buttons", [])):
            return
        record = self.store.find_by_message(str(message.id))
        if record is not None:
            await self.store.update_record(record, payload={**(record.get("payload") or {}), **spec})
            return
        await self.store.add_record(
            message_id=str(message.id), channel_id=str(message.channel.id),
            guild_id=str(message.guild.id) if message.guild else None,
            set_id=None, set_name=None, jump_url=message.jump_url, source="interaction", payload=spec,
        )

    async def _remember_edit(self, message: Any, spec: dict[str, Any], set_id: str | None = None) -> None:
        if message is None:
            return
        record = self.store.find_by_message(str(message.id))
        if record is not None:
            payload = {**(record.get("payload") or {}), **spec}
            await self.store.update_record(record, payload=payload, set_id=set_id or record.get("set_id"))
        else:
            await self._remember_form_message(message, spec)

    def _spec_from(self, data: dict[str, Any], *, partial: bool) -> dict[str, Any]:
        set_id = data.get("set_id")
        message_set = self.store.get_set(set_id) if set_id else None
        return render(
            message_set,
            content=data.get("content"),
            embed=data.get("embed"),
            buttons=data.get("buttons"),
            partial=partial and message_set is None,
        )

    # ---------------------------------------------------------------- commands
    async def respond(self, data: dict[str, Any]) -> dict[str, Any]:
        kind = data.get("type") or "defer_update"
        if kind not in RESPOND_TYPES:
            raise MessageError(f"type must be one of {', '.join(RESPOND_TYPES)}")
        ephemeral = bool(data.get("ephemeral", False))
        spec: dict[str, Any] = {}
        if kind == "reply":
            spec = self._spec_from(data, partial=False)
        elif kind == "update_message":
            spec = self._spec_from(data, partial=True)
        pending = self._get_pending(data.get("interaction_id"))
        async with pending.lock:
            try:
                if pending.ack is None:
                    return await self._first_response(pending, kind, ephemeral, spec)
                return await self._after_ack(pending, kind, ephemeral, spec)
            except discord.HTTPException as err:
                raise discord_error(err) from err

    async def followup(self, data: dict[str, Any]) -> dict[str, Any]:
        spec = self._spec_from(data, partial=False)
        ephemeral = bool(data.get("ephemeral", False))
        pending = self._get_pending(data.get("interaction_id"))
        async with pending.lock:
            try:
                if pending.ack is None:
                    await self._first_response(pending, "defer_update", False, {})
                return await self._after_ack(pending, "reply", ephemeral, spec)
            except discord.HTTPException as err:
                raise discord_error(err) from err

    async def send_message(self, data: dict[str, Any], *, source: str = "home_assistant") -> dict[str, Any]:
        client = self._require_client()
        set_id = data.get("set_id") or None
        message_set = self.store.get_set(set_id) if set_id else None
        channel_id = validate_channel_id(
            data.get("channel_id")
            or (message_set or {}).get("channel_id")
            or self.options.get("default_channel_id"),
            required=True,
        )
        spec = render(message_set, content=data.get("content"), embed=data.get("embed"), buttons=data.get("buttons"))
        try:
            channel = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
            if not hasattr(channel, "send"):
                raise MessageError(f"Channel {channel_id} is not a text channel")
            message = await channel.send(**send_kwargs(spec))
        except discord.HTTPException as err:
            raise MessageError(f"{discord_error(err)} (channel {channel_id})") from err
        record = await self.store.add_record(
            set_id=set_id,
            set_name=(message_set or {}).get("name"),
            channel_id=channel_id,
            guild_id=str(message.guild.id) if message.guild else None,
            message_id=str(message.id),
            jump_url=message.jump_url,
            source=source,
            payload=spec,
        )
        return {
            "message_id": str(message.id),
            "channel_id": channel_id,
            "jump_url": message.jump_url,
            "record_id": record["id"],
        }

    async def edit_message(self, data: dict[str, Any], *, source: str = "home_assistant") -> dict[str, Any]:
        client = self._require_client()
        message_id = str(data.get("message_id") or "").strip()
        if not message_id.isdigit():
            raise MessageError("A valid message_id is required")
        record = self.store.find_by_message(message_id)
        channel_id = validate_channel_id(
            data.get("channel_id") or (record or {}).get("channel_id"), required=True
        )
        spec = self._spec_from(data, partial=True)
        try:
            partial = client.get_partial_messageable(int(channel_id)).get_partial_message(int(message_id))
            await partial.edit(**edit_kwargs(spec))
        except discord.HTTPException as err:
            raise MessageError(f"{discord_error(err)} (channel {channel_id}, message {message_id})") from err
        if record is not None:
            payload = {**(record.get("payload") or {}), **spec}
            await self.store.update_record(
                record, payload=payload, set_id=data.get("set_id") or record.get("set_id"), edited_by=source
            )
        else:
            await self._remember_form_message(partial, spec)
        return {"message_id": message_id, "channel_id": channel_id}

    async def handle_command(self, command: str, data: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "respond": self.respond,
            "followup": self.followup,
            "send_message": self.send_message,
            "edit_message": self.edit_message,
        }
        if command == "list_sets":
            return {"sets": [{"id": s["id"], "name": s["name"]} for s in self.store.list_sets()]}
        if command not in handlers:
            raise MessageError(f"Unknown command: {command}")
        return await handlers[command](data)
