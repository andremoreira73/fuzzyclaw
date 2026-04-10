from rest_framework import serializers

from .models import AgentRun, Briefing, Run


class FilesystemAgentSerializer(serializers.Serializer):
    """Read-only serializer for filesystem agent definitions."""
    name = serializers.CharField()
    description = serializers.CharField()
    model_choice = serializers.CharField()
    tools = serializers.ListField(child=serializers.CharField())
    prompt = serializers.CharField()
    path = serializers.CharField()


class FilesystemSkillSerializer(serializers.Serializer):
    """Read-only serializer for filesystem skill definitions."""
    name = serializers.CharField()
    description = serializers.CharField()
    path = serializers.CharField()


class BriefingSerializer(serializers.ModelSerializer):
    class Meta:
        model = Briefing
        fields = [
            'id', 'owner', 'title', 'content', 'coordinator_model',
            'is_active', 'schedule_text', 'created_at', 'updated_at',
        ]
        read_only_fields = ['owner', 'created_at', 'updated_at']


class AgentRunSerializer(serializers.ModelSerializer):
    """Execution-managed fields are read-only. Only ``user_notes`` is writable,
    so the stored run history cannot be rewritten through the REST API. The
    coordinator writes execution state through the ORM directly, not the API.
    """

    class Meta:
        model = AgentRun
        fields = [
            'id', 'run', 'agent_name', 'status', 'container_id',
            'started_at', 'completed_at', 'report', 'raw_data',
            'error_message', 'user_notes', 'created_at',
        ]
        read_only_fields = [
            'id', 'run', 'agent_name', 'status', 'container_id',
            'started_at', 'completed_at', 'report', 'raw_data',
            'error_message', 'created_at',
        ]


class RunSerializer(serializers.ModelSerializer):
    """Execution-managed fields are read-only. Only ``user_notes`` is writable.

    Runs are launched via ``POST /api/briefings/{id}/launch/`` and cancelled
    via ``POST /api/runs/{id}/cancel/``. Direct mutation of status, reports,
    and timestamps through PATCH is not permitted — the run log is an audit
    surface, not a user-editable document.
    """
    agent_runs = AgentRunSerializer(many=True, read_only=True)

    class Meta:
        model = Run
        fields = [
            'id', 'briefing', 'status', 'started_at', 'completed_at',
            'coordinator_report', 'error_message', 'triggered_by',
            'user_notes', 'agent_runs', 'created_at',
        ]
        read_only_fields = [
            'id', 'briefing', 'status', 'started_at', 'completed_at',
            'coordinator_report', 'error_message', 'triggered_by',
            'created_at',
        ]
