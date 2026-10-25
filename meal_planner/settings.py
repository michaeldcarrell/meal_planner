"""
Django settings for meal_planner.

All secrets and deployment-specific values come from the environment (`.env`,
loaded by python-decouple). See README.md for the full list of variable names.
"""
import sys

from decouple import config
from django.core.exceptions import ImproperlyConfigured
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config('SECRET_KEY')
DEBUG = config('DEBUG', default=False, cast=bool)

ALLOWED_HOSTS = ['*']
CSRF_TRUSTED_ORIGINS = [config('APP_BASE_URL', default='https://meals.carrell.us')]

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True

# Public URL used to build links in emails and calendar events.
APP_BASE_URL = config('APP_BASE_URL', default='https://meals.carrell.us').rstrip('/')

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "meals",
]

SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "SCOPE": ["profile", "email"],
        "APP": {
            "client_id": config('GOOGLE_CLIENT_ID', default=''),
            "secret": config('GOOGLE_CLIENT_SECRET', default=''),
            "key": "",
        },
    }
}

LOGIN_URL = "/accounts/google/login/"
SOCIALACCOUNT_LOGIN_ON_GET = True
SOCIALACCOUNT_ONLY = True
ACCOUNT_EMAIL_VERIFICATION = "none"
# The proxy also answers on plain http, and Google only accepts the https callback, so never
# build an http one. (Not SECURE_SSL_REDIRECT: Airflow calls /internal/ over http inside Docker.)
ACCOUNT_DEFAULT_HTTP_PROTOCOL = "http" if DEBUG else "https"
ACCOUNT_ADAPTER = 'meals.adapter.CustomAccountAdapter'
SOCIALACCOUNT_ADAPTER = 'meals.adapter.CustomSocialAccountAdapter'

MIDDLEWARE = [
    "allauth.account.middleware.AccountMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "meal_planner.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / 'templates'],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "meals.context_processors.ui",
            ],
        },
    },
]

WSGI_APPLICATION = "meal_planner.wsgi.application"

# Host Postgres (reached as host.docker.internal from the container). With no
# DB_HOST set (local dev / tests) fall back to SQLite.
if config('DB_HOST', default=''):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": config('DB_NAME', default='meal_planner'),
            "USER": config('DB_USER'),
            "PASSWORD": config('DB_PASSWORD'),
            "HOST": config('DB_HOST'),
            "PORT": config('DB_PORT', default='5432'),
        }
    }
elif not DEBUG and 'test' not in sys.argv:
    # SQLite lives inside the container and is wiped on every redeploy, taking the allowed-user
    # list, recipes and ratings with it. Refuse to start rather than run on it in production.
    raise ImproperlyConfigured('DB_HOST is not set. Add the DB_* variables to the Portainer stack (see README).')
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

LANGUAGE_CODE = "en-us"
TIME_ZONE = config('TIME_ZONE', default='America/Denver')
USE_I18N = True
USE_TZ = True

# WhiteNoise serves static files (the app's own under static/, plus the admin's) from gunicorn.
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTHENTICATION_BACKENDS = (
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
)

LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"

# Outbound email (plan summaries). Gmail SMTP with an app password works.
EMAIL_BACKEND = config('EMAIL_BACKEND', default='django.core.mail.backends.smtp.EmailBackend')
EMAIL_HOST = config('EMAIL_HOST', default='smtp.gmail.com')
EMAIL_PORT = config('EMAIL_PORT', default=587, cast=int)
EMAIL_HOST_USER = config('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')
EMAIL_USE_TLS = config('EMAIL_USE_TLS', default=True, cast=bool)
DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', default='Meal Planner <meals@carrell.us>')

# Claude (meal generation). ANTHROPIC_API_KEY is read by the SDK itself.
CLAUDE_MODEL = config('CLAUDE_MODEL', default='claude-opus-5-5')

# Google Calendar: a service account JSON key file; the target calendar must be
# shared with the service account's email ("Make changes to events").
GOOGLE_SERVICE_ACCOUNT_FILE = config('GOOGLE_SERVICE_ACCOUNT_FILE', default='/app/secrets/google_service_account.json')

# Kroger (King Soopers) developer app — https://developer.kroger.com
KROGER_CLIENT_ID = config('KROGER_CLIENT_ID', default='')
KROGER_CLIENT_SECRET = config('KROGER_CLIENT_SECRET', default='')
# New developer apps live in the certification environment (https://api-ce.kroger.com/v1)
# until Kroger approves them for production.
KROGER_API_BASE = config('KROGER_API_BASE', default='https://api.kroger.com/v1')

# Shared secret Airflow sends to the /internal/ endpoints.
INTERNAL_API_SECRET = config('INTERNAL_API_SECRET', default='')

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
