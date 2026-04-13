"""Container entrypoint for the fuzzy always-on assistant.

Persistent idle loop — blocks on Redis XREAD until a message arrives on
the board, builds a fresh agent for each conversation, posts the response,
then goes back to sleep. Runs as a docker compose service, not a one-shot
container.

Env vars:
  AGENT_FILE         — path to agent .md (default /app/agent.md)
  SKILLS_DIR         — path to skills dir (default /app/skills)
  SELF_ID            — board identity (default 'fuzzy')
  BOARD_STREAM       — Redis stream key (default 'fuzzyclaw:board:fuzzy')
  OWNER_ID           — user ID for memory namespace (required)
  REDIS_URL          — Redis connection string (db=1 for board)
  DATABASE_URL       — PostgreSQL for persistent memory
  API_BASE_URL       — REST API base (default http://web:8200/api)
  API_TOKEN          — DRF auth token for platform queries
  + LLM API key(s) for the agent's configured provider
"""
import json
import logging
import os
import re
import signal
import sys
import time
import traceback
from datetime import datetime, timezone

import redis as redis_lib
import yaml

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain_core.messages import HumanMessage

from agent_tools import build_tools
from agent_tools.memory import build_memory_tools, get_memory_store
from agent_tools.message_board import get_board_redis, setup_message_board

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(asctime)s %(message)s')
logger = logging.getLogger(__name__)

# Graceful shutdown flag
_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logger.info("Received signal %d, shutting down...", signum)
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ---------------------------------------------------------------------------
# Frontmatter parser (standalone copy — no core.registry dependency)
# ---------------------------------------------------------------------------

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from markdown text."""
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
        raise ValueError(f"'tools' must be a list in frontmatter of {filepath}, got {type(tools).__name__}")

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
    """Instantiate a LangChain chat model from a model name string."""
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
# Build system prompt
# ---------------------------------------------------------------------------

def build_system_prompt(agent_def: dict, board_prompt: str) -> str:
    """Assemble the full system prompt for fuzzy."""
    prompt = f"""You are fuzzy, the FuzzyClaw platform assistant.

{agent_def['prompt']}"""

    # Append mounted volumes info
    agent_volumes_raw = os.environ.get('AGENT_VOLUMES', '')
    if agent_volumes_raw:
        try:
            agent_volumes = json.loads(agent_volumes_raw)
            if agent_volumes:
                vol_lines = []
                for vol in agent_volumes:
                    mode_label = 'read-only' if vol['mode'] == 'ro' else 'read-write'
                    vol_lines.append(f"- {vol['mount']} ({mode_label})")
                prompt += "\n\n## Mounted Volumes\n" + "\n".join(vol_lines)
                prompt += "\nUse these exact paths when reading or writing files."
        except (json.JSONDecodeError, KeyError):
            logger.warning("Failed to parse AGENT_VOLUMES env var")

    if board_prompt:
        prompt += board_prompt

    return prompt


# ---------------------------------------------------------------------------
# Single conversation handler
# ---------------------------------------------------------------------------

def handle_message(agent_def: dict, model, base_tools: list, board_redis,
                   self_id: str, board_stream: str, owner_id: str,
                   message_content: str, sender: str, trigger_id: str):
    """Handle one user message: build agent, invoke, post response."""
    logger.info("Handling message from '%s': %s", sender, message_content[:100])

    # Signal that fuzzy is working (board panel polls this for typing indicator)
    status_key = f"fuzzyclaw:fuzzy:status"
    try:
        board_redis.set(status_key, "thinking", ex=300)  # auto-expire after 5min safety net
    except Exception:
        pass

    # Set up board tools scoped to this conversation, starting after the
    # triggering message so the agent doesn't re-read it
    board = setup_message_board(board_redis, self_id, board_stream, initial_position=trigger_id)

    board_prompt = board.prompt_section if board else ''
    system_prompt = build_system_prompt(agent_def, board_prompt)

    agent_tools = list(base_tools)

    # Memory tools
    store_ctx = None
    if agent_def['memory']:
        store_ctx = get_memory_store(agent_def['name'])

    def run_agent(store=None):
        tools = list(agent_tools)
        middleware = []

        if store is not None:
            if not owner_id:
                logger.warning("OWNER_ID missing — memory tools disabled")
            else:
                memory_tools = build_memory_tools(store, agent_def['name'], owner_id)
                tools.extend(memory_tools)
                logger.info("Memory tools enabled (namespace: owner=%s, agent=%s)", owner_id, agent_def['name'])

        if board:
            tools.extend(board.tools)
            middleware.extend(board.middleware)

        agent = create_deep_agent(
            model=model,
            tools=tools,
            system_prompt=system_prompt,
            backend=FilesystemBackend(root_dir="/", virtual_mode=True),
            skills=['/app/skills'],
            middleware=middleware,
        )

        result = agent.invoke(
            {"messages": [HumanMessage(content=message_content)]},
        )
        content = result["messages"][-1].content
        # Content may be a list of blocks (e.g. from tool-call responses) —
        # extract text parts into a single string
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            content = "\n".join(p for p in parts if p)
        return content

    posted_via_tool = False

    try:
        if store_ctx is not None:
            with store_ctx as store:
                store.setup()
                response = run_agent(store)
        else:
            response = run_agent()
    except Exception as e:
        logger.error("Agent invocation failed: %s\n%s", e, traceback.format_exc())
        response = "Sorry, I encountered an internal error while processing your message. Please try again."

    # Check if the agent already posted on the board (avoid duplicate)
    try:
        recent = board_redis.xrevrange(
            f"fuzzyclaw:board:{board_stream}", count=5,
        )
        for _, data in recent:
            if data.get('from') == self_id and data.get('to') in (sender, 'all', 'human'):
                posted_via_tool = True
                break
    except Exception:
        pass

    if not posted_via_tool and response:
        try:
            board_redis.xadd(f"fuzzyclaw:board:{board_stream}", {
                'from': self_id,
                'to': sender,
                'content': response,
                'ts': datetime.now(timezone.utc).isoformat(),
            })
            logger.info("Posted response to '%s' (%d chars)", sender, len(response))
        except Exception as e:
            logger.error("Failed to post response on board: %s", e)
    elif posted_via_tool:
        logger.info("Agent already posted via board tool — skipping safety-net post.")

    # Clear thinking status
    try:
        board_redis.delete(status_key)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main idle loop
# ---------------------------------------------------------------------------

def main():
    agent_file = os.environ.get('AGENT_FILE', '/app/agent.md')
    self_id = os.environ.get('SELF_ID', 'fuzzy')
    board_stream = os.environ.get('BOARD_STREAM', 'fuzzy')
    owner_id = os.environ.get('OWNER_ID', '')
    stream_key = f"fuzzyclaw:board:{board_stream}"

    if not owner_id:
        logger.error("OWNER_ID env var is required — fuzzy needs a user context")
        sys.exit(1)

    logger.info("Starting fuzzy from %s", agent_file)
    agent_def = parse_agent_file(agent_file)
    logger.info("Agent: %s (model: %s)", agent_def['name'], agent_def['model_choice'])

    model = get_model(agent_def['model_choice'])

    # Warn if platform_query tools are listed but no API token is configured
    if 'platform_query' in agent_def['tools'] and not os.environ.get('API_TOKEN'):
        logger.warning(
            "API_TOKEN env var is empty — platform query tools will return 401 errors. "
            "Generate a token with: docker compose exec web python manage.py drf_create_token <username>"
        )

    # Build standard tools (excluding message_board — handled per-conversation)
    standard_tool_names = [t for t in agent_def['tools'] if t != 'message_board']
    base_tools = build_tools(standard_tool_names)

    # Connect to Redis for the message board
    board_redis = get_board_redis()
    if board_redis is None:
        logger.error("Redis unavailable — fuzzy cannot run without the message board")
        sys.exit(1)

    # Register as permanent participant
    participants_key = f"fuzzyclaw:board:{board_stream}:participants"
    board_redis.sadd(participants_key, self_id)
    logger.info("Registered as board participant: %s on %s", self_id, stream_key)

    # Start reading from the latest position (skip history on startup)
    last_id = '$'
    logger.info("Fuzzy idle loop started — listening on %s", stream_key)

    try:
        while not _shutdown:
            try:
                # Block up to 5s, then check shutdown flag
                streams = board_redis.xread(
                    {stream_key: last_id},
                    block=5000,
                    count=10,
                )
            except redis_lib.ConnectionError as e:
                logger.warning("Redis connection lost, retrying in 5s: %s", e)
                time.sleep(5)
                board_redis = get_board_redis()
                if board_redis is None:
                    logger.error("Redis reconnection failed")
                    continue
                board_redis.sadd(participants_key, self_id)
                continue

            if not streams:
                continue  # Timeout, loop back and check shutdown flag

            for entry_id, data in streams[0][1]:
                last_id = entry_id
                recipient = data.get('to', '')
                sender = data.get('from', '')

                # Only process messages addressed to us
                if recipient != self_id and recipient != 'all':
                    continue

                # Skip our own messages
                if sender == self_id:
                    continue

                content = data.get('content', '').strip()
                if not content:
                    continue

                logger.info("Wake-up: message from '%s' (id: %s)", sender, entry_id)
                handle_message(
                    agent_def=agent_def,
                    model=model,
                    base_tools=base_tools,
                    board_redis=board_redis,
                    self_id=self_id,
                    board_stream=board_stream,
                    owner_id=owner_id,
                    message_content=content,
                    sender=sender,
                    trigger_id=entry_id,
                )
                logger.info("Back to idle.")

    finally:
        # Deregister on shutdown
        try:
            board_redis.srem(participants_key, self_id)
            logger.info("Deregistered board participant: %s", self_id)
        except Exception:
            pass
        logger.info("Fuzzy shut down.")


if __name__ == '__main__':
    main()
