# Design Notes — FuzzyClaw

## Vision

Domain-agnostic agent orchestration platform. Users write markdown briefings (instructions + schedule), a coordinator agent interprets them and dispatches specialist agents — each running in its own Docker container — to execute the work. Results flow back through PostgreSQL and are browsable via a Django dashboard. Django is the management and interaction plane; Deep Agents (LangChain/LangGraph) is the execution plane.

## Engine

**In:** Briefing (markdown with steps and schedule) + registered specialist agents + skills on disk
**Out:** Specialist reports + coordinator synthesis report in PostgreSQL
**Must be true:**

- One container per launched specialist agent — security and isolation. Coordinator runs host-side in Celery.
- Privilege hierarchy: user → Django → coordinator → specialists (one-way, no escalation)
- Model choice is configurable per agent (strong models for coordinator, cheap for specialists)
- Skills follow the [Agent Skills specification](https://agentskills.io/home). Filesystem is source of truth.
- Persistent agent memory lives in PostgreSQL (Deep Agents' PostgresStore)

## Data Model

```
User ──1:N──> Briefing ──1:N──> Run ──1:N──> AgentRun
                                                │
                              agents/*.md ◄─────┘ (by agent_name)
```

Models: `Briefing` (owner, title, content, coordinator_model, is_active, schedule_text), `Run` (briefing FK, status, coordinator_report, triggered_by), `AgentRun` (run FK, agent_name, status, container_id, report, raw_data), `AgentImage` (agent_name, file_hash, image_tag). Agents and skills live on filesystem only (`agents/*.md`, `skills/*/SKILL.md`), read at runtime by `core/registry.py`.

## Component Specs

### Message Board (implementing)

Per-run shared communication channel. Agents, coordinator, and human exchange messages. Dashboard shows a floating, draggable panel.

**Architecture: Redis-only.** Redis Streams is the single source of truth. No Django model for messages. Messages are ephemeral — live during the run, cleaned up with it.

**Addressing:** `{agent_name}_{agent_run_id}`, `coordinator_{run_id}`, or `human`.

**Container-side tools** (`agent_tools/message_board.py`): `post_message`, `read_messages`, `list_participants`. Gated by `message_board` in agent frontmatter tools list.

**Notification middleware** (`agent_tools/board_middleware.py`): `BoardNotificationMiddleware` — a `before_model` hook that does a non-blocking Redis check between agent steps. If new messages are found for this agent, injects a SystemMessage so the LLM knows to check the board. Used by both specialist agents (`agent_runner.py`) and the coordinator (`agent_runtime.py`).

**Coordinator guard middleware** (`core/coordinator_middleware.py`): `CoordinatorGuardMiddleware` — an `after_model` hook that prevents the coordinator from finishing its ReAct loop while agents are still running. If the LLM returns text (no tool calls) but AgentRuns with `status='running'` exist, it injects a system message and redirects back to the model node via `jump_to`.

**Coordinator board access**: The coordinator registers as `coordinator_{run_id}` on the board and gets `post_message`, `read_messages`, `list_participants` tools — same as specialist agents. This lets the coordinator communicate directly with agents and humans.

**Dashboard panel** (Alpine.js): draggable, run selector, All/To-me filter, HTMX polling every 3s, `@` autocomplete for participants.

**Django views** (Redis-only): `board_messages`, `board_reply`, `board_badge`, `board_active_runs`, `board_participants`. Board visibility is based on stream content (`XLEN > 0`), not run status or participant count — runs with messages show in the dropdown regardless of status.

**Connection pooling:** Django board views share a module-level `ConnectionPool` instead of creating a new Redis connection per request.

**Lessons from v1:** Don't name specific tools in agent prompts. Don't build a sync layer when both sides can read Redis. `hx-boost="true"` on `<body>` breaks HTMX partials. Alpine `@click` only works inside `x-data` scope.

### Briefing Scheduling (implemented)

NL schedule → Gemini Flash LLM call → cron → `django-celery-beat` PeriodicTask. Direct LLM call from Django, not via coordinator. `sync_schedule()` handles idempotency, pause/resume, concurrent run guard. See `core/scheduling.py`.

### Direct Agent Dispatch (planned)

Dashboard area to fire individual agents without coordinator/briefing. Agent picker + task textarea + report display. Same container launch path. Good candidate for personal assistant agent (Documents + Gmail).

## Code Review Fix Sprint (2026-03-25)

Cross-model review by GPT-5.4 Codex + Claude Opus. Full findings in `code_review.md`.

### Group 1: Messaging Stability (active blocker)

**Fix 1.1 — HITL timeout vs agent timeout (#8)**
- **Core change:** Agents with `message_board` tool get `FUZZYCLAW_HITL_TIMEOUT` as their effective lifetime instead of `FUZZYCLAW_AGENT_TIMEOUT`
- **Affected layers:** settings → agent_tools.py (`check_reports`) → containers.py (pass timeout env var) → message_board.py (clamp `max_wait`)
- **Status:** done

**Fix 1.2 — Board polling restart on reopen (#9)**
- **Core change:** Call `startFeedPolling()` in `onPanelOpen()` when `currentRunId` is already set
- **Affected layers:** templates/base.html (Alpine.js)
- **Status:** done

**Fix 1.3 — Board messages newest-first (#10)**
- **Core change:** Replace `XRANGE` with `XREVRANGE` + reverse in `board_messages` view
- **Affected layers:** core/views.py → update mocks in core/tests.py
- **Status:** done

**Fix 1.4 — Board reply error handling (#11)**
- **Core change:** Return HTTP 502 with error HTML on Redis write failure instead of silent 200
- **Affected layers:** core/views.py (`board_reply`)
- **Status:** done

**Fix 1.5 — message_board.py reliability (audit item)**
- **Core change:** Handle Redis connection failure in `get_board_redis()` (return None instead of crash), add backoff when stream has traffic but nothing addressed to this agent
- **Affected layers:** agent_tools/message_board.py
- **Status:** done

### Group 2: Security Criticals (must-fix before multi-user)

**Fix 2.1 — Cross-user API scoping (#1)**
- **Core change:** Add `get_queryset()` to `BriefingViewSet`, `RunViewSet`, `AgentRunViewSet` — scope to `request.user`
- **Affected layers:** core/api_views.py → tests (new `APIIsolationTests` class)
- **Status:** done

**Fix 2.2 — Markdown XSS sanitization (#2)**
- **Core change:** Sanitize HTML with `bleach.clean()` before `mark_safe()` in markdown template filters
- **Affected layers:** core/templatetags/markdown_extras.py → requirements.txt (pin bleach) → tests
- **Status:** done

**Fix 2.3 — Symlink bypass in volume validation (#5)**
- **Core change:** Use `os.path.realpath()` instead of `os.path.normpath()` in `_resolve_volume_host_path()`
- **Affected layers:** core/containers.py → tests (symlink traversal)
- **Status:** done

### Group 3: Operational Robustness

**Fix 3.1 — Container slot leak on startup failure (#3)**
- **Core change:** Wrap post-increment setup in try/except, decrement `_container_count` on failure
- **Affected layers:** core/containers.py (`start_agent_container`)
- **Status:** done

**Fix 3.2 — Base image hash for runtime changes (#4)**
- **Core change:** Hash `Dockerfile.agent`, `requirements-agent.txt`, `agent_runner.py`, `agent_tools/**` into base image rebuild decision. Store hash as Docker image label.
- **Affected layers:** core/containers.py (`_ensure_base_image`, new `_hash_base_image_inputs`)
- **Status:** done

**Fix 3.3 — Early agent startup error handling (#6)**
- **Core change:** Wrap entire `main()` in agent_runner.py in outer try/except that writes `error.json` + signals Redis
- **Affected layers:** agent_runner.py
- **Status:** done

### Round 2 Fixes (Codex follow-up)

**R2-1 — Cross-tenant write access on Run/AgentRun APIs**
- Added `perform_create()` / `perform_update()` with FK ownership validation
- **Status:** done

**R2-2 — Container-side HITL clamp**
- Changed `max_wait` from `min(hitl, agent)` to `hitl_timeout` directly
- **Status:** done

**R2-3 — ask_human() drops non-human messages**
- ~~Added `_pending_messages` buffer~~ — removed: `ask_human` tool dropped in simplification (2026-03-28)
- **Status:** superseded

**R2-4 — Board send failure not surfaced in UI**
- JS renders error text above input; `x-text` (not `x-html`) to prevent injection
- **Status:** done

**R2-5 — Empty task description bypasses error path**
- Moved check inside outer try block in `agent_runner.py`
- **Status:** done

### Round 3 Fix (Codex follow-up)

**R3-1 — Buffered messages returned immediately**
- ~~`read_messages()` returns instantly when `_pending_messages` has content~~ — removed: buffer dropped with `ask_human` (2026-03-28)
- **Status:** superseded

**R3-2 — Mention-only empty messages rejected**
- `board_reply` rejects `@agent_name` with no message body (400)
- **Status:** done

### Resolved Decision: API execution state writability

The coordinator updates `status`, `report`, `container_id`, etc. on Runs and AgentRuns via the API. These fields are intentionally kept writable. The security boundary is **ownership scoping** (queryset filtered to `request.user`), not field-level read-only restrictions. Locking execution fields would break the coordinator workflow. Accepted as the trust model after 3-round cross-model review (GPT-5.4 Codex + Claude Opus, 2026-03-25).

## Graffiti Wall

Still to do:

- ~~Code review fixes~~ — completed (3 rounds, 170 tests passing)
- ~~Message Board — stabilize~~ — hardened 2026-03-28: simplified tools, middleware, coordinator guard, view gates removed
- ~~Message Board — fix addressing~~ — done 2026-03-29: multi-mention, no-@ defaults to coordinator
- Stop button (cancel a running run from the UI)
- WhatsApp channel (as Message Board delivery channel, reference nanoclaw)
- Direct agent dispatch (talk to a specific agent without coordinator/briefing)
- Login / logout page styling

### Board Addressing Fix (next)

**Problem:** `board_reply` only parses one `@recipient` at the start. Everything without `@` defaults to `to: all`, which means every agent and coordinator sees it. This causes agents to read messages not meant for them.

**Fix:**
1. Parse all `@agent` tokens from the message (not just the first). For each mentioned recipient, do a separate `XADD` with `to: that_recipient`.
2. If no `@` is present, default to `coordinator_{run_id}` instead of `all`. The coordinator is the natural recipient when the human is just talking into the board.
3. `@all` becomes an explicit broadcast — the user must type it to reach everyone.

**Affected layers:** `core/views.py` (`board_reply`), Alpine.js `sendMessage()` (no change needed — message format stays the same), tests.

**Status:** done (2026-03-29)

Known issues:

- BoardNotificationMiddleware and `read_messages` have separate cursors. The middleware can re-detect a message that `read_messages` already consumed, sending a redundant `[Board: You have N new message(s)]` to the LLM. Not harmful but wasteful — consider sharing the cursor or having the middleware skip if `read_messages` was the most recent tool call.

Idea:

- Keep specialist container on until the coordinator tells it can go.
  While the specialist is ephemeral, by keeping it on for a while the coordinator
  has an opportunity to use the same agent (and its existing context) to refine stuff.
  **Update:** Message Board makes this even more relevant — a waiting agent is a paused
  container, not a dead one.

- **`wait` tool** — Agents often finish their ReAct loop instead of waiting for a human reply, because `read_messages(wait_seconds=1800)` requires the LLM to actively choose to block. A `wait` tool with explicit semantics ("idle for N seconds or until a board message arrives") could make the intent clearer and give the `BoardNotificationMiddleware` time to fire between steps. Could also serve as a general "yield and check for updates" primitive. The agent calls `wait()`, the middleware checks for messages, and the agent gets notified naturally. Related to shenlong prompting issues observed 2026-03-28/29 where the agent moves on instead of waiting for the human.
