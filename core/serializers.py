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
    class Meta:
        model = AgentRun
        fields = [
            'id', 'run', 'agent_name', 'status', 'container_id',
            'started_at', 'completed_at', 'report', 'raw_data',
            'error_message', 'created_at',
        ]
        read_only_fields = ['created_at']
        # Note: status/report/etc. are kept writable because the coordinator
        # updates execution state via the API. Queryset scoping ensures users
        # can only modify their own objects.


class RunSerializer(serializers.ModelSerializer):
    agent_runs = AgentRunSerializer(many=True, read_only=True)

    class Meta:
        model = Run
        fields = [
            'id', 'briefing', 'status', 'started_at', 'completed_at',
            'coordinator_report', 'error_message', 'triggered_by',
            'agent_runs', 'created_at',
        ]
        read_only_fields = ['created_at']
        # Note: execution fields kept writable for coordinator use.
        # Queryset scoping prevents cross-user modification.
