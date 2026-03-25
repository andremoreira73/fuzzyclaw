"""Container-side message board tools.

Provides post_message, read_messages, list_participants, and ask_human for
specialist agents running inside Docker containers. Communicates via Redis
Streams — no Django imports.

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


def build_message_board_tools(redis_client, self_id: str, run_id: str) -> list:
    """Build LangChain tools for the message board.

    Args:
        redis_client: Connected Redis client (decode_responses=True).
        self_id: This agent's board identity (e.g. market-researcher_423).
        run_id: Current run ID.

    Returns:
        List of @tool functions: post_message, read_messages, list_participants,
        ask_human.
    """
    stream_key = f"fuzzyclaw:board:{run_id}"
    participants_key = f"fuzzyclaw:board:{run_id}:participants"
    # Use HITL timeout as max_wait — the coordinator extends this agent's
    # lifetime to match when message_board is in the tools list.
    max_wait = int(os.environ.get('FUZZYCLAW_HITL_TIMEOUT', '1800'))

    # Track read position across calls within this agent's lifetime
    last_seen_id = '0-0'
    # Buffer for messages skipped by ask_human() (non-human sender)
    _pending_messages = []

    @tool
    def post_message(to: str, message: str) -> str:
        """Post a message to the run's message board.

        Args:
            to: Recipient identifier — e.g. 'human', 'summarizer_456', or 'all'.
            message: The message content. Keep it short; for long content, write
                     a file to your comms directory and reference the path here.
        """
        redis_client.xadd(stream_key, {
            'from': self_id,
            'to': to,
            'content': message,
            'ts': datetime.now(timezone.utc).isoformat(),
        })
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
        nonlocal last_seen_id, _pending_messages
        wait_seconds = max(0, min(wait_seconds, max_wait))

        # Drain any messages buffered by ask_human() first
        messages = list(_pending_messages)
        _pending_messages = []

        # If we already have buffered messages, return them immediately
        if messages:
            logger.info("Board: %s returning %d buffered message(s)", self_id, len(messages))
            return json.dumps(messages)

        deadline = time.time() + wait_seconds

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
            logger.info("Board: %s received %d message(s)", self_id, len(messages))
        return json.dumps(messages)

    @tool
    def list_participants() -> str:
        """List all currently active participants on this run's message board.

        Returns a JSON list of participant IDs (e.g. ['market-researcher_423',
        'summarizer_456']). Use this to discover who else is working on the
        same run before sending them a message.
        """
        members = redis_client.smembers(participants_key)
        participants = sorted(members)
        logger.info("Board: %s listed %d participants", self_id, len(participants))
        return json.dumps(participants)

    @tool
    def ask_human(question: str) -> str:
        """Ask the human operator a question and wait for their response.
        This posts your question to the message board and blocks until the
        human replies (up to 30 minutes). Use this when you need guidance,
        clarification, or a decision before proceeding.

        Returns the human's response text, or an empty string if they
        don't respond within the timeout.

        Args:
            question: Your question for the human operator.
        """
        # Post the question
        redis_client.xadd(stream_key, {
            'from': self_id,
            'to': 'human',
            'content': question,
            'ts': datetime.now(timezone.utc).isoformat(),
        })
        logger.info("Board: %s asks human: %s", self_id, question[:100])

        # Block waiting for response — buffer non-human messages for read_messages()
        nonlocal last_seen_id, _pending_messages
        deadline = time.time() + max_wait

        while time.time() < deadline:
            remaining_ms = max(100, int((deadline - time.time()) * 1000))
            try:
                streams = redis_client.xread(
                    {stream_key: last_seen_id},
                    block=remaining_ms,
                    count=100,
                )
            except Exception as e:
                logger.warning("Board ask_human read failed: %s", e)
                break

            if streams:
                for entry_id, data in streams[0][1]:
                    last_seen_id = entry_id
                    recipient = data.get('to', '')
                    sender = data.get('from', '')
                    is_for_us = recipient == self_id or recipient == 'all'
                    if is_for_us and sender == 'human':
                        response = data.get('content', '')
                        logger.info("Board: %s got human response: %s", self_id, response[:100])
                        return response
                    elif is_for_us:
                        # Buffer non-human messages so read_messages() can return them later
                        _pending_messages.append({
                            'from': sender,
                            'to': recipient,
                            'content': data.get('content', ''),
                            'ts': data.get('ts', ''),
                        })

        logger.info("Board: %s ask_human timed out", self_id)
        return ""

    return [post_message, read_messages, list_participants, ask_human]
