"""Coordinator middleware — prevents early exit while agents are running.

The coordinator's LLM sometimes returns text (e.g. "I'm waiting for the agent")
instead of calling check_reports again. In a ReAct loop, returning text ends the
loop. This middleware catches that case and sends the LLM back for another turn.
"""
import logging
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, hook_config
from langchain_core.messages import SystemMessage

logger = logging.getLogger(__name__)


class CoordinatorGuardMiddleware(AgentMiddleware):
    """Prevent the coordinator from finishing while agents are still running.

    after_model hook: if the LLM returned text (no tool calls) but there are
    still running AgentRuns for this run, inject a system message and redirect
    back to the model node.
    """

    def __init__(self, run_id: int):
        self.run_id = run_id

    @hook_config(can_jump_to=["model"])
    def after_model(self, state, runtime) -> dict[str, Any] | None:
        last = state["messages"][-1]

        # LLM is calling tools — let it proceed
        if getattr(last, 'tool_calls', None):
            return None

        # LLM returned text — check if agents are still running
        from .models import AgentRun

        active = AgentRun.objects.filter(
            run_id=self.run_id, status__in=('pending', 'running'),
        ).count()

        if active == 0:
            return None

        logger.info(
            "Coordinator guard: %d agent(s) still active for run %d, redirecting",
            active, self.run_id,
        )
        return {
            "messages": [
                SystemMessage(
                    content=f"[{running} agent(s) still running. You must call check_reports to wait for them before finishing.]"
                )
            ],
            "jump_to": "model",
        }
