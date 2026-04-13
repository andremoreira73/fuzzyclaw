"""Dashboard views for FuzzyClaw web UI."""
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import redis as redis_lib
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import PasswordChangeDoneView, PasswordChangeView
from django.core.exceptions import SuspiciousFileOperation
from django.http import FileResponse, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.utils import timezone as dj_timezone
from django.views.decorators.http import require_POST

from .forms import ProfileForm
from .models import AgentImage, AgentRun, Briefing, Run
from .scheduling import get_schedule_status, sync_schedule

logger = logging.getLogger(__name__)
from .registry import get_available_agents, get_available_skills


def _valid_model(model_name: str) -> bool:
    """Check if model_name is a registered coordinator model."""
    return model_name in settings.FUZZYCLAW_MODELS


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@login_required
def dashboard(request):
    user_briefings = Briefing.objects.filter(owner=request.user)
    recent_runs = (
        Run.objects.filter(briefing__owner=request.user)
        .select_related('briefing')
        .order_by('-created_at')[:8]
    )
    active_count = user_briefings.filter(is_active=True).count()
    context = {
        'briefing_count': user_briefings.count(),
        'active_briefing_count': active_count,
        'briefing_sublabel': f"{active_count} active",
        'agent_count': len(get_available_agents()),
        'skill_count': len(get_available_skills()),
        'recent_runs': recent_runs,
        'total_agent_runs': AgentRun.objects.filter(run__briefing__owner=request.user).count(),
    }
    return render(request, 'core/dashboard.html', context)


# ---------------------------------------------------------------------------
# Briefings
# ---------------------------------------------------------------------------

@login_required
def briefing_list(request):
    briefings = Briefing.objects.filter(owner=request.user)
    context = {
        'nav': 'briefings',
        'briefings': briefings,
        'active_count': briefings.filter(is_active=True).count(),
        'breadcrumbs': [{'label': 'Briefings'}],
    }
    return render(request, 'core/briefing_list.html', context)


@login_required
def briefing_create(request):
    if request.method == 'POST':
        model_choice = request.POST.get('coordinator_model', 'gemini-2.5-pro')
        if not _valid_model(model_choice):
            messages.error(request, f'Unknown model: {model_choice}')
            return redirect('core:briefing_create')
        briefing = Briefing.objects.create(
            owner=request.user,
            title=request.POST.get('title', 'Untitled Briefing'),
            content=request.POST.get('content', ''),
            coordinator_model=model_choice,
            schedule_text=request.POST.get('schedule_text', ''),
            is_active=request.POST.get('is_active') == 'on',
        )
        messages.success(request, f'Briefing "{briefing.title}" created.')

        if 'launch' in request.POST:
            from .tasks import launch_run
            run = Run.objects.create(
                briefing=briefing, status='pending', triggered_by='manual',
            )
            launch_run(run)
            messages.info(request, f'Run #{run.id} launched.')
            return redirect('core:run_detail', pk=run.pk)

        return redirect('core:briefing_detail', pk=briefing.pk)

    context = {
        'nav': 'briefings',
        'model_choices': settings.FUZZYCLAW_MODEL_CHOICES,
        'breadcrumbs': [
            {'label': 'Briefings', 'url': reverse('core:briefing_list')},
            {'label': 'New'},
        ],
    }
    return render(request, 'core/briefing_form.html', context)


@login_required
def briefing_detail(request, pk):
    briefing = get_object_or_404(Briefing, pk=pk, owner=request.user)
    runs = briefing.runs.order_by('-created_at')[:20]
    context = {
        'nav': 'briefings',
        'briefing': briefing,
        'runs': runs,
        'model_choices': settings.FUZZYCLAW_MODEL_CHOICES,
        'toggle_url': reverse('core:briefing_toggle', args=[briefing.pk]),
        'model_url': reverse('core:briefing_model', args=[briefing.pk]),
        'schedule_status': get_schedule_status(briefing),
        'breadcrumbs': [
            {'label': 'Briefings', 'url': reverse('core:briefing_list')},
            {'label': briefing.title},
        ],
    }
    return render(request, 'core/briefing_detail.html', context)


@login_required
@require_POST
def briefing_save(request, pk):
    briefing = get_object_or_404(Briefing, pk=pk, owner=request.user)
    briefing.title = request.POST.get('title', briefing.title)
    briefing.content = request.POST.get('content', briefing.content)
    model_choice = request.POST.get('coordinator_model', briefing.coordinator_model)
    if model_choice and _valid_model(model_choice):
        briefing.coordinator_model = model_choice
    briefing.schedule_text = request.POST.get('schedule_text', briefing.schedule_text)
    briefing.save()
    messages.success(request, 'Briefing saved.')

    if 'launch' in request.POST:
        from .tasks import launch_run
        run = Run.objects.create(
            briefing=briefing, status='pending', triggered_by='manual',
        )
        launch_run(run)
        messages.info(request, f'Run #{run.id} launched.')
        return redirect('core:run_detail', pk=run.pk)

    return redirect('core:briefing_detail', pk=briefing.pk)


@login_required
@require_POST
def briefing_toggle(request, pk):
    """HTMX endpoint: flip is_active and return the updated toggle partial."""
    briefing = get_object_or_404(Briefing, pk=pk, owner=request.user)
    briefing.is_active = not briefing.is_active
    # Also save form fields if included from the page
    update_fields = ['is_active']
    schedule_text = request.POST.get('schedule_text')
    if schedule_text is not None:
        briefing.schedule_text = schedule_text.strip()
        update_fields.append('schedule_text')
    coordinator_model = request.POST.get('coordinator_model')
    if coordinator_model is not None and _valid_model(coordinator_model):
        briefing.coordinator_model = coordinator_model
        update_fields.append('coordinator_model')
    briefing.save(update_fields=update_fields)
    # Sync schedule: pause/resume the PeriodicTask based on is_active
    try:
        sync_schedule(briefing)
    except Exception:
        logger.exception("Failed to sync schedule for briefing %d", briefing.pk)
    html = render_to_string('core/partials/toggle.html', {
        'name': 'is_active',
        'checked': briefing.is_active,
        'label': 'Active',
        'hint': "Inactive briefings won't run on schedule.",
        'toggle_url': reverse('core:briefing_toggle', args=[briefing.pk]),
    }, request=request)
    return HttpResponse(html)


@login_required
@require_POST
def briefing_model(request, pk):
    """HTMX endpoint: save coordinator_model and return the updated select partial."""
    briefing = get_object_or_404(Briefing, pk=pk, owner=request.user)
    coordinator_model = request.POST.get('coordinator_model', briefing.coordinator_model)
    if coordinator_model != briefing.coordinator_model and _valid_model(coordinator_model):
        briefing.coordinator_model = coordinator_model
        briefing.save(update_fields=['coordinator_model'])
    html = render_to_string('core/partials/model_select.html', {
        'selected': briefing.coordinator_model,
        'model_choices': settings.FUZZYCLAW_MODEL_CHOICES,
        'model_url': reverse('core:briefing_model', args=[briefing.pk]),
    }, request=request)
    return HttpResponse(html)


@login_required
@require_POST
def briefing_schedule(request, pk):
    """HTMX endpoint: save schedule_text, parse it, and sync the PeriodicTask."""
    briefing = get_object_or_404(Briefing, pk=pk, owner=request.user)

    # Save form fields so the user doesn't have to Save first
    update_fields = []
    schedule_text = request.POST.get('schedule_text', '').strip()
    if schedule_text != (briefing.schedule_text or '').strip():
        briefing.schedule_text = schedule_text
        update_fields.append('schedule_text')
    coordinator_model = request.POST.get('coordinator_model')
    if coordinator_model and coordinator_model != briefing.coordinator_model and _valid_model(coordinator_model):
        briefing.coordinator_model = coordinator_model
        update_fields.append('coordinator_model')
    if update_fields:
        briefing.save(update_fields=update_fields)

    try:
        result = sync_schedule(briefing)
    except Exception as e:
        logger.exception("Failed to parse schedule for briefing %d", briefing.pk)
        result = {'action': 'error', 'error': str(e)}

    schedule_status = get_schedule_status(briefing)
    html = render_to_string('core/partials/schedule_status.html', {
        'briefing': briefing,
        'schedule_status': schedule_status,
        'schedule_result': result,
    }, request=request)
    return HttpResponse(html)


@login_required
@require_POST
def briefing_launch(request, pk):
    from .tasks import launch_run
    briefing = get_object_or_404(Briefing, pk=pk, owner=request.user)
    run = Run.objects.create(
        briefing=briefing, status='pending', triggered_by='manual',
    )
    launch_run(run)
    messages.info(request, f'Run #{run.id} launched for "{briefing.title}".')
    return redirect('core:run_detail', pk=run.pk)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

@login_required
def run_detail(request, pk):
    run = get_object_or_404(
        Run.objects.select_related('briefing'),
        pk=pk,
        briefing__owner=request.user,
    )
    agent_runs = run.agent_runs.order_by('created_at')
    context = {
        'nav': 'briefings',
        'run': run,
        'agent_runs': agent_runs,
        'breadcrumbs': [
            {'label': 'Briefings', 'url': reverse('core:briefing_list')},
            {'label': run.briefing.title, 'url': reverse('core:briefing_detail', args=[run.briefing.pk])},
            {'label': f'Run #{run.id}'},
        ],
    }
    return render(request, 'core/run_detail.html', context)


@login_required
def run_status(request, pk):
    """HTMX endpoint for polling run status."""
    run = get_object_or_404(
        Run.objects.select_related('briefing'),
        pk=pk,
        briefing__owner=request.user,
    )
    agent_runs = run.agent_runs.order_by('created_at')
    context = {'run': run, 'agent_runs': agent_runs}
    return render(request, 'core/partials/run_status.html', context)


# ---------------------------------------------------------------------------
# Agents & Skills (read-only)
# ---------------------------------------------------------------------------

@login_required
def agent_list(request):
    agents = get_available_agents()
    image_names = set(
        AgentImage.objects.filter(build_error='').values_list('agent_name', flat=True)
    )
    for agent in agents:
        agent['has_image'] = agent['name'] in image_names
    context = {
        'nav': 'agents',
        'agents': agents,
        'breadcrumbs': [{'label': 'Agents'}],
    }
    return render(request, 'core/agent_list.html', context)


@login_required
def skill_list(request):
    skills = get_available_skills()
    context = {
        'nav': 'skills',
        'skills': skills,
        'breadcrumbs': [{'label': 'Skills'}],
    }
    return render(request, 'core/skill_list.html', context)


# ---------------------------------------------------------------------------
# Message Board — all reads/writes go to Redis directly
# ---------------------------------------------------------------------------

_board_pool = redis_lib.ConnectionPool.from_url(
    settings.FUZZYCLAW_REDIS_URL, decode_responses=True,
)

# Fuzzy board constants — permanent assistant stream (single stream, multi-user via user_id field)
FUZZY_STREAM_KEY = 'fuzzyclaw:board:fuzzy'
FUZZY_PARTICIPANTS_KEY = 'fuzzyclaw:board:fuzzy:participants'


def _get_board_redis():
    """Get a Redis client for board operations (shared connection pool)."""
    return redis_lib.Redis(connection_pool=_board_pool)


def _parse_ts(ts_str: str):
    """Parse an ISO timestamp string into a datetime, or None."""
    try:
        return datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return None


@login_required
def board_messages(request, run_pk):
    """HTMX endpoint: return board messages as HTML partial.

    Reads directly from Redis Streams via XRANGE. No DB model involved.

    Query params:
      filter: 'all' (default) or 'human' (only messages to/from human)
    """
    run = get_object_or_404(
        Run.objects.select_related('briefing'),
        pk=run_pk,
        briefing__owner=request.user,
    )
    filter_mode = request.GET.get('filter', 'all')

    board_messages_list = []
    try:
        r = _get_board_redis()
        stream_key = f"fuzzyclaw:board:{run.id}"
        entries = list(reversed(r.xrevrange(stream_key, count=200)))

        for entry_id, data in entries:
            sender = data.get('from', '')
            recipient = data.get('to', '')

            if filter_mode == 'human':
                if recipient != 'human' and sender != 'human':
                    continue

            board_messages_list.append({
                'id': entry_id,
                'sender': sender,
                'recipient': recipient,
                'content': data.get('content', ''),
                'created_at': _parse_ts(data.get('ts', '')),
            })
    except Exception as e:
        logger.warning("board_messages: Redis read failed: %s", e)

    context = {
        'board_messages': board_messages_list,
        'run': run,
    }
    return render(request, 'core/partials/board_messages.html', context)


@login_required
@require_POST
def board_reply(request, run_pk):
    """HTMX endpoint: human posts a reply to the board.

    Writes directly to Redis Streams via XADD. No DB write.
    POST body: message (text with @recipient prefix)
    """
    run = get_object_or_404(
        Run.objects.select_related('briefing'),
        pk=run_pk,
        briefing__owner=request.user,
    )

    raw_message = request.POST.get('message', '').strip()
    if not raw_message:
        return HttpResponse(status=400)

    # Parse @recipient mentions and extract message body.
    # Multiple @mentions supported: "@agent_1 @agent_2 hello" sends to both.
    # No @mentions: defaults to coordinator for this run.
    mentions = re.findall(r'@([\w-]+)', raw_message)
    content = re.sub(r'@[\w-]+[:\s]*', '', raw_message).strip()

    if not content:
        return HttpResponse(status=400)

    recipients = mentions if mentions else [f"coordinator_{run.id}"]

    try:
        r = _get_board_redis()
        stream_key = f"fuzzyclaw:board:{run.id}"
        ts = dj_timezone.now().isoformat()
        for recipient in recipients:
            r.xadd(stream_key, {
                'from': 'human',
                'to': recipient,
                'content': content,
                'ts': ts,
            })
    except Exception as e:
        logger.warning("board_reply: Redis write failed: %s", e)
        return HttpResponse(
            '<div class="text-red-600 text-sm p-2">Failed to send message. Please try again.</div>',
            status=502,
        )

    # Return updated messages partial
    return board_messages(request, run_pk)


@login_required
def board_badge(request):
    """HTMX endpoint: return badge HTML showing count of runs with board messages.

    Includes fuzzy's permanent stream in the count.
    """
    waiting_count = 0
    try:
        r = _get_board_redis()
        # Fuzzy's permanent stream is excluded from the badge — it always
        # has messages, which would trigger auto-open on every page load.
        # The pulsing dot on the selector handles fuzzy activity instead.
        recent_run_ids = list(
            Run.objects.filter(briefing__owner=request.user)
            .order_by('-created_at')
            .values_list('id', flat=True)[:20]
        )
        for run_id in recent_run_ids:
            stream_key = f"fuzzyclaw:board:{run_id}"
            if r.xlen(stream_key) > 0:
                waiting_count += 1
    except Exception as e:
        logger.warning("board_badge: Redis read failed: %s", e)

    context = {'waiting_count': waiting_count}
    return render(request, 'core/partials/board_badge.html', context)


@login_required
def board_participants(request, run_pk):
    """HTMX endpoint: return participant list for @autocomplete."""
    run = get_object_or_404(
        Run.objects.select_related('briefing'),
        pk=run_pk,
        briefing__owner=request.user,
    )

    enriched = []
    try:
        r = _get_board_redis()
        participants_key = f"fuzzyclaw:board:{run.id}:participants"
        members = r.smembers(participants_key)

        for p in sorted(members):
            parts = p.rsplit('_', 1)
            agent_name = parts[0] if parts else p
            agent_run_id = parts[1] if len(parts) == 2 else ''

            status = 'unknown'
            if agent_run_id.isdigit():
                ar = AgentRun.objects.filter(pk=int(agent_run_id), run=run).first()
                if ar:
                    status = ar.status

            enriched.append({
                'id': p,
                'agent_name': agent_name,
                'status': status,
            })
    except Exception as e:
        logger.warning("board_participants: Redis read failed: %s", e)

    context = {'participants': enriched, 'run': run}
    return render(request, 'core/partials/board_participants.html', context)


@login_required
def board_active_runs(request):
    """JSON endpoint: return list of runs with board messages (for run selector).

    Includes fuzzy as the first entry for all authenticated users.
    """
    data = [{'id': 'fuzzy', 'title': 'Fuzzy Assistant'}]
    try:
        r = _get_board_redis()
        recent_runs = (
            Run.objects.filter(briefing__owner=request.user)
            .select_related('briefing')
            .order_by('-created_at')[:20]
        )
        for run in recent_runs:
            stream_key = f"fuzzyclaw:board:{run.id}"
            if r.xlen(stream_key) > 0:
                data.append({'id': run.id, 'title': run.briefing.title[:30]})
    except Exception as e:
        logger.warning("board_active_runs: Redis read failed: %s", e)

    return JsonResponse(data, safe=False)


# ---------------------------------------------------------------------------
# Fuzzy Board — permanent assistant stream (not tied to a run)
# ---------------------------------------------------------------------------


@login_required
def fuzzy_status(request):
    """JSON endpoint: return fuzzy's current status (thinking or idle)."""
    try:
        r = _get_board_redis()
        status = r.get('fuzzyclaw:fuzzy:status')
        return JsonResponse({'status': status or 'idle'})
    except Exception:
        return JsonResponse({'status': 'idle'})


@login_required
def fuzzy_board_messages(request):
    """HTMX endpoint: return fuzzy board messages as HTML partial.

    Same format as run board messages, but reads from the permanent fuzzy stream.
    Filters by user_id so each user only sees their own conversation.
    """
    filter_mode = request.GET.get('filter', 'all')
    user_id = str(request.user.id)

    board_messages_list = []
    try:
        r = _get_board_redis()
        entries = list(reversed(r.xrevrange(FUZZY_STREAM_KEY, count=500)))

        for entry_id, data in entries:
            # Filter by user_id — each user sees only their conversation.
            # Messages without user_id (pre-migration) are hidden from everyone.
            msg_user_id = data.get('user_id', '')
            if msg_user_id != user_id:
                continue

            sender = data.get('from', '')
            recipient = data.get('to', '')

            if filter_mode == 'human':
                if recipient != 'human' and sender != 'human':
                    continue

            board_messages_list.append({
                'id': entry_id,
                'sender': sender,
                'recipient': recipient,
                'content': data.get('content', ''),
                'created_at': _parse_ts(data.get('ts', '')),
            })
    except Exception as e:
        logger.warning("fuzzy_board_messages: Redis read failed: %s", e)

    context = {
        'board_messages': board_messages_list,
        'run': None,
    }
    return render(request, 'core/partials/board_messages.html', context)


@login_required
@require_POST
def fuzzy_board_reply(request):
    """HTMX endpoint: human posts a message to fuzzy's board.

    POST body: message (text, @mentions optional but defaults to @fuzzy).
    """
    raw_message = request.POST.get('message', '').strip()
    if not raw_message:
        return HttpResponse(status=400)

    # Parse @mentions — default to fuzzy
    mentions = re.findall(r'@([\w-]+)', raw_message)
    content = re.sub(r'@[\w-]+[:\s]*', '', raw_message).strip()

    if not content:
        return HttpResponse(status=400)

    recipients = mentions if mentions else ['fuzzy']

    try:
        r = _get_board_redis()
        ts = dj_timezone.now().isoformat()
        for recipient in recipients:
            r.xadd(FUZZY_STREAM_KEY, {
                'from': 'human',
                'to': recipient,
                'content': content,
                'ts': ts,
                'user_id': str(request.user.id),
            })
    except Exception as e:
        logger.warning("fuzzy_board_reply: Redis write failed: %s", e)
        return HttpResponse(
            '<div class="text-red-600 text-sm p-2">Failed to send message. Please try again.</div>',
            status=502,
        )

    return fuzzy_board_messages(request)


@login_required
def fuzzy_board_participants(request):
    """HTMX endpoint: return participant list for fuzzy's board."""
    enriched = []
    try:
        r = _get_board_redis()
        members = r.smembers(FUZZY_PARTICIPANTS_KEY)

        for p in sorted(members):
            enriched.append({
                'id': p,
                'agent_name': p,
                'status': 'running',  # fuzzy is always-on
            })
    except Exception as e:
        logger.warning("fuzzy_board_participants: Redis read failed: %s", e)

    context = {'participants': enriched, 'run': None}
    return render(request, 'core/partials/board_participants.html', context)


# ---------------------------------------------------------------------------
# Account management
# ---------------------------------------------------------------------------

class CustomPasswordChangeView(PasswordChangeView):
    template_name = 'core/password_change.html'
    success_url = reverse_lazy('core:password_change_done')


class CustomPasswordChangeDoneView(PasswordChangeDoneView):
    template_name = 'core/password_change_done.html'


@login_required
def logout_confirm_view(request):
    """GET renders sign-out confirmation; form POSTs to Django's built-in LogoutView."""
    return render(request, 'core/logout_confirm.html')


@login_required
def profile_view(request):
    """User profile — view and edit name/email, link to password change."""
    if request.method == 'POST':
        form = ProfileForm(request.POST, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, 'Your profile has been updated.')
            return redirect('core:profile')
    else:
        form = ProfileForm(instance=request.user)

    return render(request, 'core/profile.html', {'form': form})


# ---------------------------------------------------------------------------
# File Manager
# ---------------------------------------------------------------------------

def _get_user_root(user) -> Path:
    """Return the user's data directory, creating it if needed."""
    user_root = Path(settings.FUZZYCLAW_DATA_DIR) / 'users' / str(user.id)
    user_root.mkdir(parents=True, exist_ok=True)
    return user_root


def _resolve_user_path(user, relative_path: str) -> Path:
    """Resolve a relative path within a user's data directory.

    Returns the absolute Path. Raises SuspiciousFileOperation if the path
    escapes the user's root.
    """
    user_root = _get_user_root(user)
    # Normalize and resolve to catch .. traversal
    resolved = (user_root / relative_path).resolve()
    if not str(resolved).startswith(str(user_root.resolve())):
        raise SuspiciousFileOperation("Path traversal detected")
    return resolved


def _file_info(path: Path) -> dict:
    """Build file/folder info dict for template rendering."""
    stat = path.stat()
    return {
        'name': path.name,
        'is_dir': path.is_dir(),
        'size': stat.st_size if path.is_file() else None,
        'modified': datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
    }


def _list_all_folders(user) -> list[str]:
    """Return all folder paths relative to the user's root, for move destination picker."""
    user_root = _get_user_root(user)
    folders = ['']  # root
    for dirpath, dirnames, _ in os.walk(user_root):
        for d in sorted(dirnames):
            rel = os.path.relpath(os.path.join(dirpath, d), user_root)
            folders.append(rel)
    return folders


@login_required
def file_manager(request):
    """File manager — main page."""
    subpath = request.GET.get('path', '')
    try:
        current_dir = _resolve_user_path(request.user, subpath)
    except SuspiciousFileOperation:
        return HttpResponse("Invalid path.", status=400)

    if not current_dir.is_dir():
        return HttpResponse("Not a directory.", status=404)

    entries = sorted(current_dir.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    items = [_file_info(e) for e in entries]

    # Build breadcrumb parts
    parts = [p for p in subpath.strip('/').split('/') if p] if subpath else []
    breadcrumb_parts = []
    for i, part in enumerate(parts):
        breadcrumb_parts.append({
            'name': part,
            'path': '/'.join(parts[:i + 1]),
        })

    context = {
        'nav': 'files',
        'items': items,
        'subpath': subpath,
        'breadcrumb_parts': breadcrumb_parts,
        'breadcrumbs': [{'label': 'Files'}],
        'all_folders': _list_all_folders(request.user),
    }
    return render(request, 'core/file_manager.html', context)


@login_required
def file_list_partial(request):
    """HTMX partial — file list for a given subpath."""
    subpath = request.GET.get('path', '')
    try:
        current_dir = _resolve_user_path(request.user, subpath)
    except SuspiciousFileOperation:
        return HttpResponse("Invalid path.", status=400)

    if not current_dir.is_dir():
        return HttpResponse("Not a directory.", status=404)

    entries = sorted(current_dir.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    items = [_file_info(e) for e in entries]

    parts = [p for p in subpath.strip('/').split('/') if p] if subpath else []
    breadcrumb_parts = []
    for i, part in enumerate(parts):
        breadcrumb_parts.append({
            'name': part,
            'path': '/'.join(parts[:i + 1]),
        })

    context = {
        'items': items,
        'subpath': subpath,
        'breadcrumb_parts': breadcrumb_parts,
        'all_folders': _list_all_folders(request.user),
    }
    return render(request, 'core/partials/file_list.html', context)


@login_required
@require_POST
def file_upload(request):
    """Upload file(s) to the user's directory."""
    subpath = request.POST.get('path', '')
    try:
        target_dir = _resolve_user_path(request.user, subpath)
    except SuspiciousFileOperation:
        return HttpResponse("Invalid path.", status=400)

    if not target_dir.is_dir():
        return HttpResponse("Target directory not found.", status=404)

    max_size = getattr(settings, 'FUZZYCLAW_FILE_UPLOAD_MAX_SIZE', 50 * 1024 * 1024)

    for uploaded in request.FILES.getlist('files'):
        if uploaded.size > max_size:
            messages.error(request, f'File "{uploaded.name}" exceeds size limit.')
            continue
        # Sanitize filename
        safe_name = Path(uploaded.name).name
        if not safe_name or safe_name.startswith('.'):
            messages.error(request, f'Invalid filename: "{uploaded.name}"')
            continue
        dest = target_dir / safe_name
        with open(dest, 'wb') as f:
            for chunk in uploaded.chunks():
                f.write(chunk)

    return redirect(reverse('core:file_manager') + f'?path={subpath}')


@login_required
def file_download(request):
    """Download a file from the user's directory."""
    filepath = request.GET.get('path', '')
    if not filepath:
        return HttpResponse("No file specified.", status=400)
    try:
        resolved = _resolve_user_path(request.user, filepath)
    except SuspiciousFileOperation:
        return HttpResponse("Invalid path.", status=400)

    if not resolved.is_file():
        return HttpResponse("File not found.", status=404)

    return FileResponse(open(resolved, 'rb'), as_attachment=True, filename=resolved.name)


@login_required
@require_POST
def file_delete(request):
    """Delete a file from the user's directory."""
    filepath = request.POST.get('path', '')
    if not filepath:
        return HttpResponse("No file specified.", status=400)
    try:
        resolved = _resolve_user_path(request.user, filepath)
    except SuspiciousFileOperation:
        return HttpResponse("Invalid path.", status=400)

    if not resolved.is_file():
        return HttpResponse("File not found.", status=404)

    resolved.unlink()
    # Redirect to parent directory
    parent_subpath = str(Path(filepath).parent)
    if parent_subpath == '.':
        parent_subpath = ''
    return redirect(reverse('core:file_manager') + f'?path={parent_subpath}')


@login_required
@require_POST
def folder_create(request):
    """Create a subfolder in the user's directory."""
    subpath = request.POST.get('path', '')
    folder_name = request.POST.get('name', '').strip()
    if not folder_name or '/' in folder_name or folder_name.startswith('.'):
        messages.error(request, 'Invalid folder name.')
        return redirect(reverse('core:file_manager') + f'?path={subpath}')

    try:
        parent = _resolve_user_path(request.user, subpath)
    except SuspiciousFileOperation:
        return HttpResponse("Invalid path.", status=400)

    new_dir = parent / folder_name
    new_dir.mkdir(exist_ok=True)
    return redirect(reverse('core:file_manager') + f'?path={subpath}')


@login_required
@require_POST
def folder_delete(request):
    """Delete a subfolder (and all contents) from the user's directory."""
    folderpath = request.POST.get('path', '')
    if not folderpath:
        return HttpResponse("No folder specified.", status=400)
    try:
        resolved = _resolve_user_path(request.user, folderpath)
    except SuspiciousFileOperation:
        return HttpResponse("Invalid path.", status=400)

    user_root = _get_user_root(request.user)
    if resolved == user_root.resolve():
        return HttpResponse("Cannot delete root directory.", status=400)

    if not resolved.is_dir():
        return HttpResponse("Folder not found.", status=404)

    shutil.rmtree(resolved)
    parent_subpath = str(Path(folderpath).parent)
    if parent_subpath == '.':
        parent_subpath = ''
    return redirect(reverse('core:file_manager') + f'?path={parent_subpath}')


@login_required
@require_POST
def file_rename(request):
    """Rename a file or folder within the user's directory."""
    filepath = request.POST.get('path', '')
    new_name = request.POST.get('name', '').strip()
    if not filepath or not new_name:
        return HttpResponse("Missing path or name.", status=400)
    if '/' in new_name or new_name.startswith('.'):
        return HttpResponse("Invalid name.", status=400)

    try:
        resolved = _resolve_user_path(request.user, filepath)
    except SuspiciousFileOperation:
        return HttpResponse("Invalid path.", status=400)

    if not resolved.exists():
        return HttpResponse("Not found.", status=404)

    new_path = resolved.parent / new_name
    # Ensure the new path is still inside the user root
    try:
        _resolve_user_path(request.user, str(Path(filepath).parent / new_name))
    except SuspiciousFileOperation:
        return HttpResponse("Invalid name.", status=400)

    if new_path.exists():
        messages.error(request, f'"{new_name}" already exists.')
    else:
        resolved.rename(new_path)

    parent_subpath = str(Path(filepath).parent)
    if parent_subpath == '.':
        parent_subpath = ''
    return redirect(reverse('core:file_manager') + f'?path={parent_subpath}')


@login_required
@require_POST
def file_move(request):
    """Move a file or folder to another location within the user's directory."""
    filepath = request.POST.get('path', '')
    destination = request.POST.get('destination', '').strip()
    if not filepath:
        return HttpResponse("No file specified.", status=400)

    try:
        src = _resolve_user_path(request.user, filepath)
    except SuspiciousFileOperation:
        return HttpResponse("Invalid source path.", status=400)

    if not src.exists():
        return HttpResponse("Source not found.", status=404)

    try:
        dest_dir = _resolve_user_path(request.user, destination)
    except SuspiciousFileOperation:
        return HttpResponse("Invalid destination.", status=400)

    if not dest_dir.is_dir():
        return HttpResponse("Destination is not a directory.", status=400)

    target = dest_dir / src.name
    if target.exists():
        messages.error(request, f'"{src.name}" already exists in the destination.')
    else:
        shutil.move(str(src), str(target))

    parent_subpath = str(Path(filepath).parent)
    if parent_subpath == '.':
        parent_subpath = ''
    return redirect(reverse('core:file_manager') + f'?path={parent_subpath}')
