# Channels Plugin for Agent Zero

Unified messaging adapter that connects Telegram, Discord, and WhatsApp to Agent Zero. Users message your bot on any platform, the agent processes and replies automatically.

**Status**: Telegram is production-ready. Discord and WhatsApp adapters are included but less tested.

---

## For AI Agents: Installation & Setup Guide

You are helping a user install the Channels plugin for Agent Zero. Follow these steps exactly.

### Step 1: Install the plugin

Copy this plugin to the Agent Zero user plugins directory:

```
/a0/usr/plugins/channels/
```

If the user is running Agent Zero via Docker, the `usr/` directory is on the `a0-usr` persistent volume. Files here survive container rebuilds.

The plugin auto-installs its pip dependencies (`aiogram`, `discord.py`) to `/a0/usr/lib/` on first boot. No manual pip install is needed.

### Step 2: Create a Telegram bot

Guide the user through creating a bot with BotFather:

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot`
3. Choose a display name (e.g., "My Assistant")
4. Choose a username ending in `bot` (e.g., `my_assistant_bot`)
5. BotFather replies with a **bot token** like `7123456789:AAF...`. Save this token.

**Important**: Tell the user to also disable "Group Privacy" if they want the bot to work in group chats:
- Message @BotFather
- Send `/mybots` → select the bot → Bot Settings → Group Privacy → Turn off

### Step 3: Configure the plugin

In Agent Zero's web UI, go to **Settings** (gear icon) → **Channels** plugin → **Config** tab.

Set these fields:

| Field | Value |
|---|---|
| **Telegram Enabled** | Toggle ON |
| **Bot Token** | Paste the token from BotFather |
| **Allowed Chats** | Comma-separated chat IDs to restrict access (leave empty to allow all) |

Click **Save**, then restart Agent Zero (or send any message to trigger a reload).

#### Finding a chat ID

The user's personal chat ID can be found by:
1. Messaging [@userinfobot](https://t.me/userinfobot) on Telegram
2. It replies with the user's numeric chat ID

For groups: add the bot to the group, send a message, then check the bot's logs or use the `/status` command.

**Security**: If the bot is public-facing, always set `allowed_chats` to restrict who can interact with the agent. Without it, anyone who finds the bot can use it.

### Step 4: Verify

1. Open Telegram and message the bot
2. You should see:
   - 👀 reaction (message received)
   - 🤔 reaction (agent is thinking)
   - Typing indicator while processing
   - Agent's reply
   - 👍 reaction (done)
3. Try `/help` — the bot should list available commands
4. Try `/status` — shows connection state and conversation count

### Troubleshooting

| Problem | Fix |
|---|---|
| No response from bot | Check the token is correct. Check Agent Zero logs for `[channels]` or `[telegram]` entries. |
| "Telegram adapter not yet implemented" in logs | Dependencies not installed. Send another message — `_01_channels_deps.py` auto-installs on first boot. Check `/a0/usr/lib/` for `aiogram`. |
| Bot works in DM but not in group | Disable Group Privacy via BotFather (Step 2). |
| Reactions don't appear | Some Telegram clients don't show bot reactions. The agent still works — check for the text reply. |
| "Not allowed" or no response in group | The group's chat ID isn't in `allowed_chats`. Add it or leave the field empty. |

---

## Features

### Reaction Lifecycle
Config-driven emoji feedback on every message:
- **Seen** (👀) — immediately on receipt
- **Processing** (🤔) — agent is thinking
- **Done** (👍) — response sent
- **Error** (💔) — timeout or failure

All emojis are customizable in settings. Reactions and typing indicators can be toggled on/off per platform.

### Bot Commands
Registered automatically with BotFather on connect:
- `/help` — shows bot name and available commands
- `/status` — bus state, adapter count, active conversations
- `/forget` — clears conversation history for the current chat

Custom commands can be defined in `telegram_commands` (JSON array). Commands listed in `telegram_handle_commands_internally` are handled by the plugin directly without invoking the agent.

### Agent Tools
The plugin gives the agent two tools:

**`send_message`** — proactively send a message to any connected platform/channel.

**`channel_status`** — check connections, list conversations, read message history.

### Auto-Install Dependencies
On first boot, the plugin installs `aiogram` and `discord.py` to `/a0/usr/lib/` (persistent Docker volume). Dependencies survive container rebuilds without manual intervention.

---

## Architecture

```
Inbound:  Platform → Adapter → route_inbound() → background thread
          → AgentContext(BACKGROUND) → agent processes → auto-reply

Outbound: Agent tool send_message → dispatch_outbound_threadsafe()
          → queue.Queue → drain loop → adapter.send()
```

### Key Design Decisions
- **`channels_helpers/`** namespace (not `helpers/`) — avoids Python module cache collisions with other plugins
- **Per-conversation locks** — messages are serialized per chat, preventing race conditions
- **Daemon thread + asyncio event loop** — adapters run their own event loops (Telegram long-polling, Discord gateway)
- **Config-driven** — all behavior is controllable via the settings UI, no code changes needed

### File Structure
```
channels/
  plugin.yaml              # Plugin manifest
  default_config.yaml      # Default settings (all tokens empty)
  requirements.txt         # pip dependencies
  LICENSE                  # MIT
  channels_helpers/
    schema.py              # ChannelMessage, Attachment, enums
    adapter.py             # ChannelAdapter ABC
    bus.py                 # ChannelBus — message routing singleton
    runner.py              # Daemon thread + adapter registration
    adapters/
      telegram.py          # aiogram v3, long-polling, reactions, commands
      discord.py           # discord.py v2, gateway
      whatsapp.py          # WhatsApp Cloud API (webhook)
  tools/
    send_message.py        # Agent tool: send to platform
    channel_status.py      # Agent tool: check status/history
  extensions/
    python/message_loop_start/
      _01_channels_deps.py # Auto-install pip deps to persistent volume
      _10_channels_boot.py # Start bus + adapters on first message
    python/system_prompt/
      _20_channels_context.py  # Inject channel context into agent prompt
  prompts/
    agent.system.tool.channels.md  # Tool documentation for agent
  api/
    status.py              # REST API: start/stop/status/conversations
  webui/
    main.html              # Plugin dashboard
    config.html            # Settings UI
    channels-store.js      # Alpine.js store
```

---

## Configuration Reference

### Telegram

| Key | Default | Description |
|---|---|---|
| `telegram_enabled` | `false` | Enable Telegram adapter |
| `telegram_bot_token` | `""` | Bot token from @BotFather |
| `telegram_allowed_chats` | `""` | Comma-separated chat IDs (empty = allow all) |
| `telegram_parse_mode` | `Markdown` | Message format: Markdown, HTML, MarkdownV2 |
| `telegram_typing_enabled` | `true` | Show typing indicator while processing |
| `telegram_reaction_enabled` | `true` | Send emoji reactions |
| `telegram_reaction_seen` | 👀 | Emoji on message receipt |
| `telegram_reaction_processing` | 🤔 | Emoji while agent thinks |
| `telegram_reaction_done` | 👍 | Emoji when response sent |
| `telegram_reaction_error` | 💔 | Emoji on timeout/failure |
| `telegram_response_mode` | `auto` | When to respond: auto, all, mentions, replies, commands_only |
| `telegram_commands` | JSON array | Bot commands registered with BotFather |
| `telegram_handle_commands_internally` | `help,status,forget` | Commands handled by plugin (not forwarded to agent) |
| `telegram_cooldown_seconds` | `0` | Per-user rate limit (0 = disabled) |
| `telegram_max_concurrent` | `3` | Max parallel agent contexts |

### Discord

| Key | Default | Description |
|---|---|---|
| `discord_enabled` | `false` | Enable Discord adapter |
| `discord_bot_token` | `""` | Bot token from Discord Developer Portal |
| `discord_allowed_guilds` | `""` | Comma-separated guild IDs |
| `discord_allowed_channels` | `""` | Comma-separated channel IDs |

### WhatsApp

| Key | Default | Description |
|---|---|---|
| `whatsapp_enabled` | `false` | Enable WhatsApp adapter |
| `whatsapp_phone_id` | `""` | WhatsApp Business phone number ID |
| `whatsapp_access_token` | `""` | WhatsApp Cloud API access token |
| `whatsapp_verify_token` | `""` | Webhook verification token |
| `whatsapp_webhook_port` | `8402` | Webhook listener port |

---

## License

MIT — see [LICENSE](LICENSE).
