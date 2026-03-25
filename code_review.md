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
