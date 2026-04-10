"""Container orchestration — image builder and agent dispatcher.

Two-phase design:
  Phase A: sync_agent_images() — admin-triggered, pre-builds Docker images
  Phase B: start_agent_container() — non-blocking dispatch, returns immediately

Async flow:
  1. start_agent_container() launches container, returns container_id
  2. Container writes report.json + signals Redis Stream on completion
  3. Coordinator polls via check_reports / read_agent_report
  4. cleanup_run() removes containers, comms dirs, Redis stream
"""
import hashlib
import io
import json
import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path

import docker
import redis as redis_lib
from django.conf import settings
from django.utils import timezone

from .models import AgentImage
from .registry import get_agent, get_available_agents, parse_agent_md, validate_volumes

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_docker_client():
    """Get a Docker client from the environment."""
    return docker.from_env()


def compute_file_hash(*paths: Path) -> str:
    """Compute a combined SHA-256 hash of one or more files."""
    hasher = hashlib.sha256()
    for path in sorted(paths):
        if path.is_file():
            hasher.update(path.read_bytes())
    return hasher.hexdigest()


def image_tag_for_agent(agent_name: str) -> str:
    """Generate the Docker image tag for an agent."""
    prefix = settings.FUZZYCLAW_AGENT_IMAGE_PREFIX
    return f"{prefix}-{agent_name}:latest"


def _collect_all_skill_requirements() -> list[Path]:
    """Collect requirements.txt files from all skills."""
    skills_dir = settings.FUZZYCLAW_SKILLS_DIR
    req_files = []
    if skills_dir.is_dir():
        for skill_dir in sorted(skills_dir.iterdir()):
            req_path = skill_dir / 'requirements.txt'
            if req_path.is_file():
                req_files.append(req_path)
    return req_files


def _hash_paths_for_agent(agent_path: Path) -> str:
    """Compute the combined hash for an agent file and all skill deps."""
    paths = [agent_path] + _collect_all_skill_requirements()
    return compute_file_hash(*paths)


def _to_host_path(container_path: Path) -> str:
    """Translate a container-internal path to the host path.

    The Celery worker runs inside a container where the project is at /app,
    but Docker volume mounts need HOST paths (since Docker daemon runs on the
    host). FUZZYCLAW_HOST_PROJECT_DIR provides the host-side project root.
    """
    host_root = getattr(settings, 'FUZZYCLAW_HOST_PROJECT_DIR', str(settings.BASE_DIR))
    base_dir = str(settings.BASE_DIR)
    container_str = str(container_path)
    if container_str.startswith(base_dir):
        relative = container_str[len(base_dir):]
        return host_root + relative
    return container_str


def _resolve_volume_host_path(host_path: str) -> str:
    """Resolve a volume host path from agent frontmatter.

    Paths starting with './' are resolved relative to HOST_PROJECT_DIR.
    Uses realpath to resolve symlinks before security validation.
    """
    if host_path.startswith('./') or host_path.startswith('../'):
        host_root = getattr(settings, 'FUZZYCLAW_HOST_PROJECT_DIR', str(settings.BASE_DIR))
        return os.path.realpath(os.path.join(host_root, host_path))
    return os.path.realpath(host_path)


def _validate_volume_mount(vol: dict) -> None:
    """Validate a single volume mount against security allowlist/blocklist.

    Raises RuntimeError if the mount is not permitted.
    """
    host_path = _resolve_volume_host_path(vol['host'])

    # Check blocklist (startswith match, but '/' only blocks root exactly)
    # Resolve blocklist entries too so symlinks match on both sides
    blocklist = getattr(settings, 'FUZZYCLAW_VOLUME_BLOCKLIST', [])
    for blocked in blocklist:
        blocked_normalized = os.path.realpath(blocked).rstrip('/')
        if blocked_normalized == '':
            # '/' entry — only block mounting root itself
            if host_path == '/':
                raise RuntimeError(
                    f"Volume mount blocked: '{host_path}' matches blocklist entry '{blocked}'."
                )
        elif host_path == blocked_normalized or host_path.startswith(blocked_normalized + '/'):
            raise RuntimeError(
                f"Volume mount blocked: '{host_path}' matches blocklist entry '{blocked}'."
            )

    # Check allowlist — empty means everything allowed (minus blocklist),
    # non-empty means only those paths (minus blocklist).
    # Resolve allowlist entries too
    allowlist = getattr(settings, 'FUZZYCLAW_VOLUME_ALLOWLIST', [])
    if allowlist:
        allowed = False
        for allow_path in allowlist:
            allow_normalized = os.path.realpath(allow_path).rstrip('/')
            if host_path == allow_normalized or host_path.startswith(allow_normalized + '/'):
                allowed = True
                break
        if not allowed:
            raise RuntimeError(
                f"Volume mount denied: '{host_path}' is not under any allowlisted path."
            )


# ---------------------------------------------------------------------------
# Phase A: Image Builder
# ---------------------------------------------------------------------------

def _hash_base_image_inputs() -> str:
    """Compute hash of all files baked into the base agent image."""
    base_dir = settings.BASE_DIR
    paths = [
        base_dir / 'Dockerfile.agent',
        base_dir / 'requirements-agent.txt',
        base_dir / 'agent_runner.py',
    ]
    agent_tools_dir = base_dir / 'agent_tools'
    if agent_tools_dir.is_dir():
        for py_file in sorted(agent_tools_dir.rglob('*.py')):
            paths.append(py_file)
    existing = [p for p in paths if p.is_file()]
    if not existing:
        return 'empty'
    return compute_file_hash(*existing)


def _ensure_base_image(client, force_rebuild=False):
    """Build the base agent image if it doesn't exist or inputs changed."""
    base_tag = f"{settings.FUZZYCLAW_AGENT_IMAGE_PREFIX}-base:latest"
    current_hash = _hash_base_image_inputs()

    if not force_rebuild:
        try:
            image = client.images.get(base_tag)
            stored_hash = image.labels.get('fuzzyclaw.base_hash', '')
            if stored_hash == current_hash:
                logger.info("Base image %s up to date (hash match)", base_tag)
                return base_tag, False
            logger.info("Base image %s exists but inputs changed, rebuilding", base_tag)
        except docker.errors.ImageNotFound:
            pass

    logger.info("Building base image %s ...", base_tag)
    image, build_log = client.images.build(
        path=str(settings.BASE_DIR),
        dockerfile=str(settings.BASE_DIR / 'Dockerfile.agent'),
        tag=base_tag,
        labels={'fuzzyclaw.base_hash': current_hash},
        rm=True,
    )
    for chunk in build_log:
        if 'stream' in chunk:
            logger.debug(chunk['stream'].strip())
    logger.info("Base image %s built successfully", base_tag)
    return base_tag, True


def _build_agent_image(client, agent_def: dict, base_tag: str) -> str:
    """Build a per-agent image layered on the base.

    Thin layer: copies agent .md + installs skill deps.
    """
    agent_name = agent_def['name']
    agent_tag = image_tag_for_agent(agent_name)
    agent_path = Path(agent_def['path'])

    # Build a minimal Dockerfile in memory
    lines = [f"FROM {base_tag}"]

    # Collect all skill requirements (every agent gets all skills)
    skill_req_files = _collect_all_skill_requirements()
    if skill_req_files:
        lines.append("USER root")
        # We'll concatenate all skill requirements and install them
        lines.append("COPY skill_requirements.txt /tmp/skill_requirements.txt")
        lines.append("RUN pip install --no-cache-dir -r /tmp/skill_requirements.txt && rm /tmp/skill_requirements.txt")
        lines.append("USER appuser")

    lines.append("COPY agent.md /app/agent.md")
    dockerfile_content = "\n".join(lines) + "\n"

    # Create a temporary build context
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        # Write the inline Dockerfile
        (tmpdir_path / 'Dockerfile').write_text(dockerfile_content)

        # Copy agent .md
        shutil.copy2(agent_path, tmpdir_path / 'agent.md')

        # Concatenate skill requirements if any
        if skill_req_files:
            combined_reqs = []
            for req_file in skill_req_files:
                combined_reqs.append(req_file.read_text(encoding='utf-8'))
            (tmpdir_path / 'skill_requirements.txt').write_text('\n'.join(combined_reqs))

        logger.info("Building agent image %s ...", agent_tag)
        image, build_log = client.images.build(
            path=str(tmpdir_path),
            tag=agent_tag,
            rm=True,
        )
        for chunk in build_log:
            if 'stream' in chunk:
                logger.debug(chunk['stream'].strip())

    logger.info("Agent image %s built successfully", agent_tag)
    return agent_tag


def sync_agent_images(
    agents_dir: Path | None = None,
    force_base: bool = False,
    force_all: bool = False,
) -> dict:
    """Scan agents/ dir, build/rebuild/remove images as needed.

    Args:
        force_base: Rebuild the base image even if it exists.
        force_all: Rebuild base + all per-agent images.

    Returns: {built: [], removed: [], unchanged: [], errors: []}
    """
    if agents_dir is None:
        agents_dir = settings.FUZZYCLAW_AGENTS_DIR

    result = {'built': [], 'removed': [], 'unchanged': [], 'errors': []}

    client = get_docker_client()

    # Ensure base image exists — returns (tag, was_rebuilt)
    try:
        base_tag, base_rebuilt = _ensure_base_image(client, force_rebuild=force_base or force_all)
        if base_rebuilt and not force_all:
            logger.info("Base image was rebuilt — forcing rebuild of all agent images")
            force_all = True
    except Exception as e:
        logger.error("Failed to build base image: %s", e)
        result['errors'].append({'agent': '_base', 'error': str(e)})
        return result

    # Scan agents
    agents = get_available_agents(agents_dir)
    current_agent_names = set()

    for agent_def in agents:
        agent_name = agent_def['name']
        current_agent_names.add(agent_name)
        agent_path = Path(agent_def['path'])
        file_hash = _hash_paths_for_agent(agent_path)
        agent_tag = image_tag_for_agent(agent_name)

        # Check if image exists and is current
        if not force_all:
            try:
                existing = AgentImage.objects.get(agent_name=agent_name)
                if existing.file_hash == file_hash and not existing.build_error:
                    result['unchanged'].append(agent_name)
                    continue
            except AgentImage.DoesNotExist:
                pass

        # Build or rebuild
        try:
            _build_agent_image(client, agent_def, base_tag)
            AgentImage.objects.update_or_create(
                agent_name=agent_name,
                defaults={
                    'file_hash': file_hash,
                    'image_tag': agent_tag,
                    'build_error': '',
                },
            )
            result['built'].append(agent_name)
        except Exception as e:
            logger.error("Failed to build image for %s: %s", agent_name, e)
            AgentImage.objects.update_or_create(
                agent_name=agent_name,
                defaults={
                    'file_hash': file_hash,
                    'image_tag': agent_tag,
                    'build_error': str(e),
                },
            )
            result['errors'].append({'agent': agent_name, 'error': str(e)})

    # Remove images for deleted agents
    stale = AgentImage.objects.exclude(agent_name__in=current_agent_names)
    for img in stale:
        try:
            client.images.remove(img.image_tag, force=True)
        except docker.errors.ImageNotFound:
            pass
        except Exception as e:
            logger.warning("Failed to remove image %s: %s", img.image_tag, e)
        img.delete()
        result['removed'].append(img.agent_name)

    return result


# ---------------------------------------------------------------------------
# Phase B: Dispatcher (non-blocking)
# ---------------------------------------------------------------------------

def _get_env_key_for_model(model_name: str) -> str | None:
    """Return the environment variable name for the model's API key."""
    model_cfg = settings.FUZZYCLAW_MODELS.get(model_name)
    if model_cfg:
        return model_cfg.get('env_key')
    return None


def _count_running_containers(client) -> int:
    """Count currently running fuzzyclaw agent containers."""
    prefix = settings.FUZZYCLAW_AGENT_IMAGE_PREFIX
    containers = client.containers.list(
        filters={'name': prefix, 'status': 'running'}
    )
    return len(containers)


# In-process container counter — immune to TOCTOU race with Docker daemon.
# _count_running_containers queries Docker but the daemon lags behind
# container.run() calls, so rapid-fire dispatches all see count=0.
# This lock+counter is the authoritative gate.
_container_lock = threading.Lock()
_container_count = 0


def _get_redis_client():
    """Get a Redis client for streams (db=1). Returns None if unavailable."""
    redis_url = getattr(settings, 'FUZZYCLAW_REDIS_URL', '')
    if not redis_url:
        return None
    try:
        r = redis_lib.from_url(redis_url)
        r.ping()
        return r
    except Exception as e:
        logger.warning("Redis unavailable for streams: %s", e)
        return None


def start_agent_container(
    agent_name: str,
    task_description: str,
    agent_run_id: int,
    run_id: int,
) -> str:
    """Launch a specialist agent in a Docker container (non-blocking).

    Returns the container ID immediately. Does NOT wait for completion or
    read the report — use read_agent_report() for that.

    Thread-safe — multiple calls can run concurrently (parallel dispatch).
    """
    # Check image exists
    try:
        agent_image = AgentImage.objects.get(agent_name=agent_name)
    except AgentImage.DoesNotExist:
        raise RuntimeError(f"No image built for agent '{agent_name}'. Run `manage.py sync_images`.")

    if agent_image.build_error:
        raise RuntimeError(
            f"Image for '{agent_name}' has build error: {agent_image.build_error}"
        )

    # Acquire slot via in-process counter (race-free)
    global _container_count
    max_containers = getattr(settings, 'FUZZYCLAW_MAX_CONTAINERS', 10)
    with _container_lock:
        if _container_count >= max_containers:
            raise RuntimeError(
                f"Container concurrency limit reached ({_container_count}/{max_containers}). "
                "Try again later."
            )
        _container_count += 1

    try:
        return _start_agent_container_inner(agent_name, task_description, agent_run_id, run_id, agent_image)
    except Exception:
        # Roll back the slot on any failure before containers.run succeeds
        with _container_lock:
            _container_count = max(0, _container_count - 1)
        raise


def _start_agent_container_inner(agent_name, task_description, agent_run_id, run_id, agent_image):
    """Inner implementation of start_agent_container after slot acquisition."""
    # Each call gets its own Docker client (thread-safe)
    client = get_docker_client()

    # Create comms dir — group-writable so container's appuser can write
    comms_base = settings.BASE_DIR / 'comms'
    comms_dir = comms_base / str(agent_run_id)
    comms_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(comms_dir, 0o775)

    # Determine which API key to pass
    agent_def = get_agent(agent_name)
    model_name = agent_def['model_choice']
    env_key = _get_env_key_for_model(model_name)

    env = {
        'TASK_DESCRIPTION': task_description,
        'AGENT_FILE': '/app/agent.md',
        'COMMS_DIR': '/app/comms',
        'SKILLS_DIR': '/app/skills',
        # Redis signaling env vars for agent_runner.py
        'REDIS_URL': getattr(settings, 'FUZZYCLAW_REDIS_URL', ''),
        'RUN_ID': str(run_id),
        'AGENT_RUN_ID': str(agent_run_id),
        # Message board identity
        'SELF_ID': f"{agent_name}_{agent_run_id}",
        'FUZZYCLAW_HITL_TIMEOUT': str(getattr(settings, 'FUZZYCLAW_HITL_TIMEOUT', 1800)),
        'FUZZYCLAW_AGENT_TIMEOUT': str(getattr(settings, 'FUZZYCLAW_AGENT_TIMEOUT', 600)),
    }

    # Pass model defaults so specialist get_model() matches coordinator behavior
    model_cfg = settings.FUZZYCLAW_MODELS.get(model_name, {})
    model_defaults = model_cfg.get('defaults', {})
    if model_defaults:
        env['MODEL_DEFAULTS'] = json.dumps(model_defaults)

    # Pass only the needed API key
    if env_key:
        key_value = os.environ.get(env_key, '')
        if key_value:
            env[env_key] = key_value

    # LangSmith tracing (optional — pass through if set)
    for langsmith_var in (
        'LANGCHAIN_TRACING_V2', 'LANGCHAIN_API_KEY',
        'LANGCHAIN_PROJECT', 'LANGSMITH_ENDPOINT',
    ):
        val = os.environ.get(langsmith_var, '')
        if val:
            env[langsmith_var] = val

    # ScrapingBee keys (for web_scrape and web_search tools)
    agent_tools = agent_def.get('tools', [])
    if set(agent_tools) & {'web_scrape', 'web_search', 'career_scrape'}:
        for scrape_var in ('SCRAPINGBEE_API_KEY', 'SCRAPINGBEE_ENDPOINT'):
            val = os.environ.get(scrape_var, '')
            if val:
                env[scrape_var] = val
        # Derive scrape token limit from model's input capacity (80%)
        model_cfg = settings.FUZZYCLAW_MODELS.get(model_name, {})
        max_input = model_cfg.get('max_input_tokens', 80_000)
        env['SCRAPE_MAX_TOKENS'] = str(int(max_input * 0.8))

    # Optional persistent memory
    has_memory = agent_def.get('memory', False)
    if isinstance(has_memory, str):
        has_memory = has_memory.lower() in ('true', '1', 'yes')

    if has_memory:
        database_url = os.environ.get('DATABASE_URL', '')
        if database_url:
            env['DATABASE_URL'] = database_url

    # Volumes — must use HOST paths since Docker daemon runs on the host
    host_skills = _to_host_path(settings.FUZZYCLAW_SKILLS_DIR)
    host_comms = _to_host_path(comms_dir)
    volumes = {
        host_skills: {'bind': '/app/skills', 'mode': 'ro'},
        host_comms: {'bind': '/app/comms', 'mode': 'rw'},
    }

    # Custom volume mounts from agent frontmatter
    custom_volumes = agent_def.get('volumes', [])
    agent_volumes_info = []  # For AGENT_VOLUMES env var
    if custom_volumes:
        # Structural validation was done at parse time; security check here
        for vol in custom_volumes:
            _validate_volume_mount(vol)
            resolved_host = _resolve_volume_host_path(vol['host'])
            mount_path = vol['mount']
            mode = vol['mode']
            volumes[resolved_host] = {'bind': mount_path, 'mode': mode}
            agent_volumes_info.append({
                'mount': mount_path,
                'mode': mode,
            })

    if agent_volumes_info:
        env['AGENT_VOLUMES'] = json.dumps(agent_volumes_info)

    # Container name
    container_name = f"{settings.FUZZYCLAW_AGENT_IMAGE_PREFIX}-{agent_name}-run{agent_run_id}"

    # Resource limits
    mem_limit = getattr(settings, 'FUZZYCLAW_AGENT_MEM_LIMIT', '512m')
    cpu_limit = getattr(settings, 'FUZZYCLAW_AGENT_CPU_LIMIT', 0.5)
    network = getattr(settings, 'FUZZYCLAW_DOCKER_NETWORK', 'fuzzyclaw_default')

    logger.info(
        "Starting container %s (image: %s) for agent_run %d",
        container_name, agent_image.image_tag, agent_run_id,
    )

    container = client.containers.run(
        agent_image.image_tag,
        detach=True,
        name=container_name,
        environment=env,
        volumes=volumes,
        network=network,
        mem_limit=mem_limit,
        nano_cpus=int(cpu_limit * 1e9),
    )

    return container.id


def read_agent_report(agent_run_id: int) -> tuple[dict, int]:
    """Read a specialist's report from the comms directory.

    Returns (report_dict, exit_code). Checks report.json, error.json, then
    falls back to container logs.

    Does NOT clean up — that's handled by cleanup_run().
    """
    comms_dir = settings.BASE_DIR / 'comms' / str(agent_run_id)
    report_path = comms_dir / 'report.json'
    error_path = comms_dir / 'error.json'

    report = None

    if report_path.is_file():
        try:
            with open(report_path, encoding='utf-8') as f:
                report = json.load(f)
            return report, 0
        except Exception as e:
            logger.error("Failed to read report.json for agent_run %d: %s", agent_run_id, e)

    if error_path.is_file():
        try:
            with open(error_path, encoding='utf-8') as f:
                report = json.load(f)
            return report, 1
        except Exception as e:
            logger.error("Failed to read error.json for agent_run %d: %s", agent_run_id, e)

    # No report file — try container logs as last resort
    from .models import AgentRun
    try:
        agent_run = AgentRun.objects.get(pk=agent_run_id)
        if agent_run.container_id:
            client = get_docker_client()
            try:
                container = client.containers.get(agent_run.container_id)
                logs = container.logs(tail=200).decode('utf-8', errors='replace')
                return {'status': 'failed', 'error': f'No report file. Container logs:\n{logs}'}, -1
            except docker.errors.NotFound:
                pass
    except AgentRun.DoesNotExist:
        pass

    return {'status': 'failed', 'error': 'No report file found and container not available.'}, -1


def get_container_status(agent_run_id: int, agent_name: str) -> str:
    """Check Docker container status for an agent run.

    Returns 'running', 'exited', or 'removed'.
    """
    container_name = f"{settings.FUZZYCLAW_AGENT_IMAGE_PREFIX}-{agent_name}-run{agent_run_id}"
    try:
        client = get_docker_client()
        container = client.containers.get(container_name)
        return container.status  # 'running', 'exited', 'created', etc.
    except docker.errors.NotFound:
        return 'removed'
    except Exception as e:
        logger.warning("Failed to check container status for %s: %s", container_name, e)
        return 'removed'


def cleanup_run(run_id: int) -> dict:
    """Clean up all resources for a run: containers, comms dirs, Redis stream.

    Also releases container slots from the in-process concurrency counter.

    Returns {'containers_removed': int, 'comms_removed': int, 'stream_deleted': bool}.
    """
    from .models import AgentRun

    result = {'containers_removed': 0, 'comms_removed': 0, 'stream_deleted': False}

    agent_runs = AgentRun.objects.filter(run_id=run_id)

    # Release any remaining container slots (e.g. agents that were never read)
    unread_count = agent_runs.filter(status='running').count()
    if unread_count:
        global _container_count
        with _container_lock:
            _container_count = max(0, _container_count - unread_count)

    # Remove Docker containers
    try:
        client = get_docker_client()
    except Exception as e:
        logger.warning("Cannot connect to Docker for cleanup: %s", e)
        client = None

    comms_base = settings.BASE_DIR / 'comms'

    for ar in agent_runs:
        # Remove container
        if client and ar.container_id:
            try:
                container = client.containers.get(ar.container_id)
                container.remove(force=True)
                result['containers_removed'] += 1
            except docker.errors.NotFound:
                pass
            except Exception as e:
                logger.warning("Failed to remove container %s: %s", ar.container_id, e)

        # Also try by name pattern (in case container_id wasn't stored)
        if client:
            container_name = f"{settings.FUZZYCLAW_AGENT_IMAGE_PREFIX}-{ar.agent_name}-run{ar.id}"
            try:
                container = client.containers.get(container_name)
                container.remove(force=True)
                result['containers_removed'] += 1
            except docker.errors.NotFound:
                pass
            except Exception as e:
                pass  # Already tried by ID

        # Remove comms dir
        comms_dir = comms_base / str(ar.id)
        if comms_dir.is_dir():
            try:
                shutil.rmtree(comms_dir)
                result['comms_removed'] += 1
            except Exception as e:
                logger.warning("Failed to cleanup comms dir %s: %s", comms_dir, e)

    # Delete Redis streams (completion + board)
    try:
        r = _get_redis_client()
        if r:
            stream_key = f"fuzzyclaw:run:{run_id}:done"
            board_key = f"fuzzyclaw:board:{run_id}"
            participants_key = f"fuzzyclaw:board:{run_id}:participants"
            r.delete(stream_key, board_key, participants_key)
            result['stream_deleted'] = True
    except Exception as e:
        logger.warning("Failed to delete Redis streams for run %d: %s", run_id, e)

    logger.info("Cleanup for run %d: %s", run_id, result)
    return result
