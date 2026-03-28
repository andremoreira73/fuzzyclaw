"""Board notification middleware for specialist agents.

Checks Redis between agent steps (before each LLM call) for new messages
addressed to this agent. If found, injects a system message so the LLM
knows to check the board.

No Django imports — runs inside Docker containers.
"""
import logging
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import SystemMessage

logger = logging.getLogger(__name__)


class BoardNotificationMiddleware(AgentMiddleware):
    """Notify the agent of pending board messages before each LLM call.

    Does a non-blocking XREAD on the board stream. If messages addressed
    to this agent are found, appends a SystemMessage to the conversation
    so the LLM sees the notification naturally.
    """

    def __init__(self, redis_client, self_id: str, run_id: str):
        self._redis = redis_client
        self._self_id = self_id
        self._stream_key = f"fuzzyclaw:board:{run_id}"
        self._last_checked_id = '0-0'

    def before_model(self, state, runtime) -> dict[str, Any] | None:
        try:
            streams = self._redis.xread(
                {self._stream_key: self._last_checked_id},
                count=100,
            )
        except Exception as e:
            logger.warning("Board middleware: Redis read failed: %s", e)
            return None

        if not streams:
            return None

        count = 0
        for entry_id, data in streams[0][1]:
            self._last_checked_id = entry_id
            recipient = data.get('to', '')
            if recipient == self._self_id or recipient == 'all':
                count += 1

        if count == 0:
            return None

        logger.info("Board middleware: %s has %d new message(s)", self._self_id, count)
        return {
            "messages": [
                SystemMessage(
                    content=f"[Board: You have {count} new message(s). Use read_messages to see them.]"
                )
            ]
        }
