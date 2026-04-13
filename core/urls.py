from django.urls import path

from . import views

app_name = 'core'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),

    # Account management
    path('profile/', views.profile_view, name='profile'),
    path('logout/confirm/', views.logout_confirm_view, name='logout_confirm'),
    path('password-change/', views.CustomPasswordChangeView.as_view(), name='password_change'),
    path('password-change/done/', views.CustomPasswordChangeDoneView.as_view(), name='password_change_done'),

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

    # Message Board
    path('board/badge/', views.board_badge, name='board_badge'),
    path('board/active-runs/', views.board_active_runs, name='board_active_runs'),
    path('runs/<int:run_pk>/board/', views.board_messages, name='board_messages'),
    path('runs/<int:run_pk>/board/reply/', views.board_reply, name='board_reply'),
    path('runs/<int:run_pk>/board/participants/', views.board_participants, name='board_participants'),
]
