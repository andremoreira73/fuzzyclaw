"""Career page scraping tool — domain-specific for job listings.

Ported from IM_scanner. Uses ScrapingBee for JS rendering + targeted CSS
selectors for job postings on German/English career pages.
"""
import json
import logging
import os
import re

from .web_scrape import (
    SCRAPINGBEE_DEFAULT_ENDPOINT,
    USER_AGENT,
    _extract_structured_data,
    _limit_tokens,
    _request_with_retries,
    _scrape_direct,
)

logger = logging.getLogger(__name__)

# CSS selectors targeting job posting elements (English + German)
JOB_SELECTORS = [
    # English patterns
    '[class*="job"]', '[data-testid*="job"]', '[id*="job"]',
    '[class*="position"]', '[class*="vacancy"]', '[class*="career"]',
    '[class*="opening"]', '[class*="posting"]', '[class*="listing"]',
    # German patterns
    '[class*="stelle"]', '[class*="karriere"]', '[class*="bewerbung"]',
    '[class*="vakanz"]', '[class*="angebot"]',
    # Generic listing patterns
    '[class*="results"]', '[class*="search-result"]',
    'article', '.card', '[role="listitem"]',
]

# Min text length for a job element to be considered useful
MIN_ELEMENT_LENGTH = 50


def scrape_career_page(url: str) -> str:
    """Scrape a career/jobs page and extract job-specific content.

    Uses longer JS wait time and targeted CSS selectors for job postings.
    Returns structured text with job listings or a clear "no jobs found" message.
    """
    from bs4 import BeautifulSoup

    from .url_validation import validate_url
    error = validate_url(url)
    if error:
        return f"URL blocked (SSRF protection): {error}"

    api_key = os.environ.get("SCRAPINGBEE_API_KEY")

    if api_key:
        html = _fetch_with_scrapingbee(url, api_key)
    else:
        result = _scrape_direct(url)
        html = result['content'] if result['status'] == 'OK' else None

    if not html:
        return f"Failed to fetch {url}"

    # Try structured data first (JSON-LD with JobPosting schema)
    jobs_from_jsonld = _extract_job_postings_jsonld(html)

    # Try CSS selectors for job elements
    jobs_from_selectors = _extract_with_job_selectors(html, BeautifulSoup)

    # Also get the full page text as fallback
    from .web_scrape import _clean_html
    page_text = _clean_html(html)

    # Build the response
    sections = []

    if jobs_from_jsonld:
        sections.append(f"## Structured Job Data (JSON-LD)\n\n{jobs_from_jsonld}")

    if jobs_from_selectors:
        sections.append(f"## Job Listings Found via Selectors\n\n{jobs_from_selectors}")

    if not jobs_from_jsonld and not jobs_from_selectors:
        sections.append("## No job-specific elements found via selectors.")
        if page_text:
            sections.append(f"## Full Page Text (fallback)\n\n{page_text}")

    output = f"Career page: {url}\n\n" + "\n\n".join(sections)

    max_tokens = int(os.environ.get('SCRAPE_MAX_TOKENS', 64_000))
    return _limit_tokens(output, max_tokens)


def _fetch_with_scrapingbee(url: str, api_key: str) -> str | None:
    """Fetch career page with ScrapingBee — longer wait for JS-heavy pages."""
    endpoint = os.environ.get("SCRAPINGBEE_ENDPOINT", SCRAPINGBEE_DEFAULT_ENDPOINT)
    params = {
        'api_key': api_key,
        'url': url,
        'block_ads': 'True',
        'render_js': 'True',
        'wait': '10000',  # 10s wait for SPA career pages
    }
    headers = {"User-Agent": USER_AGENT}

    response = _request_with_retries(endpoint, params, headers, timeout=40)
    if response is None:
        return None
    if response.status_code == 200 and response.text:
        return response.text
    return None


def _extract_job_postings_jsonld(html: str) -> str | None:
    """Extract JobPosting schema.org data from JSON-LD."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    scripts = soup.find_all('script', type='application/ld+json')

    job_postings = []
    for script in scripts:
        try:
            data = json.loads(script.string)
            items = data if isinstance(data, list) else [data]
            if isinstance(data, dict) and '@graph' in data:
                items = data['@graph']
            for item in items:
                if isinstance(item, dict) and item.get('@type') in ('JobPosting', 'JobListing'):
                    job_postings.append(item)
        except (json.JSONDecodeError, TypeError):
            continue

    if not job_postings:
        return None

    lines = []
    for job in job_postings:
        title = job.get('title', job.get('name', ''))
        company = ''
        org = job.get('hiringOrganization', {})
        if isinstance(org, dict):
            company = org.get('name', '')
        location = ''
        loc = job.get('jobLocation', {})
        if isinstance(loc, dict):
            addr = loc.get('address', {})
            if isinstance(addr, dict):
                location = addr.get('addressLocality', addr.get('name', ''))
        url = job.get('url', job.get('sameAs', ''))
        desc = job.get('description', '')
        if desc:
            # Strip HTML tags from description
            desc = re.sub(r'<[^>]+>', ' ', desc)
            desc = re.sub(r'\s+', ' ', desc).strip()[:300]

        lines.append(f"- **{title}**")
        if company:
            lines.append(f"  Company: {company}")
        if location:
            lines.append(f"  Location: {location}")
        if url:
            lines.append(f"  URL: {url}")
        if desc:
            lines.append(f"  Summary: {desc}")
        lines.append("")

    return f"Found {len(job_postings)} job posting(s):\n\n" + "\n".join(lines)


def _inline_links(element) -> None:
    """Replace <a href="URL">text</a> with 'text (URL)' in-place.

    This preserves link destinations when the element is later converted
    to plain text via get_text().
    """
    for a_tag in element.find_all('a', href=True):
        href = a_tag['href'].strip()
        link_text = a_tag.get_text(strip=True)
        if href and not href.startswith(('#', 'javascript:', 'mailto:')):
            if link_text:
                a_tag.replace_with(f"{link_text} ({href})")
            else:
                a_tag.replace_with(href)


def _extract_with_job_selectors(html: str, BeautifulSoup) -> str | None:
    """Extract text from elements matching job-specific CSS selectors."""
    soup = BeautifulSoup(html, 'html.parser')

    found_texts = []
    seen = set()

    for selector in JOB_SELECTORS:
        try:
            elements = soup.select(selector)
        except Exception:
            continue
        for elem in elements:
            # Inline links so URLs survive get_text()
            _inline_links(elem)
            text = elem.get_text(separator=' ', strip=True)
            if len(text) < MIN_ELEMENT_LENGTH:
                continue
            # Deduplicate
            text_key = text[:100]
            if text_key in seen:
                continue
            seen.add(text_key)
            found_texts.append(text)

    if not found_texts:
        return None

    return f"Found {len(found_texts)} job-related element(s):\n\n" + "\n\n---\n\n".join(found_texts)
