"""FuzzyClaw agent runtime — coordinator agent factory."""
import logging

import redis as redis_lib
from django.conf import settings as django_settings

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage

from .coordinator_middleware import CoordinatorGuardMiddleware

logger = logging.getLogger(__name__)


def get_model(model_name: str):
    """Instantiate a LangChain chat model from a model name string.

    Supports: claude-*, gpt-*, gemini-* prefixes.
    """
    model_config = django_settings.FUZZYCLAW_MODELS.get(model_name, {})
    defaults = model_config.get('defaults', {})

    if model_name.startswith('claude'):
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model_name, **defaults)
    elif model_name.startswith('gpt'):
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model_name, **defaults)
    elif model_name.startswith('gemini'):
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(model=model_name, **defaults)
    else:
        raise ValueError(f"Unknown model prefix: '{model_name}'. Register it in FUZZYCLAW_MODELS.")


def _get_coordinator_redis():
    """Get a Redis client for the coordinator's board tools."""
    redis_url = getattr(django_settings, 'FUZZYCLAW_REDIS_URL', '')
    if not redis_url:
        return None
    try:
        r = redis_lib.from_url(redis_url, decode_responses=True)
        r.ping()
        return r
    except Exception as e:
        logger.warning("Coordinator board: Redis unavailable: %s", e)
        return None


def build_coordinator(briefing, run):
    """Build a coordinator agent for a given briefing and run.

    The coordinator gets:
    - The briefing content as system prompt context
    - Tools to list agents, dispatch specialists, check/read reports, and submit its final report
    - Message board tools to communicate with agents and humans
    - Guard middleware to prevent early exit while agents are running
    """
    from agent_tools.board_middleware import BoardNotificationMiddleware
    from agent_tools.message_board import build_message_board_tools

    from .agent_tools import (
        check_reports,
        list_available_agents,
        make_dispatch_specialist,
        make_manage_schedule,
        make_submit_coordinator_report,
        read_report,
    )

    model = get_model(briefing.coordinator_model)
    self_id = f"coordinator_{run.id}"

    system_prompt = f"""You are the FuzzyClaw coordinator agent. Your job is to execute a briefing by following its steps, dispatching specialist agents as needed, and producing a final synthesis report.

## Briefing: {briefing.title}

{briefing.content}

## Your Run ID: {run.id}

## Instructions

1. Read the briefing steps carefully.
2. Follow the instructions from the briefing in a disciplined way.
3. Use the tools available to you to act according to the instructions from the briefing.
4. If you are using specialist agents, use the tools available to start them.
5. As each specialist agent returns their reports, read them one at a time to avoid output formatting issues.
6. Be smart: make supporting decisions when the briefing doesn't cover a situation.

## Platform Constraints

- There is a container concurrency limit of {django_settings.FUZZYCLAW_MAX_CONTAINERS} agents running simultaneously. If a dispatch is rejected, wait for running agents to finish before retrying.

## Behavior

- Be concise and direct. No preamble, no filler.
- Prioritize accuracy over validation. If a specialist agent's report looks wrong or incomplete, say so.
- Prefer dispatch independent specialist agents in parallel
- If a specialist agent fails or crashes, note the failure and decide whether to retry or proceed without it.
- When things go wrong repeatedly, stop and analyze why — don't keep retrying the same approach.
- You only have a few tools. Delegate work to specialist agents.
- When calling tools, ensure all string values in arguments are properly JSON-escaped. Do not include unescaped newlines, quotes, or special characters in tool call arguments.
"""

    tools = [
        list_available_agents,
        make_dispatch_specialist(run),
        check_reports,
        read_report,
        make_submit_coordinator_report(run),
        make_manage_schedule(briefing),
    ]

    middleware = [CoordinatorGuardMiddleware(run.id)]

    # Message board — coordinator can talk to agents and humans
    board_redis = _get_coordinator_redis()
    if board_redis:
        participants_key = f"fuzzyclaw:board:{run.id}:participants"
        board_redis.sadd(participants_key, self_id)
        logger.info("Coordinator registered as board participant: %s", self_id)

        board_tools = build_message_board_tools(board_redis, self_id, str(run.id))
        tools.extend(board_tools)

        middleware.append(BoardNotificationMiddleware(board_redis, self_id, str(run.id)))

        system_prompt += f"""
## Message Board
You are `{self_id}` on this run's message board. You can send messages to the human and to specialist agents, and read their replies. Use this for coordination when needed."""

    agent = create_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        middleware=middleware,
    )

    return agent


def run_coordinator(briefing, run, max_retries=django_settings.FUZZYCLAW_COORDINATOR_MAX_RETRIES):
    """Execute the coordinator agent for a briefing/run pair. Returns the final report.

    Retries on empty responses (e.g. Gemini MALFORMED_FUNCTION_CALL) up to max_retries times.
    """
    agent = build_coordinator(briefing, run)

    for attempt in range(1, max_retries + 1):
        result = agent.invoke(
            {"messages": [HumanMessage(content=f"Execute briefing: {briefing.title}")]}
        )

        last_message = result["messages"][-1]
        if last_message.content:
            return last_message.content

        logger.warning(
            "Coordinator returned empty response on attempt %d/%d (possible MALFORMED_FUNCTION_CALL)",
            attempt, max_retries,
        )

    # Final attempt exhausted — return whatever we got
    return last_message.content or "Coordinator failed to produce a report."
