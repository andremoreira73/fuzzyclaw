"""Container entrypoint for specialist agents.

Standalone script — no Django imports. Reads env vars, runs agent, writes
report to shared volume.

Env vars:
  AGENT_FILE         — path to agent .md (default /app/agent.md)
  TASK_DESCRIPTION   — what to do
  COMMS_DIR          — path to comms dir (default /app/comms)
  SKILLS_DIR         — path to skills dir (default /app/skills)
  DATABASE_URL       — (optional) for PostgresStore persistent memory
  + LLM API key(s) for the agent's configured provider
"""
import json
import logging
import os
import sys

import yaml
import traceback

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain_core.messages import HumanMessage

from agent_tools import build_tools
from agent_tools.memory import build_memory_tools, get_memory_store

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(asctime)s %(message)s')
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Redis Streams — signal completion to coordinator
# ---------------------------------------------------------------------------

def signal_completion(status: str):
    """XADD a completion signal to the run's Redis Stream.

    Env vars: REDIS_URL, RUN_ID, AGENT_RUN_ID.
    Silently warns if Redis is unavailable — the coordinator falls back to
    filesystem polling.
    """
    redis_url = os.environ.get('REDIS_URL', '')
    run_id = os.environ.get('RUN_ID', '')
    agent_run_id = os.environ.get('AGENT_RUN_ID', '')

    if not redis_url or not run_id or not agent_run_id:
        logger.warning("Redis signaling skipped: missing REDIS_URL/RUN_ID/AGENT_RUN_ID")
        return

    try:
        import redis
        r = redis.from_url(redis_url)
        stream_key = f"fuzzyclaw:run:{run_id}:done"
        r.xadd(stream_key, {'agent_run_id': agent_run_id, 'status': status})
        logger.info("Signaled completion on stream %s (status=%s)", stream_key, status)
    except Exception as e:
        logger.warning("Redis signaling failed (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# Frontmatter parser (standalone copy — no core.registry dependency)
# ---------------------------------------------------------------------------

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from markdown text."""
    import re
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)', text, re.DOTALL)
    if not match:
        raise ValueError("No frontmatter found")

    frontmatter = yaml.safe_load(match.group(1)) or {}
    if not isinstance(frontmatter, dict):
        raise ValueError("Frontmatter must be a YAML mapping")

    return frontmatter, match.group(2).strip()


def parse_agent_file(filepath: str) -> dict:
    """Parse an agent .md file into a dict."""
    with open(filepath, encoding='utf-8') as f:
        text = f.read()

    frontmatter, prompt = parse_frontmatter(text)

    name = frontmatter.get('name')
    if not name:
        raise ValueError(f"Missing 'name' in frontmatter of {filepath}")

    tools = frontmatter.get('tools', [])
    if not isinstance(tools, list):
        tools = []

    memory = bool(frontmatter.get('memory', False))

    return {
        'name': name,
        'description': frontmatter.get('description', ''),
        'model_choice': frontmatter.get('model', 'gpt-5-mini'),
        'tools': tools,
        'memory': memory,
        'prompt': prompt,
    }


# ---------------------------------------------------------------------------
# Model instantiation
# ---------------------------------------------------------------------------

def get_model(model_name: str):
    """Instantiate a LangChain chat model from a model name string.

    Reads MODEL_DEFAULTS env var (JSON) injected by containers.py to match
    coordinator model configuration (temperature, reasoning_effort, etc.).
    """
    defaults_raw = os.environ.get('MODEL_DEFAULTS', '{}')
    try:
        defaults = json.loads(defaults_raw)
    except json.JSONDecodeError:
        defaults = {}

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
        raise ValueError(f"Unknown model prefix: '{model_name}'")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    agent_file = os.environ.get('AGENT_FILE', '/app/agent.md')
    task_description = os.environ.get('TASK_DESCRIPTION', '')
    comms_dir = os.environ.get('COMMS_DIR', '/app/comms')

    # All startup work is inside this outer try so that parse failures,
    # model init errors, and tool-build exceptions produce structured
    # error.json files instead of silent crashes.
    agent_name = 'unknown'
    board_redis = None
    self_id = os.environ.get('SELF_ID', '')
    run_id = os.environ.get('RUN_ID', '')

    try:
        if not task_description:
            raise ValueError("TASK_DESCRIPTION env var is required")

        logger.info("Starting agent from %s", agent_file)
        agent_def = parse_agent_file(agent_file)
        agent_name = agent_def['name']
        logger.info("Agent: %s (model: %s)", agent_name, agent_def['model_choice'])

        model = get_model(agent_def['model_choice'])
        # Filter out message_board — handled separately via setup_message_board()
        standard_tools = [t for t in agent_def['tools'] if t != 'message_board']
        tools = build_tools(standard_tools)

        # Optional persistent memory (PostgresStore context manager)
        store_ctx = None
        if agent_def['memory']:
            store_ctx = get_memory_store(agent_def['name'])

        # Optional message board (Redis-backed) — one setup call
        board = None
        if 'message_board' in agent_def['tools'] and self_id and run_id:
            from agent_tools.message_board import get_board_redis, setup_message_board
            board_redis = get_board_redis()
            board = setup_message_board(board_redis, self_id, run_id)

        system_prompt = f"""You are a FuzzyClaw specialist agent: {agent_def['name']}

{agent_def['prompt']}

You have access to tools as configured. Use them to get real work done.
Complete the task given to you and return a clear, structured report of your findings."""

        # Append mounted volumes info to system prompt
        agent_volumes_raw = os.environ.get('AGENT_VOLUMES', '')
        if agent_volumes_raw:
            try:
                agent_volumes = json.loads(agent_volumes_raw)
                if agent_volumes:
                    vol_lines = []
                    for vol in agent_volumes:
                        mode_label = 'read-only' if vol['mode'] == 'ro' else 'read-write'
                        vol_lines.append(f"- {vol['mount']} ({mode_label})")
                    system_prompt += "\n\n## Mounted Volumes\n" + "\n".join(vol_lines)
                    system_prompt += "\nUse bash to interact with files in these directories."
            except (json.JSONDecodeError, KeyError):
                logger.warning("Failed to parse AGENT_VOLUMES env var")

        if board:
            system_prompt += board.prompt_section

        def run_agent(store=None):
            agent_tools = list(tools)
            middleware = []

            if store is not None:
                owner_id = os.environ.get('OWNER_ID', '')
                if not owner_id:
                    logger.warning(
                        "OWNER_ID env var missing — memory tools disabled to prevent cross-user leakage"
                    )
                else:
                    memory_tools = build_memory_tools(store, agent_def['name'], owner_id)
                    agent_tools.extend(memory_tools)
                    logger.info(
                        "Memory tools enabled: remember, recall, recall_all (namespace: owner=%s, agent=%s)",
                        owner_id, agent_def['name'],
                    )

            if board:
                agent_tools.extend(board.tools)
                middleware.extend(board.middleware)

            agent_kwargs = dict(
                model=model,
                tools=agent_tools,
                system_prompt=system_prompt,
                backend=FilesystemBackend(root_dir="/app", virtual_mode=True),
                skills=['/app/skills'],
                middleware=middleware,
            )

            agent = create_deep_agent(**agent_kwargs)

            result = agent.invoke(
                {"messages": [HumanMessage(content=task_description)]},
            )
            return result["messages"][-1].content

        if store_ctx is not None:
            with store_ctx as store:
                store.setup()
                report_text = run_agent(store)
        else:
            report_text = run_agent()

        report = {
            'agent_name': agent_name,
            'status': 'completed',
            'report': report_text,
        }
        report_path = os.path.join(comms_dir, 'report.json')
        with open(report_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2)

        logger.info("Report written to %s", report_path)
        signal_completion('completed')
        sys.exit(0)

    except SystemExit:
        raise  # Don't catch sys.exit(0) from the success path

    except Exception as e:
        logger.error("Agent failed: %s", e)
        error = {
            'agent_name': agent_name,
            'status': 'failed',
            'error': str(e),
            'traceback': traceback.format_exc(),
        }
        error_path = os.path.join(comms_dir, 'error.json')
        try:
            with open(error_path, 'w', encoding='utf-8') as f:
                json.dump(error, f, indent=2)
        except Exception:
            logger.error("Could not write error.json to %s", error_path)

        signal_completion('failed')
        sys.exit(1)

    finally:
        # Deregister from message board
        if board_redis and self_id and run_id:
            try:
                participants_key = f"fuzzyclaw:board:{run_id}:participants"
                board_redis.srem(participants_key, self_id)
                logger.info("Deregistered board participant: %s", self_id)
            except Exception:
                pass


if __name__ == '__main__':
    main()
