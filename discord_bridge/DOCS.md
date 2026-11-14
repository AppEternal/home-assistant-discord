# Discord Bridge

1. Paste your Discord bot token on the **Configuration** tab and start the app.
2. Install the **Discord Bridge** integration (HACS custom repository or copy
   `custom_components/discord_bridge`) and add it under **Settings → Devices & services**.
3. Open **Discord Bridge** in the sidebar to create message sets with buttons and send them.

Button clicks fire the `discord_bridge_interaction` event. Use
`discord_bridge.respond`, `discord_bridge.followup`, `discord_bridge.send_message`
and `discord_bridge.edit_message` in automations.

## Options

| Option | Description |
| --- | --- |
| `bot_token` | Bot token from the Discord Developer Portal. |
| `default_channel_id` | Channel used when neither the set nor the action has one. |
| `auto_ack_seconds` | If no automation calls `discord_bridge.respond` within this time, the app acknowledges the click itself (Discord requires a response within 3 seconds). |
| `auto_ack_type` | How to auto-acknowledge: `defer_update` (silent), `defer_reply` (public “thinking…”), `defer_reply_ephemeral`. |
| `allowed_user_ids` | Only these Discord users may press buttons. An empty list allows everyone who can see the message; set this before using buttons for sensitive actions. |

Full documentation: https://github.com/AppEternal/home-assistant-discord
