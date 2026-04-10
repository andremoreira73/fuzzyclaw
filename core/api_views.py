from django.utils import timezone
from rest_framework import serializers as drf_serializers
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import AgentRun, Briefing, Run
from .registry import AgentNotFound, SkillNotFound, get_agent, get_available_agents, get_available_skills, get_skill
from .serializers import (
    AgentRunSerializer,
    BriefingSerializer,
    FilesystemAgentSerializer,
    FilesystemSkillSerializer,
    RunSerializer,
)


class AgentListView(APIView):
    """Read-only list of agents from filesystem."""

    def get(self, request):
        agents = get_available_agents()
        serializer = FilesystemAgentSerializer(agents, many=True)
        return Response(serializer.data)


class AgentDetailView(APIView):
    """Read-only detail of a single agent from filesystem."""

    def get(self, request, name):
        try:
            agent = get_agent(name)
        except AgentNotFound:
            return Response({'detail': f"Agent '{name}' not found."}, status=404)
        serializer = FilesystemAgentSerializer(agent)
        return Response(serializer.data)


class SkillListView(APIView):
    """Read-only list of skills from filesystem."""

    def get(self, request):
        skills = get_available_skills()
        serializer = FilesystemSkillSerializer(skills, many=True)
        return Response(serializer.data)


class SkillDetailView(APIView):
    """Read-only detail of a single skill from filesystem."""

    def get(self, request, name):
        try:
            skill = get_skill(name)
        except SkillNotFound:
            return Response({'detail': f"Skill '{name}' not found."}, status=404)
        serializer = FilesystemSkillSerializer(skill)
        return Response(serializer.data)


class BriefingViewSet(viewsets.ModelViewSet):
    """CRUD for briefings. Scoped to the authenticated user."""
    serializer_class = BriefingSerializer
    search_fields = ['title', 'content']
    filterset_fields = ['is_active']
    ordering_fields = ['created_at', 'updated_at', 'title']

    def get_queryset(self):
        return Briefing.objects.filter(owner=self.request.user)

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

    @action(detail=True, methods=['post'])
    def launch(self, request, pk=None):
        """Create a new Run for this briefing and dispatch the coordinator."""
        from .tasks import launch_coordinator

        briefing = self.get_object()
        run = Run.objects.create(
            briefing=briefing,
            status='pending',
            triggered_by='manual',
        )
        launch_coordinator.delay(run.id)
        serializer = RunSerializer(run, context={'request': request})
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class RunViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only access to runs. Runs are launched via
    ``POST /api/briefings/{id}/launch/`` and cancelled via
    ``POST /api/runs/{id}/cancel/``. Execution state (status, reports,
    timestamps) is written by the coordinator via the ORM — not through the
    REST API — so the run log remains a trustworthy audit surface.

    User annotations go in ``user_notes`` via
    ``PATCH /api/runs/{id}/notes/``.
    """
    serializer_class = RunSerializer
    filterset_fields = ['briefing', 'status', 'triggered_by']
    ordering_fields = ['created_at', 'started_at', 'completed_at']

    def get_queryset(self):
        return (
            Run.objects.filter(briefing__owner=self.request.user)
            .select_related('briefing')
            .prefetch_related('agent_runs')
        )

    @action(detail=False, methods=['get'])
    def pending(self, request):
        """Get all pending runs (for the coordinator to pick up)."""
        pending = self.get_queryset().filter(status='pending')
        serializer = self.get_serializer(pending, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Mark a stuck or unwanted run as failed. Useful for operational
        cleanup when a run is stranded in ``running`` status."""
        run = self.get_object()
        if run.status in ('completed', 'failed'):
            return Response(
                {'detail': f"Run is already {run.status}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        run.status = 'failed'
        run.error_message = 'Cancelled by user.'
        run.completed_at = timezone.now()
        run.save(update_fields=['status', 'error_message', 'completed_at'])
        serializer = self.get_serializer(run)
        return Response(serializer.data)

    @action(detail=True, methods=['patch'])
    def notes(self, request, pk=None):
        """Update the user_notes field on this run. The only user-mutable
        field — everything else is read-only."""
        run = self.get_object()
        notes_text = request.data.get('user_notes', '')
        if not isinstance(notes_text, str):
            raise drf_serializers.ValidationError(
                {'user_notes': 'Must be a string.'}
            )
        run.user_notes = notes_text
        run.save(update_fields=['user_notes'])
        serializer = self.get_serializer(run)
        return Response(serializer.data)


class AgentRunViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only access to agent runs. Execution state is written by the
    coordinator via the ORM. Only ``user_notes`` is user-mutable, via
    ``PATCH /api/agent-runs/{id}/notes/``.
    """
    serializer_class = AgentRunSerializer
    filterset_fields = ['run', 'agent_name', 'status']
    ordering_fields = ['created_at', 'started_at', 'completed_at']

    def get_queryset(self):
        return (
            AgentRun.objects.filter(run__briefing__owner=self.request.user)
            .select_related('run')
        )

    @action(detail=True, methods=['patch'])
    def notes(self, request, pk=None):
        """Update the user_notes field on this agent run."""
        agent_run = self.get_object()
        notes_text = request.data.get('user_notes', '')
        if not isinstance(notes_text, str):
            raise drf_serializers.ValidationError(
                {'user_notes': 'Must be a string.'}
            )
        agent_run.user_notes = notes_text
        agent_run.save(update_fields=['user_notes'])
        serializer = self.get_serializer(agent_run)
        return Response(serializer.data)
