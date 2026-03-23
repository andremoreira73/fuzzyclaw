"""Filesystem-first agent & skill registry.

Reads agent definitions from .md files and skill definitions from SKILL.md
directories at runtime. No database models — the filesystem is the source of truth.
"""
import re
import time
import logging
from pathlib import Path

import yaml

from django.conf import settings

logger = logging.getLogger(__name__)


class AgentNotFound(Exception):
    pass


class SkillNotFound(Exception):
    pass


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from markdown text. Returns (frontmatter_dict, body)."""
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)', text, re.DOTALL)
    if not match:
        raise ValueError("No frontmatter found")

    frontmatter = yaml.safe_load(match.group(1)) or {}
    if not isinstance(frontmatter, dict):
        raise ValueError("Frontmatter must be a YAML mapping")

    return frontmatter, match.group(2).strip()


def parse_agent_md(filepath: Path) -> dict:
    """Parse a .md agent file into a dict."""
    text = filepath.read_text(encoding='utf-8')
    frontmatter, prompt = parse_frontmatter(text)

    name = frontmatter.get('name')
    if not name:
        raise ValueError(f"Missing 'name' in frontmatter of {filepath}")

    tools = frontmatter.get('tools', [])
    if not isinstance(tools, list):
        tools = []

    memory = bool(frontmatter.get('memory', False))

    volumes = frontmatter.get('volumes', [])
    if not isinstance(volumes, list):
        volumes = []

    return {
        'name': name,
        'description': frontmatter.get('description', ''),
        'model_choice': frontmatter.get('model', 'gpt-5-mini'),
        'tools': tools,
        'memory': memory,
        'volumes': volumes,
        'prompt': prompt,
        'path': str(filepath),
    }


def parse_skill_md(filepath: Path) -> dict:
    """Parse a SKILL.md file into a dict."""
    text = filepath.read_text(encoding='utf-8')
    frontmatter, body = parse_frontmatter(text)

    name = frontmatter.get('name')
    if not name:
        name = filepath.parent.name

    return {
        'name': name,
        'description': frontmatter.get('description', ''),
        'path': str(filepath.parent),
        'body': body,
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_agent(data: dict) -> list[str]:
    """Validate agent data against registries. Returns list of errors."""
    errors = []
    if data['model_choice'] not in settings.FUZZYCLAW_MODELS:
        allowed = ', '.join(sorted(settings.FUZZYCLAW_MODELS.keys()))
        errors.append(f"Unknown model '{data['model_choice']}'. Allowed: {allowed}")
    unknown_tools = [t for t in data['tools'] if t not in settings.FUZZYCLAW_TOOLS]
    if unknown_tools:
        allowed = ', '.join(sorted(settings.FUZZYCLAW_TOOLS.keys()))
        errors.append(f"Unknown tool(s): {', '.join(unknown_tools)}. Allowed: {allowed}")
    if not data['prompt']:
        errors.append("Agent has no prompt (empty body after frontmatter).")
    errors.extend(validate_volumes(data.get('volumes', [])))
    return errors


def validate_volumes(volumes: list) -> list[str]:
    """Validate volume mount specs from agent frontmatter.

    Checks structure only — security (allowlist/blocklist) is enforced at
    launch time in containers.py since it depends on runtime settings.
    """
    errors = []
    if not isinstance(volumes, list):
        return ["'volumes' must be a JSON list."]

    RESERVED_MOUNTS = {'/app', '/app/skills', '/app/comms'}

    for i, vol in enumerate(volumes):
        prefix = f"volumes[{i}]"
        if not isinstance(vol, dict):
            errors.append(f"{prefix}: must be an object with host, mount, mode.")
            continue

        host = vol.get('host')
        mount = vol.get('mount')
        mode = vol.get('mode')

        if not host or not isinstance(host, str):
            errors.append(f"{prefix}: 'host' is required and must be a string.")
        if not mount or not isinstance(mount, str):
            errors.append(f"{prefix}: 'mount' is required and must be a string.")
        elif not mount.startswith('/'):
            errors.append(f"{prefix}: 'mount' must be an absolute path.")
        elif mount.rstrip('/') in RESERVED_MOUNTS or any(
            mount.rstrip('/').startswith(r + '/') for r in RESERVED_MOUNTS
        ):
            errors.append(f"{prefix}: mount '{mount}' conflicts with reserved path.")

        if mode not in ('ro', 'rw'):
            errors.append(f"{prefix}: 'mode' must be 'ro' or 'rw', got '{mode}'.")

    return errors


def validate_skill(data: dict) -> list[str]:
    """Validate skill data. Returns list of errors."""
    errors = []
    if not data['name']:
        errors.append("Skill has no name.")
    return errors


# ---------------------------------------------------------------------------
# TTL cache
# ---------------------------------------------------------------------------

_cache = {}
_CACHE_TTL = 30  # seconds


def _cached(key, loader):
    """Simple TTL cache wrapper."""
    now = time.monotonic()
    entry = _cache.get(key)
    if entry and (now - entry['time']) < _CACHE_TTL:
        return entry['data']
    data = loader()
    _cache[key] = {'data': data, 'time': now}
    return data


def clear_cache():
    """Clear the registry cache (useful for tests)."""
    _cache.clear()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_available_agents(agents_dir: Path | None = None) -> list[dict]:
    """Scan agents directory and return all valid agent definitions."""
    if agents_dir is None:
        agents_dir = settings.FUZZYCLAW_AGENTS_DIR

    def _load():
        if not agents_dir.is_dir():
            return []
        agents = []
        for filepath in sorted(agents_dir.glob('*.md')):
            try:
                data = parse_agent_md(filepath)
                errors = validate_agent(data)
                if errors:
                    logger.warning("Skipping invalid agent %s: %s", filepath.name, errors)
                    continue
                agents.append(data)
            except ValueError as e:
                logger.warning("Skipping unparseable agent %s: %s", filepath.name, e)
        return agents

    # Only use cache when reading from default directory
    if agents_dir == settings.FUZZYCLAW_AGENTS_DIR:
        return _cached('agents', _load)
    return _load()


def get_agent(name: str, agents_dir: Path | None = None) -> dict:
    """Get a single agent by name. Raises AgentNotFound if not found."""
    agents = get_available_agents(agents_dir)
    for agent in agents:
        if agent['name'] == name:
            return agent
    raise AgentNotFound(f"Agent '{name}' not found.")


def get_available_skills(skills_dir: Path | None = None) -> list[dict]:
    """Scan skills directory and return all valid skill definitions."""
    if skills_dir is None:
        skills_dir = settings.FUZZYCLAW_SKILLS_DIR

    def _load():
        if not skills_dir.is_dir():
            return []
        skills = []
        for filepath in sorted(skills_dir.glob('*/SKILL.md')):
            try:
                data = parse_skill_md(filepath)
                errors = validate_skill(data)
                if errors:
                    logger.warning("Skipping invalid skill %s: %s", filepath.name, errors)
                    continue
                skills.append(data)
            except ValueError as e:
                logger.warning("Skipping unparseable skill %s: %s", filepath.name, e)
        return skills

    if skills_dir == settings.FUZZYCLAW_SKILLS_DIR:
        return _cached('skills', _load)
    return _load()


def get_skill(name: str, skills_dir: Path | None = None) -> dict:
    """Get a single skill by name. Raises SkillNotFound if not found."""
    skills = get_available_skills(skills_dir)
    for skill in skills:
        if skill['name'] == name:
            return skill
    raise SkillNotFound(f"Skill '{name}' not found.")
