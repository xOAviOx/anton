# Claude Code Discord Bot

Drive [Claude Code](https://claude.com/claude-code) from Discord. Message the bot,
it runs the `claude` CLI inside one of your local project directories and streams
progress tool calls, file edits, and the final answer back into the channel.

## Features

- Run Claude Code sessions from any Discord channel you authorize
- Live progress: streamed tool-use summaries while Claude works
- Reaction controls on every run: 🛑 cancel (even while queued), 🔄 retry, 📄
  dump the full output as a file
- Plan → approve → execute: `!plan` drafts a read-only plan and waits for a ✅
  reaction before touching your files
- Git safety net: optional per-session auto-branching, `!diff` to review a run's
  changes, and `!revert` to throw them away
- Multiple projects, switchable per channel
- Session continuity so follow-up messages continue the same conversation
- State (project, session, usage) survives bot restarts
- A global concurrency cap keeps too many `claude` processes from piling up
- Long replies are automatically split to fit Discord's message limit
- Locked down to specific users (and optionally specific channels)

## Setup

1. Install dependencies:

   ```bash
   pip install -r discord_bot_requirements.txt
   ```

2. Create a Discord application and bot at the
   [Discord Developer Portal](https://discord.com/developers/applications),
   enable the **Message Content Intent**, and invite it with these permissions:
   Send Messages, Read Message History, Add Reactions. Also grant **Manage
   Messages** if you want the same reaction (🛑/🔄/📄) to be clickable more
   than once per run — without it the bot can still add its own reactions and
   read yours, it just can't clear a reaction after acting on it.

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
| `MAX_CONCURRENT` | Max `claude` processes running at once, across all channels (default `2`) |
| `ANTON_DB` | Path to the SQLite file used to persist per-channel state across restarts (default `anton.db` next to the script) |

## Persistence

Each channel's project, session id, model, and usage totals are saved to a small
SQLite database (`anton.db` by default) after every run and every `!project` /
`!model` / `!new`. If the bot restarts, channels pick back up their last project
and Claude Code session — `!cc` will `--resume` where it left off instead of
starting fresh. Delete `anton.db` (or point `ANTON_DB` elsewhere) to reset all
channel state.

## Reaction controls

Every run's status message carries its own reaction controls:

| Reaction | Effect |
| --- | --- |
| 🛑 | Cancel — kills the process if running, or stops it before it starts if still queued |
| 🔄 | Retry — reruns the exact same prompt from scratch |
| 📄 | Dump the full output as a `.md` file attachment |

Only the most recent run per channel has live controls; older status
messages' reactions become inert once a newer run starts.

## Plan mode

`!plan <prompt>` runs Claude Code in read-only **plan mode** first: it drafts
an approach without editing any files, posts the plan into the channel, and adds
two reactions to it:

| Reaction | Effect |
| --- | --- |
| ✅ | Execute the plan — resumes the same session with `acceptEdits` and carries it out |
| 🛑 | Discard the plan — nothing is changed |

Because execution resumes the planning session, Claude keeps the full context of
what it proposed. If you 🔄 retry a plan's status message it re-plans rather than
executing. As with run controls, only the latest plan per channel stays live.

## Git safety net

Claude edits your working tree directly. These commands make git the safety layer
so a bad run is easy to review and undo. They shell out with `git` in the current
project directory and no-op gracefully if it isn't a git repo.

| Command | Effect |
| --- | --- |
| `!diff` | Show what the current session changed — `git diff --stat` inline plus the full patch as a `.diff` attachment |
| `!revert` / `!undo` | Reset the project back to the checkpoint taken before the last run (`git reset --hard` + `git clean -fd`), after a ✅ confirmation |

Before every run the bot records the project's current `HEAD` as a checkpoint, so
`!diff` and `!revert` work even without auto-branching.

Set `AUTO_BRANCH=1` to go a step further: the first run of each session creates an
`anton/<timestamp>-<uuid>` branch and switches to it, so that session's edits are
isolated from your working branch. Follow-up messages in the same session stay on
that branch; `!new` or switching projects starts a fresh one. Auto-branching is
skipped when the tree is already dirty (the bot leaves your uncommitted work
alone and edits in place instead).

Because `!revert` runs `git reset --hard` and `git clean -fd`, it permanently
discards uncommitted work — that's why it always asks first.

## Security

Never commit your real `.env` — it holds your bot token. It is already listed in
`.gitignore`. Keep `ALLOWED_USER_IDS` tight and prefer restricting the bot to
specific channels.
