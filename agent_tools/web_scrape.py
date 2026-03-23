"""Web scraping tool using ScrapingBee API.

Ported from IM_scanner with improvements for general-purpose use:
- ScrapingBee handles JS rendering, ad blocking, and anti-bot
- BeautifulSoup cleans HTML into readable text
- Token limiting prevents blowing up LLM context
- Structured data extraction (JSON-LD) when available
"""
import json
import logging
import os
import re

import requests
from bs4 import BeautifulSoup
from requests.exceptions import RequestException, Timeout

logger = logging.getLogger(__name__)

SCRAPINGBEE_DEFAULT_ENDPOINT = "https://app.scrapingbee.com/api/v1/"
MAX_TOKENS_DEFAULT = 64_000  # Safety fallback — containers.py sets SCRAPE_MAX_TOKENS from model capacity
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scrape_url(url: str) -> str:
    """Scrape a URL and return cleaned text content.

    Uses ScrapingBee if API key is available, falls back to direct requests.
    Returns cleaned text suitable for LLM consumption.
    """
    api_key = os.environ.get("SCRAPINGBEE_API_KEY")

    if api_key:
        result = _scrape_with_scrapingbee(url, api_key)
    else:
        result = _scrape_direct(url)

    if result['status'] != 'OK':
        return f"Scraping failed: {result['error_message']}"

    html = result['content']
    if not html:
        return "Scraping returned empty content."

    # Try structured data first, then clean HTML
    structured = _extract_structured_data(html)
    if structured:
        text = f"[Structured data found]\n{structured}\n\n[Page text]\n{_clean_html(html)}"
    else:
        text = _clean_html(html)

    if not text.strip():
        return "Page returned no readable text content."

    # Token-limit the output
    max_tokens = int(os.environ.get('SCRAPE_MAX_TOKENS', MAX_TOKENS_DEFAULT))
    text = _limit_tokens(text, max_tokens)

    return text


# ---------------------------------------------------------------------------
# Scraping backends
# ---------------------------------------------------------------------------

def _scrape_with_scrapingbee(url: str, api_key: str) -> dict:
    """Scrape using ScrapingBee API with JS rendering and ad blocking."""
    endpoint = os.environ.get("SCRAPINGBEE_ENDPOINT", SCRAPINGBEE_DEFAULT_ENDPOINT)

    params = {
        'api_key': api_key,
        'url': url,
        'block_ads': 'True',
        'render_js': 'True',
        'wait': '5000',
    }
    headers = {"User-Agent": USER_AGENT}

    response = _request_with_retries(endpoint, params, headers)
    if response is None:
        return {'status': 'ERROR', 'content': '', 'error_message': 'Max retries reached'}

    if response.status_code == 200:
        content = response.text
        if not content:
            return {'status': 'ERROR', 'content': '', 'error_message': 'Empty response'}
        return {'status': 'OK', 'content': content, 'error_message': ''}

    return {'status': 'ERROR', 'content': '', 'error_message': f'HTTP {response.status_code}'}


def _scrape_direct(url: str) -> dict:
    """Fallback: scrape directly without ScrapingBee (no JS rendering)."""
    headers = {"User-Agent": USER_AGENT}
    try:
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 200:
            return {'status': 'OK', 'content': response.text, 'error_message': ''}
        return {'status': 'ERROR', 'content': '', 'error_message': f'HTTP {response.status_code}'}
    except Exception as e:
        return {'status': 'ERROR', 'content': '', 'error_message': str(e)}


def _request_with_retries(
    endpoint: str,
    params: dict,
    headers: dict,
    max_retries: int = 3,
    timeout: int = 30,
) -> requests.Response | None:
    """Make an HTTP request with retry logic."""
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(
                endpoint, params=params, headers=headers, timeout=timeout,
            )
            return response
        except Timeout:
            logger.warning("Request timed out (attempt %d/%d)", attempt, max_retries)
        except RequestException as e:
            logger.warning("Request failed: %s (attempt %d/%d)", e, attempt, max_retries)
    return None


# ---------------------------------------------------------------------------
# HTML → clean text
# ---------------------------------------------------------------------------

# Tags that contain non-content noise
REMOVE_TAGS = {
    'script', 'style', 'noscript', 'iframe', 'svg', 'canvas',
    'header', 'footer', 'nav', 'aside',
    'form', 'input', 'button', 'select', 'textarea',
}

# Common CSS class/id patterns for non-content elements
NOISE_PATTERNS = re.compile(
    r'cookie|consent|gdpr|privacy-banner|popup|modal|overlay|'
    r'newsletter|subscribe|signup|social-share|share-bar|'
    r'advertisement|ad-|ads-|advert|sponsor|'
    r'sidebar|widget|related-posts|recommended|'
    r'breadcrumb|pagination|pager',
    re.IGNORECASE,
)


def _clean_html(html: str) -> str:
    """Convert raw HTML to clean, readable text.

    Removes navigation, ads, cookie banners, scripts, styles, and other
    non-content noise. Preserves headings, paragraphs, lists, and tables.
    """
    soup = BeautifulSoup(html, 'html.parser')

    # Remove elements that are always noise
    for tag_name in REMOVE_TAGS:
        for element in soup.find_all(tag_name):
            element.decompose()

    # Remove elements with noisy class/id patterns
    for element in soup.find_all(True):
        if element.attrs is None:
            continue
        classes = ' '.join(element.get('class', []))
        elem_id = element.get('id', '')
        if NOISE_PATTERNS.search(classes) or NOISE_PATTERNS.search(elem_id):
            element.decompose()

    # Remove hidden elements
    for element in soup.find_all(style=re.compile(r'display\s*:\s*none')):
        element.decompose()

    # Inline links so URLs survive get_text()
    for a_tag in soup.find_all('a', href=True):
        href = a_tag['href'].strip()
        link_text = a_tag.get_text(strip=True)
        if href and not href.startswith(('#', 'javascript:', 'mailto:')):
            if link_text:
                a_tag.replace_with(f"{link_text} ({href})")
            else:
                a_tag.replace_with(href)

    # Extract text with structure
    lines = []
    for element in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                                   'p', 'li', 'td', 'th', 'blockquote',
                                   'pre', 'code', 'figcaption', 'dt', 'dd']):
        text = element.get_text(separator=' ', strip=True)
        if not text or len(text) < 3:
            continue

        # Add heading markers for structure
        if element.name in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            level = int(element.name[1])
            text = f"{'#' * level} {text}"
        elif element.name == 'li':
            text = f"- {text}"

        lines.append(text)

    # Deduplicate consecutive identical lines (common in messy HTML)
    deduped = []
    for line in lines:
        if not deduped or line != deduped[-1]:
            deduped.append(line)

    # Collapse excessive blank lines
    result = '\n'.join(deduped)
    result = re.sub(r'\n{3,}', '\n\n', result)

    return result.strip()


# ---------------------------------------------------------------------------
# Structured data extraction
# ---------------------------------------------------------------------------

def _extract_structured_data(html: str) -> str | None:
    """Extract JSON-LD structured data from the page if present.

    Useful for job postings, articles, products, etc. that use schema.org markup.
    """
    soup = BeautifulSoup(html, 'html.parser')
    scripts = soup.find_all('script', type='application/ld+json')

    structured_items = []
    for script in scripts:
        try:
            data = json.loads(script.string)
            if isinstance(data, list):
                structured_items.extend(data)
            elif isinstance(data, dict):
                # Unwrap @graph arrays
                if '@graph' in data:
                    structured_items.extend(data['@graph'])
                else:
                    structured_items.append(data)
        except (json.JSONDecodeError, TypeError):
            continue

    if not structured_items:
        return None

    # Format as readable text
    return json.dumps(structured_items, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Token limiting
# ---------------------------------------------------------------------------

def _limit_tokens(text: str, max_tokens: int) -> str:
    """Limit text to approximately max_tokens.

    Uses tiktoken if available, otherwise falls back to word-based estimation.
    Truncates to 80% of max to leave room for the agent's own output.
    """
    target = int(max_tokens * 0.8)

    try:
        import tiktoken
        try:
            encoding = tiktoken.encoding_for_model("gpt-4o")
        except KeyError:
            encoding = tiktoken.get_encoding("cl100k_base")

        tokens = encoding.encode(text)
        if len(tokens) <= max_tokens:
            return text

        truncated = encoding.decode(tokens[:target])
        return truncated + "\n\n[CONTENT TRUNCATED — original was ~{} tokens]".format(len(tokens))

    except ImportError:
        # Fallback: rough estimate of 1 token ≈ 4 chars
        char_limit = target * 4
        if len(text) <= max_tokens * 4:
            return text
        return text[:char_limit] + "\n\n[CONTENT TRUNCATED]"
