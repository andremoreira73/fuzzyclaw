"""Platform query tools — read-only access to FuzzyClaw state.

Calls the Django REST API from inside agent containers. Requires:
  API_BASE_URL — Base URL of the API (default http://web:8200/api)
  API_TOKEN    — DRF auth token for the user
"""
import json
import logging
import os

import requests
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# Shared session for connection pooling
_session: requests.Session | None = None


def _get_session() -> requests.Session:
    """Lazy-init a requests session with auth headers."""
    global _session
    if _session is None:
        token = os.environ.get('API_TOKEN', '')
        _session = requests.Session()
        if token:
            _session.headers['Authorization'] = f'Token {token}'
        _session.headers['Accept'] = 'application/json'
    return _session


def _api_url(path: str) -> str:
    """Build full API URL from a relative path."""
    base = os.environ.get('API_BASE_URL', 'http://web:8200/api').rstrip('/')
    return f"{base}/{path.lstrip('/')}"


def _api_get(path: str, params: dict | None = None) -> dict | list | str:
    """GET an API endpoint, return parsed JSON or error string."""
    try:
        resp = _get_session().get(_api_url(path), params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.HTTPError as e:
        logger.warning("Platform API error: %s %s", e.response.status_code, path)
        return f"API error: {e.response.status_code} for {path}"
    except Exception as e:
        logger.warning("Platform API request failed: %s", e)
        return f"API request failed: {e}"


def build_platform_query_tools() -> list:
    """Build LangChain tools for querying FuzzyClaw platform state.

    Returns a list of @tool functions for briefings, runs, and agent reports.
    """

    @tool
    def list_briefings() -> str:
        """List all briefings with their status and schedule info.

        Returns a JSON list of briefings (id, title, is_active, schedule_text,
        coordinator_model, created_at).
        """
        data = _api_get('briefings/')
        if isinstance(data, str):
            return data
        # DRF pagination: results may be in 'results' key
        results = data.get('results', data) if isinstance(data, dict) else data
        summary = []
        for b in results:
            summary.append({
                'id': b.get('id'),
                'title': b.get('title'),
                'is_active': b.get('is_active'),
                'schedule_text': b.get('schedule_text'),
                'coordinator_model': b.get('coordinator_model'),
                'created_at': b.get('created_at'),
            })
        return json.dumps(summary, indent=2)

    @tool
    def get_briefing(briefing_id: int) -> str:
        """Get full details of a specific briefing including its content.

        Args:
            briefing_id: The briefing ID to retrieve.
        """
        data = _api_get(f'briefings/{briefing_id}/')
        if isinstance(data, str):
            return data
        return json.dumps(data, indent=2)

    @tool
    def list_runs(briefing_id: int | None = None, status: str | None = None) -> str:
        """List runs, optionally filtered by briefing or status.

        Args:
            briefing_id: Filter by briefing ID (optional).
            status: Filter by status: pending, running, completed, failed (optional).
        """
        params = {}
        if briefing_id is not None:
            params['briefing'] = briefing_id
        if status is not None:
            params['status'] = status
        data = _api_get('runs/', params=params)
        if isinstance(data, str):
            return data
        results = data.get('results', data) if isinstance(data, dict) else data
        summary = []
        for r in results:
            summary.append({
                'id': r.get('id'),
                'briefing': r.get('briefing'),
                'status': r.get('status'),
                'triggered_by': r.get('triggered_by'),
                'started_at': r.get('started_at'),
                'completed_at': r.get('completed_at'),
                'created_at': r.get('created_at'),
            })
        return json.dumps(summary, indent=2)

    @tool
    def get_run(run_id: int) -> str:
        """Get full details of a run including the coordinator report.

        Args:
            run_id: The run ID to retrieve.
        """
        data = _api_get(f'runs/{run_id}/')
        if isinstance(data, str):
            return data
        return json.dumps(data, indent=2)

    @tool
    def list_agent_runs(run_id: int) -> str:
        """List all agent runs (specialist dispatches) for a given run.

        Args:
            run_id: The parent run ID.
        """
        data = _api_get('agent-runs/', params={'run': run_id})
        if isinstance(data, str):
            return data
        results = data.get('results', data) if isinstance(data, dict) else data
        summary = []
        for ar in results:
            summary.append({
                'id': ar.get('id'),
                'agent_name': ar.get('agent_name'),
                'status': ar.get('status'),
                'started_at': ar.get('started_at'),
                'completed_at': ar.get('completed_at'),
            })
        return json.dumps(summary, indent=2)

    @tool
    def get_agent_report(agent_run_id: int) -> str:
        """Get a specific agent run's full report and raw data.

        Args:
            agent_run_id: The agent run ID to retrieve.
        """
        data = _api_get(f'agent-runs/{agent_run_id}/')
        if isinstance(data, str):
            return data
        return json.dumps(data, indent=2)

    return [list_briefings, get_briefing, list_runs, get_run, list_agent_runs, get_agent_report]
