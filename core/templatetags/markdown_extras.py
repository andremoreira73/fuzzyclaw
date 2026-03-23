"""Template tags for rendering markdown to HTML."""
import re

import markdown2
from django import template
from django.utils.safestring import mark_safe

register = template.Library()

# Pattern for auto-linking bare URLs
URL_PATTERN = re.compile(r'((https?://)[^\s<>\)\"]+)')


@register.filter(name='render_markdown')
def render_markdown(value):
    """Convert markdown text to safe HTML."""
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
    return mark_safe(html)
