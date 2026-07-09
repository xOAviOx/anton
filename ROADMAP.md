# Anton Roadmap

Feature roadmap for the Claude Code Discord bot. Anton's value is being a
**lightweight, single-file remote control for the real `claude` CLI** — driveable
from your phone. These features lean into that identity: they add safety, control,
and automation without turning the bot into a heavyweight service.

Each feature lists **what** it does, **how** to build it (with references to
existing code in `claude_code_discord_bot.py`), and rough **effort**.

Legend — effort: 🟢 easy (≤ ~40 lines) · 🟡 medium · 🔴 involved.

---

## Phase 0 — Foundation (do first)

These aren't user-facing features, but the feature phases below depend on them.

### 0.1 Durable state 🟢 ✅ done

- **Problem:** `STATE: dict[int, ChannelState]` is in-memory; a restart loses every
  channel's `session_id`, cost totals, and project selection.
- **How:** Back `STATE` with SQLite (`anton.db`). On `state_for()` load the row;
  on mutation (session change, run finish) upsert. Store `channel_id, project,
  session_id, model, runs, total_cost`.
- **Unlocks:** history, budgets, resume-after-restart.

### 0.2 Process-group kill 🟢 ✅ done

- **Problem:** `kill()` terminates only the `claude` parent; bash/subagent/MCP
  children can be orphaned.
- **How:** Launch with `start_new_session=True` in `create_subprocess_exec`, then
  signal the whole group (`os.killpg(os.getpgid(proc.pid), SIGTERM/SIGKILL)`).
  Update `kill()` in place.

### 0.3 Global concurrency cap 🟢 ✅ done

- **Problem:** Per-channel lock exists, but N channels can each spawn a heavy run.
- **How:** Module-level `asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT", "2")))`
  acquired inside `run_claude` around the subprocess. Post a "queued…" status when
  the semaphore is contended.

---

## Phase 1 — Interactive control (highest value)

A Discord bridge is uniquely good at approval loops you can drive from your phone.

### 1.1 Reaction controls on the status message 🟢 ✅ done

- **What:** React on the live status message to act without typing — 🛑 cancel,
  🔄 retry the last prompt, 📄 dump full output as a file attachment.
- **How:** Add an `on_reaction_add` handler. Store `message.id -> (ChannelState,
  last_prompt)` so a reaction can find its run. Gate on `ALLOWED_USER_IDS`. 🛑
  calls `kill()`; 🔄 re-invokes `run_claude` with the stored prompt; 📄 sends
  `final_text` as a `discord.File`.

### 1.2 Plan → approve → execute 🟡 ✅ done

- **What:** Risky prompts run in plan mode first. Bot posts Claude's plan and waits
  for a ✅ reaction before executing; 🛑 discards.
- **How:** New command `!plan <prompt>`. Run `build_command` with
  `--permission-mode plan`. Capture the plan from the `result` event, post it, and
  `await client.wait_for("reaction_add", check=...)`. On ✅, resume the **same
  session** (`--resume new_session`) with `--permission-mode acceptEdits` and the
  original prompt to carry out the plan.

### 1.3 Live permission prompts 🔴

- **What:** Instead of blanket `acceptEdits`, when Claude wants to run a `Bash`
  command (or edit outside the repo), the bot posts the exact command and waits for
  your ✅/❌ reaction before it proceeds. True remote approval.
- **How:** Implement a tiny local MCP "permission prompt" server and pass
  `--permission-prompt-tool mcp__anton__approve`. The tool handler posts the
  requested tool + input to Discord, awaits a reaction, and returns allow/deny to
  Claude Code. Requires running an MCP server alongside the bot (stdio or HTTP).
- **Note:** Most powerful feature; build it standalone after Phase 1.1–1.2.

---

## Phase 2 — Git-native workflow

Right now Claude edits the working tree in place with no safety net. Make git the
safety layer. All of these shell out with `cwd=PROJECTS[st.project]`.

> **Implementation notes (2.1–2.3 shipped):** branch names get a short uuid
> suffix (`anton/<ts>-<uuid>`) so two runs starting in the same second don't
> collide. Branch creation is idempotent per session — it fires only while
> `run_branch` is unset, and `run_branch` is cleared on session reset (`!new` /
> project switch), so follow-up messages reuse the one branch instead of each
> forking a new one. A `pre_run_ref` (HEAD before the run) is captured on
> **every** run, so `!diff` and `!revert` work even with `AUTO_BRANCH=0`.
> `!revert` also runs `git clean -fd` to drop untracked files, and asks for a ✅
> first. New DB columns (`run_branch`, `branched_for_session`, `pre_run_ref`)
> are added by an `ALTER TABLE` migration so pre-Phase-2 databases upgrade in
> place.

### 2.1 Auto-branch per run 🟢 ✅ done

- **What:** Create `anton/<timestamp>` before each run so changes are isolated and
  reviewable. Configurable via `AUTO_BRANCH=1`.
- **How:** Before dispatch in `run_claude`, run `git switch -c anton/<ts>` (skip if
  the tree is dirty or not a git repo — warn instead). Record the branch on
  `ChannelState`.

### 2.2 `!diff` 🟢 ✅ done

- **What:** Post `git diff --stat` for the current run's changes; offer the full
  diff as a file attachment if it's large.
- **How:** Run `git diff --stat` (and `git diff` for the attachment) in the project
  dir; chunk or attach via `discord.File`.

### 2.3 `!revert` / `!undo` 🟢 ✅ done

- **What:** Throw away a bad run's changes.
- **How:** `git reset --hard` to the pre-run ref (capture `git rev-parse HEAD`
  before each run and store it on `ChannelState`), or restore a pre-run
  `git stash`. Confirm with a reaction before destroying work.

### 2.4 `!commit` and `!pr` 🟡

- **What:** `!commit` commits changes with a Claude-generated message; `!pr` pushes
  the branch and opens a GitHub PR.
- **How:** `!commit` — run a quick `claude -p "write a commit message for this
  diff"` (non-streaming), then `git commit -am`. `!pr` — `git push -u origin
  <branch>` then `gh pr create --fill`. Requires `gh` authenticated on the host;
  document in README.

---

## Phase 3 — Better input

### 3.1 Attachments as input 🟢

- **What:** Drop an image or file into Discord; the bot saves it into the project
  dir (or a temp path) so you can say "implement this mockup" or "here's the
  failing log." Claude Code reads images natively.
- **How:** In `on_message`, if `message.attachments`, `await
  attachment.save(dest)` into the project dir, and append the saved paths to the
  prompt text passed to `run_claude`.

### 3.2 Reply-to-continue 🟢

- **What:** Reply to a run's result message to send a follow-up into **that**
  session, so multiple lines of work coexist in one channel without `!new`.
- **How:** Map result-message IDs to `session_id`. In `on_message`, if
  `message.reference` points at a known result message, run with that session
  instead of the channel's current one.

---

## Phase 4 — Observability, cost & history

### 4.1 Persistent history + `!history` 🟢

- **What:** Log each run (prompt, session id, cost, duration, files changed);
  `!history` lists recent runs and lets you resume any of them.
- **How:** A `runs` table in the Phase 0 SQLite db, written at the end of
  `run_claude`. `!history` reads the last N; `!resume <id>` sets
  `st.session_id`.

### 4.2 Budget caps 🟢

- **What:** `DAILY_BUDGET_USD` — refuse new runs once the cap is hit; `!cost` shows
  today / this-week spend.
- **How:** Sum `total_cost_usd` from the `runs` table for the current day before
  admitting a run in the `cc` handler.

### 4.3 Stream assistant text live 🟡

- **What:** Show Claude's prose as it arrives, not just tool calls, so long answers
  feel alive.
- **How:** In `read_stream`, when an `assistant` event has a `text` block, append a
  truncated preview to `activity` (or a second streamed message) instead of only
  buffering `final_text`.

### 4.4 DM / mention on completion 🟢

- **What:** Ping or DM you when a long run finishes or fails, so you can
  fire-and-forget.
- **How:** After a run whose duration exceeds `NOTIFY_AFTER_SECONDS`, `await
  message.author.send(...)` or mention them in the final post.

---

## Phase 5 — Automation & triggers

### 5.1 Scheduled runs (`!schedule`) 🟡

- **What:** Cron-style recurring prompts — nightly "fix failing tests," weekly
  "update dependencies and open a PR."
- **How:** Store schedules in SQLite; a background `asyncio` task ticks each minute
  and dispatches due prompts through `run_claude` against a stored channel/project.

### 5.2 GitHub webhook triggers 🔴

- **What:** Issue labeled `claude` → auto-run Claude on it and report back; CI
  failure → auto-attempt a fix.
- **How:** Run a small `aiohttp` webhook listener beside the Discord client;
  validate the GitHub HMAC signature; map events to a project + prompt and dispatch
  into a designated channel. Turns Anton from a remote control into an autonomous
  teammate.

---

## Phase 6 — Multi-project & multi-user

### 6.1 Runtime `!addproject <name> <path>` + auto-discovery 🟢

- **What:** Add projects without editing env vars; optionally auto-discover git
  repos under a root dir.
- **How:** Mutate `PROJECTS` at runtime and persist to SQLite. Auto-discovery:
  glob for `.git` dirs under `PROJECTS_ROOT`.

### 6.2 Fan-out (`!cc-all <prompt>`) 🟡

- **What:** Run the same prompt across several projects (e.g. "bump the license
  year") and report per-project results.
- **How:** Loop the configured projects, dispatch each under the global semaphore,
  and post a compact per-project summary.

### 6.3 Per-user sessions 🟡

- **What:** If collaborators are added, keep sessions per `(channel, user)` so two
  people don't clobber one channel's session.
- **How:** Key `STATE` on `(channel_id, author_id)` instead of `channel_id`.

---

## Suggested build order

1. **Phase 0** (foundation: persistence, process-group kill, semaphore).
2. **Phase 2.1–2.3** (auto-branch, `!diff`, `!revert`) — the safety net.
3. **Phase 1.1** (reaction controls) + **Phase 1.2** (plan→approve→execute).
4. **Phase 3** (attachments, reply-to-continue).
5. **Phase 4** (history, budgets, notifications).
6. **Phase 1.3** (live permission MCP) — powerful, standalone.
7. **Phase 5–6** (automation, multi-project) as needed.

## Cross-cutting

- **Tests.** Add unit tests for the pure functions (`summarize_tool`, `chunk`,
  `build_command`, `authorized`) and any new git helpers.
- **`chunk()` code-fence safety.** Track fence state so splits don't break Markdown.
- **Security posture.** Consider a read-only / plan default so a fresh install
  isn't auto-approving shell commands out of the box.
