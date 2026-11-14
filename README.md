# home-assistant-discord

A Home Assistant app and companion integration for sending Discord messages
with buttons and handling clicks in automations. Create and update message sets
in the editor, and review sent-message history.

The bot receives interactions only; it uses no gateway intents and does not
receive regular chat messages.

## Setup

### 1. Discord application

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Under **Bot**, reset and copy the token.
3. Under **Installation**, enable **Guild Install** with the `bot` scope and
   **Send Messages** and **Embed Links** permissions. Add **View Channel** for
   private channels, then use the install link to add the bot to your server.
4. In Discord, enable **User Settings → Advanced → Developer Mode**, then
   right-click a channel and select **Copy Channel ID**.

No privileged intents or Interactions Endpoint URL are needed.

### 2. Install the app

**Settings → Apps → App store → ⋮ → Repositories** → add
`https://github.com/AppEternal/home-assistant-discord` → install **Discord Bridge**.
On the **Configuration** tab, enter the bot token and optionally a default channel
ID. Start the app and enable **Show in sidebar**.

### 3. Install the companion integration

- **HACS:** **⋮ → Custom repositories** → add the same URL as type *Integration* → download **Discord Bridge**.
- **Manual:** copy `custom_components/discord_bridge` to `/config/custom_components/`.

Restart Home Assistant, then **Settings → Devices & services → Add integration → Discord Bridge**.
Connection status appears in the editor and `binary_sensor.discord_bridge_app_connected`.

## Using it

Open **Discord Bridge**, create a message set, and click **Save & send**. The editor
provides automation YAML below each preview.

### The click event

Each click fires `discord_bridge_interaction` with:

- `custom_id`, `button_label`, and `set_id` (when sent from a set)
- `interaction_id` and `expires_at` (responses expire after 15 minutes)
- `user_id`, `user_name`, `user_display_name`, `channel_id`, `guild_id`, and `message_id`

### Actions

| Action | What it does |
| --- | --- |
| `discord_bridge.respond` | Acknowledge with `defer_update`, `defer_reply`, `reply`, or `update_message`. Replies can be `ephemeral`. |
| `discord_bridge.followup` | Send another message within 15 minutes. The first follow-up after `defer_reply` replaces the pending reply. |
| `discord_bridge.send_message` | Send a set or custom message; returns `message_id`. |
| `discord_bridge.edit_message` | Edit a sent message using a set or selected fields. |

The message actions accept `set_id`, `content`, `embed` (`title`, `description`,
`color`), and `buttons` (`label`, `custom_id`, `style`, `emoji`, `url`, `disabled`),
all templatable.

Discord requires an acknowledgement within 3 seconds. Call `respond` first;
otherwise the app acknowledges automatically after `auto_ack_seconds` (default
2 seconds), using `auto_ack_type`. After auto-acknowledgement, `reply` becomes
a follow-up and `update_message` edits the message.

### Example automation

```yaml
triggers:
  - trigger: event
    event_type: discord_bridge_interaction
    event_data: { custom_id: alarm_1 }
    id: alarm_1
actions:
  - action: discord_bridge.respond
    data:
      interaction_id: "{{ trigger.event.data.interaction_id }}"
      type: reply
      ephemeral: true
      content: "Starting {{ trigger.event.data.button_label }}"
  - action: script.alarm_1
```

## Security and storage

- Anyone who can see a message can press its buttons by default. Set
  `allowed_user_ids` for sensitive actions and consider checking `user_id` in
  automations. Disallowed clicks do not fire an event.
- The editor has no authentication of its own: Home Assistant uses ingress;
  standalone mode listens on localhost, accessible to local programs. Any
  nonempty `ALLOW_ANY_IP` value (including `0`) binds all interfaces and disables
  the ingress-only check. Use it only behind a trusted, authenticated proxy or
  on an isolated network.
- Keep tokens out of repositories, logs, and shell history. Use encrypted
  connections to Home Assistant over untrusted networks.
- `message_sets.json` and `sent_messages.json` in
  `/addon_configs/<id>_discord_bridge/` contain message content, Discord IDs,
  and links. They are included in backups; protect both the folder and backups.

## Development

Run app tests with `pytest tests`. Integration tests require
`pytest-homeassistant-custom-component` on Linux: `pytest tests_integration`.

To run outside Home Assistant, set `DISCORD_BOT_TOKEN` and `HA_TOKEN` securely in
the environment, then run from `discord_bridge/`:
`HA_WS_URL=ws://ha:8123/api/websocket DATA_FOLDER=./data PORT=8099 python -m app`.
