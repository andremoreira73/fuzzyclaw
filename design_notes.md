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

### Account Pages (implemented)

Login, logout, profile, and password change — styled to match the design system (indigo/purple gradients, rounded cards, Inter font).

**Login** (`registration/login.html`): Standalone page extending `base.html`. Django's built-in `LoginView` serves it.

**Logout** (`core/logout_confirm.html`): Confirmation page that POSTs to Django's `LogoutView`. Required because Django 5 returns 405 on GET `/logout/`. Route: `/sign-out/`.

**Profile** (`core/profile.html`): Edit first name, last name, email. Username is read-only (admin-managed). Links to password change. Uses `ProfileForm` in `core/forms.py`. Route: `/profile/`.

**Password change** (`core/password_change.html`, `core/password_change_done.html`): Custom `PasswordChangeView` subclass pointing to `core/` templates (avoids Django admin template collisions). Routes: `/password-change/`, `/password-change/done/`.

**User menu** (in `base.html` nav): Alpine.js dropdown replacing the old plain username + logout link. Shows Profile and Sign Out options with animated transitions.

**Why custom templates under `core/` instead of `registration/`:** Django admin ships its own `registration/password_change_form.html` and `registration/logged_out.html`. Putting account templates under `core/` with explicit `template_name` in views avoids `INSTALLED_APPS` ordering collisions. The login template stays at `registration/login.html` — overriding admin's login page is the standard Django pattern.

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

### Resolved Decision: API execution state writability — REVERSED (2026-04-10, F18)

Earlier note (2026-03-25): execution fields were kept writable on the assumption that the coordinator needed the REST API to update them. That assumption was wrong — the coordinator uses the ORM directly via `agent_tools.py`, never the API. The F18 finding in the 2026-04-10 review caught this: same-user mutation was leaving the run log untrustworthy as an audit surface with no operational benefit.

**Current model:** `Run` and `AgentRun` are exposed as `ReadOnlyModelViewSet`. Execution fields (`status`, `report`, `raw_data`, timestamps) are read-only in serializers. User annotations go in a new `user_notes` field (migration 0003). Runs are launched via `POST /api/briefings/{id}/launch/` and cancelled via `POST /api/runs/{id}/cancel/`. Admin retains full mutation rights via Django admin as before.

## Graffiti Wall

Still to do:

- ~~Code review fixes~~ — completed (3 rounds, 170 tests passing)
- ~~Message Board — stabilize~~ — hardened 2026-03-28: simplified tools, middleware, coordinator guard, view gates removed
- ~~Message Board — fix addressing~~ — done 2026-03-29: multi-mention, no-@ defaults to coordinator
- ~~Login / logout / profile pages~~ — done 2026-04-10: logout confirmation (fixes Django 5 GET→405), profile editing, password change, Alpine.js user dropdown
- Stop button (cancel a running run from the UI)
- WhatsApp channel (as Message Board delivery channel, reference nanoclaw)
- Direct agent dispatch (talk to a specific agent without coordinator/briefing)

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
- **Stuck runs** — runs frequently get stranded in `status=running` with nothing actually executing. Currently resolved by editing the row in Django admin or via the new `POST /api/runs/{id}/cancel/` action (F18, 2026-04-10), but those are workarounds. Root cause still unknown: likely candidates are Celery worker crashes mid-run, container OOM, unhandled exception paths in `core/containers.py` or `agent_runner.py` that skip the terminal-state write, or Redis/DB failures on the completion path. Investigate before adding a sweeper task — the real fix is probably a missing `except` branch somewhere, not a timeout-based janitor.

Idea:

- Keep specialist container on until the coordinator tells it can go.
  While the specialist is ephemeral, by keeping it on for a while the coordinator
  has an opportunity to use the same agent (and its existing context) to refine stuff.
  **Update:** Message Board makes this even more relevant — a waiting agent is a paused
  container, not a dead one.

- **`wait` tool** — Agents often finish their ReAct loop instead of waiting for a human reply, because `read_messages(wait_seconds=1800)` requires the LLM to actively choose to block. A `wait` tool with explicit semantics ("idle for N seconds or until a board message arrives") could make the intent clearer and give the `BoardNotificationMiddleware` time to fire between steps. Could also serve as a general "yield and check for updates" primitive. The agent calls `wait()`, the middleware checks for messages, and the agent gets notified naturally. Related to shenlong prompting issues observed 2026-03-28/29 where the agent moves on instead of waiting for the human.

- **`forget` memory tool** — Natural extension of the `(owner_id, agent_name)` namespace introduced in F4 (2026-04-10). An agent-side tool that calls `store.delete(namespace=(owner_id, agent_name), key=...)` lets users say "forget what I told you about ACME Corp" and have it actually persist. Variants: `forget(key)` for a single entry, `forget_all()` to wipe the namespace. Scoped to the agent's own namespace by construction, so no cross-user leakage. Lives alongside `remember`/`recall`/`recall_all` in `agent_tools/memory.py`.

- **Run two FuzzyClaw instances on one machine** — Technically doable today by running each instance with a separate `.env`, separate `docker compose -p <name>` project, and distinct host ports (8200, 6380, Postgres). The one real gotcha is `FUZZYCLAW_AGENT_IMAGE_PREFIX` — it's settings-driven but shared by default, so both instances fight over `docker ps` filters and agent container names. Per-instance prefix via `.env` is the fix. Worth a short "running multiple instances" docs section if this becomes a real use case.

- **Auto-start on laptop boot** — Simplest path: add `restart: unless-stopped` to every service in `docker-compose.yml` and run `docker compose up -d` once. As long as Docker itself auto-starts, everything comes back. No systemd unit to maintain. Reach for a systemd user service (`~/.config/systemd/user/fuzzyclaw.service` with `After=docker.service`) only if you need explicit boot-time ordering or better logging.

- **VM deployment over HTTPS on an owned domain** — Shape: Caddy sidecar in compose for zero-config Let's Encrypt, reverse-proxy to `web:8000`; `ALLOWED_HOSTS` + `CSRF_TRUSTED_ORIGINS` via env; `SECURE_PROXY_SSL_HEADER`, `SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`, HSTS in `prod.py`; firewall closes 5432/6380/8200 so only 443 is public. **Prerequisite: finish Sprint 3 (D2 SRI hashes, D5 gcc attack surface, D8 port exposure) before going public — these turn from "nice to have" into "must fix" the moment the instance is reachable.** The `django-deploying` skill covers the Caddy + certbot + compose pattern.

- **"Infinite briefing" — coordinator always on** — Not really feasible today. Blockers: Celery task time limit (~30 min default), agent container timeouts (`FUZZYCLAW_AGENT_TIMEOUT` / `FUZZYCLAW_HITL_TIMEOUT` cap at ~30 min), ReAct loop max-iterations cap, model context window fills across long loops, worker restart orphans the run. Two realistic paths:
  - **Path A — periodic wake-up** (works today, zero code): schedule the briefing every N minutes, each run reads memory, does a step, writes back, exits. The F4 `(owner_id, agent_name)` memory namespace gives continuity. "Cron for LLMs" — fits the existing architecture cleanly. Covers ~90% of "always-on" use cases.
  - **Path B — persistent session** (new feature, weeks of work): a `Session` model separate from `Run`, long-lived coordinator process outside Celery, LangGraph checkpointing. Only worth building if sub-minute reaction latency matters and Path A's interval isn't tight enough.

## Thoughts from codex

My view: this is a real project with a coherent product and architecture, not a toy agent app.

What’s strong:

- The core split is correct: Django as control plane, agents as execution plane, filesystem as source of truth for agents/skills,
  DB as the persistent product.
- The repo shows deliberate engineering choices instead of trend-chasing: HTMX/server-rendered UI, container isolation per
  specialist, explicit model/tool registries, and good test coverage for a young system.
- The codebase has a clear mental model. That matters more than polish at this stage.

What’s still immature:

- The hardest parts are exactly where this kind of system usually breaks: lifecycle control, cancellation semantics, resource
  cleanup, and tool/run scoping.
- The project is strongest at “start work” and “record work,” but still weaker at “stop work safely” and “guarantee work stays
  inside its intended boundaries.”
- A lot of the remaining risk is operational correctness, not feature completeness.

My blunt assessment:

- This is closer to a promising platform than to a fragile demo.
- The architecture is good enough to keep investing in.
- The main thing that will determine whether it becomes production-grade is not more features, but stricter execution semantics:
  cancellation, run ownership, coordinator scoping, container/resource reconciliation, and clearer invariants.

If I were prioritizing next:

1. Finish cancellation/cleanup semantics end-to-end.
2. Fully close the remaining coordinator tool-binding gaps.
3. Tighten deployment/runtime config so “misconfigured but starts anyway” is minimized.
4. Add more tests around failure and recovery paths, not just happy-path correctness.

So: strong foundation, credible design, still one layer away from being operationally trustworthy.
