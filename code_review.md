# Code Review

## Claude Opus comments are round 2

● All 5 round-2 findings fixed. Here's the summary:

R2-1: Cross-tenant write access — Added perform_create() and perform_update() to both RunViewSet and AgentRunViewSet that  
 validate FK ownership. 4 new tests (2 rejection, 2 positive).

R2-2: Container-side HITL clamp — Changed max_wait from min(hitl_timeout, agent_timeout) to just hitl_timeout. The coordinator
already extends the agent's lifetime, so the container should match.

R2-3: ask_human() message loss — Added \_pending_messages buffer. During ask_human(), non-human messages addressed to this agent
are buffered instead of discarded. read_messages() drains the buffer first.

R2-4: Board send failure UI — JS sendMessage() now reads the error response body and renders it above the input with a red  
 border. Auto-clears after 5 seconds.

R2-5: Empty task description — Moved the check inside the outer try block so it raises ValueError, producing error.json + Redis
signal like all other startup failures.

169 tests passing (up from 165). Ready for Codex's next review.

## Codex gpt-5.4 round 3

I re-reviewed the latest refactor and ran the suite locally:

```bash
DATABASE_URL=sqlite:///test.db venv/bin/python manage.py test core
```

Result: 169 tests passed.

The good news first: the original round-2 findings are mostly closed now.

- Cross-tenant `Run` / `AgentRun` writes are materially fixed. The new ownership
  checks in [core/api_views.py:87](/home/memology/Documents/git_folders/fuzzyclaw/core/api_views.py#L87)
  and [core/api_views.py:123](/home/memology/Documents/git_folders/fuzzyclaw/core/api_views.py#L123)
  do block create-path abuse, and the new regression tests cover that at
  [core/tests.py:2412](/home/memology/Documents/git_folders/fuzzyclaw/core/tests.py#L2412)
  and [core/tests.py:2420](/home/memology/Documents/git_folders/fuzzyclaw/core/tests.py#L2420).
- The container-side HITL clamp is fixed in
  [agent_tools/message_board.py:58](/home/memology/Documents/git_folders/fuzzyclaw/agent_tools/message_board.py#L58).
- Empty `TASK_DESCRIPTION` now goes through the structured failure path in
  [agent_runner.py:144](/home/memology/Documents/git_folders/fuzzyclaw/agent_runner.py#L144).

What I would still pass back:

1. `read_messages()` now has a new buffered-message latency bug. The `_pending_messages`
   idea is directionally right, but [agent_tools/message_board.py:103](/home/memology/Documents/git_folders/fuzzyclaw/agent_tools/message_board.py#L103)
   to [agent_tools/message_board.py:145](/home/memology/Documents/git_folders/fuzzyclaw/agent_tools/message_board.py#L145)
   drains the buffer and then still enters the blocking `xread()` loop even when
   buffered messages already exist. If `read_messages(wait_seconds=1800)` is called
   with buffered messages pending, it can still wait up to the full timeout before
   returning them. This means the message-loss fix is not fully correct yet.

2. Mention-only replies are still accepted as empty messages. `@agent_1` passes the
   current validation in [core/views.py:384](/home/memology/Documents/git_folders/fuzzyclaw/core/views.py#L384)
   to [core/views.py:404](/home/memology/Documents/git_folders/fuzzyclaw/core/views.py#L404),
   becomes `recipient='agent_1'` and `content=''`, and is written to Redis anyway.
   The UI also still enables submit for that case because it only checks
   `messageInput.trim()` in [templates/base.html:210](/home/memology/Documents/git_folders/fuzzyclaw/templates/base.html#L210).
   Tests still only cover fully empty input at
   [core/tests.py:2210](/home/memology/Documents/git_folders/fuzzyclaw/core/tests.py#L2210).

3. New API integrity issue: authenticated owners can still forge system-controlled
   execution state on their own objects. [core/serializers.py:33](/home/memology/Documents/git_folders/fuzzyclaw/core/serializers.py#L33)
   and [core/serializers.py:44](/home/memology/Documents/git_folders/fuzzyclaw/core/serializers.py#L44)
   keep fields like `status`, `started_at`, `completed_at`, `container_id`,
   `report`, `raw_data`, `coordinator_report`, and `error_message` writable.
   That is no longer a cross-tenant bug, but it still means any authenticated owner
   can mint arbitrary run/agent state through the API.

4. Low-severity regression: the new send-failure UI uses `x-html` in
   [templates/core/partials/board_panel.html:90](/home/memology/Documents/git_folders/fuzzyclaw/templates/core/partials/board_panel.html#L90)
   to render raw non-OK response bodies captured at
   [templates/base.html:226](/home/memology/Documents/git_folders/fuzzyclaw/templates/base.html#L226).
   Functionally the error now surfaces, which is good, but this is still a new
   HTML injection sink for same-origin error bodies.

Net: Claude closed most of the round-2 work, but I would not call the messaging
fix set completely done until `read_messages()` returns buffered messages
immediately and mention-only posts are rejected.

---

## Step by step board messaging

### Component 1: Container-Side Tools (agent_tools/message_board.py)

This is what agents use inside their Docker container to talk on the board.

get_board_redis() (line 24) — Connects to Redis using the REDIS_URL env var. Returns a client or None if Redis is down. This is the agent's Redis connection.

build_message_board_tools() (line 44) — A factory function. It takes a connected Redis client, the agent's identity (self_id, e.g. market-researcher_423), and the run_id. It builds the Redis stream key
(fuzzyclaw:board:{run_id}) and returns four LangChain @tool functions that close over these values:

- post_message(to, message) (line 68) — Simple XADD to the stream. Fields: from, to, content, ts. The agent says who it's talking to (human, all, or another agent's self_id).
- read_messages(wait_seconds) (line 86) — Reads new messages using XREAD with blocking. It tracks a last_seen_id cursor across calls so it never re-reads old messages. It filters: only returns messages
  where to matches this agent's self_id or all. If wait_seconds > 0, it loops until the deadline, doing blocking reads. There's also a \_pending_messages buffer — if ask_human() received non-human messages
  while waiting, those get drained here first.
- list_participants() (line 152) — Reads the Redis set at fuzzyclaw:board:{run_id}:participants. Just an SMEMBERS call.
- ask_human(question) (line 166) — Posts a message to: human, then blocks waiting for a reply where from == human and to matches this agent. Any non-human messages that arrive during the wait get buffered
  into \_pending_messages so they aren't lost.

**_MODFIED_**

### Component 2: Tool Wiring (agent_tools/**init**.py + agent_runner.py)

In agent_tools/**init**.py line 80-81, build_tools() skips message_board — it just does continue. The comment says "Handled by agent_runner.py (needs Redis state)."

In agent_runner.py lines 162-174, the runner checks if message_board is in the agent's tools list AND both self_id and run_id env vars are set. If so, it calls get_board_redis(), and if that succeeds, it
registers as a participant by doing SADD on the participants set. The board_redis client is stored.

Then at lines 211-216, inside run_agent(), if board_redis is not None, it calls build_message_board_tools(board_redis, self_id, run_id) and extends the agent's tool list with those four tools.

At lines 199-203, the system prompt gets a message board section appended, telling the agent its identity and that it should use the tools for human interaction.

In the finally block (lines 276-283), the agent deregisters from the participants set via SREM.

### Component 3: Container Launch (core/containers.py)

start_agent_container() at line 386 sets up the env vars the agent needs. Lines 452-458 are the relevant ones:

- REDIS_URL — from Django settings
- RUN_ID — the current run's ID
- SELF*ID — formatted as {agent_name}*{agent_run_id}
- FUZZYCLAW_HITL_TIMEOUT — from settings (default 1800s)

These are what agent_runner.py and message_board.py read to connect and identify themselves.

### Component 4: Django Views (core/views.py)

These serve the dashboard side — the human's view of the board.

\_get_board_redis() (line 310) — Django's own Redis connection. Uses settings.FUZZYCLAW_REDIS_URL.

board_messages(run_pk) (line 324) — HTMX endpoint. Reads the stream using XREVRANGE (newest first), then reversed() to get chronological order. Applies an optional filter=human to show only messages to/from
human. Returns the board_messages.html partial.

board_reply(run_pk) (line 371) — HTMX POST endpoint. Parses the @recipient message format from the input. Does XADD with from: human. On Redis failure, returns HTTP 502 with error HTML. On success, calls
board_messages() to return the updated feed.

board_badge() (line 420) — Checks how many running runs have participants in their board set. Returns a count badge.

board_participants(run_pk) (line 443) — Returns the participant list for @ autocomplete. Reads the Redis set, splits self_id to extract agent name and agent_run_id, looks up AgentRun status.

board_active_runs() (line 481) — JSON endpoint returning runs that have active board participants. Used by the run selector dropdown.

### Component 5: Alpine.js Frontend (base.html script block)

The boardPanel Alpine component (line 26) manages the floating panel.

Lifecycle: init() sets up a $watch on $store.board.open. When the panel opens, onPanelOpen() loads the run list and starts feed polling. When it closes, polling stops. A badge poll (checkPending) runs every
3s regardless — if it detects new activity and the panel is closed, it auto-opens.

loadRuns() (line 101) — Fetches /board/active-runs/, populates the <select> dropdown. If the previously selected run is still valid, keeps it. Otherwise picks the first.

loadFeed() (line 134) — Fetches /runs/{id}/board/?filter={mode} via htmx.ajax, swaps the result into #board-feed, scrolls to bottom.

startFeedPolling() (line 89) — Stops any existing interval, starts a new one that calls loadFeed() every 3 seconds.

sendMessage() (line 210) — POSTs to /runs/{id}/board/reply/ with the message. On success, clears input and reloads feed. On error, strips HTML tags and shows the error text for 5 seconds.

onInput() (line 189) — Triggers @ autocomplete: if the input starts with @ and has no space yet, fetches the participants list.

### Component 6: Templates

- board_panel.html — The panel shell: header with run selector, filter tabs (All / To me), feed container, autocomplete dropdown, input bar.
- board_messages.html — Renders each message. Human messages go right (indigo), agent messages go left (dark). Shows sender, time, recipient if not all.
- board_participants.html — Autocomplete buttons with agent status badges.
- board_badge.html — Red count bubble for the nav bar.

---

That's the full picture. Six layers: container tools, tool wiring, container launch, Django views, Alpine.js, and templates. The data flows through Redis Streams — agents XADD/XREAD, Django views  
 XADD/XREVRANGE, and the browser polls the Django views via HTMX every 3 seconds.
