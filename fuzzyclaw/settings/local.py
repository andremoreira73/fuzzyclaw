import os

import dj_database_url

import os as _os  # noqa: F811 — re-import needed before base wildcard

# Dev-only SECRET_KEY fallback — base.py requires DJANGO_SECRET_KEY to be set.
# This ensures local dev works without .env while prod/compose crash on missing key.
_os.environ.setdefault('DJANGO_SECRET_KEY', 'insecure-dev-key-change-me')

from .base import *  # noqa: F401, F403

DEBUG = True

ALLOWED_HOSTS = ['*']

DATABASES = {
    'default': dj_database_url.config(
        default=f"postgresql://{os.environ.get('POSTGRES_USER', 'fuzzyclawuser')}:{os.environ.get('DB_PASSWORD', 'changeme')}@{os.environ.get('DB_HOST', 'localhost')}:5432/{os.environ.get('POSTGRES_DB', 'fuzzyclaw_dev')}",
    ),
}

CORS_ALLOW_ALL_ORIGINS = True

# Use basic static storage for dev (no collectstatic required)
STORAGES = {
    'staticfiles': {
        'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage',
    },
}
