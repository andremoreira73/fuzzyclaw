from django.urls import include, path

from rest_framework.routers import DefaultRouter

from .api_views import (
    AgentDetailView,
    AgentListView,
    AgentRunViewSet,
    BriefingViewSet,
    RunViewSet,
    SkillDetailView,
    SkillListView,
)

router = DefaultRouter()
router.register(r'briefings', BriefingViewSet, basename='briefing')
router.register(r'runs', RunViewSet, basename='run')
router.register(r'agent-runs', AgentRunViewSet, basename='agentrun')

urlpatterns = [
    path('agents/', AgentListView.as_view(), name='agent-list'),
    path('agents/<str:name>/', AgentDetailView.as_view(), name='agent-detail'),
    path('skills/', SkillListView.as_view(), name='skill-list'),
    path('skills/<str:name>/', SkillDetailView.as_view(), name='skill-detail'),
    path('', include(router.urls)),
]
