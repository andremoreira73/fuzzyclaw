from django.urls import path

from . import views

app_name = 'core'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),

    # Briefings
    path('briefings/', views.briefing_list, name='briefing_list'),
    path('briefings/new/', views.briefing_create, name='briefing_create'),
    path('briefings/<int:pk>/', views.briefing_detail, name='briefing_detail'),
    path('briefings/<int:pk>/save/', views.briefing_save, name='briefing_save'),
    path('briefings/<int:pk>/toggle/', views.briefing_toggle, name='briefing_toggle'),
    path('briefings/<int:pk>/model/', views.briefing_model, name='briefing_model'),
    path('briefings/<int:pk>/launch/', views.briefing_launch, name='briefing_launch'),
    path('briefings/<int:pk>/schedule/', views.briefing_schedule, name='briefing_schedule'),

    # Runs
    path('runs/<int:pk>/', views.run_detail, name='run_detail'),
    path('runs/<int:pk>/status/', views.run_status, name='run_status'),

    # Agents & Skills
    path('agents/', views.agent_list, name='agent_list'),
    path('skills/', views.skill_list, name='skill_list'),
]
