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

## Send your first message

1. Open **Discord Bridge** from the sidebar or the app's **Open Web UI** button.
   Check that Discord shows connected.
2. Click **+ New message set**. Enter **Name** `Test` and **Set ID** `test`.
3. Paste the Discord channel's ID into **Channel ID** and enter `Hello from Home
   Assistant!` in **Message**. Embeds and buttons are optional.
4. Click **Save & send**, then check the Discord channel or **Sent messages**.

**Save** only stores the set; **Save & send** posts a new message each time.

### Send from Home Assistant

With both Discord and Home Assistant connected, open **Developer Tools → Actions**,
switch to YAML, paste this, replace the channel ID, and click **Perform action**:

```yaml
action: discord_bridge.send_message
data:
  channel_id: "YOUR_CHANNEL_ID"
  content: "Hello from Home Assistant!"
```

No saved set is required. To send the set above, use `set_id: test` instead of
`content`. The editor also provides YAML for sending sets and handling buttons.

### The click event

Each normal button click fires `discord_bridge_interaction` with:

- `custom_id`, `button_label`, and `set_id` (when sent from a set)
- `interaction_type: button` (or `modal_submit` for a submitted form)
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
`color`), and `buttons` (`label`, `custom_id`, `style`, `emoji`, `url`, `disabled`, `form`),
all templatable.

### Button verification / text forms

In the editor, enable **Open text form on click** on a non-link button.
Set a separate form ID, form title, input label, and optional hint, then **Save &
send** (or **Update message** in history for an existing message).

Pressing this button immediately opens a Discord pop-up with a required,
multiline input (1-4000 characters). The click still fires an event with the
button's `custom_id` and `interaction_type: button`. Submitting fires a second
event with the **form's** `custom_id`, `interaction_type: modal_submit`, and the
text in `values.message`. The submission also includes `button_custom_id` and
the original user/message details. Dismissing the form fires no submission.
Use each event's own `interaction_id` with the usual `respond`/`followup`
actions. Opening the form already acknowledges the click, so replying to that
click sends a follow-up without closing the form.

Forms can also be attached to buttons in action YAML:

```yaml
action: discord_bridge.send_message
data:
  content: "Request an action"
  buttons:
    - label: Verify
      custom_id: verify_request
      style: primary
      form:
        custom_id: verify_message
        title: Verify your request
        label: Message
        placeholder: "Type CONFIRM to continue"
```

For example, acknowledge the submission, then run an action only for matching
text (replace the example script with your own):

```yaml
triggers:
  - trigger: event
    event_type: discord_bridge_interaction
    event_data:
      custom_id: verify_message
      interaction_type: modal_submit
actions:
  - action: discord_bridge.respond
    data:
      interaction_id: "{{ trigger.event.data.interaction_id }}"
      type: reply
      ephemeral: true
      content: "Your message was received."
  - condition: template
    value_template: "{{ trigger.event.data['values']['message'] | trim == 'CONFIRM' }}"
  - action: script.your_verified_action
```

You can pass `{{ trigger.event.data['values']['message'] }}` to other actions,
or store it with `input_text.set_value` (respect that helper's length limit).
For verification, put the protected action in the **form submission**
automation, not the initial button-click automation.
Text entry is a confirmation step, not authentication: check `user_id` or set
`allowed_user_ids` for sensitive actions. Submitted text is included in Home
Assistant events and may appear in automation traces.

Open forms expire after 15 minutes or an app restart; press the button again.
Button form settings survive restarts in sent-message history. If a message is
forgotten or falls outside the last 200 records, its form button is rejected
until the message is updated or re-sent. Custom IDs beginning with `db_form:`
are reserved for the bridge.

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

## Connection warnings

- **Integration not installed or not set up:** complete setup step 3. A later
  **Connected to the Discord Bridge integration** message confirms recovery.
- **502 / connection closed:** the Supervisor could not reach Home Assistant,
  or the connection dropped. This can happen during a restart; the app retries.
  If it persists, check Home Assistant Core logs.
- **Guilds intent / PyNaCl / davey:** gateway intents are intentionally disabled
  and voice is unused. These warnings do not require enabling intents or
  installing voice packages for sending messages.

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
