"""URL validation for SSRF protection in agent scraping tools."""
import ipaddress
import socket
from urllib.parse import urlparse

# Docker service hostnames on the fuzzyclaw_default network
BLOCKED_HOSTNAMES = {'db', 'redis', 'web', 'celery', 'celery-beat', 'localhost'}


def validate_url(url: str) -> str | None:
    """Check if a URL is safe to fetch. Returns an error message or None if safe."""
    try:
        parsed = urlparse(url)
    except Exception:
        return f"Invalid URL: {url}"

    if parsed.scheme not in ('http', 'https'):
        return f"Blocked scheme: {parsed.scheme}"

    hostname = parsed.hostname
    if not hostname:
        return "No hostname in URL"

    # Block known internal hostnames
    if hostname.lower() in BLOCKED_HOSTNAMES:
        return f"Blocked internal hostname: {hostname}"

    # Resolve hostname and check IP ranges
    try:
        addr_infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        # Can't resolve — let the actual request handle the failure
        return None

    for family, _, _, _, sockaddr in addr_infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue

        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved:
            return f"Blocked address: {hostname} resolves to {ip_str}"

    return None
