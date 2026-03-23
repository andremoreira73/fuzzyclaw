"""FuzzyClaw agent runtime — coordinator agent factory."""
import logging

from django.conf import settings as django_settings

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage

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
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model_name, **defaults)


def build_coordinator(briefing, run):
    """Build a Deep Agent coordinator for a given briefing and run.

    The coordinator gets:
    - The briefing content as system prompt context
    - Tools to list agents, dispatch specialists, check/read reports, and submit its final report
    """
    from .agent_tools import (
        check_reports,
        dispatch_specialist,
        list_available_agents,
        manage_schedule,
        read_report,
        submit_coordinator_report,
    )

    model = get_model(briefing.coordinator_model)

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

    agent = create_agent(
        model=model,
        tools=[
            list_available_agents,
            dispatch_specialist,
            check_reports,
            read_report,
            submit_coordinator_report,
            manage_schedule,
        ],
        system_prompt=system_prompt,
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
