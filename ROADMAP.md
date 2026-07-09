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

### 1.3 Live permission prompts 🔴 ✅ done

- **What:** Instead of blanket `acceptEdits`, when Claude wants to run a `Bash`
  command (or edit outside the repo), the bot posts the exact command and waits for
  your ✅/❌ reaction before it proceeds. True remote approval.
- **How:** Implement a tiny local MCP "permission prompt" server and pass
  `--permission-prompt-tool mcp__anton__approve`. The tool handler posts the
  requested tool + input to Discord, awaits a reaction, and returns allow/deny to
  Claude Code. Requires running an MCP server alongside the bot (stdio or HTTP).
- **Note:** Most powerful feature; build it standalone after Phase 1.1–1.2.

> **Implementation notes (shipped):** the `--permission-prompt-tool` /
> `--mcp-config` protocol isn't documented in the public CLI reference (as of
> Claude Code 2.1.205), so this was built by empirically verifying it — a
> throwaway debug MCP server logged the real JSON-RPC traffic from a live
> `claude -p` run. Confirmed facts, since they aren't written down elsewhere:
> the tool is called with `arguments: {tool_name, input, tool_use_id}`; the
> reply must be a single `{"type":"text","text":"<json>"}` content block
> containing `{"behavior":"allow","updatedInput":{...}}` or
> `{"behavior":"deny","message":"..."}`; `--mcp-config` accepts an inline JSON
> string (no temp file needed) and its `env` block does reach the spawned
> server process; omitting `--strict-mcp-config` merges our server with
> whatever else the project already has configured rather than replacing it.
> Denials surface to Claude as a normal (non-fatal) tool error and the run
> finishes normally — confirmed with real allow, deny, and timeout runs
> against a project directory, not just unit-level checks.
>
> **Architecture:** `!strict on` persists per-channel like `!model`. When set
> (and the run isn't plan mode, and no caller already forced a
> `permission_mode` — e.g. a plan-approval execute still runs at full
> `acceptEdits`, since a human already reviewed the whole plan), `run_claude`
> swaps in `permission_mode="default"` + `STRICT_ALLOWED_TOOLS` (default
> `Read,Glob,Grep,TodoWrite`) and generates an inline `--mcp-config` pointing
> at `anton_mcp_approve.py`, passing `ANTON_DB` / `ANTON_CHANNEL_ID` /
> `ANTON_APPROVAL_TIMEOUT` via the config's `env`. That script is a genuinely
> separate OS process (`claude` spawns it directly over stdio) with no
> Discord connection of its own, so it can't post/react on its own — it just
> inserts a row into a new `approvals` SQLite table and polls it. The bot's
> `approval_watcher()` background task is the other half: it notices new
> pending rows, posts them to the right channel with ✅/❌, and the
> `on_reaction_add` handler writes the decision straight back to that row.
> `approval_watcher()` also has to catch the one case reactions can't drive:
> the helper's own timeout-deny, which needs polling to notice at all. A
> run's `finally` block denies any of its still-pending approvals when it
> ends for any other reason (killed, crashed, timed out), so a dead run can't
> leave a ✅/❌ card stuck in Discord forever.
> **Known limitation:** the helper script is single-threaded/blocking, so if
> Claude fires two approval-needing tool calls before the first is answered,
> the second just queues behind it — fine for one person driving one thing at
> a time, not built for concurrency within a single run.

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

### 2.4 `!commit` and `!pr` 🟡 ✅ done

- **What:** `!commit` commits changes with a Claude-generated message; `!pr` pushes
  the branch and opens a GitHub PR.
- **How:** `!commit` — run a quick `claude -p "write a commit message for this
  diff"` (non-streaming), then `git commit -am`. `!pr` — `git push -u origin
  <branch>` then `gh pr create --fill`. Requires `gh` authenticated on the host;
  document in README.

> **Implementation notes (shipped):** the commit message comes from a new
> `claude_text()` helper — a tool-free, read-only (`--permission-mode plan`),
> non-streaming `claude -p` in a fresh session, run *outside* `RUN_SEMAPHORE`
> since it's short-lived and shouldn't queue behind interactive runs. It stages
> with `git add -A` (not `commit -am`, so new files are included) and falls back
> to a generic message if Claude is unavailable rather than failing. `!commit`
> also accepts an explicit `!commit <msg>` override. `!pr` degrades gracefully
> when `gh` is missing/unauthenticated, refuses to run with a dirty tree or from
> `main`/`master`, and surfaces `gh`'s own error (commonly "a PR already
> exists") instead of masking it.

---

## Phase 3 — Better input

> **Implementation notes (3.1–3.2 shipped):** attachments save to a temp dir
> (`UPLOAD_DIR`, default `<tmp>/anton-uploads`), **not** the project dir, so they
> don't pollute the repo or get swept up by the git safety net; filenames are
> `basename`-sanitised and de-duped, oversized (`MAX_ATTACH_MB`) skipped, count
> capped (`MAX_ATTACH_COUNT`). `!cc`/`!plan` now run with attachments and no
> text. Reply-to-continue registers every posted result message in a bounded
> in-memory map (`RESULT_BY_MESSAGE`, evicts oldest past 500); a reply that
> starts with `!` is still treated as a command, not a continuation; the map is
> not persisted, so replies stop resolving after a restart.

### 3.1 Attachments as input 🟢 ✅ done

- **What:** Drop an image or file into Discord; the bot saves it into the project
  dir (or a temp path) so you can say "implement this mockup" or "here's the
  failing log." Claude Code reads images natively.
- **How:** In `on_message`, if `message.attachments`, `await
  attachment.save(dest)` into the project dir, and append the saved paths to the
  prompt text passed to `run_claude`.

### 3.2 Reply-to-continue 🟢 ✅ done

- **What:** Reply to a run's result message to send a follow-up into **that**
  session, so multiple lines of work coexist in one channel without `!new`.
- **How:** Map result-message IDs to `session_id`. In `on_message`, if
  `message.reference` points at a known result message, run with that session
  instead of the channel's current one.

---

## Phase 4 — Observability, cost & history

> **Implementation notes (4.1–4.4 shipped):** a `runs` table (Phase 0 db) gets
> one row per *spawned* run — a queued run cancelled before `claude` starts, or
> a missing binary, is intentionally not logged, since it never really ran.
> `outcome` is one of `ok / error / timeout / cancelled / crash / not-found`.
> `!history` reads it back per-channel; `!resume <id>` sets `st.session_id`
> (and clears the branch-tracking fields so the resumed session re-branches
> cleanly, like `!new`) but refuses if the run's project is no longer
> configured. `DAILY_BUDGET_USD` sums `cost` across **all channels** since
> local midnight (`spend_since` / `_day_start_epoch`) and is checked in `!cc`,
> `!plan`, and reply-continuations — not just `!cc`. `!cost` also reports the
> week-to-date sum (`_week_start_epoch`, Monday-anchored). Assistant `text`
> blocks now stream into `activity` alongside tool-use lines (prefixed `💬`,
> truncated via a shared `preview()` helper) instead of only surfacing in the
> final message. `NOTIFY_AFTER_SECONDS` mentions (not DMs — simpler, and the
> channel already has context) the author in-channel once a *spawned* run's
> wall time meets the threshold, skipping cancelled runs since those are
> already a deliberate user action.

### 4.1 Persistent history + `!history` 🟢 ✅ done

- **What:** Log each run (prompt, session id, cost, duration, files changed);
  `!history` lists recent runs and lets you resume any of them.
- **How:** A `runs` table in the Phase 0 SQLite db, written at the end of
  `run_claude`. `!history` reads the last N; `!resume <id>` sets
  `st.session_id`.

### 4.2 Budget caps 🟢 ✅ done

- **What:** `DAILY_BUDGET_USD` — refuse new runs once the cap is hit; `!cost` shows
  today / this-week spend.
- **How:** Sum `total_cost_usd` from the `runs` table for the current day before
  admitting a run in the `cc` handler.

### 4.3 Stream assistant text live 🟢 ✅ done

- **What:** Show Claude's prose as it arrives, not just tool calls, so long answers
  feel alive.
- **How:** In `read_stream`, when an `assistant` event has a `text` block, append a
  truncated preview to `activity` (or a second streamed message) instead of only
  buffering `final_text`.

### 4.4 DM / mention on completion 🟢 ✅ done

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

### 6.1 Runtime `!addproject <name> <path>` + auto-discovery 🟢 ✅ done

- **What:** Add projects without editing env vars; optionally auto-discover git
  repos under a root dir.
- **How:** Mutate `PROJECTS` at runtime and persist to SQLite. Auto-discovery:
  glob for `.git` dirs under `PROJECTS_ROOT`.

> **Implementation notes (shipped):** `STATIC_PROJECT_NAMES` snapshots
> `PROJECTS.keys()` right after the env/hardcoded load, before anything
> runtime-added is merged in — that snapshot is what makes static entries
> permanently win over a same-named runtime one, and is also how `!addproject`
> /`!rmproject` refuse to touch a statically-configured name (edit
> `PROJECTS`/env for those instead). Runtime entries live in a new
> `extra_projects` SQLite table and get merged into `PROJECTS` once at startup
> (`load_runtime_projects()`, called from `main()`); `!addproject` and
> `!discover` additionally update the in-memory dict immediately so they take
> effect without a restart. `!discover` recognizes both a `.git` directory and
> a `.git` *file* (the latter is how git worktrees and submodules mark
> themselves) and skips any subdirectory name already known under any source,
> so it can never silently rename or move an existing entry. Also added
> `!rmproject` (not in the original spec) for symmetry — removes a
> runtime-added project again; there's no equivalent for static ones by
> design. Verified end-to-end against the real module in a venv with
> discord.py installed: discovery, add, remove, and the static-wins-over-
> runtime merge precedence all round-trip correctly through SQLite.

### 6.2 Fan-out (`!cc-all <prompt>`) 🟡 ✅ done

- **What:** Run the same prompt across several projects (e.g. "bump the license
  year") and report per-project results.
- **How:** Loop the configured projects, dispatch each under the global semaphore,
  and post a compact per-project summary.

> **Implementation notes (shipped):** deliberately *not* built on `run_claude`
> — that function's `channel_id` doubles as the SQLite persistence key for
> the invoking channel's own project/session, and N concurrent dispatches
> all sharing the one real channel would clobber each other's row if they
> each called `save_state()`. `_fanout_one()` is a simpler, self-contained
> one-off runner: fresh session every time (no `--resume`), no live per-tool
> activity feed, no reaction controls — just spawn, collect the `result`
> event, log it, done. It still acquires `RUN_SEMAPHORE` itself, so a
> fan-out across many projects with a low `MAX_CONCURRENT` correctly
> serializes instead of spawning everything at once (verified: with
> `MAX_CONCURRENT=1` and two dispatches, total wall time ≈ sum of the two
> individual run times, not `max()` of them). Each project's result is still
> written to the `runs` table under the invoking channel_id, so `!history`/
> `!cost` see fan-out runs same as any other. Scope decision: fan-out
> ignores the channel's `!strict` setting and always uses the normal
> permission posture — with several projects potentially awaiting approval
> at once in the same channel, an approval card has no way to show *which*
> project's Bash command it's asking about (the permission-prompt-tool
> protocol only carries `tool_name`/`input`, not project context), so rather
> than ship something ambiguous, strict mode is just out of scope for
> fan-out for now.

### 6.3 Per-user sessions 🟡 ✅ done

- **What:** If collaborators are added, keep sessions per `(channel, user)` so two
  people don't clobber one channel's session.
- **How:** Key `STATE` on `(channel_id, author_id)` instead of `channel_id`.

> **Implementation notes (shipped):** opt-in via `PER_USER_SESSIONS` (default
> off = today's exact behavior). The load-bearing trick is in `state_for()`:
> it always collapses `author_id` to `0` unless the flag is on
> (`effective_author = author_id if PER_USER_SESSIONS else 0`), so every
> caller can unconditionally pass the real `message.author.id` and the
> function itself decides whether that matters. `ChannelState` gained an
> `author_id` field (set once at creation, read back by `save_state()`), so
> `save_state(channel_id, st)`'s signature never had to change — only
> `state_for()` call sites needed updating to also pass `message.author.id`.
>
> The database side needed an actual migration, not just an added column:
> `channel_state` was `PRIMARY KEY(channel_id)`, and SQLite can't alter a
> primary key in place. `_migrate_channel_state_for_per_user()` runs
> unconditionally (regardless of the flag — so the schema's always ready if
> it's flipped on later) and does the rename-old / create-new-with-composite-
> key / copy-rows-in-with-author_id=0 / drop-old dance, handling the case
> where the old table predates some of the later per-column migrations
> (falls back to `NULL`/`0` literals for any column not present). Verified
> against a simulated pre-6.3 database (single-column PK, populated row) that
> the migration preserves every field exactly and the resulting `state_for()`
> call returns it correctly; also verified the flag actually isolates two
> different `author_id`s in one channel when on, and that they share state
> (same object) when off, and that per-user state survives a simulated
> restart (fresh `STATE` dict, reload from disk).
>
> Reaction-control scoping (`RUN_BY_MESSAGE`/`PLAN_BY_MESSAGE` pruning, plus
> `RunContext`/`PlanContext`/`RevertContext`) also gained an `author_id`
> field and matching comparisons — without this, one person's run in a
> shared channel would silently clear another person's pending 🛑/🔄/📄 or
> ✅/🛑 controls even though their sessions are otherwise isolated. Reply-to-
> continue is deliberately the one exception: replying to *anyone's* result
> message resumes that thread into the replier's own per-user slot, since
> it's about which conversation thread to continue, not whose session
> originally posted it.
>
> Deliberately left channel-wide (not per-user) rather than expanding
> further: `!history`/`!cost`/budget tracking (the `runs` table has no
> author_id — these are about spend/audit trail, not conversational state),
> and the Phase 1.3 `approvals` table (also no author_id, since
> `anton_mcp_approve.py` only ever receives `ANTON_CHANNEL_ID`). The latter
> means one known gap: if `PER_USER_SESSIONS=1` and two people each have a
> `!strict`-mode run going in the same channel at once, one run ending also
> denies the other's still-pending approval. Documented in the README rather
> than fixed — plumbing an `ANTON_AUTHOR_ID` through the whole permission-
> prompt-tool chain for a narrow double-opt-in edge case wasn't worth it.

---

## Suggested build order

1. **Phase 0** (foundation: persistence, process-group kill, semaphore). ✅ done
2. **Phase 2.1–2.3** (auto-branch, `!diff`, `!revert`) — the safety net. ✅ done
3. **Phase 1.1** (reaction controls) + **Phase 1.2** (plan→approve→execute). ✅ done
4. **Phase 2.4** (`!commit`, `!pr`). ✅ done
5. **Phase 3** (attachments, reply-to-continue). ✅ done
6. **Phase 4** (history, budgets, notifications). ✅ done
7. **Phase 1.3** (live permission MCP) — powerful, standalone. ✅ done
8. **Phase 5–6** (automation, multi-project) as needed.

## Cross-cutting

- **Tests.** Add unit tests for the pure functions (`summarize_tool`, `chunk`,
  `build_command`, `authorized`) and any new git helpers.
- **`chunk()` code-fence safety.** Track fence state so splits don't break Markdown.
- **Security posture.** Consider a read-only / plan default so a fresh install
  isn't auto-approving shell commands out of the box.
