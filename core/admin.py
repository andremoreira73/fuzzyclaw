from django import forms
from django.conf import settings
from django.contrib import admin
from django.utils.html import format_html

from .models import AgentImage, AgentRun, Briefing, Run


# ---------------------------------------------------------------------------
# Custom form widgets for registry-backed fields
# ---------------------------------------------------------------------------

class ModelChoiceWidget(forms.Select):
    """Dropdown populated from FUZZYCLAW_MODELS."""
    def __init__(self, *args, **kwargs):
        kwargs['choices'] = [('', '---------')] + settings.FUZZYCLAW_MODEL_CHOICES
        super().__init__(*args, **kwargs)


class BriefingAdminForm(forms.ModelForm):
    class Meta:
        model = Briefing
        fields = '__all__'
        widgets = {
            'coordinator_model': ModelChoiceWidget(),
        }


# ---------------------------------------------------------------------------
# Admin classes
# ---------------------------------------------------------------------------

@admin.register(Briefing)
class BriefingAdmin(admin.ModelAdmin):
    form = BriefingAdminForm
    list_display = ('id', 'title', 'owner', 'coordinator_model', 'is_active', 'schedule_text', 'updated_at')
    list_filter = ('is_active', 'owner', 'coordinator_model')
    search_fields = ('title', 'content')
    readonly_fields = ('created_at', 'updated_at')

    fieldsets = (
        (None, {
            'fields': ('owner', 'title', 'content', 'is_active'),
        }),
        ('Coordinator', {
            'fields': ('coordinator_model',),
        }),
        ('Schedule', {
            'fields': ('schedule_text',),
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )


class AgentRunInline(admin.TabularInline):
    model = AgentRun
    extra = 0
    readonly_fields = ('agent_name', 'status_colored', 'container_id', 'started_at', 'completed_at')
    fields = ('agent_name', 'status_colored', 'container_id', 'started_at', 'completed_at')

    def status_colored(self, obj):
        colors = {
            'pending': '#6366f1',
            'running': '#f59e0b',
            'completed': '#10b981',
            'failed': '#ef4444',
        }
        return format_html(
            '<span style="color: {}; font-weight: bold;">{}</span>',
            colors.get(obj.status, 'black'),
            obj.get_status_display().upper(),
        )
    status_colored.short_description = 'Status'


@admin.register(Run)
class RunAdmin(admin.ModelAdmin):
    list_display = ('id', 'briefing', 'status_colored', 'triggered_by', 'agent_run_count', 'created_at')
    list_filter = ('status', 'triggered_by', 'created_at')
    readonly_fields = ('created_at',)
    ordering = ['-created_at']
    date_hierarchy = 'created_at'
    inlines = [AgentRunInline]

    fieldsets = (
        (None, {
            'fields': ('briefing', 'status', 'triggered_by'),
        }),
        ('Timing', {
            'fields': ('started_at', 'completed_at', 'created_at'),
        }),
        ('Coordinator Report', {
            'fields': ('coordinator_report',),
        }),
        ('Error Info', {
            'fields': ('error_message',),
            'classes': ('collapse',),
        }),
    )

    def status_colored(self, obj):
        colors = {
            'pending': '#6366f1',
            'running': '#f59e0b',
            'completed': '#10b981',
            'failed': '#ef4444',
        }
        return format_html(
            '<span style="color: {}; font-weight: bold;">{}</span>',
            colors.get(obj.status, 'black'),
            obj.get_status_display().upper(),
        )
    status_colored.short_description = 'Status'

    def agent_run_count(self, obj):
        return obj.agent_runs.count()
    agent_run_count.short_description = 'Agents'

    actions = ['create_pending_run']

    def create_pending_run(self, request, queryset):
        count = 0
        for run in queryset:
            Run.objects.create(briefing=run.briefing, status='pending', triggered_by='manual')
            count += 1
        self.message_user(request, f"Created {count} pending run(s).")
    create_pending_run.short_description = "Create pending run for selected briefing(s)"


@admin.register(AgentRun)
class AgentRunAdmin(admin.ModelAdmin):
    list_display = ('agent_name', 'run', 'status_colored', 'container_id', 'created_at')
    list_filter = ('status', 'agent_name', 'created_at')
    search_fields = ('agent_name', 'report')
    readonly_fields = ('created_at',)
    ordering = ['-created_at']

    fieldsets = (
        (None, {
            'fields': ('run', 'agent_name', 'status', 'container_id'),
        }),
        ('Timing', {
            'fields': ('started_at', 'completed_at', 'created_at'),
        }),
        ('Report', {
            'fields': ('report', 'raw_data'),
        }),
        ('Error Info', {
            'fields': ('error_message',),
            'classes': ('collapse',),
        }),
    )

    def status_colored(self, obj):
        colors = {
            'pending': '#6366f1',
            'running': '#f59e0b',
            'completed': '#10b981',
            'failed': '#ef4444',
        }
        return format_html(
            '<span style="color: {}; font-weight: bold;">{}</span>',
            colors.get(obj.status, 'black'),
            obj.get_status_display().upper(),
        )
    status_colored.short_description = 'Status'

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('run', 'run__briefing')


@admin.register(AgentImage)
class AgentImageAdmin(admin.ModelAdmin):
    list_display = ('agent_name', 'image_tag', 'hash_short', 'built_at', 'has_error')
    list_filter = ('built_at',)
    search_fields = ('agent_name',)
    readonly_fields = ('built_at',)

    fieldsets = (
        (None, {
            'fields': ('agent_name', 'image_tag', 'file_hash'),
        }),
        ('Build Info', {
            'fields': ('built_at', 'build_error'),
        }),
    )

    def hash_short(self, obj):
        return obj.file_hash[:12] + '...' if obj.file_hash else ''
    hash_short.short_description = 'Hash'

    def has_error(self, obj):
        if obj.build_error:
            return format_html('<span style="color: #ef4444; font-weight: bold;">YES</span>')
        return format_html('<span style="color: #10b981;">OK</span>')
    has_error.short_description = 'Build Status'

    actions = ['rebuild_all_images']

    def rebuild_all_images(self, request, queryset):
        from .containers import sync_agent_images
        result = sync_agent_images()
        built = len(result['built'])
        errors = len(result['errors'])
        if errors:
            self.message_user(
                request,
                f"Rebuilt {built} image(s), {errors} error(s).",
                level='warning',
            )
        else:
            self.message_user(request, f"Rebuilt {built} image(s). All OK.")
    rebuild_all_images.short_description = "Rebuild all agent images"
