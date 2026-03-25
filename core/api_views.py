from rest_framework import serializers as drf_serializers
from rest_framework import viewsets
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


class RunViewSet(viewsets.ModelViewSet):
    """Full CRUD for runs. Scoped to the authenticated user's briefings."""
    serializer_class = RunSerializer
    filterset_fields = ['briefing', 'status', 'triggered_by']
    ordering_fields = ['created_at', 'started_at', 'completed_at']

    def get_queryset(self):
        return (
            Run.objects.filter(briefing__owner=self.request.user)
            .select_related('briefing')
            .prefetch_related('agent_runs')
        )

    def perform_create(self, serializer):
        briefing = serializer.validated_data.get('briefing')
        if briefing and briefing.owner != self.request.user:
            raise drf_serializers.ValidationError(
                {'briefing': 'You do not own this briefing.'}
            )
        serializer.save()

    def perform_update(self, serializer):
        briefing = serializer.validated_data.get('briefing')
        if briefing and briefing.owner != self.request.user:
            raise drf_serializers.ValidationError(
                {'briefing': 'You do not own this briefing.'}
            )
        serializer.save()

    @action(detail=False, methods=['get'])
    def pending(self, request):
        """Get all pending runs (for the coordinator to pick up)."""
        pending = self.get_queryset().filter(status='pending')
        serializer = self.get_serializer(pending, many=True)
        return Response(serializer.data)


class AgentRunViewSet(viewsets.ModelViewSet):
    """CRUD for agent runs. Scoped to the authenticated user's runs."""
    serializer_class = AgentRunSerializer
    filterset_fields = ['run', 'agent_name', 'status']
    ordering_fields = ['created_at', 'started_at', 'completed_at']

    def get_queryset(self):
        return (
            AgentRun.objects.filter(run__briefing__owner=self.request.user)
            .select_related('run')
        )

    def perform_create(self, serializer):
        run = serializer.validated_data.get('run')
        if run and run.briefing.owner != self.request.user:
            raise drf_serializers.ValidationError(
                {'run': 'You do not own this run.'}
            )
        serializer.save()

    def perform_update(self, serializer):
        run = serializer.validated_data.get('run')
        if run and run.briefing.owner != self.request.user:
            raise drf_serializers.ValidationError(
                {'run': 'You do not own this run.'}
            )
        serializer.save()
