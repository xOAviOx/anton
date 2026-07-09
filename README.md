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
  changes, `!revert` to throw them away, and `!commit` / `!pr` to ship them
- Richer input: attach files/images for Claude to read, and reply to a result to
  continue that session
- History & cost: every run is logged (`!history`, `!resume <id>`), spend is
  tracked (`!cost`), an optional `DAILY_BUDGET_USD` caps it, and long runs can
  `@`-mention you when they finish
- Live permission prompts: `!strict on` makes Bash/Edit/Write/etc. wait for a
  ✅/❌ Discord reaction before Claude Code is allowed to run them, instead of
  auto-accepting
- Multiple projects, switchable per channel — add new ones at runtime with
  `!addproject`, or auto-discover git repos under a folder with `!discover`
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
| `!commit [msg]` | Stage everything and commit. With no message, Claude writes a Conventional Commits message from the staged diff; pass your own to override |
| `!pr` | Push the current branch to `origin` and open a GitHub PR with `gh pr create --fill` |

Before every run the bot records the project's current `HEAD` as a checkpoint, so
`!diff` and `!revert` work even without auto-branching.

`!commit` refuses when nothing is staged and never runs mid-session; if Claude
can't be reached to write a message it falls back to a generic one rather than
failing. `!pr` needs the [GitHub CLI](https://cli.github.com/) (`gh`) installed
and authenticated (`gh auth login`) on the host, refuses to run with uncommitted
changes or from `main`/`master`, and surfaces `gh`'s error (e.g. "a PR already
exists") if it can't create one.

Set `AUTO_BRANCH=1` to go a step further: the first run of each session creates an
`anton/<timestamp>-<uuid>` branch and switches to it, so that session's edits are
isolated from your working branch. Follow-up messages in the same session stay on
that branch; `!new` or switching projects starts a fresh one. Auto-branching is
skipped when the tree is already dirty (the bot leaves your uncommitted work
alone and edits in place instead).

Because `!revert` runs `git reset --hard` and `git clean -fd`, it permanently
discards uncommitted work — that's why it always asks first.

## Richer input

**Attachments.** Drop files or images onto a `!cc` or `!plan` message and the bot
saves them, then appends their paths to the prompt so Claude Code can read them —
"implement this mockup", "here's the failing log". You can even send `!cc` with
no text as long as something is attached. Files are saved under `UPLOAD_DIR`
(a temp dir by default, so they never pollute your repo or the git safety net);
attachments over `MAX_ATTACH_MB` (default 25) are skipped and no more than
`MAX_ATTACH_COUNT` (default 10) are taken.

**Reply to continue.** Reply to any of a run's result messages to send a
follow-up into *that* run's session, instead of the channel's current one. This
lets several lines of work coexist in one channel without `!new` — reply to
thread A, then reply to thread B, and each resumes where it left off. The channel
then follows whichever thread you replied to last.

Two things to know: a reply that itself starts with `!` (e.g. replying with
`!diff`) is treated as a normal command, not a continuation — so commands stay
unambiguous. And the result → session map is in memory only, so after a bot
restart, replying to an old message just does nothing (use `!cc` to start fresh).

## History, cost & notifications

Every run is logged to the SQLite db (prompt, session, cost, duration, files
changed, outcome), and Claude's prose now streams into the live status message
as it arrives — not just tool calls.

| Command | Effect |
| --- | --- |
| `!history [n]` | List the last `n` runs in this channel (default 10, max 25) with cost, files changed, outcome, and a `#id` |
| `!resume <id>` | Resume the session from run `#id`; the next `!cc` continues it |
| `!cost` | Spend today and this week (across all channels), plus remaining daily budget |

Set `DAILY_BUDGET_USD` to cap total spend per local day — once reached, `!cc`,
`!plan`, and reply-continuations are refused until midnight (0, the default,
disables the cap). `!resume` re-runs a past thread; because history is on disk,
it survives restarts (unlike reply-to-continue's in-memory map).

Set `NOTIFY_AFTER_SECONDS` (default 120; 0 disables) so any run that takes at
least that long `@`-mentions you in the channel when it finishes — fire a task
from your phone, walk away, and get pinged when it's done.

## Live permission prompts

`!cc` normally runs with `PERMISSION_MODE` (default `acceptEdits`) plus
`ALLOWED_TOOLS`, which auto-accepts file edits and the listed tools — fast, but
no per-action oversight. `!strict on` swaps that out for true remote approval:
Claude Code is launched with `--permission-mode default` and a much smaller
allowed-tools list (`STRICT_ALLOWED_TOOLS`, default `Read,Glob,Grep,TodoWrite`
— safe, read-only). Anything else it wants to do — Bash, Edit, Write,
WebFetch, WebSearch, spawning a Task sub-agent — gets routed to
`anton_mcp_approve.py`, a tiny stdio MCP server the bot spawns alongside
`claude` for that run, wired in via `--permission-prompt-tool
mcp__anton__approve`. It posts the exact command/file/URL to the channel and
waits; react ✅ to allow it or ❌ to deny it. No reaction within
`APPROVAL_TIMEOUT_SECONDS` (default 600 = 10 min) denies it automatically.

`!strict` is per-channel and persists like `!model`. Plan mode (`!plan`) is
unaffected — it's already read-only — and executing an *approved* plan still
runs at full speed (`acceptEdits`), since you already reviewed the whole plan
up front; strict mode is specifically for direct `!cc` runs where nobody's
looked at what Claude's about to do yet.

`anton_mcp_approve.py` must stay next to `claude_code_discord_bot.py` — the
bot passes its own path to `claude` as the MCP server command. It needs no
extra dependencies (stdlib only) and talks to the bot only through the same
`anton.db` SQLite file (an `approvals` table), since it runs as `claude`'s own
subprocess and has no Discord connection of its own.

## Multi-project

Projects configured via `PROJECTS`/env are static — they can't be changed or
removed at runtime, only added to. `!addproject <name> <path>` registers a new
one (validated: the path must exist and be a directory) and persists it to
`anton.db`, so it survives restarts; `!rmproject <name>` removes a
runtime-added one again (static ones aren't removable this way — edit
`PROJECTS`/env instead). `!projects` marks runtime-added entries `(added)` so
you can tell them apart from what's statically configured.

`!discover [path]` scans a directory one level deep for git repos (recognizing
both a `.git` folder and a `.git` file, so worktrees and submodules count) and
registers any not already known, using the subdirectory name as the project
name. Skips anything whose name is already taken, so it won't silently move or
rename an existing entry. Set `PROJECTS_ROOT` to also run this automatically
at startup — handy if you keep all your repos under one parent folder and
want new clones to show up without restarting the bot (just run `!discover`
again after cloning).

## Security

Never commit your real `.env` — it holds your bot token. It is already listed in
`.gitignore`. Keep `ALLOWED_USER_IDS` tight and prefer restricting the bot to
specific channels.
