# CLAUDE.md

> **Guest agent?** Read `skills/guest-agent/SKILL.md` first — it explains how to register work and discover skills.

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

FuzzyClaw is a domain-agnostic agent orchestration platform. Users write markdown briefings (instructions + schedule), a coordinator agent interprets them and dispatches specialist agents — each running in its own Docker container — to execute the work. Results flow back through PostgreSQL and are browsable via a Django dashboard. Django is the management and interaction plane; Deep Agents (LangChain/LangGraph) is the execution plane.

Not a single-purpose tool. The same FuzzyClaw instance can run web monitoring, checklist audits, or anything else — differentiated only by its briefings, agents, and skills.

## Architecture

**Django (Python)** — Management plane: web UI, auth, ORM, admin, REST API
- PostgreSQL ORM for briefings, runs, agent reports
- Agents and skills live on the filesystem (`agents/*.md`, `skills/*/SKILL.md`) — read at runtime by `core/registry.py`
- Dashboard with HTMX + Tailwind CSS (no separate JS frontend)
- REST API: read-only for agents/skills, full CRUD for briefings/runs/agent-runs
- Admin for briefings and execution history only

**Deep Agents (Python)** — Execution plane: coordinator + specialist agents
- Coordinator agent: strong model (Claude Opus / Gemini 2.5 Pro) reads briefings, dispatches specialists by name
- Specialist agents: configurable models (GPT-5-mini / Gemini Flash / etc.) with tools and access to all skills
- One Docker container per launched agent (security + isolation)
- Persistent agent memory in PostgreSQL via PostgresStore

Both systems share the same PostgreSQL database.

## Key Design Principles

- **Django manages, agents execute**: No separate frontend app. Django handles web + config, Deep Agents handles work.
- **Domain-agnostic**: Not hardcoded for any domain. Briefings define what to do. Skills and agents are pluggable.
- **Natural language control plane**: Users write markdown briefings with steps; the coordinator interprets them.
- **Database is the product**: Value is structured persistence (reports, history, search, filtering), not just task execution.
- **Container per agent**: Every specialist runs in its own Docker container. Security by default.
- **LLM flexibility**: Model choice is configurable per agent. Multi-provider via LangChain.
- **Filesystem is source of truth for agents/skills**: Drop a `.md` file, it's live. No DB sync, no admin forms.
- **Skills are universal**: Every agent sees every skill. Skills are knowledge (documented workflows), not permissions. Tools are the specialization lever.
- **HTMX-first frontend**: Server-rendered HTML with HTMX for interactivity. No JS build step. Hypermedia, not SPA.

## Data Model

| What | Where | Purpose |
|------|-------|---------|
| Briefing | DB (Django model) | User-authored instructions + schedule for the coordinator |
| Run | DB (Django model) | Execution record for a briefing. Contains coordinator_report. |
| AgentRun | DB (Django model) | A specialist's participation in a run. Contains report + raw_data. Links to agent by `agent_name`. |
| Agent | Filesystem (`agents/*.md`) | Specialist definition: YAML frontmatter (name, model, tools, memory, volumes) + prompt body. |
| Skill | Filesystem (`skills/*/SKILL.md`) | Skill directory with docs + optional subdirectories. All agents see all skills. |

See `design_notes.md` for full schema details.

## Container Orchestration

Two-phase design: pre-build images, then fast dispatch.

```bash
docker compose up -d                                 # start platform (db, redis, web, celery, celery-beat)
docker compose exec web python manage.py sync_images # build agent Docker images (base + per-agent)
./sync_agents.sh                                     # shortcut: rebuild images + restart celery
./sync_agents.sh --force                             # force rebuild everything
```

- **Base image** (`fuzzyclaw-agent-base`) — shared deps, built once (~437MB)
- **Per-agent images** — thin layer: just `COPY agent.md` + skill deps (sub-second build)
- Agent containers communicate via shared volume (`comms/{agent_run_id}/report.json`)
- Coordinator runs host-side in Celery worker (ReAct agent via `create_agent`), NOT in a container
- Specialists run in isolated containers with `agent_runner.py` entrypoint

## Agent Definition Format

Agents are `.md` files in `agents/` with YAML frontmatter parsed by `yaml.safe_load`:

```yaml
---
name: market-researcher
description: Searches the web for market intelligence.
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

Fields: `name`, `description`, `model` (validated against `FUZZYCLAW_MODELS`), `tools` (validated against `FUZZYCLAW_TOOLS`), `memory` (boolean), `volumes` (optional list of host/mount/mode objects). Multi-line YAML is fully supported.

## Agent Tools (container-side)

Tools live in `agent_tools/` package (baked into base image, no Django):

| Tool | File | Description |
|------|------|-------------|
| `bash` | `bash.py` | Shell execution (only grant to trusted models) |
| `web_search` | `web_search.py` | Google search via ScrapingBee SERP API |
| `web_scrape` | `web_scrape.py` | Page scraping with HTML cleaning |
| `career_scrape` | `career_scrape.py` | Career page scraping with job-specific selectors |
| `remember/recall/recall_all` | `memory.py` | PostgresStore persistent memory |

## Scheduling

Briefings have a `schedule_text` field (natural language). Clicking "Schedule" on the briefing detail page triggers a cheap LLM call (Gemini Flash) that parses it into a cron expression and creates a `django-celery-beat` PeriodicTask. No cron jobs on the host.

The `is_active` toggle pauses/resumes the schedule. Deleting a briefing cleans up the PeriodicTask automatically.

## First-Time Setup (for Claude Code on a fresh machine)

If the user says "set this up" or "install FuzzyClaw", follow these steps in order:

### Prerequisites (verify first)
```bash
python3 --version    # need 3.11+
docker --version     # need Docker with compose plugin
docker compose version
```
If any are missing, tell the user what to install and stop.

### 1. Environment file
```bash
cp .env.example .env
```
Then tell the user: "Edit `.env` and add your API keys. You need at least one LLM provider (OPENAI_API_KEY, GOOGLE_API_KEY, or ANTHROPIC_API_KEY). Also set DB_PASSWORD, POSTGRES_DB, POSTGRES_USER, and DJANGO_SECRET_KEY. ScrapingBee and LangSmith keys are optional."

**Do NOT proceed until the user confirms `.env` is configured.** Never write API keys yourself.

### 2. Docker socket GID
The `web` and `celery` containers need access to the Docker socket to launch agent containers. Check the host's socket GID and set it in `.env`:
```bash
stat -c '%g' /var/run/docker.sock    # check the GID — set DOCKER_GID in .env to this value
```
The default is `983`. Common values: 999 (Ubuntu/Debian), 974 (Fedora). macOS Docker Desktop handles this automatically.

### 3. Start platform services
```bash
docker compose up -d
```
Wait for all services to be healthy. Verify:
```bash
docker compose ps    # all 5 services should be running: db, redis, web, celery, celery-beat
```

### 4. Run migrations (usually automatic, but verify)
```bash
docker compose exec web python manage.py migrate
```

### 5. Create superuser
```bash
docker compose exec web python manage.py createsuperuser
```
The user needs to enter username/password interactively. Tell them to run this command themselves with `!` prefix if needed.

### 6. Build agent Docker images
```bash
docker compose exec web python manage.py sync_images
```
This builds the base image (~437MB, takes a few minutes first time) and per-agent thin layers (sub-second each).

### 7. Verify
```bash
docker compose exec web python manage.py check_agents    # validates agent .md files
docker compose exec web python manage.py check_skills    # validates skill directories
```

Dashboard should be available at `http://localhost:8200`.

### After editing agents or skills
```bash
./sync_agents.sh          # rebuild changed images + restart celery
./sync_agents.sh --force  # force rebuild all images
```

### Running tests (local venv, not Docker)
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
DATABASE_URL=sqlite:///test.db python manage.py test core
```

## Build & Run (quick reference)

```bash
tailwindcss -i static/css/input.css -o static/css/tailwind.css --minify  # build CSS (once)
tailwindcss -i static/css/input.css -o static/css/tailwind.css --watch   # dev: rebuild on change
docker compose up -d                                      # start platform
docker compose exec web python manage.py sync_images      # build agent images
./sync_agents.sh                                          # after editing agents
docker compose logs celery --tail=50                      # check celery logs
docker compose exec web python manage.py createsuperuser  # create admin user
DATABASE_URL=sqlite:///test.db python manage.py test core  # run tests locally
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Web/Auth/ORM | Django 5.1 + PostgreSQL |
| Frontend | HTMX 2.0 + Tailwind CSS (local CLI build) + Alpine.js (in Django templates) |
| Agent orchestration | Deep Agents (LangChain/LangGraph) |
| Coordinator LLM | Claude Opus / Gemini 2.5 Pro |
| Specialist LLMs | GPT-5-mini / Gemini Flash / GPT-5.4 (configurable per agent) |
| Container isolation | Docker (one container per specialist agent) |
| Task scheduling | Celery + Celery Beat + Redis |
| Persistent memory | PostgresStore (LangGraph) in PostgreSQL |
| Deployment | Docker Compose |

## Important Conventions

- Use `docker compose` (no hyphen) — modern Docker syntax
- Place `from X import Y` statements at the top of .py files, not inline
- Never hardcode credentials — use env vars from `.env`
- Agent frontmatter uses proper YAML (parsed by `yaml.safe_load`)
- Templates use djLint-compatible formatting with named endblocks (`{% endblock content %}`)
- HTMX partials live in `templates/core/partials/` — reusable components with `{% include %}` + `with`
- The `in_and_out/` directory is the standard host-side data exchange point with agent containers
