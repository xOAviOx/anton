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
import signal
import sqlite3
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

# Durable state (survives restarts): channel -> project/session/model/usage.
DB_PATH = os.getenv(
    "ANTON_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "anton.db")
)

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
# Durable state (SQLite) — survives bot restarts
# ---------------------------------------------------------------------------
def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS channel_state (
            channel_id  INTEGER PRIMARY KEY,
            project     TEXT,
            session_id  TEXT,
            model       TEXT,
            runs        INTEGER NOT NULL DEFAULT 0,
            total_cost  REAL NOT NULL DEFAULT 0.0
        )
        """
    )
    return conn


def _load_row(channel_id: int) -> Optional[sqlite3.Row]:
    conn = _db()
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT project, session_id, model, runs, total_cost "
            "FROM channel_state WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
    finally:
        conn.close()


def save_state(channel_id: int, st: "ChannelState") -> None:
    """Persist the durable fields of a channel's state to SQLite. Called after
    every mutation (session/project/model change, run completion) so a bot
    restart doesn't lose in-flight conversations."""
    conn = _db()
    try:
        conn.execute(
            """
            INSERT INTO channel_state (channel_id, project, session_id, model, runs, total_cost)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                project    = excluded.project,
                session_id = excluded.session_id,
                model      = excluded.model,
                runs       = excluded.runs,
                total_cost = excluded.total_cost
            """,
            (channel_id, st.project, st.session_id, st.model, st.runs, st.total_cost),
        )
        conn.commit()
    finally:
        conn.close()


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
    # Usage tracking (cumulative for this channel, persisted across restarts)
    runs: int = 0
    total_cost: float = 0.0
    last_rate_limit: Optional[dict] = None  # most recent rate_limit_event payload (not persisted)

STATE: dict[int, ChannelState] = {}

def state_for(channel_id: int) -> ChannelState:
    st = STATE.get(channel_id)
    if st is None:
        row = _load_row(channel_id)
        if row is not None:
            st = ChannelState(
                project=row["project"] or DEFAULT_PROJECT,
                session_id=row["session_id"],
                model=row["model"] if row["model"] is not None else (CLAUDE_MODEL or None),
                runs=row["runs"],
                total_cost=row["total_cost"],
            )
        else:
            st = ChannelState(project=DEFAULT_PROJECT)
            save_state(channel_id, st)
        STATE[channel_id] = st
    return st

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def build_command(prompt: str, session_id: Optional[str],
                  model: Optional[str] = None) -> list[str]:
    """Resolve the claude binary and assemble the headless invocation."""
    resolved = shutil.which(CLAUDE_BIN) or CLAUDE_BIN
    args = [
        resolved,
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", PERMISSION_MODE,
        "--allowedTools", ALLOWED_TOOLS,
    ]
    if model:
        args += ["--model", model]
    if session_id:
        args += ["--resume", session_id]

    # On Windows, npm installs `claude.cmd`, which must run via cmd.exe.
    if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
        args = ["cmd", "/c"] + args
    return args


def summarize_tool(name: str, inp: dict) -> str:
    """One-line, human-readable summary of a tool_use block."""
    def short(s, n=90):
        s = " ".join(str(s).split())
        return s if len(s) <= n else s[: n - 1] + "…"

    icons = {"Bash": "🖥️", "Read": "📖", "Edit": "✏️", "Write": "📝",
             "Glob": "🔎", "Grep": "🔎", "TodoWrite": "🗒️", "WebFetch": "🌐",
             "WebSearch": "🌐", "Task": "🤖"}
    icon = icons.get(name, "🔧")
    if name == "Bash":
        return f"{icon} `{short(inp.get('command', ''))}`"
    if name in ("Read", "Edit", "Write"):
        return f"{icon} {name}: {short(inp.get('file_path', ''))}"
    if name in ("Glob", "Grep"):
        return f"{icon} {name}: {short(inp.get('pattern', ''))}"
    if name == "TodoWrite":
        return f"{icon} updating todo list"
    if name == "Task":
        return f"{icon} sub-agent: {short(inp.get('description', ''))}"
    return f"{icon} {name}"


def chunk(text: str, size: int = MAX_MSG) -> list[str]:
    """Split text into Discord-sized pieces, preferring newline boundaries."""
    text = text.strip()
    if not text:
        return []
    out, buf = [], ""
    for line in text.split("\n"):
        while len(line) > size:  # a single very long line
            out.append(line[:size])
            line = line[size:]
        if len(buf) + len(line) + 1 > size:
            out.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        out.append(buf)
    return out


async def kill(proc: Optional[asyncio.subprocess.Process]):
    """Terminate a run, including any children it spawned (bash, subagents, MCP
    servers). `claude` is launched in its own process group (see run_claude), so
    we signal the whole group rather than just the parent PID — otherwise
    `!cancel` and timeouts can leave orphaned children running."""
    if not proc or proc.returncode is not None:
        return

    def _signal(sig: int):
        if os.name != "nt":
            try:
                os.killpg(os.getpgid(proc.pid), sig)
                return
            except ProcessLookupError:
                return
        # Windows has no process groups here; fall back to the direct handle.
        try:
            proc.terminate() if sig == signal.SIGTERM else proc.kill()
        except ProcessLookupError:
            pass

    try:
        _signal(signal.SIGTERM)
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        _signal(signal.SIGKILL)

# ---------------------------------------------------------------------------
# Core: run Claude Code and stream progress into a Discord message
# ---------------------------------------------------------------------------
async def run_claude(message: discord.Message, st: ChannelState, prompt: str):
    channel_id = message.channel.id
    project_path = PROJECTS[st.project]
    cmd = build_command(prompt, st.session_id, st.model)

    status = await message.reply(f"🧠 Working in **{st.project}**…")
    activity: list[str] = []
    final_text = ""
    new_session = st.session_id
    cost = None
    is_error = False
    last_edit = 0.0

    async def render():
        nonlocal last_edit
        tail = activity[-12:]
        body = f"🧠 **{st.project}** · running…\n" + ("\n".join(tail) if tail else "_thinking…_")
        try:
            await status.edit(content=body[:MAX_MSG])
        except discord.HTTPException:
            pass
        last_edit = time.monotonic()

    # Background ticker so the status message updates even during quiet thinking phases.
    async def ticker():
        while True:
            await asyncio.sleep(STATUS_EDIT_INTERVAL)
            if time.monotonic() - last_edit > STATUS_EDIT_INTERVAL:
                await render()

    tick_task: Optional[asyncio.Task] = None

    try:
        spawn_kwargs = {}
        if os.name != "nt":
            # Own process group so kill() can signal the whole tree, not just
            # the `claude` parent (it spawns bash/subagents/MCP children).
            spawn_kwargs["start_new_session"] = True
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=project_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **spawn_kwargs,
        )
        st.proc = proc

        stderr_task = asyncio.create_task(proc.stderr.read())
        tick_task = asyncio.create_task(ticker())

        async def read_stream():
            nonlocal final_text, new_session, cost, is_error
            if proc.stdout is None:
                return
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = evt.get("type")

                if etype == "system":
                    if evt.get("subtype") == "init":
                        new_session = evt.get("session_id", new_session)
                    elif evt.get("subtype") == "api_retry":
                        activity.append(f"⏳ retrying ({evt.get('error', 'error')})…")

                elif etype == "assistant":
                    for block in evt.get("message", {}).get("content", []):
                        if block.get("type") == "tool_use":
                            activity.append(summarize_tool(block.get("name", "?"),
                                                           block.get("input", {})))

                elif etype == "result":
                    final_text = evt.get("result") or final_text
                    new_session = evt.get("session_id", new_session)
                    cost = evt.get("total_cost_usd", cost)
                    is_error = bool(evt.get("is_error"))

                elif etype == "rate_limit_event":
                    info = evt.get("rate_limit_info")
                    if info:
                        st.last_rate_limit = info

                if time.monotonic() - last_edit > STATUS_EDIT_INTERVAL:
                    await render()

        await asyncio.wait_for(read_stream(), timeout=RUN_TIMEOUT)
        await proc.wait()

        tick_task.cancel()
        stderr_bytes = await stderr_task
        stderr = stderr_bytes.decode(errors="replace")

        st.session_id = new_session  # persist for next !cc
        st.runs += 1
        if cost is not None:
            st.total_cost += cost

        # Final status line
        footer = f"✅ done · **{st.project}**"
        if cost is not None:
            footer += f" · ${cost:.4f}"
        if is_error or proc.returncode not in (0, None):
            footer = f"⚠️ finished with issues · **{st.project}** (exit {proc.returncode})"
        await status.edit(content=footer[:MAX_MSG])

        pieces = chunk(final_text) or ["_(no textual output)_"]
        for p in pieces:
            await message.channel.send(p)
        if (is_error or proc.returncode not in (0, None)) and stderr.strip():
            for p in chunk("stderr:\n" + stderr):
                await message.channel.send(f"```\n{p}\n```")

    except asyncio.TimeoutError:
        await kill(st.proc)
        await status.edit(content=f"⏱️ timed out after {RUN_TIMEOUT}s · **{st.project}**")
    except FileNotFoundError:
        await status.edit(
            content="❌ `claude` not found. Install Claude Code and make sure it's on PATH "
                    "(or set CLAUDE_BIN to the full path)."
        )
    except asyncio.CancelledError:
        await kill(st.proc)
        await status.edit(content=f"🛑 cancelled · **{st.project}**")
        raise
    except Exception as e:  # noqa: BLE001 - surface anything else to the user
        await kill(st.proc)
        await status.edit(content=f"❌ error: {type(e).__name__}: {str(e)[:400]}")
    finally:
        st.proc = None
        if tick_task is not None and not tick_task.done():
            tick_task.cancel()
        # Even on a timeout/error, `new_session` may hold a session id from
        # the init event — keep it so the next !cc can still --resume.
        if new_session:
            st.session_id = new_session
        save_state(channel_id, st)

# ---------------------------------------------------------------------------
# Discord client + command handling
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

HELP = (
    "**Claude Code control**\n"
    f"`{PREFIX}cc <prompt>` — run Claude Code in the current project (continues the session)\n"
    f"`{PREFIX}new` — start a fresh session\n"
    f"`{PREFIX}project [name]` — show or switch project\n"
    f"`{PREFIX}projects` — list projects\n"
    f"`{PREFIX}model [name]` — show or switch model (sonnet / opus / haiku / default)\n"
    f"`{PREFIX}usage` — runs, cost, and rate-limit utilization\n"
    f"`{PREFIX}status` — current project / session / activity\n"
    f"`{PREFIX}cancel` — kill the running task\n"
)


def authorized(message: discord.Message) -> bool:
    if message.author.id not in ALLOWED_USER_IDS:
        return False
    if ALLOWED_CHANNEL_IDS and message.channel.id not in ALLOWED_CHANNEL_IDS:
        return False
    return True


@client.event
async def on_ready():
    print(f"Logged in as {client.user} (id {client.user.id})")
    print(f"Allowed users: {ALLOWED_USER_IDS or 'NONE — nobody can use the bot!'}")
    print(f"Projects: {list(PROJECTS) or 'NONE — set PROJECTS'}")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.content.startswith(PREFIX):
        return
    if not authorized(message):
        return  # silently ignore non-allowlisted users/channels

    parts = message.content[len(PREFIX):].split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    st = state_for(message.channel.id)

    if cmd == "help":
        await message.reply(HELP)

    elif cmd == "projects":
        if not PROJECTS:
            await message.reply("No projects configured. Set PROJECTS in env or the script.")
        else:
            lines = "\n".join(f"• `{n}` → {p}" for n, p in PROJECTS.items())
            await message.reply(f"**Projects:**\n{lines}")

    elif cmd == "project":
        if not arg:
            await message.reply(f"Current project: **{st.project or 'none'}**")
        elif arg not in PROJECTS:
            await message.reply(f"Unknown project `{arg}`. Try `{PREFIX}projects`.")
        else:
            st.project = arg
            st.session_id = None  # sessions are scoped to a directory
            save_state(message.channel.id, st)
            await message.reply(f"Switched to **{arg}** (fresh session).")

    elif cmd == "model":
        if not arg:
            await message.reply(
                f"Current model: **{st.model or 'default'}**\n"
                f"Switch with `{PREFIX}model <sonnet|opus|haiku|default|full-model-string>`."
            )
        elif arg.lower() in ("default", "reset", "none"):
            st.model = None
            save_state(message.channel.id, st)
            await message.reply("Model reset to **default**.")
        else:
            st.model = arg.lower() if arg.lower() in KNOWN_MODELS else arg
            save_state(message.channel.id, st)
            await message.reply(f"Model set to **{st.model}** for this channel.")

    elif cmd == "usage":
        lines = [
            f"**Usage — {st.project or 'none'}**",
            f"Runs this session: **{st.runs}**",
            f"Cost this session: **${st.total_cost:.4f}**",
            f"Model: **{st.model or 'default'}**",
        ]
        rl = st.last_rate_limit
        if rl:
            util = rl.get("utilization")
            rl_type = rl.get("rateLimitType", "limit")
            if util is not None:
                lines.append(f"Rate limit ({rl_type}): **{float(util) * 100:.0f}%** used")
            resets = rl.get("resetsAt")
            if resets:
                lines.append(f"Resets: <t:{int(resets)}:R>")
        else:
            lines.append("_Rate-limit info appears after your first `!cc` run._")
        await message.reply("\n".join(lines))

    elif cmd == "new":
        st.session_id = None
        save_state(message.channel.id, st)
        await message.reply(f"Started a fresh session for **{st.project or 'none'}**.")

    elif cmd == "status":
        running = "yes" if st.proc and st.proc.returncode is None else "no"
        await message.reply(
            f"Project: **{st.project or 'none'}**\n"
            f"Session: `{st.session_id or 'none'}`\n"
            f"Running: {running}"
        )

    elif cmd == "cancel":
        if st.proc and st.proc.returncode is None:
            await kill(st.proc)
            await message.reply("🛑 Killed the running task.")
        else:
            await message.reply("Nothing is running.")

    elif cmd == "cc":
        if not arg:
            await message.reply(f"Usage: `{PREFIX}cc <prompt>`")
            return
        if not st.project or st.project not in PROJECTS:
            await message.reply(f"No valid project set. Use `{PREFIX}project <name>`.")
            return
        if st.lock.locked():
            await message.reply(
                f"A task is already running in this channel. `{PREFIX}cancel` to stop it, "
                "or use another channel."
            )
            return
        async with st.lock:
            await run_claude(message, st, arg)


def main():
    if not TOKEN:
        sys.exit("DISCORD_TOKEN is not set.")
    if not ALLOWED_USER_IDS:
        sys.exit("ALLOWED_USER_IDS is empty — refusing to start an open bot.")
    if not PROJECTS:
        print("WARNING: no PROJECTS configured; !cc won't work until you add one.")
    client.run(TOKEN)


if __name__ == "__main__":
    main()
