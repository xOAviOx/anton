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
  !plan <prompt>    Draft a plan first (read-only). Bot posts the plan and waits
                    for a ✅ reaction to execute it (or 🛑 to discard).
  !new              Start a fresh session for this channel.
  !project [name]   Show the current project, or switch to a configured one.
  !projects         List configured projects.
  !model [name]     Show or switch the model (sonnet / opus / haiku / default).
  !usage            Runs, cost, and rate-limit utilization for this channel.
  !history [n]      List recent runs in this channel (default 10).
  !resume <id>      Resume the session from a past run (see !history).
  !cost             Spend today / this week, and remaining daily budget.
  !diff             Show what the current session changed (stat + full patch).
  !revert / !undo   Throw away the last run's changes (asks to confirm first).
  !commit [msg]     Stage all changes and commit; Claude writes the message if
                    you don't supply one.
  !pr               Push the current branch and open a GitHub PR (needs `gh`).
  !status           Show current project + session + whether a run is active.
  !cancel           Kill the currently running Claude Code process.
  !help             Show this help.

Every run's status message also carries reaction controls: 🛑 cancel (works
even while a run is still queued behind MAX_CONCURRENT), 🔄 retry the same
prompt, 📄 dump the full output as a file attachment.

Attach files or images to a !cc/!plan message and their paths are passed to
Claude Code (e.g. "implement this mockup"). Reply to a run's result message to
send a follow-up into *that* session, so several lines of work can coexist in
one channel without !new.

With AUTO_BRANCH=1, each fresh session's edits are isolated on an `anton/<ts>`
git branch so they're easy to review (!diff) or discard (!revert).

Every run is logged for !history / !resume and cost tracking. Set
DAILY_BUDGET_USD to cap daily spend and NOTIFY_AFTER_SECONDS to be @-mentioned
when a long run finishes.

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
import io
import json
import os
import shutil
import signal
import sqlite3
import sys
import tempfile
import time
import uuid
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

# Global cap on simultaneous `claude` processes, across all channels.
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "2"))

# Git safety net (Phase 2). When on, each fresh session auto-creates an
# `anton/<ts>-<uuid>` branch in the project before Claude touches the tree, so a
# run's edits are isolated and reviewable (!diff) or throwable-away (!revert).
AUTO_BRANCH = os.getenv("AUTO_BRANCH", "0").strip().lower() in ("1", "true", "yes", "on")

# Attachments (Phase 3.1): files dropped alongside a !cc/!plan (or a reply-to-
# continue) are saved under UPLOAD_DIR and their paths appended to the prompt so
# Claude Code can Read them. Temp dir by default → no repo pollution and no
# interaction with the git safety net.
UPLOAD_DIR = os.getenv("UPLOAD_DIR", os.path.join(tempfile.gettempdir(), "anton-uploads"))
MAX_ATTACH_MB = float(os.getenv("MAX_ATTACH_MB", "25"))
MAX_ATTACH_COUNT = int(os.getenv("MAX_ATTACH_COUNT", "10"))

# Cost & notifications (Phase 4). DAILY_BUDGET_USD caps total spend across all
# channels per local day (0 = no cap). NOTIFY_AFTER_SECONDS @-mentions you when a
# run that took at least this long finishes (0 = never).
DAILY_BUDGET_USD = float(os.getenv("DAILY_BUDGET_USD", "0"))
NOTIFY_AFTER_SECONDS = int(os.getenv("NOTIFY_AFTER_SECONDS", "120"))


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
    # Phase 2 git-safety columns, added by migration so pre-Phase-2 databases
    # upgrade in place. run_branch: the anton/… branch for the current session;
    # branched_for_session: which session_id we already branched for (idempotence);
    # pre_run_ref: HEAD captured before the latest run, for !revert.
    for col in ("run_branch TEXT", "branched_for_session TEXT", "pre_run_ref TEXT"):
        try:
            conn.execute(f"ALTER TABLE channel_state ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass  # duplicate column — already migrated
    # Phase 4: one row per run, for !history / !resume and daily-budget sums.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id     INTEGER NOT NULL,
            project        TEXT,
            session_id     TEXT,
            prompt         TEXT,
            cost           REAL,
            duration       REAL,
            files_changed  INTEGER,
            outcome        TEXT,
            ts             REAL NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _load_row(channel_id: int) -> Optional[sqlite3.Row]:
    conn = _db()
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT project, session_id, model, runs, total_cost, "
            "run_branch, branched_for_session, pre_run_ref "
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
            INSERT INTO channel_state
                (channel_id, project, session_id, model, runs, total_cost,
                 run_branch, branched_for_session, pre_run_ref)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                project              = excluded.project,
                session_id           = excluded.session_id,
                model                = excluded.model,
                runs                 = excluded.runs,
                total_cost           = excluded.total_cost,
                run_branch           = excluded.run_branch,
                branched_for_session = excluded.branched_for_session,
                pre_run_ref          = excluded.pre_run_ref
            """,
            (channel_id, st.project, st.session_id, st.model, st.runs, st.total_cost,
             st.run_branch, st.branched_for_session, st.pre_run_ref),
        )
        conn.commit()
    finally:
        conn.close()


def log_run(*, channel_id: int, project: Optional[str], session_id: Optional[str],
            prompt: str, cost: Optional[float], duration: Optional[float],
            files_changed: Optional[int], outcome: str, ts: float) -> None:
    """Append one row to the runs table (Phase 4.1). Best-effort — a logging
    failure must never propagate into the run's own error handling."""
    try:
        conn = _db()
        try:
            conn.execute(
                "INSERT INTO runs (channel_id, project, session_id, prompt, cost, "
                "duration, files_changed, outcome, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (channel_id, project, session_id, prompt, cost, duration,
                 files_changed, outcome, ts),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - never let history logging break a run
        pass


def spend_since(epoch: float) -> float:
    """Total cost across all channels for runs at or after `epoch` (Phase 4.2)."""
    conn = _db()
    try:
        return float(conn.execute(
            "SELECT COALESCE(SUM(cost), 0) FROM runs WHERE ts >= ?", (epoch,)
        ).fetchone()[0])
    finally:
        conn.close()


def recent_runs(channel_id: int, n: int) -> list[sqlite3.Row]:
    """The most recent `n` runs for a channel, newest first (Phase 4.1)."""
    conn = _db()
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT id, project, session_id, prompt, cost, duration, files_changed, "
            "outcome, ts FROM runs WHERE channel_id = ? ORDER BY id DESC LIMIT ?",
            (channel_id, n),
        ).fetchall()
    finally:
        conn.close()


def run_by_id(channel_id: int, run_id: int) -> Optional[sqlite3.Row]:
    """One run by id, scoped to the channel (so you can't resume another's)."""
    conn = _db()
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT id, project, session_id, prompt FROM runs "
            "WHERE id = ? AND channel_id = ?",
            (run_id, channel_id),
        ).fetchone()
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
    # Git safety net (Phase 2), persisted. run_branch is the isolation branch for
    # the current session; branched_for_session records the session it was made
    # for; pre_run_ref is HEAD just before the latest run (the !revert target).
    run_branch: Optional[str] = None
    branched_for_session: Optional[str] = None
    pre_run_ref: Optional[str] = None

STATE: dict[int, ChannelState] = {}

# Caps how many `claude` processes can run at once across all channels/projects.
RUN_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT)

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
                run_branch=row["run_branch"],
                branched_for_session=row["branched_for_session"],
                pre_run_ref=row["pre_run_ref"],
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
                  model: Optional[str] = None,
                  permission_mode: Optional[str] = None) -> list[str]:
    """Resolve the claude binary and assemble the headless invocation."""
    resolved = shutil.which(CLAUDE_BIN) or CLAUDE_BIN
    args = [
        resolved,
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", permission_mode or PERMISSION_MODE,
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


async def claude_text(prompt: str, cwd: str, *, model: Optional[str] = None,
                      timeout: int = 120) -> Optional[str]:
    """One-off, tool-free, non-streaming `claude -p` for short helper prompts
    (e.g. writing a commit message). Returns the trimmed text, or None on any
    failure. Deliberately NOT gated by RUN_SEMAPHORE — it's short-lived and
    shouldn't queue behind long interactive runs. Fresh session each time (no
    --resume) so it can't pollute or depend on a channel's conversation.
    """
    resolved = shutil.which(CLAUDE_BIN) or CLAUDE_BIN
    args = [
        resolved,
        "-p", prompt,
        "--output-format", "text",
        "--permission-mode", "plan",  # read-only: it must not touch the tree
        "--allowedTools", "",         # no tools at all
    ]
    if model:
        args += ["--model", model]
    if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
        args = ["cmd", "/c"] + args
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, FileNotFoundError, OSError):
        return None
    if proc.returncode not in (0, None):
        return None
    text = out.decode(errors="replace").strip()
    return text or None


# ---------------------------------------------------------------------------
# Attachments (Phase 3.1): save files dropped in Discord and reference them in
# the prompt so Claude Code can Read them (images natively, text via Read).
# ---------------------------------------------------------------------------
async def save_attachments(message: "discord.Message") -> list[str]:
    """Save a message's attachments under UPLOAD_DIR and return absolute paths.
    Skips attachments over MAX_ATTACH_MB and caps the count at MAX_ATTACH_COUNT.
    Best-effort: an attachment that fails to save is skipped, not fatal."""
    atts = getattr(message, "attachments", None) or []
    if not atts:
        return []
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    dest_dir = tempfile.mkdtemp(prefix="msg-", dir=UPLOAD_DIR)
    saved: list[str] = []
    used: set[str] = set()
    for att in atts[:MAX_ATTACH_COUNT]:
        size = getattr(att, "size", 0) or 0
        if size > MAX_ATTACH_MB * 1024 * 1024:
            continue  # too big — skip (caller notes the skip)
        # Sanitise: keep only the base name so a crafted filename can't escape.
        name = os.path.basename(att.filename or "file") or "file"
        # De-dupe within this message so two same-named files don't clobber.
        if name in used:
            root, ext = os.path.splitext(name)
            i = 1
            while f"{root}-{i}{ext}" in used:
                i += 1
            name = f"{root}-{i}{ext}"
        dest = os.path.join(dest_dir, name)
        try:
            await att.save(dest)
        except Exception:  # noqa: BLE001 - a bad attachment shouldn't kill the run
            continue
        used.add(name)
        saved.append(dest)
    return saved


def augment_prompt(text: str, paths: list[str]) -> str:
    """Append saved attachment paths to a prompt so Claude knows to read them."""
    if not paths:
        return text
    listing = "\n".join(f"- {p}" for p in paths)
    note = "Attached files (saved locally — read them with the Read tool):\n" + listing
    return f"{text}\n\n{note}" if text.strip() else note


def preview(text: str, n: int = 200) -> str:
    """Collapse whitespace and truncate to ~n chars with an ellipsis."""
    s = " ".join(str(text).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def summarize_tool(name: str, inp: dict) -> str:
    """One-line, human-readable summary of a tool_use block."""
    def short(s, n=90):
        return preview(s, n)

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
# Git safety net (Phase 2): auto-branch, diff, revert. All shell out in the
# project directory. These are best-effort — a non-git project just skips them.
# ---------------------------------------------------------------------------
async def _git(cwd: str, *args: str) -> tuple[int, str, str]:
    """Run `git <args>` in cwd. Returns (returncode, stdout, stderr), stripped."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return (
        proc.returncode if proc.returncode is not None else -1,
        out.decode(errors="replace").strip(),
        err.decode(errors="replace").strip(),
    )


async def _is_git_repo(cwd: str) -> bool:
    code, out, _ = await _git(cwd, "rev-parse", "--is-inside-work-tree")
    return code == 0 and out == "true"


async def _count_changed(project_path: str, ref: Optional[str]) -> Optional[int]:
    """Number of files changed since `ref` (for the runs log). None if we can't
    tell (no ref, not a git repo)."""
    if not ref or not await _is_git_repo(project_path):
        return None
    code, out, _ = await _git(project_path, "diff", "--name-only", ref)
    if code != 0:
        return None
    return len([ln for ln in out.splitlines() if ln.strip()])


# ---------------------------------------------------------------------------
# Cost & budget (Phase 4.2). Days/weeks are local time; sums come from the runs
# table so they survive restarts and span every channel (one global budget knob).
# ---------------------------------------------------------------------------
def _day_start_epoch() -> float:
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def _week_start_epoch() -> float:
    # Monday 00:00 of the current local week.
    t = time.localtime()
    monday = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
    return monday - t.tm_wday * 86400


def budget_blocked() -> Optional[str]:
    """A user-facing message if today's spend has hit DAILY_BUDGET_USD, else None."""
    if DAILY_BUDGET_USD <= 0:
        return None
    today = spend_since(_day_start_epoch())
    if today >= DAILY_BUDGET_USD:
        return (f"🚫 Daily budget of ${DAILY_BUDGET_USD:.2f} reached "
                f"(spent ${today:.4f} today). Resets at local midnight, or raise "
                "`DAILY_BUDGET_USD`.")
    return None


async def _ensure_branch(st: ChannelState, project_path: str) -> Optional[str]:
    """Called before every run. Always records pre_run_ref (HEAD) so !revert has a
    target. When AUTO_BRANCH is on and this session hasn't branched yet, create an
    isolation branch `anton/<ts>-<uuid>` and switch to it. Returns the name of a
    branch newly created on this call (for a one-time notice), else None.

    Idempotent per session: branched_for_session is keyed to session_id so
    follow-up messages in the same session stay on the one branch instead of
    forking a new one each time.
    """
    if not await _is_git_repo(project_path):
        return None

    code, head, _ = await _git(project_path, "rev-parse", "HEAD")
    if code == 0 and head:
        st.pre_run_ref = head  # capture per run, for !revert

    if not AUTO_BRANCH:
        return None
    # Branch exactly once per session: run_branch is set on creation and cleared
    # on session reset (!new / !project switch), so follow-up messages in the same
    # session reuse the one branch instead of each forking a new one.
    if st.run_branch:
        return None

    # Don't fork off a dirty tree — warn instead (handled by caller via return).
    code, dirty, _ = await _git(project_path, "status", "--porcelain")
    if code != 0:
        return None
    if dirty:
        return None  # caller leaves the tree as-is; auto-branch skipped this run

    # ts + short uuid so two branches made in the same second don't collide.
    ts = time.strftime("%Y%m%d-%H%M%S")
    branch = f"anton/{ts}-{uuid.uuid4().hex[:6]}"
    code, _, err = await _git(project_path, "switch", "-c", branch)
    if code != 0:
        return None
    st.run_branch = branch
    st.branched_for_session = st.session_id  # informational (may backfill later)
    return branch


# ---------------------------------------------------------------------------
# Reaction controls: 🛑 cancel / 🔄 retry / 📄 dump output on run status
# messages, and ✅ execute / 🛑 discard on plan-mode proposals.
# ---------------------------------------------------------------------------
@dataclass
class RunContext:
    channel_id: int
    prompt: str
    trigger_message: discord.Message
    permission_mode: Optional[str] = None
    plan_mode: bool = False
    final_text: str = ""
    cancelled: bool = False  # set by a 🛑 reaction while the run is still queued

@dataclass
class PlanContext:
    channel_id: int
    session_id: str
    prompt: str
    trigger_message: discord.Message

@dataclass
class RevertContext:
    channel_id: int
    ref: str      # pre-run HEAD to reset back to
    project: str

@dataclass
class ResultContext:
    channel_id: int
    session_id: str
    project: str

# Keyed by the Discord message id carrying the controls. Pruned to the most
# recent entry per channel (see run_claude) so these stay small forever.
RUN_BY_MESSAGE: dict[int, RunContext] = {}
PLAN_BY_MESSAGE: dict[int, PlanContext] = {}
REVERT_BY_MESSAGE: dict[int, "RevertContext"] = {}
# Maps a posted result message → the session that produced it, so replying to it
# continues that session (Phase 3.2). NOT pruned per channel — replying to an old
# result is the point — just bounded so it can't grow forever. In-memory only:
# replies stop resolving after a restart.
RESULT_BY_MESSAGE: dict[int, "ResultContext"] = {}
RESULT_MAP_MAX = 500


def _register_result(message_id: int, ctx: "ResultContext") -> None:
    """Record a result→session mapping, evicting the oldest once over the cap."""
    RESULT_BY_MESSAGE[message_id] = ctx
    while len(RESULT_BY_MESSAGE) > RESULT_MAP_MAX:
        RESULT_BY_MESSAGE.pop(next(iter(RESULT_BY_MESSAGE)))


async def _set_post_run_reactions(status: discord.Message):
    """Swap the in-progress 🛑 for 🔄 retry / 📄 dump-output controls. Clearing
    the old reaction needs Manage Messages; if the bot doesn't have it, 🛑
    just stays visible alongside the others (harmless — it's a no-op once the
    process has already exited)."""
    try:
        await status.clear_reaction("🛑")
    except discord.HTTPException:
        pass
    for emoji in ("🔄", "📄"):
        try:
            await status.add_reaction(emoji)
        except discord.HTTPException:
            pass

# ---------------------------------------------------------------------------
# Core: run Claude Code and stream progress into a Discord message
# ---------------------------------------------------------------------------
async def run_claude(message: discord.Message, st: ChannelState, prompt: str, *,
                      permission_mode: Optional[str] = None, plan_mode: bool = False):
    channel_id = message.channel.id
    project_path = PROJECTS[st.project]
    cmd = build_command(prompt, st.session_id, st.model, permission_mode)

    icon = "📋" if plan_mode else "🧠"
    verb = "Planning" if plan_mode else "Working"
    status = await message.reply(f"{icon} {verb} in **{st.project}**…")

    ctx = RunContext(
        channel_id=channel_id, prompt=prompt, trigger_message=message,
        permission_mode=permission_mode, plan_mode=plan_mode,
    )
    # Only the latest run/plan for a channel should have live controls — an
    # older 🛑/🔄/📄 or ✅/🛑 pair would act on state a newer run already moved on from.
    for mid in [m for m, c in RUN_BY_MESSAGE.items() if c.channel_id == channel_id]:
        RUN_BY_MESSAGE.pop(mid, None)
    for mid in [m for m, c in PLAN_BY_MESSAGE.items() if c.channel_id == channel_id]:
        PLAN_BY_MESSAGE.pop(mid, None)
    RUN_BY_MESSAGE[status.id] = ctx
    try:
        await status.add_reaction("🛑")
    except discord.HTTPException:
        pass

    activity: list[str] = []
    final_text = ""
    new_session = st.session_id
    cost = None
    is_error = False
    last_edit = 0.0
    run_start = time.monotonic()  # for duration (Phase 4.1) / notify (4.4)
    spawned = False               # did the subprocess actually start?
    outcome = "ok"                # ok | error | timeout | cancelled | crash | not-found

    async def render():
        nonlocal last_edit
        tail = activity[-12:]
        state_word = "planning" if plan_mode else "running"
        body = f"{icon} **{st.project}** · {state_word}…\n" + ("\n".join(tail) if tail else "_thinking…_")
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

    if RUN_SEMAPHORE.locked():
        try:
            await status.edit(
                content=f"⏳ queued · **{st.project}** (max {MAX_CONCURRENT} concurrent runs)…"
            )
        except discord.HTTPException:
            pass

    async with RUN_SEMAPHORE:
        try:
            if ctx.cancelled:
                await status.edit(content=f"🛑 cancelled (was queued) · **{st.project}**")
                return

            # Git safety net: record the pre-run ref and (if AUTO_BRANCH) isolate
            # this session's edits on an anton/… branch. Skipped for plan mode,
            # which is read-only. Best-effort — non-git projects just no-op.
            if not plan_mode:
                try:
                    new_branch = await _ensure_branch(st, project_path)
                    if new_branch:
                        await message.channel.send(
                            f"🌿 Isolating changes on branch `{new_branch}`."
                        )
                except Exception:  # noqa: BLE001 - git must never break a run
                    pass

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
            spawned = True

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
                            btype = block.get("type")
                            if btype == "tool_use":
                                activity.append(summarize_tool(block.get("name", "?"),
                                                               block.get("input", {})))
                            elif btype == "text":
                                # Stream Claude's prose live, not just tool calls (4.3).
                                txt = block.get("text", "").strip()
                                if txt:
                                    activity.append(f"💬 {preview(txt)}")

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

            st.session_id = new_session  # persist for next !cc / plan approval
            st.runs += 1
            if cost is not None:
                st.total_cost += cost
            ctx.final_text = final_text

            failed = is_error or proc.returncode not in (0, None)
            if failed:
                outcome = "error"

            async def send_result(text: str):
                """Post a result chunk and map it to this session so a reply to it
                continues the conversation (Phase 3.2)."""
                m = await message.channel.send(text)
                if new_session:
                    _register_result(m.id, ResultContext(
                        channel_id=channel_id, session_id=new_session, project=st.project,
                    ))
                return m

            if failed:
                footer = f"⚠️ finished with issues · **{st.project}** (exit {proc.returncode})"
                await status.edit(content=footer[:MAX_MSG])
                pieces = chunk(final_text) or ["_(no textual output)_"]
                for p in pieces:
                    await send_result(p)
                if stderr.strip():
                    for p in chunk("stderr:\n" + stderr):
                        await message.channel.send(f"```\n{p}\n```")

            elif plan_mode:
                footer = f"📋 plan ready · **{st.project}**"
                if cost is not None:
                    footer += f" · ${cost:.4f}"
                await status.edit(content=footer[:MAX_MSG])

                plan_body = f"📋 **Plan for {st.project}:**\n\n" + (final_text or "_(empty plan)_")
                plan_msg = None
                for p in chunk(plan_body) or [plan_body[:MAX_MSG]]:
                    plan_msg = await message.channel.send(p)
                if plan_msg and new_session:
                    PLAN_BY_MESSAGE[plan_msg.id] = PlanContext(
                        channel_id=channel_id, session_id=new_session,
                        prompt=prompt, trigger_message=message,
                    )
                    try:
                        await plan_msg.add_reaction("✅")
                        await plan_msg.add_reaction("🛑")
                    except discord.HTTPException:
                        pass
                    await message.channel.send(
                        "React ✅ on the plan above to execute it, or 🛑 to discard."
                    )

            else:
                footer = f"✅ done · **{st.project}**"
                if cost is not None:
                    footer += f" · ${cost:.4f}"
                await status.edit(content=footer[:MAX_MSG])
                pieces = chunk(final_text) or ["_(no textual output)_"]
                for p in pieces:
                    await send_result(p)

        except asyncio.TimeoutError:
            outcome = "timeout"
            await kill(st.proc)
            await status.edit(content=f"⏱️ timed out after {RUN_TIMEOUT}s · **{st.project}**")
        except FileNotFoundError:
            outcome = "not-found"
            await status.edit(
                content="❌ `claude` not found. Install Claude Code and make sure it's on PATH "
                        "(or set CLAUDE_BIN to the full path)."
            )
        except asyncio.CancelledError:
            outcome = "cancelled"
            await kill(st.proc)
            await status.edit(content=f"🛑 cancelled · **{st.project}**")
            raise
        except Exception as e:  # noqa: BLE001 - surface anything else to the user
            outcome = "crash"
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
            # Log the run for !history and budget sums (Phase 4.1). Only once the
            # process actually started — a queued run cancelled before spawn, or a
            # missing binary, isn't a "run". Covers every outcome, not just success.
            if spawned:
                duration = time.monotonic() - run_start
                try:
                    files_changed = await _count_changed(project_path, st.pre_run_ref)
                except Exception:  # noqa: BLE001
                    files_changed = None
                log_run(
                    channel_id=channel_id, project=st.project, session_id=new_session,
                    prompt=prompt[:500], cost=cost, duration=duration,
                    files_changed=files_changed, outcome=outcome, ts=time.time(),
                )
                # @-mention if the run was long enough that you may have wandered off.
                if (NOTIFY_AFTER_SECONDS > 0 and duration >= NOTIFY_AFTER_SECONDS
                        and outcome != "cancelled"):
                    ok = outcome == "ok"
                    emoji = "✅" if ok else "⚠️"
                    word = "finished" if ok else f"ended ({outcome})"
                    try:
                        await message.channel.send(
                            f"{message.author.mention} {emoji} Run {word} in "
                            f"**{st.project}** · {int(duration)}s"
                        )
                    except discord.HTTPException:
                        pass
            await _set_post_run_reactions(status)

# ---------------------------------------------------------------------------
# Discord client + command handling
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

HELP = (
    "**Claude Code control**\n"
    f"`{PREFIX}cc <prompt>` — run Claude Code in the current project (continues the session)\n"
    f"`{PREFIX}plan <prompt>` — draft a plan first; react ✅ to execute it, 🛑 to discard\n"
    f"`{PREFIX}new` — start a fresh session\n"
    f"`{PREFIX}project [name]` — show or switch project\n"
    f"`{PREFIX}projects` — list projects\n"
    f"`{PREFIX}model [name]` — show or switch model (sonnet / opus / haiku / default)\n"
    f"`{PREFIX}usage` — runs, cost, and rate-limit utilization\n"
    f"`{PREFIX}history [n]` — recent runs in this channel; resume any of them\n"
    f"`{PREFIX}resume <id>` — resume the session from a past run\n"
    f"`{PREFIX}cost` — spend today / this week (and budget, if set)\n"
    f"`{PREFIX}status` — current project / session / activity\n"
    f"`{PREFIX}cancel` — kill the running task\n"
    "\n"
    "**Git safety net**\n"
    f"`{PREFIX}diff` — show what the current session changed (stat + full patch)\n"
    f"`{PREFIX}revert` — throw away the last run's changes (asks to confirm)\n"
    f"`{PREFIX}commit [msg]` — stage all & commit (Claude writes the message if omitted)\n"
    f"`{PREFIX}pr` — push the current branch and open a GitHub PR (needs `gh`)\n"
    "Set `AUTO_BRANCH=1` to isolate each session's edits on an `anton/…` branch.\n"
    "\n"
    "**Reactions on a run's status message**\n"
    "🛑 cancel · 🔄 retry the same prompt · 📄 dump the full output as a file\n"
    "\n"
    "**Richer input**\n"
    "Attach files/images to `!cc` or `!plan` and Claude can read them. "
    "Reply to a result message to continue *that* session.\n"
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
async def on_reaction_add(reaction: discord.Reaction, user):
    if user.bot or user.id not in ALLOWED_USER_IDS:
        return  # ignore the bot's own reactions and anyone off the allowlist

    emoji = str(reaction.emoji)
    msg = reaction.message
    channel = msg.channel

    # ✅ execute / 🛑 discard on a plan proposal (Phase 1.2).
    plan = PLAN_BY_MESSAGE.get(msg.id)
    if plan is not None:
        if emoji not in ("✅", "🛑"):
            return
        # One decision per plan — drop it so a later reaction can't re-fire.
        PLAN_BY_MESSAGE.pop(msg.id, None)
        st = state_for(plan.channel_id)
        if emoji == "🛑":
            await channel.send("🛑 Plan discarded.")
        else:  # ✅ — resume the same session and carry the plan out.
            if st.lock.locked():
                await channel.send(
                    "A task is already running in this channel — can't execute the plan yet."
                )
                PLAN_BY_MESSAGE[msg.id] = plan  # keep it approvable once free
            else:
                st.session_id = plan.session_id  # resume the planning session
                async with st.lock:
                    await run_claude(
                        plan.trigger_message, st, plan.prompt,
                        permission_mode="acceptEdits",
                    )
        try:
            await reaction.remove(user)
        except discord.HTTPException:
            pass
        return

    # ✅ confirm / 🛑 keep on a revert confirmation (Phase 2.3).
    rev = REVERT_BY_MESSAGE.get(msg.id)
    if rev is not None:
        if emoji not in ("✅", "🛑"):
            return
        REVERT_BY_MESSAGE.pop(msg.id, None)  # one decision per confirmation
        st = state_for(rev.channel_id)
        if emoji == "🛑":
            await channel.send("🛑 Revert cancelled — your changes are untouched.")
        elif st.proc and st.proc.returncode is None:
            await channel.send("A run started meanwhile — cancel it first, then revert.")
        else:
            project_path = PROJECTS.get(rev.project)
            if not project_path:
                await channel.send(f"Project `{rev.project}` is no longer configured.")
            else:
                code, _, err = await _git(project_path, "reset", "--hard", rev.ref)
                if code != 0:
                    await channel.send(f"❌ Revert failed:\n```\n{err[:1500]}\n```")
                else:
                    await _git(project_path, "clean", "-fd")  # drop untracked files too
                    st.pre_run_ref = None  # checkpoint consumed
                    save_state(rev.channel_id, st)
                    await channel.send(
                        f"↩️ Reverted **{rev.project}** to `{rev.ref[:8]}`."
                    )
        try:
            await reaction.remove(user)
        except discord.HTTPException:
            pass
        return

    ctx = RUN_BY_MESSAGE.get(msg.id)
    if ctx is None:
        return

    if emoji not in ("🛑", "🔄", "📄"):
        return

    st = state_for(ctx.channel_id)

    if emoji == "🛑":
        ctx.cancelled = True
        if st.proc and st.proc.returncode is None:
            await kill(st.proc)
        else:
            await channel.send("🛑 Cancel requested — will stop before starting.")

    elif emoji == "🔄":
        if st.lock.locked():
            await channel.send("A task is already running in this channel.")
        else:
            async with st.lock:
                await run_claude(
                    ctx.trigger_message, st, ctx.prompt,
                    permission_mode=ctx.permission_mode, plan_mode=ctx.plan_mode,
                )

    elif emoji == "📄":
        text = ctx.final_text or "_(no output captured for this run)_"
        await channel.send(file=discord.File(io.BytesIO(text.encode("utf-8")), filename="output.md"))

    try:
        await reaction.remove(user)  # let the same button be pressed again
    except discord.HTTPException:
        pass  # needs Manage Messages; harmless if missing


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Reply-to-continue (Phase 3.2): a reply to one of our result messages that
    # ISN'T itself a command continues that message's session. Replies that start
    # with the prefix (e.g. replying with `!diff`) fall through to normal command
    # handling, so commands stay unambiguous.
    ref_id = getattr(getattr(message, "reference", None), "message_id", None)
    if (ref_id is not None and ref_id in RESULT_BY_MESSAGE
            and not message.content.startswith(PREFIX)):
        if not authorized(message):
            return
        rctx = RESULT_BY_MESSAGE[ref_id]
        st = state_for(message.channel.id)
        if rctx.project not in PROJECTS:
            await message.reply(
                f"That thread's project (`{rctx.project}`) is no longer configured."
            )
            return
        if st.lock.locked():
            await message.reply(
                f"A task is already running in this channel. `{PREFIX}cancel` to stop it, "
                "or use another channel."
            )
            return
        files = await save_attachments(message)
        prompt = augment_prompt(message.content.strip(), files)
        if not prompt.strip():
            return  # empty reply with no attachments — nothing to do
        if (blocked := budget_blocked()):
            await message.reply(blocked)
            return
        # Resume the replied-to thread. Sessions are dir-scoped, so align project.
        st.project = rctx.project
        st.session_id = rctx.session_id
        async with st.lock:
            await run_claude(message, st, prompt)
        return

    if not message.content.startswith(PREFIX):
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
            st.run_branch = st.branched_for_session = st.pre_run_ref = None
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

    elif cmd == "cost":
        today = spend_since(_day_start_epoch())
        week = spend_since(_week_start_epoch())
        lines = [
            "**Spend** (all channels)",
            f"Today: **${today:.4f}**",
            f"This week: **${week:.4f}**",
        ]
        if DAILY_BUDGET_USD > 0:
            left = max(0.0, DAILY_BUDGET_USD - today)
            lines.append(
                f"Daily budget: **${today:.4f} / ${DAILY_BUDGET_USD:.2f}** "
                f"(${left:.4f} left)"
            )
        else:
            lines.append("_No daily budget set (`DAILY_BUDGET_USD`)._")
        await message.reply("\n".join(lines))

    elif cmd == "history":
        try:
            n = max(1, min(25, int(arg))) if arg else 10
        except ValueError:
            n = 10
        rows = recent_runs(message.channel.id, n)
        if not rows:
            await message.reply("No runs recorded yet for this channel.")
            return
        lines = [f"**Last {len(rows)} run(s)**"]
        for r in rows:
            mark = {"ok": "✅", "error": "⚠️", "timeout": "⏱️",
                    "cancelled": "🛑", "crash": "❌", "not-found": "❓"}.get(r["outcome"], "•")
            bits = [f"`#{r['id']}`", mark, f"<t:{int(r['ts'])}:R>", f"**{r['project'] or '?'}**"]
            if r["cost"] is not None:
                bits.append(f"${r['cost']:.4f}")
            if r["files_changed"] is not None:
                bits.append(f"{r['files_changed']}f")
            lines.append(" · ".join(bits) + f" — {preview(r['prompt'] or '', 80)}")
        lines.append(f"_Resume any with `{PREFIX}resume <id>`._")
        for c in chunk("\n".join(lines)):
            await message.channel.send(c)

    elif cmd == "resume":
        if not arg.isdigit():
            await message.reply(f"Usage: `{PREFIX}resume <id>` (see `{PREFIX}history`).")
            return
        row = run_by_id(message.channel.id, int(arg))
        if row is None:
            await message.reply(f"No run `#{arg}` in this channel. Try `{PREFIX}history`.")
            return
        if not row["session_id"]:
            await message.reply(f"Run `#{arg}` has no session to resume.")
            return
        if row["project"] and row["project"] not in PROJECTS:
            await message.reply(
                f"Run `#{arg}`'s project (`{row['project']}`) is no longer configured."
            )
            return
        if row["project"]:
            st.project = row["project"]
        st.session_id = row["session_id"]
        # Fresh branch state so the resumed session re-branches cleanly (like !new).
        st.run_branch = st.branched_for_session = st.pre_run_ref = None
        save_state(message.channel.id, st)
        await message.reply(
            f"Resumed session from run `#{arg}` in **{st.project or 'none'}**. "
            f"Next `{PREFIX}cc` continues it."
        )

    elif cmd == "new":
        st.session_id = None
        # New session → next run branches fresh instead of reusing the old one.
        st.run_branch = st.branched_for_session = st.pre_run_ref = None
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
        if not st.project or st.project not in PROJECTS:
            await message.reply(f"No valid project set. Use `{PREFIX}project <name>`.")
            return
        files = await save_attachments(message)
        if not arg and not files:
            await message.reply(f"Usage: `{PREFIX}cc <prompt>` (or attach a file).")
            return
        if st.lock.locked():
            await message.reply(
                f"A task is already running in this channel. `{PREFIX}cancel` to stop it, "
                "or use another channel."
            )
            return
        if (blocked := budget_blocked()):
            await message.reply(blocked)
            return
        async with st.lock:
            await run_claude(message, st, augment_prompt(arg, files))

    elif cmd == "plan":
        if not st.project or st.project not in PROJECTS:
            await message.reply(f"No valid project set. Use `{PREFIX}project <name>`.")
            return
        files = await save_attachments(message)
        if not arg and not files:
            await message.reply(f"Usage: `{PREFIX}plan <prompt>` (or attach a file).")
            return
        if st.lock.locked():
            await message.reply(
                f"A task is already running in this channel. `{PREFIX}cancel` to stop it, "
                "or use another channel."
            )
            return
        if (blocked := budget_blocked()):
            await message.reply(blocked)
            return
        async with st.lock:
            await run_claude(message, st, augment_prompt(arg, files), plan_mode=True)

    elif cmd == "diff":
        if not st.project or st.project not in PROJECTS:
            await message.reply(f"No valid project set. Use `{PREFIX}project <name>`.")
            return
        project_path = PROJECTS[st.project]
        if not await _is_git_repo(project_path):
            await message.reply(f"**{st.project}** isn't a git repository — nothing to diff.")
            return
        # Diff against the pre-run ref when we have one (shows exactly what this
        # session changed, staged + unstaged + untracked), else the working tree.
        base = st.pre_run_ref
        stat_args = ["diff", "--stat"] + ([base] if base else [])
        full_args = ["diff"] + ([base] if base else [])
        _, stat, _ = await _git(project_path, *stat_args)
        _, patch, _ = await _git(project_path, *full_args)
        against = f"since last run (`{base[:8]}`)" if base else "in the working tree"
        if not stat and not patch:
            await message.reply(f"No changes {against} in **{st.project}**.")
            return
        header = f"📊 **Diff {against}** · {st.project}\n```\n{stat[:MAX_MSG - 200]}\n```"
        await message.reply(header)
        if patch:
            await message.channel.send(
                file=discord.File(io.BytesIO(patch.encode("utf-8")), filename="changes.diff")
            )

    elif cmd in ("revert", "undo"):
        if not st.project or st.project not in PROJECTS:
            await message.reply(f"No valid project set. Use `{PREFIX}project <name>`.")
            return
        if st.proc and st.proc.returncode is None:
            await message.reply(
                f"A run is active — `{PREFIX}cancel` it before reverting."
            )
            return
        project_path = PROJECTS[st.project]
        if not await _is_git_repo(project_path):
            await message.reply(f"**{st.project}** isn't a git repository — nothing to revert.")
            return
        if not st.pre_run_ref:
            await message.reply(
                "No pre-run checkpoint recorded yet — run something first, then "
                f"`{PREFIX}revert` throws away that run's changes."
            )
            return
        # Show what would be discarded and ask for a ✅ before destroying work.
        _, stat, _ = await _git(project_path, "diff", "--stat", st.pre_run_ref)
        preview = (f"```\n{stat[:1500]}\n```" if stat else "_(no tracked changes; "
                   "untracked files will also be cleaned)_")
        warn = await message.reply(
            f"⚠️ **Revert {st.project} to `{st.pre_run_ref[:8]}`?**\n{preview}\n"
            "This runs `git reset --hard` + `git clean -fd` and **cannot be undone**.\n"
            "React ✅ to confirm or 🛑 to keep your changes."
        )
        REVERT_BY_MESSAGE[warn.id] = RevertContext(
            channel_id=message.channel.id, ref=st.pre_run_ref, project=st.project,
        )
        try:
            await warn.add_reaction("✅")
            await warn.add_reaction("🛑")
        except discord.HTTPException:
            pass

    elif cmd == "commit":
        if not st.project or st.project not in PROJECTS:
            await message.reply(f"No valid project set. Use `{PREFIX}project <name>`.")
            return
        if st.proc and st.proc.returncode is None:
            await message.reply(f"A run is active — `{PREFIX}cancel` it before committing.")
            return
        project_path = PROJECTS[st.project]
        if not await _is_git_repo(project_path):
            await message.reply(f"**{st.project}** isn't a git repository — nothing to commit.")
            return
        # Stage everything, then check there's actually something to commit.
        await _git(project_path, "add", "-A")
        code, staged, _ = await _git(project_path, "diff", "--cached", "--stat")
        if not staged:
            await message.reply(f"Nothing staged to commit in **{st.project}**.")
            return

        notice = await message.reply("✍️ Writing a commit message…")
        # A user-supplied `!commit <msg>` wins; otherwise ask Claude for one.
        subject = arg.strip()
        if not subject:
            _, diff, _ = await _git(project_path, "diff", "--cached")
            gen = await claude_text(
                "Write a Conventional Commits message (a concise `type(scope): summary` "
                "subject under 72 chars, optionally a short body) for this staged diff. "
                "Output ONLY the commit message, no preamble, no backticks:\n\n"
                + diff[:12000],
                cwd=project_path, model=st.model,
            )
            subject = (gen or "").strip()
        if not subject:
            subject = "chore: update via anton"  # graceful fallback
            fell_back = True
        else:
            fell_back = False

        code, out, err = await _git(project_path, "commit", "-m", subject)
        if code != 0:
            await notice.edit(content=f"❌ Commit failed:\n```\n{(err or out)[:1500]}\n```")
            return
        _, short_hash, _ = await _git(project_path, "rev-parse", "--short", "HEAD")
        first_line = subject.splitlines()[0]
        tag = " _(default message — Claude was unavailable)_" if fell_back else ""
        await notice.edit(
            content=f"✅ Committed `{short_hash}` on **{st.project}**:\n> {first_line}{tag}"
        )

    elif cmd == "pr":
        if not st.project or st.project not in PROJECTS:
            await message.reply(f"No valid project set. Use `{PREFIX}project <name>`.")
            return
        project_path = PROJECTS[st.project]
        if not await _is_git_repo(project_path):
            await message.reply(f"**{st.project}** isn't a git repository.")
            return
        if shutil.which("gh") is None:
            await message.reply(
                "`gh` (GitHub CLI) isn't installed on the host, so I can't open a PR. "
                "Install it and run `gh auth login`, then try again."
            )
            return
        # Refuse if there are uncommitted changes — PR them intentionally.
        _, dirty, _ = await _git(project_path, "status", "--porcelain")
        if dirty:
            await message.reply(
                f"You have uncommitted changes — `{PREFIX}commit` them first, then `{PREFIX}pr`."
            )
            return
        _, branch, _ = await _git(project_path, "rev-parse", "--abbrev-ref", "HEAD")
        if branch in ("", "HEAD"):
            await message.reply("Not on a branch (detached HEAD) — can't open a PR.")
            return
        if branch in ("main", "master"):
            await message.reply(
                f"You're on `{branch}` — open PRs from a feature branch "
                f"(e.g. run with `AUTO_BRANCH=1`, or `git switch -c`)."
            )
            return

        notice = await message.reply(f"🚀 Pushing `{branch}` and opening a PR…")
        code, _, err = await _git(project_path, "push", "-u", "origin", branch)
        if code != 0:
            await notice.edit(content=f"❌ Push failed:\n```\n{err[:1500]}\n```")
            return
        # gh isn't git; run it directly (still async, still in the project dir).
        proc = await asyncio.create_subprocess_exec(
            "gh", "pr", "create", "--fill",
            cwd=project_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await proc.communicate()
        out = out_b.decode(errors="replace").strip()
        err = err_b.decode(errors="replace").strip()
        if proc.returncode != 0:
            # Most common cause: a PR already exists for this branch.
            await notice.edit(
                content=f"⚠️ Pushed `{branch}`, but `gh pr create` failed:\n"
                        f"```\n{(err or out)[:1500]}\n```"
            )
            return
        await notice.edit(content=f"✅ PR opened for **{st.project}**: {out or '(see GitHub)'}")


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
