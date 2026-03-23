# Design Notes — FuzzyClaw

## Vision

Domain-agnostic agent orchestration platform. Users write markdown briefings (instructions + schedule), a coordinator agent interprets them and dispatches specialist agents — each running in its own Docker container — to execute the work. Results flow back through PostgreSQL and are browsable via a Django dashboard. Django is the management and interaction plane; Deep Agents (LangChain/LangGraph) is the execution plane.

**Not a single-purpose tool.** The same FuzzyClaw instance can run web monitoring (like IM_scanner), checklist audits (like AssetPrism), or anything else — differentiated only by its briefings, agents, and skills. Two parallel projects can share the same underlying architecture with different focus.

## Engine

**In:** Briefing (markdown with steps and schedule) + registered specialist agents + skills on disk
**Out:** Specialist reports + coordinator synthesis report in PostgreSQL
**Must be true:**

- One container per launched specialist agent — security and isolation. Coordinator runs host-side in Celery.
- Privilege hierarchy: user → Django → coordinator → specialists (one-way, no escalation)
- Coordinator follows briefing steps but has autonomy for supporting decisions
- Model choice is configurable per agent (strong models for coordinator, cheap for specialists)
- Agents have real capabilities: bash, coding, tool use — same power as Claude Code / Deep Agents
- Skills follow the [Agent Skills specification](https://agentskills.io/home). Agents and skills live in `agents/` and `skills/` directories.
- Persistent agent memory lives in PostgreSQL (Deep Agents' PostgresStore)
- User interaction goes through Django (and optionally WhatsApp)

## Data Architecture

### What lives in the database (Django ORM)

Briefings (user-authored), execution history (Runs, AgentRuns), user auth. Django manages what it's good at.

### What lives on the filesystem

Agent definitions (`agents/*.md`) and skill definitions (`skills/*/SKILL.md`). Read at runtime by `core/registry.py` with 30s TTL cache. No sync commands, no admin forms — drop a `.md` file and it's live.

### Briefing

User-authored instructions for the coordinator agent. Free markdown that includes what to do (steps), when to run, and any domain-specific context. The coordinator must follow the steps but has freedom to make supporting decisions not covered by the briefing.

| Field             | Type           | Notes                                                                       |
| ----------------- | -------------- | --------------------------------------------------------------------------- |
| owner             | FK→User        |                                                                             |
| title             | CharField(200) |                                                                             |
| content           | TextField      | Markdown — steps, context, constraints for the coordinator                  |
| coordinator_model | CharField(50)  | Strong model for the coordinator (e.g. 'claude-opus-4-6', 'gemini-2.5-pro') |
| is_active         | BooleanField   |                                                                             |
| schedule_text     | CharField(200) | Free text for scheduling ("every weekday at 8am", "on request only")        |
| created_at        | DateTimeField  | auto_now_add                                                                |
| updated_at        | DateTimeField  | auto_now                                                                    |

### Agent (filesystem — `agents/*.md`)

Not a Django model. Defined as `.md` files with YAML frontmatter + prompt body. Parsed by `core/registry.py` using `yaml.safe_load`.

```
---
name: market-researcher
description: Searches the web for market intelligence and extracts actionable insights.
model: gpt-5.4
tools: ["web_search", "web_scrape", "bash"]
memory: true
volumes:
  - host: "./in_and_out/market_research"
    mount: "/data/market_research"
    mode: "rw"
---

You are a market research specialist...
```

Fields from frontmatter: `name`, `description`, `model` (→ `model_choice`), `tools` (YAML list), `memory` (boolean, default false), `volumes` (optional list of host/mount/mode objects). Body after `---` is the prompt. Validated against `FUZZYCLAW_MODELS` and `FUZZYCLAW_TOOLS` registries.

**Skills are universal** — every agent gets access to all skills in `skills/`. Skills are knowledge (documented workflows), not specialization. The agent decides which skill to follow based on its task. Tools are the specialization lever.

### Skill (filesystem — `skills/*/SKILL.md`)

Not a Django model. A directory with `SKILL.md` (frontmatter + docs) + optional subdirectories. Parsed by `core/registry.py`. Skills can optionally declare Python dependencies via `requirements.txt` (next to `SKILL.md`):

```
skills/
├── web-scraping/
│   ├── SKILL.md             ← skill definition
│   ├── requirements.txt     ← skill deps (beautifulsoup4, requests, etc.)
│   └── scripts/
│       └── scraper.py
└── checklist-validation/
    └── SKILL.md             ← no deps needed
```

Skill deps are installed at image build time (all skills for all agents), not at runtime.

### AgentImage

Tracks pre-built Docker images for specialist agents. Managed by `sync_images` command.

| Field       | Type           | Notes                                       |
| ----------- | -------------- | ------------------------------------------- |
| agent_name  | CharField(100) | Unique, matches agent name from frontmatter |
| file_hash   | CharField(64)  | SHA-256 of agent .md + skill deps           |
| image_tag   | CharField(200) | e.g. `fuzzyclaw-agent-summarizer:latest`    |
| built_at    | DateTimeField  | auto_now                                    |
| build_error | TextField      | blank if build succeeded                    |

### Run

Execution record for a briefing. One briefing → many runs.

| Field              | Type          | Notes                                                               |
| ------------------ | ------------- | ------------------------------------------------------------------- |
| briefing           | FK→Briefing   |                                                                     |
| status             | CharField(20) | pending / running / completed / failed                              |
| started_at         | DateTimeField | nullable                                                            |
| completed_at       | DateTimeField | nullable                                                            |
| coordinator_report | TextField     | The coordinator's final synthesis after all specialists report back |
| error_message      | TextField     | blank                                                               |
| triggered_by       | CharField(20) | manual / scheduled                                                  |
| created_at         | DateTimeField | auto_now_add                                                        |

### AgentRun

A specialist agent's participation in a run. One Run → many AgentRuns. Tracks which container ran, what it reported.

| Field         | Type           | Notes                                                 |
| ------------- | -------------- | ----------------------------------------------------- |
| run           | FK→Run         |                                                       |
| agent_name    | CharField(100) | Name matching filename in `agents/` dir               |
| status        | CharField(20)  | pending / running / completed / failed                |
| container_id  | CharField(100) | Docker container ID, blank until launched             |
| started_at    | DateTimeField  | nullable                                              |
| completed_at  | DateTimeField  | nullable                                              |
| report        | TextField      | What this specialist reported back                    |
| raw_data      | JSONField      | Structured data from the specialist (domain-specific) |
| error_message | TextField      | blank                                                 |
| created_at    | DateTimeField  | auto_now_add                                          |

### Relationships

```
User ──1:N──> Briefing ──1:N──> Run ──1:N──> AgentRun
                                                │
                              agents/*.md ◄─────┘ (by agent_name)
```

## Security Model: Container Privilege Hierarchy

```
User → Django → Coordinator (host-side) → Specialist containers
 (full)  (platform)  (orchestrate, Celery)    (execute, isolated)
```

**Coordinator** (runs in Celery worker, NOT in a container):

- Has Docker socket access — launches specialist containers via Docker SDK
- Has Django ORM access — reads briefings, writes to Run and AgentRun tables
- Tools: `list_available_agents`, `dispatch_specialist`, `check_reports`, `read_report`, `submit_coordinator_report`, `manage_schedule`
- No bash, no filesystem tools — coordinator orchestrates, it doesn't execute

**Specialist containers** (one per dispatched agent):

- NO Docker socket — cannot launch other containers
- NO Django ORM — no access to Django tables
- DB access only if `memory: true` — limited to PostgresStore (LangGraph persistent memory), namespaced per agent name
- Skills directory mounted read-only at `/app/skills`
- Communication via shared volume: writes `report.json` to `/app/comms`, read host-side by dispatcher
- Only the needed LLM API key passed (based on agent's model provider)
- Resource limits enforced: `FUZZYCLAW_AGENT_MEM_LIMIT` (512m), `FUZZYCLAW_AGENT_CPU_LIMIT` (0.5 cores)
- Concurrency limit: `FUZZYCLAW_MAX_CONTAINERS` (10) prevents resource exhaustion
- Container exits after writing report — dispatcher reads, writes to DB, removes container

## Container Dispatch Flow

```
Coordinator (in Celery worker, host-side)
    │
    ├─ calls dispatch_specialist("summarizer", task, run_id)
    │
    ▼
dispatch_specialist() [host-side, has Django ORM]
    │
    ├─ create AgentRun(status='running')
    ├─ mkdir comms/{agent_run_id}/
    ├─ docker run fuzzyclaw-agent-summarizer:latest
    │       │
    │       │  env: TASK_DESCRIPTION, AGENT_FILE, LLM API key
    │       │  env (if memory: true): DATABASE_URL (for PostgresStore only)
    │       │  mount: skills/ (read-only), comms/{agent_run_id}/ (read-write)
    │       │  NO: Docker socket
    │       │
    │       ├─ agent_runner.py starts
    │       ├─ parse agent.md → Deep Agent
    │       ├─ if memory: true → connect to PostgresStore (namespaced by agent name)
    │       ├─ execute task
    │       ├─ write report.json to /app/comms/
    │       └─ exit 0 (or exit 1 on failure)
    │
    ├─ container.wait() — blocks until done (with timeout)
    ├─ read comms/{agent_run_id}/report.json
    ├─ write to AgentRun: status, report, container_id, timestamps
    ├─ container.remove(), cleanup comms dir
    └─ return report to coordinator
```

## Component Specs

### Direct Agent Dispatch (planned)

- **Contract:** Dashboard area to fire individual agents one-off without a coordinator/briefing. User picks an agent, types a task, gets a report back.
- **Status:** planned
- **Why:** Two modes of work: (1) **organized** — briefings that coordinate multiple agents, run on schedule, produce synthesis reports; (2) **spontaneous** — talk to a specific agent directly for quick one-off tasks. Like the difference between a scheduled meeting and walking over to someone's desk.
- **Design notes:**
  - New dashboard section: "Quick Dispatch" or similar
  - UI: agent picker dropdown + task textarea + "Go" button
  - Creates an AgentRun (no Run, no Briefing) — or a lightweight "ad-hoc Run" with no coordinator
  - Same container launch path (`launch_agent_container`), same volume mounts, same everything
  - Report displayed inline (HTMX polling like run detail)
  - Good candidate for the personal assistant agent (Documents access + Gmail)

- **Personal agent ideas:**
  - Agent with volume mount to `~/Documents` (read-only or read-write)
  - Gmail tool (port from nanoclaw's email-reading agent on desktop)
  - Only runs on local laptop — not a production/shared concern
  - Perfect use case for direct dispatch: "check my emails for X", "find the PDF about Y"

### Briefing Scheduling (implemented)

- **Contract:** Users write a natural language schedule in `schedule_text` (e.g. "every weekday at 9am"), click the "Schedule" button, and the system parses it into a Celery Beat `PeriodicTask`. Briefings then fire automatically on schedule.
- **Status:** implemented (`core/scheduling.py`)
- **Why:** Automated scheduling alongside the existing manual "run now" capability.
- **Design notes:**

  **Flow (implemented — differs from original plan):**
  1. User writes `schedule_text` in briefing detail page
  2. User clicks **"Schedule"** button (HTMX POST, also saves schedule_text and coordinator_model)
  3. Django view calls `sync_schedule()` which makes one cheap LLM call (Gemini Flash) to parse NL → cron
  4. `PeriodicTask` created/updated via `django-celery-beat` ORM

  **Key deviation from original plan:** We do NOT use a coordinator run to parse schedules. A direct LLM call from Django is cheaper (fractions of a cent), faster (1-3 seconds), and doesn't pollute run history. The coordinator has a `manage_schedule` tool for programmatic use (e.g. "if market changed, switch to daily"), but the UI uses the direct path.

  **`sync_schedule()` logic:**
  - Idempotency key: `PeriodicTask.name = f"briefing-{briefing_id}"`
  - `schedule_text` blank → delete PeriodicTask + orphaned CrontabSchedule
  - `is_active` False → pause (set `enabled=False`), preserve cron for resume
  - `is_active` True + `schedule_text` unchanged → re-enable without LLM call
  - `is_active` True + `schedule_text` changed → parse via LLM, create/update
  - The `PeriodicTask` calls `launch_briefing_scheduled(briefing_id)` which creates a Run with `triggered_by='scheduled'` and calls `launch_coordinator`
  - Concurrent run guard: skips if a run is already pending/running for this briefing

  **Timezone:** `CELERY_TIMEZONE` (default `Europe/Berlin`) drives both the LLM prompt and Celery Beat. If the user specifies a different timezone in the schedule text, the LLM converts to the system timezone.

  **UI:** HTMX Schedule button with loading spinner, schedule status partial showing active/paused/stale state. The `is_active` toggle pauses/resumes the schedule. Briefing `post_delete` signal cleans up the PeriodicTask.

## Resolved Decisions

| Question                 | Decision                                                                                                                                                                                                                                                              |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Agent framework          | Deep Agents (LangChain/LangGraph) — replaces nanoclaw. Python-native, multi-provider LLM, Django ORM access.                                                                                                                                                          |
| Container isolation      | One container per launched agent (coordinator + each specialist). Keep nanoclaw's best pattern.                                                                                                                                                                       |
| LLM providers            | Multi-provider via LangChain: Claude (Opus/Sonnet), GPT-5/GPT-5-mini (OpenAI), Gemini (Google). Configurable per agent.                                                                                                                                               |
| Agent definition format  | `.md` files in `agents/` dir — YAML frontmatter (name, description, model, tools, memory, volumes) + prompt body. Filesystem is source of truth, read at runtime by `core/registry.py`.                                                                               |
| Skill format             | Directory with `SKILL.md` in `skills/` dir. Filesystem only, read at runtime by `core/registry.py`.                                                                                                                                                                   |
| Tools                    | YAML list in agent frontmatter. Python code in `agent_tools/` (container-side) and `core/agent_tools.py` (coordinator-side). Validated against `FUZZYCLAW_TOOLS` registry.                                                                                             |
| Briefing → agent mapping | Briefing specifies steps; coordinator decides which specialists to dispatch based on briefing content + available agents.                                                                                                                                             |
| Reports                  | Two levels: specialist reports (AgentRun.report) + coordinator synthesis (Run.coordinator_report).                                                                                                                                                                    |
| Persistent memory        | Per-briefing scope. Agent memory namespaced by briefing ID in PostgresStore — prevents cross-contamination between different use cases.                                                                                                                               |
| Coordinator launch       | Django triggers coordinator container (Docker SDK) via Celery task. "Run Now" button → `launch_coordinator.delay(run_id)` → returns immediately.                                                                                                                      |
| Specialist launch        | Coordinator dispatches by agent name. Admin-triggered `sync_images` pre-builds Docker images. Dispatch = `docker run` against pre-built image (no build on critical path). No in-process fallback.                                                                    |
| Container image strategy | Two-layer: shared base image (all agent deps, ~437MB, built once) + per-agent thin layer (just `COPY agent.md` + skill deps, sub-second build). Editing an agent `.md` → `sync_images` only rebuilds the thin layer.                                                  |
| Container communication  | Shared volume (`comms/{agent_run_id}/`). Agent writes `report.json`, dispatcher reads host-side. Not network sockets, not DB writes. Clean separation.                                                                                                                |
| Coordinator placement    | Coordinator runs host-side in Celery worker (has Django ORM). NOT in a container. Only specialists are containerized.                                                                                                                                                 |
| Skill + agent management | Filesystem only. Drop `.md` files in `agents/`, skill dirs in `skills/`. Dashboard shows read-only view. No admin forms.                                                                                                                                              |
| Image lifecycle          | Background Celery task watches `agents/` dir, tracks file hashes, builds/rebuilds/deletes images. Decouples slow image builds from fast agent dispatch.                                                                                                               |
| API auth                 | Django token auth for the agent runtime                                                                                                                                                                                                                               |
| WhatsApp                 | Defer, but keep infra available                                                                                                                                                                                                                                       |
| Scheduling               | Celery Beat with `django-celery-beat` (DatabaseScheduler). Direct LLM call (Gemini Flash) parses NL schedule → cron. Coordinator also has `manage_schedule` tool for programmatic schedule changes. No cron on host.                                                  |
| `manage_schedule` tool   | Coordinator tool (not skill). Wraps `sync_schedule()` to create/update/delete Celery Beat `PeriodicTask` entries. Allows coordinators to adapt briefing frequency based on agent findings.                                                                             |
| Scraping method          | ScrapingBee API (existing account) — available as a tool/skill, not baked into the platform                                                                                                                                                                           |
| Frontend                 | Django + HTMX + Tailwind CDN. No separate JS frontend.                                                                                                                                                                                                                |

### Superseded Decisions

| Original Decision           | Replaced By                                        | Why                                                                                                                                              |
| --------------------------- | -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| Fork nanoclaw (TypeScript)  | Deep Agents (Python)                               | Nanoclaw's Claude SDK locks to one provider. Deep Agents gives multi-provider + Python-native + Django ORM access.                               |
| Target model (URL-centric)  | Agent model (general-purpose)                      | URL monitoring is just one use case. Platform must be domain-agnostic.                                                                           |
| ScanResult (per-finding)    | AgentRun (per-specialist) + Run.coordinator_report | Results are reports, not atomic findings. Domain-specific structure lives in raw_data JSON.                                                      |
| Pydantic shared package     | Deep Agents handles validation                     | Validation now happens within the agent runtime, not as a separate shared schema layer.                                                          |
| Agent/Skill in DB           | Filesystem registry (`core/registry.py`)           | Managing agents via admin forms doesn't scale. Drop 100 `.md` files and they just work — no sync, no forms.                                      |
| `sync_agents`/`sync_skills` | `check_agents`/`check_skills` (validation only)    | No DB to sync to. Commands now validate files and exit with error code if invalid.                                                               |
| Image build via Celery task | `manage.py sync_images` (admin-triggered)          | No periodic scan. The person editing agents works at code level — they trigger builds when ready. Users only see briefings and available agents. |
| In-process specialist run   | Container-only dispatch                            | Security: `bash` tool in Celery worker = shared environment. Containers isolate agents by default. No feature flag.                              |
| `AgentRun.agent` FK         | `AgentRun.agent_name` CharField                    | No Agent model to FK to. Name string is sufficient — matches filename in `agents/` dir.                                                          |
| `monitoring` app            | `core` app                                         | Name was too domain-specific. Platform is domain-agnostic.                                                                                       |

## Open Questions

(None currently — all resolved below)

## Graffiti Wall

Done:
- ~~Briefing scheduling~~ — implemented via NL-to-cron with LLM parsing
- ~~LangSmith visibility for sub-agents~~ — LANGSMITH_ENDPOINT passed to containers
- ~~Skills path bug~~ — fixed /app/app/skills → /skills virtual path
- ~~Universal skills~~ — all agents see all skills, removed per-agent skills config
- ~~YAML frontmatter parser~~ — replaced custom line parser with yaml.safe_load
- ~~DRY templates~~ — extracted 5 reusable partials (toggle, model_select, status_badge, empty_state, stat_card)
- ~~HTMX active toggle~~ — instant save without form submit
- ~~djLint~~ — formatter for Django templates, no more Prettier mangling

Still to do:
- Stop button (cancel a running run from the UI)
- WhatsApp channel (notifications + commands)
- Direct agent dispatch (talk to a specific agent without coordinator/briefing)
- Login / logout page styling
