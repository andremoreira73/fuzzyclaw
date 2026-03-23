"""Web search tool using ScrapingBee Google SERP API.

Returns structured search results (title, URL, snippet) for a given query.
Requires SCRAPINGBEE_API_KEY environment variable.
"""
import json
import logging
import os

import requests
from requests.exceptions import RequestException, Timeout

logger = logging.getLogger(__name__)

SCRAPINGBEE_SERP_ENDPOINT = "https://app.scrapingbee.com/api/v1/store/google"
DEFAULT_NUM_RESULTS = 10


def search_web(query: str, num_results: int | None = None) -> str:
    """Search Google via ScrapingBee SERP API and return formatted results.

    Returns a structured text summary of search results suitable for LLM consumption.
    Falls back to an error message if the API is unavailable.
    """
    api_key = os.environ.get("SCRAPINGBEE_API_KEY")
    if not api_key:
        return "Error: SCRAPINGBEE_API_KEY not set. Web search unavailable."

    if num_results is None:
        num_results = int(os.environ.get("SEARCH_NUM_RESULTS", DEFAULT_NUM_RESULTS))

    params = {
        "api_key": api_key,
        "search": query,
        "nb_results": str(num_results),
    }

    try:
        response = requests.get(
            SCRAPINGBEE_SERP_ENDPOINT,
            params=params,
            timeout=30,
        )
    except Timeout:
        return "Error: Search request timed out."
    except RequestException as e:
        return f"Error: Search request failed: {e}"

    if response.status_code != 200:
        return f"Error: Search API returned HTTP {response.status_code}"

    try:
        data = response.json()
    except (json.JSONDecodeError, ValueError):
        return "Error: Could not parse search API response."

    organic = data.get("organic_results", [])
    if not organic:
        return f"No results found for: {query}"

    # Format results for LLM consumption
    lines = [f"Search results for: {query}\n"]
    for i, result in enumerate(organic, 1):
        title = result.get("title", "(no title)")
        url = result.get("url", "")
        snippet = result.get("description", "")
        lines.append(f"{i}. {title}")
        lines.append(f"   URL: {url}")
        if snippet:
            lines.append(f"   {snippet}")
        lines.append("")

    # Include related queries if available
    related = data.get("related_queries", [])
    if related:
        related_terms = [q.get("query", "") for q in related[:5] if q.get("query")]
        if related_terms:
            lines.append(f"Related searches: {', '.join(related_terms)}")

    return "\n".join(lines)
