#!/usr/bin/env python3
"""
Anton's permission-prompt MCP server (Phase 1.3 — live permission prompts).

Claude Code spawns this as a tiny stdio MCP server (via --mcp-config) whenever
a channel is in `!strict` mode, and calls its one tool through
`--permission-prompt-tool mcp__anton__approve` whenever it wants to use a tool
that isn't in the channel's reduced STRICT_ALLOWED_TOOLS list — typically
Bash, Edit, Write, WebFetch, WebSearch, or Task.

This process has no Discord connection of its own — it can't, `claude` owns
its stdio pipes for the MCP session. Instead it writes a row to the shared
`approvals` SQLite table (same anton.db as the bot) and polls for a decision.
The bot's own `approval_watcher()` background task (in
claude_code_discord_bot.py) is the other half: it notices the new pending
row, posts it to the right Discord channel with ✅/❌ reactions, and writes
the decision back to that row when a human reacts. If nobody reacts in time,
this script marks its own row denied-by-timeout so the bot can update the
stale Discord message.

Protocol notes — empirically verified against Claude Code 2.1.205 by running
a debug stdio MCP server and observing the real requests, since the public
CLI reference doesn't document the exact schema at time of writing:
  - tools/call arguments arrive as {"tool_name": str, "input": dict,
    "tool_use_id": str} (plus a "_meta" block we don't need).
  - The result must be exactly one {"type": "text", "text": <json string>}
    content block, where the JSON is either
    {"behavior": "allow", "updatedInput": {...}} or
    {"behavior": "deny", "message": "..."}. We don't offer input editing, so
    updatedInput always echoes the original input unchanged.

Known limitation: this script's main loop is single-threaded and blocks
(polling) while a request is pending, so if Claude fires two tool calls
needing permission before the first is answered, the second just waits its
turn once the first resolves — fine for a personal bot's typical one-thing-
at-a-time usage, not something we've tried to make concurrent.
"""
import json
import os
import sqlite3
import sys
import time

DB_PATH = os.environ["ANTON_DB"]
CHANNEL_ID = int(os.environ["ANTON_CHANNEL_ID"])
TIMEOUT_SECONDS = float(os.environ.get("ANTON_APPROVAL_TIMEOUT", "600"))
POLL_INTERVAL = 1.0


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    # Idempotent: whichever of the bot or this script starts first creates it.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS approvals (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id    INTEGER NOT NULL,
            tool_name     TEXT,
            input_json    TEXT,
            tool_use_id   TEXT,
            status        TEXT NOT NULL DEFAULT 'pending',
            message       TEXT,
            created_ts    REAL NOT NULL,
            decided_ts    REAL
        )
        """
    )
    conn.commit()
    return conn


def send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _fetch_status(approval_id: int):
    conn = _db()
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT status, message FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
    finally:
        conn.close()


def handle_call(rid, arguments: dict) -> None:
    tool_name = arguments.get("tool_name", "?")
    tool_input = arguments.get("input", {}) or {}
    tool_use_id = arguments.get("tool_use_id")

    conn = _db()
    try:
        cur = conn.execute(
            "INSERT INTO approvals (channel_id, tool_name, input_json, tool_use_id, "
            "status, created_ts) VALUES (?, ?, ?, ?, 'pending', ?)",
            (CHANNEL_ID, tool_name, json.dumps(tool_input), tool_use_id, time.time()),
        )
        conn.commit()
        approval_id = cur.lastrowid
    finally:
        conn.close()

    deadline = time.monotonic() + TIMEOUT_SECONDS
    status, message = None, None
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        row = _fetch_status(approval_id)
        if row and row["status"] != "pending":
            status, message = row["status"], row["message"] or ""
            break

    if status is None:
        # Nobody answered in time — deny by default and record it ourselves so the
        # bot's approval_watcher() notices and updates the now-stale Discord message
        # (it has nothing else to trigger that edit off of).
        status = "deny"
        message = f"Timed out after {int(TIMEOUT_SECONDS)}s waiting for approval — denied by default."
        conn = _db()
        try:
            conn.execute(
                "UPDATE approvals SET status = ?, message = ?, decided_ts = ? "
                "WHERE id = ? AND status = 'pending'",
                (status, message, time.time(), approval_id),
            )
            conn.commit()
        finally:
            conn.close()

    if status == "allow":
        result = {"behavior": "allow", "updatedInput": tool_input}
    else:
        result = {"behavior": "deny", "message": message or "Denied."}

    send({
        "jsonrpc": "2.0", "id": rid,
        "result": {"content": [{"type": "text", "text": json.dumps(result)}]},
    })


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, rid = req.get("method"), req.get("id")

        if method == "initialize":
            send({
                "jsonrpc": "2.0", "id": rid,
                "result": {
                    "protocolVersion": req.get("params", {}).get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "anton", "version": "1.0.0"},
                },
            })
        elif method == "notifications/initialized":
            continue  # notification — no response expected
        elif method == "tools/list":
            send({
                "jsonrpc": "2.0", "id": rid,
                "result": {
                    "tools": [{
                        "name": "approve",
                        "description": "Ask a human on Discord to allow or deny a tool call.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "tool_name": {"type": "string"},
                                "input": {"type": "object"},
                                "tool_use_id": {"type": "string"},
                            },
                        },
                    }]
                },
            })
        elif method == "tools/call":
            params = req.get("params", {})
            if params.get("name") == "approve":
                handle_call(rid, params.get("arguments", {}) or {})
            elif rid is not None:
                send({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": -32601, "message": "unknown tool"}})
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid, "result": {}})


if __name__ == "__main__":
    main()
