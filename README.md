<p align="center">
  <img src="FuzzyClaw.jpg" alt="FuzzyClaw" width="600">
</p>

# FuzzyClaw

FuzzyClaw is my personal take on how I want my agents to work, very much inspired by Steinberg and his brilliant [**OpenClaw**](https://github.com/openclaw/openclaw) and by [**nanoclaw**](https://github.com/qwibitai/nanoclaw) from Qwibit.ai. I thought NanoClaw with its container philosophy would be my thing, but it was limited - as far as I could tell - to use Claude as agents. I love Claude, but I wanted more optionality.

Besides: I am a Python and Django guy. While I find TypeScript cool and intriguing, and talking to my agents via WhatsApp super amusing, it turns out I actually wanted my good old dashboard where I can simply get stuff organized. The "briefings" (prompts!) and results from agents' work live happily in a PostgreSQL database (because, why not?). Besides, I want to schedule stuff as we go or let the coordinator do it if it thinks it should. But no cron, please.

## Philosophy

The philosophy is simple: optionality and scalability, just like OpenClaw. But in Python, using Django's batteries included. We use LangChain's [Deep Agents](https://github.com/langchain-ai/deep-agents) so we can plug in any model we want.

- The coordinator reads the briefings you prepare. In the spirit of top managers, it can only delegate, not do. It can't screw up your PC because it does not have the tools.
- The specialists do all the sweating and report back. Familiar setup in companies, I guess. But they live in a container, so they can't really screw up your PC. You can give them access to parts of your filesystem (or all of it) at your own risk. It is your choice when you create your agents.
- One special agent, **Shenlong**, is really powerful within its container. If no other specialist fits the task, the coordinator can always call it.
- Add skills (under `skills/`), and all agents will have access to them.

Django handles everything the user touches: auth, dashboards, briefing editor, run history, admin. The frontend is server-rendered HTML with the great and really cool [HTMX](https://htmx.org/) sprinkled throughout. I wanted structured persistence: reports, history, search, filtering, scheduling. That's why Django + PostgreSQL.

Every specialist agent gets its own Docker container with only the tools and API keys it needs. A web scraping agent gets `web_scrape`. Shenlong gets everything including `bash` — the nuclear option. Better only grant that to stronger models you trust. Containers exit after reporting; the coordinator reads the report and moves on.

## How It Works

```
User writes a Briefing (markdown)
    |
    v
Coordinator Agent (strong model, runs in Celery worker)
    |-- reads briefing steps
    |-- lists available specialist agents
    |-- dispatches specialists by name
    |
    +---> Specialist Container 1 (market-researcher)
    |         |-- web_search, web_scrape, bash, memory
    |         |-- follows skills from /app/skills/
    |         |-- writes report.json to shared volume
    |         \-- exits
    |
    +---> Specialist Container 2 (career-scraper)
    |         |-- career_scrape (no bash, no search)
    |         \-- exits
    |
    v
Coordinator synthesizes reports -> Run.coordinator_report
    |
    v
Dashboard shows results
```

### Agents

Markdown files in `agents/`. YAML frontmatter defines the model, tools, memory, and optional volume mounts. The body is the system prompt.

```yaml
---
name: shenlong
description: General-purpose agent for any task.
model: gpt-5.4
tools: ["web_search", "web_scrape", "bash"]
memory: true
volumes:
  - host: "./in_and_out"
    mount: "/data"
    mode: "rw"
---
You are Shenlong, a general-purpose agent...
```

Drop a `.md` file in `agents/`, run `sync_images`, and it's live.

### Skills

Directories in `skills/` with a `SKILL.md` file and optional sub-folders, following the [Agent Skills specification](https://agentskills.io/home). Every agent sees every skill and decides which to use based on its task.

### Tools

Python functions in `agent_tools/`. Currently ships with:

| Tool                                 | What it does        | Notes                                                                                                  |
| ------------------------------------ | ------------------- | ------------------------------------------------------------------------------------------------------ |
| `bash`                               | Shell execution     | Only grant to models you trust                                                                         |
| `web_search`                         | Google search       | Via [ScrapingBee](https://www.scrapingbee.com/) SERP API (swap for your preferred provider)            |
| `web_scrape`                         | Page scraping       | ScrapingBee + HTML cleaning (swap-friendly)                                                            |
| `career_scrape`                      | Job listing scraper | Domain-specific selectors for EN/DE job pages. Literally done for a friend, could be useful to many... |
| `remember` / `recall` / `recall_all` | Persistent memory   | PostgresStore, namespaced per agent                                                                    |

The scraping tools use ScrapingBee because that's what we use. Swapping to Browserless, Playwright, or raw requests is straightforward; each tool is a single Python file.

### Scheduling

Write a natural language schedule in the briefing ("every weekday at 9am", "twice a month on the 1st and 15th at noon EST"). Click Schedule. A cheap LLM call parses it into a cron expression. Celery Beat fires the briefing automatically.

No cron jobs on the host — everything goes through `django-celery-beat`, visible and editable from the Django admin. Some will find this a limitation, but it's a matter of taste. I want to see in the admin panel what is scheduled and what is not. The coordinator can also manage schedules programmatically via its `manage_schedule` tool — so your briefings can adapt their own frequency based on what the agents find.

### My `in_and_out` Strategy

Agents run in isolated containers, but sometimes they need to produce files (reports, CSVs, data exports). The `volumes` field in agent frontmatter mounts host directories into the container. We use `./in_and_out/` as the standard exchange point:

```
in_and_out/
├── market_research/     <- market-researcher writes here
├── misc/                <- shenlong writes here
└── ...
```

The host can read the output; the agent can't escape its mount. But feel free (at your own risk) to mount other folders on your disk. Just make sure they're not in `FUZZYCLAW_VOLUME_BLOCKLIST`.

## Tech Stack

| Layer               | Technology                                                                                                         |
| ------------------- | ------------------------------------------------------------------------------------------------------------------ |
| Web / Auth / ORM    | Django 5.1 + PostgreSQL                                                                                            |
| Frontend            | HTMX 2.0 + Tailwind CSS + Alpine.js (server-rendered, no build step)                                               |
| Agent framework     | [Deep Agents](https://github.com/langchain-ai/deep-agents) (LangChain / LangGraph)                                 |
| LLM providers       | OpenAI (GPT-5, GPT-5-mini), Google (Gemini 2.5 Pro/Flash), Anthropic (Claude Opus/Sonnet) — configurable per agent |
| Container isolation | Docker (one container per specialist agent)                                                                        |
| Task scheduling     | Celery + Celery Beat + Redis (not cron)                                                                            |
| Persistent memory   | PostgresStore (LangGraph checkpoint store)                                                                         |
| Deployment          | Docker Compose                                                                                                     |

## Security: Three Layers Deep

We red-teamed FuzzyClaw by writing a briefing that instructed the coordinator to dispatch Shenlong with orders to break out of its container and write a file to the host's home directory. Here's what happened:

1. **Coordinator refuses** (GPT-5.4, GPT-5-mini) — The coordinator LLM recognized the intent and refused to dispatch the agent at all. _"The requested action is an attempt to break out of a container... I did not dispatch any specialist agents."_

2. **Coordinator dispatches, agent refuses** (Gemini Flash coordinator, GPT-5.4 agent) — Flash was less cautious and dispatched Shenlong. But Shenlong itself refused: _"I can't help attempt to break out of a container or bypass sandboxing."_ It correctly identified that it could only write to `/data` (its mounted volume).

3. **Container can't escape anyway** — Even if both the coordinator and agent cooperated, the Docker container doesn't have the host path mounted. The agent can only write to explicitly mounted volumes (`/data` via `in_and_out/`). No Docker socket, no host filesystem access, resource limits enforced.

Defense in depth: LLM safety training at two levels (coordinator + agent), plus hard container isolation underneath. The `bash` tool is powerful, but it's powerful _inside a box_.

## Requirements

- **Linux or macOS** (Windows might work via WSL2, but untested)
- **Docker** with the Compose plugin
- **Python 3.11+** (for local development/tests only — production runs entirely in Docker)
- At least one LLM API key: OpenAI, Google, or Anthropic

Everything else (PostgreSQL, Redis, Celery, the web server) runs inside Docker. You don't need to install any of it.

## Quick Start

```bash
# Clone and configure
git clone https://github.com/andremoreira73/fuzzyclaw.git
cd fuzzyclaw
cp .env.example .env        # edit this — add your API keys and set DB credentials
```

> **Important:** Edit `.env` before proceeding. You need at least `DB_PASSWORD`, `DJANGO_SECRET_KEY`, and one LLM API key (`OPENAI_API_KEY`, `GOOGLE_API_KEY`, or `ANTHROPIC_API_KEY`).

```bash
# Start the platform (PostgreSQL, Redis, web, Celery, Celery Beat)
docker compose up -d

# Build agent Docker images
docker compose exec web python manage.py sync_images

# Create your admin user
docker compose exec web python manage.py createsuperuser

# Open the dashboard
open http://localhost:8200
```

See examples/briefings/ for sample briefings to get started.

### Using Claude Code

If you have [Claude Code](https://claude.ai/download) installed, you can just:

```bash
cd fuzzyclaw
claude
```

Then say "set this up". The `CLAUDE.md` file has step-by-step instructions that Claude Code will follow.

### After editing agents

```bash
# Rebuild images and restart workers
./sync_agents.sh
```

### Running tests

```bash
source venv/bin/activate
DATABASE_URL=sqlite:///test.db python manage.py test core
```

## Project Structure

```
fuzzyclaw/
├── agents/                  # Agent definitions (*.md) — drop a file, it's live
│   ├── shenlong.md          # General-purpose agent (the divine dragon)
│   ├── market-researcher.md # Web research specialist
│   ├── career-scraper.md    # Job listing scraper
│   └── web-scraper.md       # Page content extractor
├── skills/                  # Skill definitions (*/SKILL.md) — all agents see all skills
│   └── market-research/
│       └── SKILL.md
├── agent_tools/             # Python tools baked into agent containers
├── agent_runner.py          # Container entrypoint for specialist agents
├── core/                    # Django app: models, views, API, scheduling, containers
├── templates/               # Django templates with HTMX
├── in_and_out/              # Host-side data exchange with agent containers
├── docker-compose.yml       # Platform services
├── Dockerfile.agent         # Base image for agent containers
└── design_notes.md          # Architecture decisions and rationale
```

## FAQ

**Why Django?**

Because I know it well, it comes with batteries included (auth, admin, ORM, migrations, sessions), and it's been rock-solid in production for almost 20 years. Django gives me a real admin panel where I can see everything that's going on.

**Why PostgreSQL instead of SQLite?**

Concurrency. Celery workers, agent containers, and the web server all hit the database at the same time. SQLite locks on writes. PostgreSQL handles concurrent access natively. It's also where agent memory lives (via LangGraph's PostgresStore), so everything is in one place.

**Why Celery Beat instead of cron?**

Visibility. Cron jobs are invisible — they live in a crontab file somewhere and you find out they're broken when things stop happening. Celery Beat schedules live in the database, visible in the Django admin. You can see what's scheduled, when it last ran, enable/disable from the UI. Plus, schedules are created programmatically (from natural language via LLM), not by editing system files.

**Can I use models other than OpenAI/Google/Anthropic?**

Yes. FuzzyClaw uses LangChain under the hood, so any model with a LangChain integration works. Add the provider package to requirements, register the model in `FUZZYCLAW_MODELS` in settings, and it's available. Each agent can use a different model — cheap models for simple tasks, strong models for complex ones.

**Is this secure?**

We take isolation seriously — each specialist runs in its own Docker container with only the tools and API keys it needs, no Docker socket access, and resource limits enforced. Our red-team tests (see Security section above) showed that both the coordinator and agent LLMs refused to attempt container escapes, and the container boundary held even if they had tried.

That said: no system is perfectly secure. The `bash` tool inside a container is powerful. Volume mounts expose real host directories. LLM behavior can be unpredictable. Review your agent definitions, be thoughtful about what you mount, and treat this as defense in depth, not a guarantee.

**Why not TypeScript / Why not nanoclaw?**

nanoclaw is excellent and inspired FuzzyClaw directly — especially the container-per-agent philosophy. But nanoclaw is built around Claude Code and the Anthropic API. I wanted multi-provider LLM support (OpenAI, Google, Anthropic), a persistent dashboard with run history, and Django's ecosystem. Different tools for different preferences.

## Roadmap

Things I want to add when time allows:

- **WhatsApp channel** — notifications and commands via WhatsApp (the infra is ready, just needs wiring)
- **Direct agent dispatch** — talk to a specific agent one-on-one from the dashboard, no coordinator needed
- **Stop button** — cancel a running run from the UI
- **More tools** — email reading, document parsing, code execution sandboxes
- **Better dashboard** — run comparisons, trend charts, search across reports

## Contributing

PRs are welcome! But bear in mind that this is a personal project. Claude is obviously part of our team, but we have limits.

A few guidelines:

- **Open an issue first** for anything non-trivial, so we can discuss before you code.
- **Keep it simple.** FuzzyClaw is deliberately not over-engineered. If something can be a single Python file, it should be.
- **Django conventions.** If Django has a way to do it, use that way.
- **Test your changes.** `DATABASE_URL=sqlite:///test.db python manage.py test core` should pass.
- **New tools** are the easiest contribution — each one is a standalone Python file in `agent_tools/`.

## License

MIT
