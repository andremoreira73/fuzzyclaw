---
name: guest-agent
description: Entry point for any external coding agent (Claude Code, Codex, Gemini CLI, etc.) working inside this FuzzyClaw instance. Read this skill first when starting a session in this project directory. It explains how to register your work as a Run so it appears on the FuzzyClaw dashboard, and how to discover and use other available skills. Use this skill whenever you need to log completed work, understand the FuzzyClaw data model, or find out what skills are available for a task.
---

# Guest Agent

You are a **guest agent** — an external coding agent (Claude Code, Codex, Gemini CLI, etc.) working interactively with the user inside a FuzzyClaw instance. You are not one of FuzzyClaw's orchestrated agents that run in Docker containers. Instead, you work directly with the user and register your results in FuzzyClaw's database so everything is visible from one dashboard.

This skill covers two things: how to register your work, and how to discover other skills you can use.

## Why this matters

FuzzyClaw is the user's central control panel for all agent work. Some runs are orchestrated by FuzzyClaw's coordinator (automated, scheduled). Others — like the work you do interactively — happen outside that loop. By registering your work, the user gets a single timeline of everything that was done, by whom, and when. Without registration, your work is invisible to the dashboard and the user loses track of it.

## How FuzzyClaw organizes work

Three concepts matter:

| Concept      | What it is                                                   | Example                                                     |
| ------------ | ------------------------------------------------------------ | ----------------------------------------------------------- |
| **Briefing** | A category or project that groups related runs               | "Expenses Processing"                                       |
| **Run**      | One execution under a briefing — a session's worth of work   | "Processed April invoices"                                  |
| **AgentRun** | Your participation in that run — your name, report, and data | agent: `claude-code`, report: "5 invoices, total EUR 2,340" |

A Briefing has many Runs. A Run has one or more AgentRuns. When you register work, you create a Run and an AgentRun under a Briefing.

## Registering your work

When you finish a piece of work that should be tracked, call the `register_run` management command. This creates a completed Run and AgentRun in the database.

### The command

```bash
docker compose exec web python manage.py register_run \
  --briefing "Briefing Title" \
  --agent your-agent-name \
  --report "What you did and what the results were" \
  --raw-data '{"key": "value"}'
```

Run this from the project root (`/home/memology/Documents/fuzzy-lyfx`).

### Arguments

| Argument               | Required | Description                                                                                                                                 |
| ---------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `--briefing`           | Yes      | Briefing title. Auto-created if it doesn't exist yet.                                                                                       |
| `--agent`              | Yes      | Your agent name (e.g. `claude-code`, `codex`, `gemini-cli`). Use a consistent name across sessions so the dashboard can track your history. |
| `--report`             | No       | Inline report text. For longer reports, use `--report-file` instead.                                                                        |
| `--report-file`        | No       | Path to a file containing the report (useful for multi-paragraph reports).                                                                  |
| `--coordinator-report` | No       | A short summary for the run level (shown in the run list). If omitted, the dashboard shows the agent report.                                |
| `--raw-data`           | No       | JSON string with structured data. Use this for machine-readable results (totals, counts, file lists, etc.).                                 |
| `--user-notes`         | No       | Notes from the user (not from you).                                                                                                         |
| `--status`             | No       | `completed` (default) or `failed`.                                                                                                          |

### Examples

Simple registration:

```bash
docker compose exec web python manage.py register_run \
  --briefing "Expenses Processing" \
  --agent claude-code \
  --report "Processed 5 invoices from inbox. Total: EUR 2,340.50. All moved to processed/."
```

With structured data:

```bash
docker compose exec web python manage.py register_run \
  --briefing "Expenses Processing" \
  --agent claude-code \
  --report "Processed March invoices. 2 flagged for review (missing category)." \
  --raw-data '{"invoice_count": 8, "total_eur": 4521.30, "flagged": 2, "month": "2026-03"}'
```

From a report file:

```bash
docker compose exec web python manage.py register_run \
  --briefing "Market Research" \
  --agent codex \
  --report-file /tmp/research_report.md \
  --coordinator-report "Competitor analysis for Q2 complete."
```

### When to register

Register at the **end** of a work session, once you have results worth tracking. Not every conversation needs a run — only register when you produced something meaningful (processed files, generated reports, completed a defined task). Quick questions, debugging, or exploratory conversations don't need registration.

### Writing good reports

The report field is what the user sees on the dashboard. Write it as a concise summary of what you did and what the outcome was. Think of it as a commit message for your work session:

- What was the task
- What did you do
- What was the result (counts, totals, files created/modified)
- Anything that needs follow-up

Use the `raw_data` field for structured data that might be useful for filtering or aggregation later (amounts, dates, counts, categories).

## Discovering skills

FuzzyClaw skills live in the `skills/` directory of the FuzzyClaw folder. Each skill is a folder with a `SKILL.md` file that explains a specific workflow or domain.

To see what's available:

```bash
ls skills/
```

To read a skill:

```bash
cat skills/<skill-name>/SKILL.md
```

Read the relevant skill before starting work. For example, if the user asks you to process expenses, read `skills/expenses/SKILL.md` first — it contains the rules, file locations, CSV structure, and vendor matching logic you need.

Skills may also contain subdirectories with reference data, templates, or scripts. Check the skill's SKILL.md for pointers to these resources.

## Data directory

User-specific data lives under `data/users/<user_id>/`. Skills that work with user data store their files there. Check the relevant skill for exact file paths.
