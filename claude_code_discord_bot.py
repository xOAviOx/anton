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

