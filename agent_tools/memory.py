"""Persistent memory tools backed by PostgresStore.

Provides remember/recall/recall_all tools that persist across container runs.
Memories are namespaced by (owner_id, agent_name) so one user's memories
never leak into another user's, even when both run the same agent.
"""
import logging
import os

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


def get_memory_store(agent_name: str):
    """Connect to PostgresStore for persistent agent memory, if DATABASE_URL is set.

    Returns a PostgresStore context manager (must be used with `with`), or None.
    """
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        return None

    try:
        from langgraph.store.postgres import PostgresStore
        store_ctx = PostgresStore.from_conn_string(database_url)
        logger.info("PostgresStore available for agent '%s'", agent_name)
        return store_ctx
    except Exception as e:
        logger.warning("Failed to connect to PostgresStore: %s", e)
        return None


def build_memory_tools(store, agent_name: str, owner_id: str, briefing_id: str = ''):
    """Build recall/remember tools backed by PostgresStore.

    Namespace is (owner_id, agent_name, briefing_id) for specialists, or
    (owner_id, agent_name) for agents not tied to a briefing (e.g. fuzzy).
    Memories are scoped per user, per agent, per briefing — no leakage.
    """
    if not owner_id:
        raise ValueError("build_memory_tools requires a non-empty owner_id")
    if briefing_id:
        namespace = (owner_id, agent_name, briefing_id)
    else:
        namespace = (owner_id, agent_name)

    @tool
    def remember(key: str, content: str) -> str:
        """Store a piece of knowledge in persistent memory for future runs.
        Use a descriptive key so you can find it later.

        Args:
            key: A short, descriptive key for this memory (e.g. "llm-eval-findings", "run-count").
            content: The content to remember. Can be any text.
        """
        store.put(namespace, key, {"content": content})
        return f"Remembered '{key}' successfully."

    @tool
    def recall(key: str) -> str:
        """Retrieve a specific memory by key from persistent storage.

        Args:
            key: The key of the memory to retrieve.
        """
        item = store.get(namespace, key)
        if item is None:
            return f"No memory found for key '{key}'."
        return item.value.get("content", "(empty)")

    @tool
    def recall_all() -> str:
        """List and retrieve ALL memories from persistent storage.
        Use this at the start of a task to see everything you remember."""
        items = store.search(namespace)
        if not items:
            return "No memories found. This appears to be your first run."
        results = []
        for item in items:
            content = item.value.get("content", "(empty)")
            results.append(f"[{item.key}] {content}")
        return f"Found {len(items)} memory/memories:\n\n" + "\n\n".join(results)

    return [remember, recall, recall_all]
