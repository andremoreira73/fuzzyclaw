"""Template tags for rendering markdown to HTML."""
import re

import bleach
import markdown2
from django import template
from django.utils.safestring import mark_safe

register = template.Library()

# Pattern for auto-linking bare URLs
URL_PATTERN = re.compile(r'((https?://)[^\s<>\)\"]+)')

ALLOWED_TAGS = [
    'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'ul', 'ol', 'li', 'a', 'strong', 'em', 'code', 'pre',
    'blockquote', 'table', 'thead', 'tbody', 'tr', 'th', 'td',
    'br', 'hr', 'img', 'div', 'span',
]

ALLOWED_ATTRS = {
    'a': ['href', 'title'],
    'img': ['src', 'alt', 'title'],
    'code': ['class'],
    'pre': ['class'],
    'td': ['align'],
    'th': ['align'],
}

ALLOWED_PROTOCOLS = ['http', 'https', 'mailto']


@register.filter(name='render_markdown')
def render_markdown(value):
    """Convert markdown text to sanitized HTML."""
    if not value:
        return ''
    html = markdown2.markdown(
        value,
        extras=[
            'fenced-code-blocks',
            'tables',
            'break-on-newline',
            'cuddled-lists',
            'link-patterns',
        ],
        link_patterns=[(URL_PATTERN, r'\1')],
    )
    clean_html = bleach.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )
    return mark_safe(clean_html)
