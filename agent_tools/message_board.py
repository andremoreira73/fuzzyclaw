"""Container-side message board tools.

Provides post_message, read_messages, and list_participants for specialist
agents running inside Docker containers. Communicates via Redis Streams —
no Django imports.

Env vars:
  REDIS_URL              — Redis connection string (db=1)
  SELF_ID                — This agent's board identity (e.g. market-researcher_423)
  RUN_ID                 — Current run ID
  FUZZYCLAW_HITL_TIMEOUT — Max wait for human response (default 1800s)
"""
import json
import logging
import os
import time
from datetime import datetime, timezone

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


def get_board_redis():
    """Connect to Redis for the message board.

    Returns a Redis client with decode_responses=True, or None if unavailable.
    """
    redis_url = os.environ.get('REDIS_URL', '')
    if not redis_url:
        logger.warning("Message board: REDIS_URL not set")
        return None

    import redis
    try:
        r = redis.from_url(redis_url, decode_responses=True)
        r.ping()
        return r
    except Exception as e:
        logger.warning("Message board: Redis unavailable: %s", e)
        return None


def build_message_board_tools(redis_client, self_id: str, run_id: str,
                              initial_position: str = '0-0',
                              extra_fields: dict | None = None) -> list:
    """Build LangChain tools for the message board.

    Args:
        redis_client: Connected Redis client (decode_responses=True).
        self_id: This agent's board identity (e.g. market-researcher_423).
        run_id: Current run ID.
        initial_position: Starting stream position for read_messages (default
            '0-0' reads all history). Pass a message ID to skip earlier entries.
        extra_fields: Optional dict of extra fields to include in every xadd
            (e.g. {'user_id': '1'} for multi-user scoping).

    Returns:
        List of @tool functions: post_message, read_messages, list_participants.
    """
    stream_key = f"fuzzyclaw:board:{run_id}"
    participants_key = f"fuzzyclaw:board:{run_id}:participants"
    # Use HITL timeout as max_wait — the coordinator extends this agent's
    # lifetime to match when message_board is in the tools list.
    max_wait = int(os.environ.get('FUZZYCLAW_HITL_TIMEOUT', '1800'))
    agent_wait = int(os.environ.get('FUZZYCLAW_AGENT_TIMEOUT', '600'))

    _extra = extra_fields or {}

    # Track read position across calls within this agent's lifetime
    last_seen_id = initial_position
    # Minimum wait floor after posting — None means no floor active
    _reply_wait_floor: int | None = None

    @tool
    def post_message(to: str, message: str) -> str:
        """Post a message to the run's message board.

        Args:
            to: Recipient identifier — e.g. 'human', 'summarizer_456', or 'all'.
            message: The message content. Keep it short; for long content, write
                     a file to your comms directory and reference the path here.
        """
        try:
            entry = {
                'from': self_id,
                'to': to,
                'content': message,
                'ts': datetime.now(timezone.utc).isoformat(),
                **_extra,
            }
            redis_client.xadd(stream_key, entry)
        except Exception as e:
            logger.warning("Board post failed: %s", e)
            return f"Error: message board unavailable ({e}). Retry the tool call."
        nonlocal _reply_wait_floor
        # Only arm the wait floor if there isn't one active already.
        # This prevents re-arming when the agent sends a follow-up message
        # (e.g. relaying an answer) after already receiving a reply.
        if _reply_wait_floor is None:
            _reply_wait_floor = max_wait if to == 'human' else agent_wait
        logger.info("Board: %s -> %s: %s", self_id, to, message[:100])
        return f"Message posted to {to}."

    @tool
    def read_messages(wait_seconds: int = 0) -> str:
        """Read new messages addressed to you (or 'all') on this run's board.

        Blocks up to wait_seconds for new messages. Use wait_seconds=1800 when
        you need a human response before continuing.

        Returns a JSON list of messages, each with 'from', 'to', 'content', 'ts'.
        Empty list if no messages arrived before the timeout.

        Args:
            wait_seconds: How long to block waiting for messages (0 = check and
                          return immediately, max 1800). Use 1800 when waiting
                          for a human reply.
        """
        nonlocal last_seen_id, _reply_wait_floor
        # Enforce minimum wait after posting — HITL timeout for humans,
        # agent timeout for other agents. The LLM tends to pick short waits
        # (60s) but recipients need time to process.
        if _reply_wait_floor is not None:
            wait_seconds = max(wait_seconds, _reply_wait_floor)
        wait_seconds = max(0, min(wait_seconds, max_wait))

        messages = []
        deadline = time.time() + wait_seconds
        read_error: Exception | None = None

        while True:
            remaining_ms = max(100, int((deadline - time.time()) * 1000)) if wait_seconds > 0 else None

            try:
                streams = redis_client.xread(
                    {stream_key: last_seen_id},
                    block=remaining_ms,
                    count=100,
                )
            except Exception as e:
                logger.warning("Board read failed: %s", e)
                read_error = e
                break

            if streams:
                for entry_id, data in streams[0][1]:
                    last_seen_id = entry_id
                    recipient = data.get('to', '')
                    if recipient == self_id or recipient == 'all':
                        messages.append({
                            'from': data.get('from', ''),
                            'to': recipient,
                            'content': data.get('content', ''),
                            'ts': data.get('ts', ''),
                        })

                if messages:
                    break  # Got messages addressed to us
                elif streams:
                    # Stream has traffic but nothing for us — brief backoff
                    time.sleep(0.1)

            if time.time() >= deadline:
                break

        if messages:
            # Clear the wait floor — we got a reply
            _reply_wait_floor = None
            logger.info("Board: %s received %d message(s)", self_id, len(messages))
            return json.dumps(messages)
        if read_error is not None:
            return f"Error: message board unavailable ({read_error}). Retry the tool call."
        return json.dumps(messages)

    @tool
    def list_participants() -> str:
        """List all currently active participants on this run's message board.

        Returns a JSON list of participant IDs (e.g. ['market-researcher_423',
        'summarizer_456']). Use this to discover who else is working on the
        same run before sending them a message.
        """
        try:
            members = redis_client.smembers(participants_key)
        except Exception as e:
            logger.warning("Board list_participants failed: %s", e)
            return f"Error: message board unavailable ({e}). Retry the tool call."
        participants = sorted(members)
        logger.info("Board: %s listed %d participants", self_id, len(participants))
        return json.dumps(participants)

    return [post_message, read_messages, list_participants]


class BoardSetup:
    """Result of setup_message_board() — tools, middleware, and prompt section."""

    def __init__(self, tools, middleware, prompt_section):
        self.tools = tools
        self.middleware = middleware
        self.prompt_section = prompt_section


def setup_message_board(redis_client, self_id: str, run_id: str, initial_position: str = '0-0', extra_fields: dict | None = None) -> BoardSetup | None:
    """Set up all message board components for an agent.

    Handles participant registration, tool creation, middleware creation,
    and system prompt section — one call, one place.

    Args:
        redis_client: Connected Redis client, or None.
        self_id: This agent's board identity (e.g. market-researcher_423).
        run_id: Current run ID.
        initial_position: Starting stream position for read_messages (default
            '0-0'). Pass a message ID to skip earlier entries.

    Returns:
        BoardSetup with tools, middleware, and prompt_section, or None if
        Redis is unavailable.
    """
    if redis_client is None or not self_id or not run_id:
        return None

    # Register as participant
    participants_key = f"fuzzyclaw:board:{run_id}:participants"
    try:
        redis_client.sadd(participants_key, self_id)
        logger.info("Registered as board participant: %s", self_id)
    except Exception as e:
        logger.warning("Message board registration failed: %s", e)
        return None

    tools = build_message_board_tools(redis_client, self_id, run_id, initial_position, extra_fields)

    from agent_tools.board_middleware import BoardNotificationMiddleware
    middleware = [BoardNotificationMiddleware(redis_client, self_id, run_id)]

    prompt_section = f"""

## Message Board
You are `{self_id}` on this run's message board. You have tools to send messages to the human operator and to other agents, and to wait for their replies. This is how you interact with the human — use your message board tools whenever the task requires human input, feedback, or a conversation. When you send a message to the human, always wait for their actual response before proceeding. Never assume or fabricate what the human said."""

    logger.info("Message board tools enabled: post_message, read_messages, list_participants")
    logger.info("Board notification middleware enabled")

    return BoardSetup(tools, middleware, prompt_section)
