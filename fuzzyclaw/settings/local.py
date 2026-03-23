import os

import dj_database_url

from .base import *  # noqa: F401, F403

DEBUG = True

ALLOWED_HOSTS = ['*']

DATABASES = {
    'default': dj_database_url.config(
        default=f"postgresql://{os.environ.get('POSTGRES_USER', 'fuzzyclawuser')}:{os.environ.get('DB_PASSWORD', 'changeme')}@{os.environ.get('DB_HOST', 'localhost')}:5432/{os.environ.get('POSTGRES_DB', 'fuzzyclaw_dev')}",
    ),
}

CORS_ALLOW_ALL_ORIGINS = True
