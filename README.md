# Claude Code Discord Bot

Drive [Claude Code](https://claude.com/claude-code) from Discord. Message the bot,
it runs the `claude` CLI inside one of your local project directories and streams
progress — tool calls, file edits, and the final answer — back into the channel.

## Features

- Run Claude Code sessions from any Discord channel you authorize
- Live progress: streamed tool-use summaries while Claude works
- Multiple projects, switchable per channel
- Session continuity so follow-up messages continue the same conversation
- Long replies are automatically split to fit Discord's message limit
- Locked down to specific users (and optionally specific channels)

## Setup

1. Install dependencies:

   ```bash
   pip install -r discord_bot_requirements.txt
   ```

2. Create a Discord application and bot at the
   [Discord Developer Portal](https://discord.com/developers/applications),
   enable the **Message Content Intent**, and invite it to your server.

3. Copy the example env file and fill in your values:

   ```bash
   cp discord_bot.env.example .env
   ```

   At minimum set `DISCORD_TOKEN`, `ALLOWED_USER_IDS`, and `PROJECTS`.

4. Run it:

   ```bash
   python claude_code_discord_bot.py
   ```

## Configuration

All configuration is via environment variables — see
[`discord_bot.env.example`](discord_bot.env.example) for the full, commented list.
Key options:

| Variable | Purpose |
| --- | --- |
| `DISCORD_TOKEN` | Bot token (required) |
| `ALLOWED_USER_IDS` | Comma-separated user IDs allowed to use the bot (required) |
| `ALLOWED_CHANNEL_IDS` | Restrict the bot to specific channels (recommended) |
| `PROJECTS` | JSON map of `alias -> absolute path` |
| `DEFAULT_PROJECT` | Which project alias to use by default |
| `CLAUDE_BIN` | Path to the `claude` binary if not on `PATH` |
| `PERMISSION_MODE` | `acceptEdits` to auto-approve writes, or `default` |
| `RUN_TIMEOUT` | Hard timeout per run, in seconds |

## Security

Never commit your real `.env` — it holds your bot token. It is already listed in
`.gitignore`. Keep `ALLOWED_USER_IDS` tight and prefer restricting the bot to
specific channels.
