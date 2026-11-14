"""Entry point: python -m app"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any

import discord
from aiohttp import web

from .bot import Bridge
from .ha_link import HALink
from .store import Store
from .web import create_app

_LOGGER = logging.getLogger("discord_bridge")

DEFAULT_OPTIONS: dict[str, Any] = {
    "bot_token": "",
    "default_channel_id": "",
    "auto_ack_seconds": 2.0,
    "auto_ack_type": "defer_update",
    "allowed_user_ids": [],
    "log_level": "info",
}


def load_options() -> dict[str, Any]:
    path = Path(os.environ.get("OPTIONS_FILE", "/data/options.json"))
    options = dict(DEFAULT_OPTIONS)
    if path.exists():
        options.update(json.loads(path.read_text(encoding="utf-8")))
    if os.environ.get("DISCORD_BOT_TOKEN"):
        options["bot_token"] = os.environ["DISCORD_BOT_TOKEN"]
    return options


async def run_bot(bridge: Bridge, token: str) -> None:
    delay = 10
    while True:
        client = bridge.create_client()
        try:
            await client.start(token)
            return
        except discord.LoginFailure:
            bridge.bot_error = "Discord rejected the bot token. Check the app configuration."
            _LOGGER.error(bridge.bot_error)
            return
        except discord.PrivilegedIntentsRequired:
            bridge.bot_error = "Discord refused the requested intents."
            _LOGGER.error(bridge.bot_error)
            return
        except Exception as err:  # noqa: BLE001
            bridge.bot_error = f"Discord connection failed: {err}"
            _LOGGER.warning("%s; retrying in %s s", bridge.bot_error, delay)
        finally:
            if not client.is_closed():
                await client.close()
        await asyncio.sleep(delay)
        delay = min(delay * 2, 300)


async def main() -> None:
    options = load_options()
    logging.basicConfig(
        level=str(options.get("log_level", "info")).upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)

    store = Store(Path(os.environ.get("DATA_FOLDER", "/config")))
    store.load()
    bridge = Bridge(options, store)

    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
    ha_url = os.environ.get("HA_WS_URL", "ws://supervisor/core/websocket")
    ha_token = os.environ.get("HA_TOKEN") or supervisor_token
    tasks: list[asyncio.Task] = []
    if ha_token:
        bridge.ha = HALink(ha_url, ha_token, bridge.handle_command)
        tasks.append(asyncio.create_task(bridge.ha.run()))
    else:
        _LOGGER.warning("No Home Assistant token available; running without Home Assistant")

    token = (options.get("bot_token") or "").strip()
    if token:
        tasks.append(asyncio.create_task(run_bot(bridge, token)))
    else:
        bridge.bot_error = "No bot token configured. Add it on the app's Configuration tab."
        _LOGGER.error(bridge.bot_error)

    app = create_app(bridge, ingress_only=bool(supervisor_token) and not os.environ.get("ALLOW_ANY_IP"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", "8099"))
    host = "0.0.0.0" if supervisor_token or os.environ.get("ALLOW_ANY_IP") else "127.0.0.1"
    await web.TCPSite(runner, host, port).start()
    _LOGGER.info("Editor listening on %s:%s", host, port)

    try:
        await asyncio.Event().wait()
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if bridge.client and not bridge.client.is_closed():
            await bridge.client.close()
        await runner.cleanup()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
