#!/usr/bin/env python3
"""
Claude Code <-> Discord bridge
==============================

Run Claude Code on your PC and drive it from Discord. You type a prompt in a
channel, the bot runs Claude Code headlessly in the matching project directory,
streams live progress (tool calls) back into Discord, and posts the final answer.

Commands (default prefix "!"):
  !cc <prompt>      Run Claude Code with <prompt> in the current project.
                    Continues the channel's session if one exists.
  !new              Start a fresh session for this channel.
  !project [name]   Show the current project, or switch to a configured one.
  !projects         List configured projects.
  !status           Show current project + session + whether a run is active.
  !cancel           Kill the currently running Claude Code process.
  !help             Show this help.

Each Discord channel keeps its own project + session, so you can run several
projects in parallel from different channels.

--------------------------------------------------------------------------------
SETUP
--------------------------------------------------------------------------------
1. Install Claude Code and log in (or set ANTHROPIC_API_KEY):
       npm install -g @anthropic-ai/claude-code
       claude            # log in once, interactively
   For an always-on/headless box, `claude setup-token` gives a long-lived token,
   or set ANTHROPIC_API_KEY for pay-as-you-go API billing.

2. Install Python deps:
       pip install "discord.py>=2.3" python-dotenv

3. Create a Discord application + bot:
       https://discord.com/developers/applications  ->  New Application  ->  Bot
   - Copy the bot TOKEN.
   - Under "Privileged Gateway Intents", enable **MESSAGE CONTENT INTENT**.
   - Invite it: OAuth2 -> URL Generator -> scopes: bot; bot permissions:
     Send Messages, Read Message History. Open the URL, add it to your server.

4. Get your user ID + channel IDs: Discord -> Settings -> Advanced -> enable
   Developer Mode, then right-click yourself / a channel -> Copy ID.

5. Put a `.env` file next to this script (or export the vars):
       DISCORD_TOKEN=your-bot-token
       ALLOWED_USER_IDS=111111111111111111            # comma-separated, REQUIRED
       ALLOWED_CHANNEL_IDS=222222222222222222         # comma-separated, optional
       # Optional: projects as JSON {"name": "/abs/path"}. If unset, uses PROJECTS below.
       PROJECTS={"maestro":"/home/avi/projects/maestro","vitals":"/home/avi/projects/vitals"}
       DEFAULT_PROJECT=maestro

6. Run it:
       python claude_code_discord_bot.py

--------------------------------------------------------------------------------
SECURITY — read this
--------------------------------------------------------------------------------
This lets whoever is on the allowlist run shell commands + edit files on your
machine, in the configured project dirs, WITHOUT per-action approval (that's the
whole point of remote control). So:
  - ALLOWED_USER_IDS is mandatory. Keep it to just you.
  - Prefer also setting ALLOWED_CHANNEL_IDS to a private channel.
  - Point PROJECTS only at repos you're OK with an agent touching.
  - Consider running the bot inside a container / dedicated user account.
"""

import asyncio
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # .env support is optional

import discord

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PREFIX = "!"
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")

# Permission posture for headless runs. acceptEdits auto-approves file writes and
# common fs commands; listing tools in ALLOWED_TOOLS auto-approves them too.
PERMISSION_MODE = os.getenv("PERMISSION_MODE", "acceptEdits")
ALLOWED_TOOLS = os.getenv("ALLOWED_TOOLS", "Read,Edit,Write,Bash,Glob,Grep,TodoWrite")

# Default model for new channels. Empty string = Claude Code's own default.
# Accepts aliases like "sonnet", "opus", "haiku" or a full model string.
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "")
KNOWN_MODELS = ("sonnet", "opus", "haiku")  # convenience aliases for !model

RUN_TIMEOUT = int(os.getenv("RUN_TIMEOUT", "1800"))  # seconds; hard cap per run
STATUS_EDIT_INTERVAL = 1.3  # seconds between live status edits (Discord rate limits)
MAX_MSG = 1900              # Discord hard limit is 2000; leave headroom

# Projects: env JSON wins, else edit this dict directly.
_PROJECTS_ENV = os.getenv("PROJECTS")
if _PROJECTS_ENV:
    PROJECTS = json.loads(_PROJECTS_ENV)
else:
    PROJECTS = {
        # "maestro": "/home/avi/projects/maestro",
        # "vitals":  "/home/avi/projects/vitals",
    }
DEFAULT_PROJECT = os.getenv("DEFAULT_PROJECT") or (next(iter(PROJECTS), None))

ALLOWED_USER_IDS = {
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x
}
ALLOWED_CHANNEL_IDS = {
    int(x) for x in os.getenv("ALLOWED_CHANNEL_IDS", "").replace(" ", "").split(",") if x
}

TOKEN = os.getenv("DISCORD_TOKEN")

# ---------------------------------------------------------------------------
# Per-channel state
# ---------------------------------------------------------------------------
@dataclass
class ChannelState:
    project: Optional[str] = None
    session_id: Optional[str] = None
    model: Optional[str] = CLAUDE_MODEL or None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    proc: Optional[asyncio.subprocess.Process] = None  # active process, if any
    # Usage tracking (cumulative for this channel, since the bot started)
    runs: int = 0
    total_cost: float = 0.0
    last_rate_limit: Optional[dict] = None  # most recent rate_limit_event payload

STATE: dict[int, ChannelState] = {}

def state_for(channel_id: int) -> ChannelState:
    st = STATE.get(channel_id)
    if st is None:
        st = ChannelState(project=DEFAULT_PROJECT)
        STATE[channel_id] = st
    return st

