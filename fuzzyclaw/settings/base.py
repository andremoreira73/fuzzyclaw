import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'insecure-dev-key-change-me')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Third-party
    'rest_framework',
    'rest_framework.authtoken',
    'django_filters',
    'drf_spectacular',
    'corsheaders',
    'django_celery_beat',

    # Local
    'core',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'fuzzyclaw.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'fuzzyclaw.wsgi.application'
ASGI_APPLICATION = 'fuzzyclaw.asgi.application'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/accounts/login/'

# DRF
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.TokenAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ],
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 50,
}

# drf-spectacular
SPECTACULAR_SETTINGS = {
    'TITLE': 'FuzzyClaw API',
    'DESCRIPTION': 'REST API for the FuzzyClaw agent orchestration platform.',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
}

# Celery
CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', 'redis://localhost:6379/0')
CELERY_RESULT_BACKEND = os.environ.get('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TASK_SOFT_TIME_LIMIT = 1800
CELERY_TASK_TIME_LIMIT = 2400
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'
CELERY_TIMEZONE = 'Europe/Berlin'

# ---------------------------------------------------------------------------
# FuzzyClaw: Model & Tool Registries
# ---------------------------------------------------------------------------
# Centralised source of truth for allowed LLM models and agent tools.
# Validated at save-time in models, exposed in admin/API/frontend.

FUZZYCLAW_MODELS = {
    # OpenAI
    'gpt-5': {'provider': 'openai', 'label': 'GPT-5', 'tier': 'strong', 'env_key': 'OPENAI_API_KEY', 'max_input_tokens': 128_000, 'defaults': {'temperature': 1, 'reasoning_effort': 'high', 'use_responses_api': True}},
    'gpt-5-mini': {'provider': 'openai', 'label': 'GPT-5 Mini', 'tier': 'cheap', 'env_key': 'OPENAI_API_KEY', 'max_input_tokens': 128_000, 'defaults': {'temperature': 1, 'reasoning_effort': 'high', 'use_responses_api': True}},
    'gpt-5.4': {'provider': 'openai', 'label': 'GPT-5.4', 'tier': 'strong', 'env_key': 'OPENAI_API_KEY', 'max_input_tokens': 128_000, 'defaults': {'temperature': 1, 'reasoning_effort': 'high', 'use_responses_api': True}},
    'gpt-5.4-mini': {'provider': 'openai', 'label': 'GPT-5.4 Mini', 'tier': 'strong', 'env_key': 'OPENAI_API_KEY', 'max_input_tokens': 128_000, 'defaults': {'temperature': 1, 'reasoning_effort': 'high', 'use_responses_api': True}},
    # Google
    'gemini-2.5-pro': {'provider': 'google_genai', 'label': 'Gemini 2.5 Pro', 'tier': 'strong', 'env_key': 'GOOGLE_API_KEY', 'max_input_tokens': 1_000_000, 'defaults': {'temperature': 1, 'thinking_budget': -1}},
    'gemini-2.5-flash': {'provider': 'google_genai', 'label': 'Gemini 2.5 Flash', 'tier': 'cheap', 'env_key': 'GOOGLE_API_KEY', 'max_input_tokens': 1_000_000, 'defaults': {'temperature': 1, 'thinking_budget': -1}},
    # Anthropic
    'claude-opus-4-6': {'provider': 'anthropic', 'label': 'Claude Opus 4.6', 'tier': 'strong', 'env_key': 'ANTHROPIC_API_KEY', 'max_input_tokens': 200_000, 'defaults': {'temperature': 1}},
    'claude-sonnet-4-6': {'provider': 'anthropic', 'label': 'Claude Sonnet 4.6', 'tier': 'strong', 'env_key': 'ANTHROPIC_API_KEY', 'max_input_tokens': 200_000, 'defaults': {'temperature': 1}},
}

FUZZYCLAW_TOOLS = {
    # Key = tool name used in Agent.tools JSON.  Value = human-readable description.
    'bash': 'Execute shell commands (requires container isolation for safety).',
    'career_scrape': 'Scrape career/jobs pages with job-specific selectors (English + German).',
    'web_scrape': 'Scrape a web page and return cleaned text (uses ScrapingBee API).',
    'web_search': 'Search the web for information.',
    # Deep Agents built-in filesystem tools (always available, listed for reference)
    'ls': 'List directory contents (built-in).',
    'read_file': 'Read a file (built-in).',
    'write_file': 'Write a file (built-in).',
    'edit_file': 'Edit a file (built-in).',
    'glob': 'Find files by pattern (built-in).',
    'grep': 'Search file contents (built-in).',
}

# Convenience helpers
def _model_choices():
    """Build model choices, marking unavailable models (no API key)."""
    choices = []
    for key, cfg in FUZZYCLAW_MODELS.items():
        available = bool(os.environ.get(cfg['env_key']))
        label = cfg['label'] if available else f"{cfg['label']} (no API key)"
        choices.append((key, label))
    return choices

FUZZYCLAW_COORDINATOR_MAX_RETRIES = 3
FUZZYCLAW_SCHEDULE_PARSER_MODEL = 'gemini-2.5-flash'
FUZZYCLAW_MODEL_CHOICES = _model_choices()
FUZZYCLAW_TOOL_NAMES = sorted(FUZZYCLAW_TOOLS.keys())

# Agent & skill filesystem locations
FUZZYCLAW_AGENTS_DIR = BASE_DIR / 'agents'
FUZZYCLAW_SKILLS_DIR = BASE_DIR / 'skills'

# Container Orchestration
FUZZYCLAW_AGENT_IMAGE_PREFIX = 'fuzzyclaw-agent'

# Volume mount security — allowlist + blocklist.
# Empty allowlist = everything allowed (minus blocklist).
# Non-empty allowlist = only those paths (minus blocklist).
# Configurable via comma-separated env vars, e.g.:
#   FUZZYCLAW_VOLUME_ALLOWLIST=/home/user/projects,/data/shared
#   FUZZYCLAW_VOLUME_BLOCKLIST=/,/etc,/root  (has sensible defaults)
_vol_allowlist_env = os.environ.get('FUZZYCLAW_VOLUME_ALLOWLIST', '')
FUZZYCLAW_VOLUME_ALLOWLIST = [p.strip() for p in _vol_allowlist_env.split(',') if p.strip()]

_vol_blocklist_env = os.environ.get('FUZZYCLAW_VOLUME_BLOCKLIST', '')
FUZZYCLAW_VOLUME_BLOCKLIST = (
    [p.strip() for p in _vol_blocklist_env.split(',') if p.strip()]
    if _vol_blocklist_env
    else ['/', '/etc', '/var/run/docker.sock', '/root', '/proc', '/sys', '/dev']
)
FUZZYCLAW_REDIS_URL = os.environ.get('FUZZYCLAW_REDIS_URL', 'redis://redis:6379/1')
FUZZYCLAW_DOCKER_NETWORK = 'fuzzyclaw_default'
FUZZYCLAW_AGENT_MEM_LIMIT = '512m'
FUZZYCLAW_AGENT_CPU_LIMIT = 0.5      # CPU cores
FUZZYCLAW_AGENT_TIMEOUT = 600        # seconds
FUZZYCLAW_MAX_CONTAINERS = 10        # max concurrent agent containers
# Host path of the project directory — needed because the Celery worker runs
# inside a container but talks to the host Docker daemon via the socket.
# Paths in volume mounts must be host paths, not container paths.
FUZZYCLAW_HOST_PROJECT_DIR = os.environ.get('HOST_PROJECT_DIR', str(BASE_DIR))

# Logging
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
}
