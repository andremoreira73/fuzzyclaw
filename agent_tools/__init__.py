"""Container-side agent tools.

This package provides tool implementations for specialist agents running
inside Docker containers. It is NOT a Django app — no Django imports allowed.

Tools are registered by name and resolved at runtime based on the agent's
`tools` frontmatter list.
"""
import logging

from langchain_core.tools import tool

from .bash import run_bash
from .career_scrape import scrape_career_page
from .memory import build_memory_tools, get_memory_store
from .web_scrape import scrape_url
from .web_search import search_web

logger = logging.getLogger(__name__)


def build_tools(tool_names: list[str]) -> list:
    """Build LangChain tools for the agent container.

    Resolves tool names from the agent's frontmatter into callable tool objects.
    Filesystem tools (ls, read_file, etc.) come from Deep Agents automatically.
    """
    available = {}

    @tool
    def bash(command: str) -> str:
        """Execute a bash command and return its output.

        Args:
            command: The shell command to execute.
        """
        return run_bash(command)

    @tool
    def web_scrape(url: str) -> str:
        """Scrape a web page and return its cleaned text content.
        Uses ScrapingBee API for JavaScript rendering and ad blocking.
        Returns cleaned text suitable for LLM analysis.

        Args:
            url: The URL to scrape.
        """
        return scrape_url(url)

    @tool
    def career_scrape(url: str) -> str:
        """Scrape a career/jobs page and extract job postings.
        Optimized for job listing pages with longer JS wait times and
        targeted CSS selectors for job elements (English and German).
        Returns structured job data when available.

        Args:
            url: The career page URL to scrape.
        """
        return scrape_career_page(url)

    available['career_scrape'] = career_scrape

    @tool
    def web_search(query: str) -> str:
        """Search Google and return structured results (titles, URLs, snippets).
        Use this to find information, news articles, company announcements, etc.

        Args:
            query: The search query (like you'd type into Google).
        """
        return search_web(query)

    available['bash'] = bash
    available['web_scrape'] = web_scrape
    available['web_search'] = web_search

    resolved = []
    for name in tool_names:
        if name in available:
            resolved.append(available[name])
        else:
            logger.warning("Unknown tool '%s' — skipping.", name)
    return resolved
