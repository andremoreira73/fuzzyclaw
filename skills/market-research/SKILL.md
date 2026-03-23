---
name: market-research
description: Conduct structured market research using web search and scraping. Use when tasked with market analysis, competitive intelligence, industry trends, or business research.
---

# Market Research Skill

## Overview

This skill guides structured market research for strategic decisions. You run autonomously — there is no user interaction during execution. Your task description from the coordinator is your research brief.

## Workflow

### 1. Scope
- Parse the task description to identify: objective, market/segments, geography, time horizon, and specific research questions.
- If the task is vague, define reasonable scope based on what's stated and note your assumptions.
- Check `/data/market_research/<project-name>/` for any input documents provided by the user (e.g. briefs, prior reports, reference material). The project name will be specified in your task description. Use `bash` with `ls` and `cat` to read these files.

### 2. Data Collection
- Use `web_search` to find relevant sources (reports, articles, company data).
- Use `web_scrape` to extract detail from the most promising results.
- Work systematically: market structure first, then competitors, then trends.
- Track which sources you used and their quality tier (see methodology reference).

### 3. Analysis & Synthesis
- Analyze collected data against the research questions.
- Identify patterns, validate/invalidate hypotheses.
- Structure findings using the deliverable format from [references/methodology.md](references/methodology.md).

### 4. Report
- Return a structured report as your output. Follow the deliverable format: executive summary, market overview, segment analysis, trends, and sources.
- Every factual claim must have an inline citation with URL.
- Flag information gaps explicitly — do not fill them with speculation.
- Save the report as a markdown file to `/data/market_research/<project-name>/YYYY-MM-DD-report.md` using `bash`. This persists the deliverable alongside the input documents.

## Key References

- [references/methodology.md](references/methodology.md) — Deliverable format, source hierarchy, citation rules, forecast standards
- [assets/brief_template.md](assets/brief_template.md) — Research brief structure (use to understand what a well-scoped request looks like)
