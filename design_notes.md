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

### Message Board (implemented)

Per-run shared communication channel. Agents, coordinator, and human exchange messages. Dashboard shows a floating, draggable panel.

**Architecture: Redis-only.** Redis Streams is the single source of truth. No Django model for messages. Messages are ephemeral — live during the run, cleaned up with it.

**Addressing:** `{agent_name}_{agent_run_id}`, `coordinator_{run_id}`, or `human`. Multi-mention supported; no-`@` defaults to coordinator. `@all` for explicit broadcast.

**Container-side tools** (`agent_tools/message_board.py`): `post_message`, `read_messages`, `list_participants`. Gated by `message_board` in agent frontmatter tools list.

**Notification middleware** (`agent_tools/board_middleware.py`): `BoardNotificationMiddleware` — a `before_model` hook that does a non-blocking Redis check between agent steps. If new messages are found for this agent, injects a SystemMessage so the LLM knows to check the board. Used by both specialist agents (`agent_runner.py`) and the coordinator (`agent_runtime.py`).

**Coordinator guard middleware** (`core/coordinator_middleware.py`): `CoordinatorGuardMiddleware` — an `after_model` hook that prevents the coordinator from finishing its ReAct loop while agents are still running. If the LLM returns text (no tool calls) but AgentRuns with `status='running'` exist, it injects a system message and redirects back to the model node via `jump_to`.

**Coordinator board access**: The coordinator registers as `coordinator_{run_id}` on the board and gets `post_message`, `read_messages`, `list_participants` tools — same as specialist agents.

**Dashboard panel** (Alpine.js): draggable, run selector, All/To-me filter, HTMX polling every 3s, `@` autocomplete with arrow key navigation and type-ahead filtering.

**Django views** (Redis-only): `board_messages`, `board_reply`, `board_badge`, `board_active_runs`, `board_participants`. Board visibility is based on stream content (`XLEN > 0`), not run status or participant count.

**Connection pooling:** Django board views share a module-level `ConnectionPool` instead of creating a new Redis connection per request.

**Mention parser:** `@([\w-]+)` regex — captures word chars and hyphens only. Trailing punctuation (e.g. `@fuzzy: hello`) is stripped, not included in the recipient name.

**Lessons learned:** Don't name specific tools in agent prompts. Don't build a sync layer when both sides can read Redis. `hx-boost="true"` on `<body>` breaks HTMX partials. Alpine `@click` only works inside `x-data` scope.

### Briefing Scheduling (implemented)

NL schedule → Gemini Flash LLM call → cron → `django-celery-beat` PeriodicTask. Direct LLM call from Django, not via coordinator. `sync_schedule()` handles idempotency, pause/resume, concurrent run guard. See `core/scheduling.py`.

### Account Pages (implemented)

Login, logout, profile, password change. Django 5 returns 405 on GET `/logout/` — hence the confirmation page that POSTs. Account templates live under `core/` (not `registration/`) to avoid `INSTALLED_APPS` ordering collisions with Django admin's own templates.

### Fuzzy — Always-On Platform Assistant (implemented)

Persistent agent ("fuzzy") that starts with `docker compose up` and stays alive indefinitely. Idle until the user posts on the board, wakes up, responds, goes idle. Not a coordinator — it's the user's personal assistant for FuzzyClaw.

**Architecture:** Docker Compose service running `fuzzy_runner.py`. Idle loop blocks on `XREAD` against `fuzzyclaw:board:fuzzy`. Each incoming message creates a fresh agent invocation (clean context, persistent memory). Graceful shutdown on SIGTERM. Auto-reconnects on Redis connection loss.

**Board identity:** `fuzzy` on stream `fuzzyclaw:board:fuzzy` (permanent, never cleaned up). Always appears as first entry in the board panel's run selector. Dashboard routes fuzzy-specific URLs (`/board/fuzzy/`, `/board/fuzzy/reply/`, `/board/fuzzy/participants/`).

**Platform query tools** (`agent_tools/platform_query.py`): `list_briefings`, `get_briefing`, `list_runs`, `get_run`, `list_agent_runs`, `get_agent_report`. Calls the Django REST API via `requests` with a DRF auth token (`API_TOKEN` env var). Registered as the `platform_query` tool bundle in `build_tools()`.

**Memory:** `(owner_id, "fuzzy")` namespace in PostgresStore. Memory never collides across users — the sender's user ID determines the namespace.

**Container:** `Dockerfile.fuzzy` (same base as `Dockerfile.agent`, different entrypoint). Skills mounted read-only, user data mounted read-write at `/app/data`.

**Multi-user (implemented):** One fuzzy container serves all users. The container is stateless between conversations — per-user scoping comes from the message sender:

- `user_id` field in every board message (set by Django view, read by fuzzy runner)
- Memory namespace: `(sender_user_id, "fuzzy")` — derived from message, not env var
- Board stream: single stream, filtered by `user_id` in Django views (each user sees only their conversation)
- Platform queries: service account token (staff user) + `?owner=<sender_id>` filter on all API ViewSets
- `OWNER_ID` env var is now optional fallback (empty by default), data volume mounted at `./data` (all users)

**Activity indicator:** Redis key `fuzzyclaw:fuzzy:status` set to `"thinking"` while processing (auto-expires after 5min). Board panel polls `/board/fuzzy/status/` every 2s. Shows bouncing-dot typing indicator in the feed area and a `●` prefix on "Fuzzy Assistant" in the run selector. Both disappear when fuzzy finishes.

**Board tools `initial_position`:** `setup_message_board()` accepts an `initial_position` parameter (default `'0-0'`). Fuzzy passes the triggering message's stream ID so the agent's `read_messages` tool starts after the trigger, not from the beginning of the permanent stream.

**Duplicate-post prevention:** After the agent's ReAct loop, the runner checks the last 5 stream entries. If fuzzy already posted via the board tool, the safety-net post is skipped.

**Conversational memory (implemented):** Board history is read before each invocation and passed as HumanMessage/AIMessage pairs. Living summary approach: up to 15 messages pass as-is. Over 15, a cheap LLM (Gemini Flash) summarizes older messages, keeps last 3 interaction pairs. Summary stored in Redis (`fuzzyclaw:fuzzy:summary:{owner_id}`), compounds across cycles. Config: `FUZZY_HISTORY_MAX=15`, `FUZZY_HISTORY_KEEP_RECENT=3`, `FUZZY_SUMMARY_MODEL=gemini-2.5-flash`.

**Briefing-scoped agent memories (implemented):** Specialist memory namespace is now `(owner_id, agent_name, briefing_id)`. `BRIEFING_ID` env var passed from `containers.py` to agent containers. Fuzzy stays at `(owner_id, "fuzzy")` — global, not tied to a briefing. Briefing ID shown in the UI (list + detail page) so users understand memory compartmentalization.

**Redis persistence (implemented):** Named volume `redis_data:/data` added. Board streams, fuzzy summaries, and conversation history survive `docker compose down`/`up`. Redis saves RDB on graceful shutdown (SIGTERM).

**Setup (phase 1):** Create a DRF auth token (Admin > Auth Token > Tokens, or `docker compose exec web python manage.py drf_create_token <username>`), set it as `FUZZYCLAW_FUZZY_API_TOKEN` in `.env`, then `docker compose up -d fuzzy`.

Full plan: `code_reviews/fuzzy-always-on-assistant.md`.

## Design Decisions

### API execution state: read-only (2026-04-10)

`Run` and `AgentRun` are exposed as `ReadOnlyModelViewSet`. The coordinator uses the ORM directly via `agent_tools.py`, never the REST API — so API-level mutation of execution fields (`status`, `report`, `raw_data`, timestamps) was pure attack surface with no operational benefit. User annotations go in `user_notes` (migration 0003). Runs are launched via `POST /api/briefings/{id}/launch/` and cancelled via `POST /api/runs/{id}/cancel/`.

### Fuzzy filesystem: shared in multi-user mode (2026-04-14)

Fuzzy runs as a single shared container with `./data:/app/data:rw`. In multi-user setups, all users' files under `data/users/` are visible to fuzzy (and thus to any user via bash). Accepted trade-off — bash is too valuable to remove, and reliably sandboxing shell path access in a shared container adds more complexity than it's worth. Memory, platform queries, and board messages remain properly scoped per-user. For full filesystem isolation, deploy one fuzzy container per user (single-user mode with `OWNER_ID` set).

### Code review hardening (2026-03-25, 3 rounds)

Cross-model review by GPT-5.4 Codex + Claude Opus. All fixes landed. Full findings in `code_reviews/`. Key areas: cross-user API scoping, XSS sanitization, symlink bypass in volume validation, container slot leak, base image hashing, agent error handling, board reliability. 174 tests passing.

## Graffiti Wall

To do:

- Stop button (cancel a running run from the UI)
- WhatsApp channel (as Message Board delivery channel, reference nanoclaw)
- Cross-board @fuzzy — wake fuzzy from within any run board (forward message to fuzzy's stream with run context, respond on the originating run board)
- Flush fuzzy conversation button — clear the board stream + living summary for the current user (Django view + board panel UI)

DONE: Fuzzy: the assistant (2026-04-13)

- has memory, platform query tools, web access, skills
- accessible via chat board even when no run is going on
- oversees the whole thing, not only one specific briefing
- typing indicator + pulsing dot while thinking
- conversational memory with living summary (board history + Gemini Flash summarization)
- briefing-scoped agent memories (`owner_id.agent_name.briefing_id`)
- multi-user scoping (`user_id` in messages, service account API filter, per-user memory)
- Redis persistence for board streams and summaries
- supersedes "Direct agent dispatch" idea

DONE: Direct agent dispatch — superseded by fuzzy (always available, does specialist work via skills)

DONE: Filesystem for users:

- comfortable access for users, see @code/reviews/fuzzyclaw-multiuser-files.md
- should have a button between "skills" and "board" in the navbar

Skills:

- allow user to add skills using the new "Filesystem for users"

Agents:

- allow users to add agents using the new "Filesystem for users" and a button to run sync agents.

Mobile version:

- user hyperview and hxml at a later point.

Connectors:

- allow users to connect to their favorite apps (google mail, outlook, etc.)

Priority guidance (from Codex assessment): operational correctness before features — cancellation/cleanup semantics, failure/recovery paths, deployment hardening. The architecture is solid; what's missing is the "stop work safely" and "stay inside boundaries" layer.

Known issues:

- BoardNotificationMiddleware and `read_messages` have separate cursors. The middleware can re-detect a message that `read_messages` already consumed, sending a redundant `[Board: You have N new message(s)]` to the LLM. Not harmful but wasteful — consider sharing the cursor or having the middleware skip if `read_messages` was the most recent tool call.
- **Stuck runs** — runs frequently get stranded in `status=running` with nothing actually executing. Currently resolved by editing the row in Django admin or via `POST /api/runs/{id}/cancel/`, but those are workarounds. Root cause still unknown: likely candidates are Celery worker crashes mid-run, container OOM, unhandled exception paths in `core/containers.py` or `agent_runner.py` that skip the terminal-state write, or Redis/DB failures on the completion path. Investigate before adding a sweeper task — the real fix is probably a missing `except` branch somewhere, not a timeout-based janitor.
- **Coordinator finishes while agents still running** (observed 2026-04-13) — The coordinator marked failed agent_runs (from `/data` path error), dispatched retries, then concluded the run while a retry shenlong was still active in its container. The `CoordinatorGuardMiddleware` should have blocked this. Likely cause: the guard checks `AgentRun.status == 'running'` in the DB, but the failed agent_run was already marked `failed` and the new retry hadn't been recorded yet (or was recorded but the coordinator's model call happened before the DB write). The human had to tell the orphaned shenlong to stop via the board. Investigate the race between dispatch, DB status writes, and the guard's query timing.

Ideas:

- **Persistent specialist containers** — Keep specialist container alive until the coordinator dismisses it. The coordinator can reuse the same agent (and its existing context) for refinement. Message Board makes this natural — a waiting agent is a paused container, not a dead one.

- **`wait` tool** — Agents often finish their ReAct loop instead of waiting for a human reply. A `wait` tool with explicit semantics ("idle for N seconds or until a board message arrives") would give `BoardNotificationMiddleware` time to fire between steps. General "yield and check for updates" primitive.

- **`forget` memory tool** — Extension of the `(owner_id, agent_name)` namespace. Agent-side tool calling `store.delete(namespace=(owner_id, agent_name), key=...)`. Variants: `forget(key)` for a single entry, `forget_all()` to wipe the namespace. Scoped by construction, no cross-user leakage. Lives alongside `remember`/`recall`/`recall_all` in `agent_tools/memory.py`.

- **Memory TTL / expiration** — PostgresStore already has `expires_at` and `ttl_minutes` columns. `store.put()` accepts `ttl_minutes`. Options: default TTL on specialist memories (e.g. 30 days, stale findings auto-expire), no TTL on fuzzy (user preferences persist), or agent-controlled TTL via an optional `expires_in_days` param on the `remember` tool so the LLM decides what's ephemeral vs permanent.

- **VM deployment over HTTPS** — Caddy sidecar in compose for zero-config Let's Encrypt, reverse-proxy to `web:8000`. **Prerequisite: finish remaining deployment hardening (SRI hashes, attack surface, port exposure) before going public.**

- **"Infinite briefing" — coordinator always on** — Not feasible today (Celery time limits, container timeouts, context window). **Path A — periodic wake-up** works now: schedule the briefing every N minutes, each run reads memory, does a step, writes back, exits. "Cron for LLMs." **Path B — persistent session** needs a `Session` model, long-lived process outside Celery, LangGraph checkpointing — only worth it if sub-minute latency matters.

- **Auto-start on laptop boot** — Simplest path: add `restart: unless-stopped` to every service in `docker-compose.yml` and run `docker compose up -d` once. As long as Docker itself auto-starts, everything comes back. No systemd unit to maintain. Reach for a systemd user service (`~/.config/systemd/user/fuzzyclaw.service` with `After=docker.service`) only if you need explicit boot-time ordering or better logging.

- **Latency investigation** — End-to-end run latency is noticeable. Need to profile where time goes: LangGraph/Deep Agents overhead, coordinator ReAct loop, container startup, model API calls, Redis polling intervals. Before optimizing, measure.

- **Dockerfile hardening pass** — (1) Multi-stage builds: gcc, postgresql-client, and build tools currently stay in the final image for Dockerfile, Dockerfile.agent, and Dockerfile.fuzzy. A builder stage would shave these out. (2) `.dockerignore` is effectively empty (2 bytes) — should exclude `.git`, `venv`, `data/`, `node_modules`, `__pycache__`. (3) BuildKit cache mounts (`--mount=type=cache`) for pip would speed up rebuilds, especially on low-RAM VMs. (4) `docker-compose.prod.yml` uses unpinned tags for postgres and redis — should pin by digest like the dev compose does.

- **VM deployment over HTTPS** — ~~Caddy sidecar~~ Done (2026-04-14): nginx + certbot, deployed to GCP VM. See `VM_installation/` skill and `docker-compose.prod.yml`.

### Memory & scoping overhaul (2026-04-13)

Three changes shipped together: briefing-scoped specialist memories (`owner_id.agent_name.briefing_id` namespace via `BRIEFING_ID` env var), fuzzy conversational memory (board-history-as-context with living summary via Gemini Flash), and per-user fuzzy scoping (`user_id` in board messages, `?owner=` API filter for staff, dynamic `OWNER_ID`). Redis persistence added (`redis_data` volume) so board streams and summaries survive restarts. Old un-scoped memories wiped. Full plan: `code_reviews/fuzzy-memory-and-scoping.md`.
