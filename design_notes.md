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

**Dashboard panel** (Alpine.js): draggable, run selector, All/To-me filter, HTMX polling every 3s, `@` autocomplete for participants.

**Django views** (Redis-only): `board_messages`, `board_reply`, `board_badge`, `board_active_runs`, `board_participants`. Board visibility is based on stream content (`XLEN > 0`), not run status or participant count.

**Connection pooling:** Django board views share a module-level `ConnectionPool` instead of creating a new Redis connection per request.

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

**Multi-user path:** One fuzzy container serves all users. The container is stateless between conversations — per-user scoping comes from the message sender:
- Memory namespace: `(sender_user_id, "fuzzy")`
- Board streams: `fuzzyclaw:board:fuzzy:{user_id}` (per-user, for privacy)
- Platform queries: service account token + `?owner=<sender_id>` filter on the API
- Phase 1 (current): single-user, `OWNER_ID=1` hardcoded, user's own DRF token
- Phase 2: service account user, API ViewSets accept `?owner=` param for service accounts, per-user board streams, `OWNER_ID` derived from message sender

**Setup (phase 1):** Create a DRF auth token (Admin > Auth Token > Tokens, or `docker compose exec web python manage.py drf_create_token <username>`), set it as `FUZZYCLAW_FUZZY_API_TOKEN` in `.env`, then `docker compose up -d fuzzy`.

Full plan: `code_reviews/fuzzy-always-on-assistant.md`.

### Direct Agent Dispatch (superseded by Fuzzy)

Originally planned as a dashboard area to fire individual agents without coordinator/briefing. Fuzzy replaces this — it's always available and can do specialist work directly via skills.

## Design Decisions

### API execution state: read-only (2026-04-10)

`Run` and `AgentRun` are exposed as `ReadOnlyModelViewSet`. The coordinator uses the ORM directly via `agent_tools.py`, never the REST API — so API-level mutation of execution fields (`status`, `report`, `raw_data`, timestamps) was pure attack surface with no operational benefit. User annotations go in `user_notes` (migration 0003). Runs are launched via `POST /api/briefings/{id}/launch/` and cancelled via `POST /api/runs/{id}/cancel/`.

### Code review hardening (2026-03-25, 3 rounds)

Cross-model review by GPT-5.4 Codex + Claude Opus. All fixes landed. Full findings in `code_reviews/`. Key areas: cross-user API scoping, XSS sanitization, symlink bypass in volume validation, container slot leak, base image hashing, agent error handling, board reliability. 170 tests passing.

## Graffiti Wall

To do:

- Stop button (cancel a running run from the UI)
- WhatsApp channel (as Message Board delivery channel, reference nanoclaw)
- Direct agent dispatch (talk to a specific agent without coordinator/briefing)

DONE: Fuzzy: the assistant

- has memory;
- is accessible via chat board even when no run is going on
- oversees the whole thing, not only one specific briefing!

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

- **VM deployment over HTTPS** — Caddy sidecar in compose for zero-config Let's Encrypt, reverse-proxy to `web:8000`. **Prerequisite: finish remaining deployment hardening (SRI hashes, attack surface, port exposure) before going public.**

- **"Infinite briefing" — coordinator always on** — Not feasible today (Celery time limits, container timeouts, context window). **Path A — periodic wake-up** works now: schedule the briefing every N minutes, each run reads memory, does a step, writes back, exits. "Cron for LLMs." **Path B — persistent session** needs a `Session` model, long-lived process outside Celery, LangGraph checkpointing — only worth it if sub-minute latency matters.

- **Auto-start on laptop boot** — Simplest path: add `restart: unless-stopped` to every service in `docker-compose.yml` and run `docker compose up -d` once. As long as Docker itself auto-starts, everything comes back. No systemd unit to maintain. Reach for a systemd user service (`~/.config/systemd/user/fuzzyclaw.service` with `After=docker.service`) only if you need explicit boot-time ordering or better logging.
