"""Message sets: validation and rendering of messages with buttons."""

from __future__ import annotations

import copy
import re
from typing import Any

SET_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
SNOWFLAKE_RE = re.compile(r"^\d{5,25}$")
BUTTON_STYLES = ("primary", "secondary", "success", "danger", "link")
MAX_BUTTONS = 25
BUTTONS_PER_ROW = 5
FORM_PREFIX = "db_form:"


class MessageError(ValueError):
    """Raised when a message or message set is invalid."""


def _str(value: Any) -> str:
    return "" if value is None else str(value)


def parse_color(value: Any) -> int | None:
    """Accept '#5865F2', '5865F2', '0x5865F2' or an int."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise MessageError("Embed color must be a hex color like #5865F2")
    if isinstance(value, int):
        color = value
    else:
        text = str(value).strip().lower().removeprefix("#").removeprefix("0x")
        try:
            color = int(text, 16)
        except ValueError as err:
            raise MessageError(f"Invalid embed color: {value}") from err
    if not 0 <= color <= 0xFFFFFF:
        raise MessageError(f"Invalid embed color: {value}")
    return color


def normalize_embed(embed: Any) -> dict[str, Any] | None:
    if not embed:
        return None
    if not isinstance(embed, dict):
        raise MessageError("Embed must be an object with title/description/color")
    title = _str(embed.get("title")).strip()
    description = _str(embed.get("description")).strip()
    color = parse_color(embed.get("color"))
    if not title and not description:
        return None
    if len(title) > 256:
        raise MessageError("Embed title is limited to 256 characters")
    if len(description) > 4096:
        raise MessageError("Embed description is limited to 4096 characters")
    result: dict[str, Any] = {"title": title, "description": description}
    if color is not None:
        result["color"] = f"#{color:06X}"
    return result


def normalize_buttons(buttons: Any) -> list[dict[str, Any]]:
    if buttons is None:
        return []
    if not isinstance(buttons, list):
        raise MessageError("Buttons must be a list")
    if len(buttons) > MAX_BUTTONS:
        raise MessageError(f"A message can have at most {MAX_BUTTONS} buttons")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(buttons, start=1):
        if not isinstance(raw, dict):
            raise MessageError(f"Button {index} must be an object")
        style = _str(raw.get("style") or "primary").lower()
        if style not in BUTTON_STYLES:
            raise MessageError(
                f"Button {index}: style must be one of {', '.join(BUTTON_STYLES)}"
            )
        label = _str(raw.get("label")).strip()
        emoji = _str(raw.get("emoji")).strip()
        if not label and not emoji:
            raise MessageError(f"Button {index} needs a label or an emoji")
        if len(label) > 80:
            raise MessageError(f"Button {index}: label is limited to 80 characters")
        button: dict[str, Any] = {
            "label": label,
            "style": style,
            "emoji": emoji,
            "disabled": bool(raw.get("disabled", False)),
        }
        if style == "link":
            if raw.get("form") is not None:
                raise MessageError(f"Button {index}: link buttons cannot open a form")
            url = _str(raw.get("url")).strip()
            if not url.startswith(("https://", "http://", "discord://")):
                raise MessageError(f"Button {index}: link buttons need a valid URL")
            button["url"] = url
        else:
            custom_id = _str(raw.get("custom_id")).strip()
            if not custom_id:
                raise MessageError(f"Button {index} ({label or emoji}) needs a custom ID")
            if len(custom_id) > 100:
                raise MessageError(f"Button {index}: custom ID is limited to 100 characters")
            if custom_id in seen:
                raise MessageError(f"Duplicate button custom ID: {custom_id}")
            seen.add(custom_id)
            button["custom_id"] = custom_id
            if raw.get("form") is not None:
                button["form"] = normalize_form(raw["form"])
        if button.get("custom_id", "").startswith(FORM_PREFIX):
            raise MessageError(f"Button {index}: custom IDs starting with {FORM_PREFIX} are reserved")
        result.append(button)
    return result


def normalize_form(raw: Any) -> dict[str, str]:
    """A button form contains one required, multiline message input."""
    if not isinstance(raw, dict):
        raise MessageError("Form must be an object with custom_id, title, label and optional placeholder")
    form = {
        "custom_id": _str(raw.get("custom_id")).strip(),
        "title": _str(raw.get("title")).strip(),
        "label": _str(raw.get("label")).strip(),
        "placeholder": _str(raw.get("placeholder")),
    }
    for key, limit in (("custom_id", 100), ("title", 45), ("label", 45), ("placeholder", 100)):
        if key != "placeholder" and not form[key]:
            raise MessageError(f"Form {key} is required")
        if len(form[key]) > limit:
            raise MessageError(f"Form {key} is limited to {limit} characters")
    return form


def validate_channel_id(channel_id: Any, *, required: bool = False) -> str:
    text = _str(channel_id).strip()
    if not text:
        if required:
            raise MessageError("A channel ID is required (set one on the message or in the app options)")
        return ""
    if not SNOWFLAKE_RE.match(text):
        raise MessageError(f"Invalid Discord channel ID: {text}")
    return text


def normalize_set(raw: Any) -> dict[str, Any]:
    """Validate a message set coming from the editor or the store."""
    if not isinstance(raw, dict):
        raise MessageError("Message set must be an object")
    set_id = _str(raw.get("id")).strip()
    if not SET_ID_RE.match(set_id):
        raise MessageError(
            "Set ID must be 1-64 characters: lowercase letters, numbers, '_' or '-'"
        )
    content = _str(raw.get("content"))
    if len(content) > 2000:
        raise MessageError("Message content is limited to 2000 characters")
    result = {
        "id": set_id,
        "name": _str(raw.get("name")).strip() or set_id,
        "channel_id": validate_channel_id(raw.get("channel_id")),
        "content": content,
        "embed": normalize_embed(raw.get("embed")),
        "buttons": normalize_buttons(raw.get("buttons")),
    }
    if not (result["content"].strip() or result["embed"] or result["buttons"]):
        raise MessageError("A message needs content, an embed or at least one button")
    return result


def render(
    message_set: dict[str, Any] | None = None,
    *,
    content: Any = None,
    embed: Any = None,
    buttons: Any = None,
    partial: bool = False,
) -> dict[str, Any]:
    """Build a message spec from an optional set plus overrides.

    Returns a dict that may contain the keys ``content``, ``embed`` and
    ``buttons``. With a set every key is present, so editing a message with it
    replaces the whole message. With ``partial=True`` and no set, only the
    overridden keys are present, so an edit keeps the rest of the message.
    """
    spec: dict[str, Any] = {}
    if message_set is not None:
        spec = {
            "content": message_set.get("content", ""),
            "embed": copy.deepcopy(message_set.get("embed")),
            "buttons": copy.deepcopy(message_set.get("buttons", [])),
        }
    if content is not None:
        spec["content"] = _str(content)
    if embed is not None:
        spec["embed"] = normalize_embed(embed)
    if buttons is not None:
        spec["buttons"] = normalize_buttons(buttons)
    if len(_str(spec.get("content"))) > 2000:
        raise MessageError("Message content is limited to 2000 characters")
    has_body = bool(_str(spec.get("content")).strip() or spec.get("embed") or spec.get("buttons"))
    if partial:
        if not spec:
            raise MessageError("Nothing to send: give a set_id, content, embed or buttons")
    elif not has_body:
        raise MessageError("A message needs content, an embed or at least one button")
    return spec


def find_button(spec: dict[str, Any] | None, custom_id: str) -> dict[str, Any] | None:
    for button in (spec or {}).get("buttons") or []:
        if button.get("custom_id") == custom_id:
            return button
    return None
