"""
Django settings for trips.aarondaws.com.au.

Production values come from environment variables, loaded by systemd from
/etc/trips/trips.env (see deploy/trips.env.example). Run locally with
DJANGO_DEBUG=1 and the defaults below are enough.
"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.utils.csp import CSP

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def env_list(name, default=""):
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


DEBUG = env_bool("DJANGO_DEBUG")

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is off.")
    SECRET_KEY = "dev-only-insecure-key"

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

# Cloudflare Access (Zero Trust) in front of /manage/ and the admin; see core/cfaccess.py.
CF_ACCESS_TEAM_DOMAIN = os.environ.get("CF_ACCESS_TEAM_DOMAIN", "")  # e.g. yourteam.cloudflareaccess.com
CF_ACCESS_AUD = os.environ.get("CF_ACCESS_AUD", "")  # the application's "Application Audience (AUD) Tag"

# The admin lives at a non-default path to keep it out of reach of bots scanning /admin/.
ADMIN_URL = os.environ.get("DJANGO_ADMIN_PATH", "admin/")


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.forms",
    "axes",
    "core",
    "journeys",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "core.cfaccess.CloudflareAccessMiddleware",
    # Must stay last: blocks logins after repeated failures.
    "axes.middleware.AxesMiddleware",
]

AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "django.template.context_processors.csp",
                "core.context_processors.site",
            ],
        },
    },
]

# Let form widgets use project templates (the place picker lives in templates/manage/widgets/).
FORM_RENDERER = "django.forms.renderers.TemplatesSetting"

WSGI_APPLICATION = "config.wsgi.application"


# Database
# SQLite for now; switching to PostgreSQL later is a change to this block plus a data copy.

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("DJANGO_DB_PATH", BASE_DIR / "db.sqlite3"),
        "OPTIONS": {
            # IMMEDIATE avoids "database is locked" errors when two requests write at once;
            # WAL lets pages keep reading while a write is in progress.
            "transaction_mode": "IMMEDIATE",
            "timeout": 5,
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
        },
    }
}


# Password validation

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "manage:login"
LOGIN_REDIRECT_URL = "manage:dashboard"
LOGOUT_REDIRECT_URL = "core:home"

# Login protection (django-axes): lock a username + IP pair out for an hour after 5 failures.
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = 1
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]
AXES_RESET_ON_SUCCESS = True
AXES_CLIENT_IP_CALLABLE = "core.net.client_ip"


# Internationalization

LANGUAGE_CODE = "en-au"
TIME_ZONE = "Australia/Brisbane"
USE_I18N = True
USE_TZ = True


# Static files (served directly by Caddy in production)

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = os.environ.get("DJANGO_STATIC_ROOT", BASE_DIR / "staticfiles")
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # Hashed filenames in production so browsers and Cloudflare can cache them forever.
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        if DEBUG
        else "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Uploaded trip documents are private: stored outside the web root and only
# served through a login-protected view, never directly by Caddy.
MEDIA_ROOT = os.environ.get("DJANGO_MEDIA_ROOT", BASE_DIR / "media")
MEDIA_URL = "/media-not-served/"
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 25 * 1024 * 1024  # for inbound email; Caddy caps every other public path at 1 MB


# Security (production sits behind Cloudflare -> Caddy, which terminates TLS)

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

# Caddy already redirects HTTP to HTTPS and sends the HSTS header.
SILENCED_SYSTEM_CHECKS = ["security.W004", "security.W008"]

# Content-Security-Policy: only this site's own scripts (plus the head's theme script, by nonce),
# map tiles from OpenStreetMap and OpenRailwayMap, and nothing framing the site. Enforced when
# DJANGO_CSP_ENFORCE is set (live since 15 Nov 2026); otherwise report-only. Either way browsers
# report what it blocks (or would block) to /csp-report/, logged as warnings.
CONTENT_SECURITY_POLICY = {
    "default-src": [CSP.SELF],
    "script-src": [CSP.SELF, CSP.NONCE],
    "style-src": [CSP.SELF, CSP.UNSAFE_INLINE],
    "img-src": [CSP.SELF, "data:", "https://tile.openstreetmap.org", "https://*.tiles.openrailwaymap.org"],
    "connect-src": [CSP.SELF],
    "font-src": [CSP.SELF],
    "object-src": [CSP.NONE],
    "base-uri": [CSP.SELF],
    "form-action": [CSP.SELF],
    "frame-ancestors": [CSP.SELF],
    "report-uri": ["/csp-report/"],
}
SECURE_CSP = CONTENT_SECURITY_POLICY if env_bool("DJANGO_CSP_ENFORCE") else {}
SECURE_CSP_REPORT_ONLY = {} if env_bool("DJANGO_CSP_ENFORCE") else CONTENT_SECURITY_POLICY


# Logging: everything WARNING and above goes to stdout, which systemd keeps in the journal.

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "WARNING"},
}

# Live flight schedules for transport auto-fill (AeroDataBox via RapidAPI, free Basic plan); off when empty.
AERODATABOX_RAPIDAPI_KEY = os.environ.get("AERODATABOX_RAPIDAPI_KEY", "")

# Sydney's train, metro, light rail and ferry timetables (Transport for NSW Open Data, free key);
# Sydney's routes are left out while it's empty. Brisbane's and Melbourne's need no key.
TFNSW_API_KEY = os.environ.get("TFNSW_API_KEY", "")

# Melbourne's live train and tram times (Transport Victoria open data, free key); timetabled times while empty.
TRANSPORT_VIC_API_KEY = os.environ.get("TRANSPORT_VIC_API_KEY", "")

# Each city's full timetable for the public journey planner (journeys/timetable.py), beside the database.
TRANSIT_DIR = Path(os.environ.get("DJANGO_TRANSIT_DIR") or Path(DATABASES["default"]["NAME"]).with_name("transit"))

# Real flight routes from adsb.lol's free open archive (journeys/adsb_archive.py); set to 0 to draw arcs.
ADSBLOL_TRACKS = env_bool("ADSBLOL_TRACKS", True)

# Real flight routes (Flightradar24 API, Explorer plan) instead of adsb.lol; off when empty.
FLIGHTRADAR24_API_TOKEN = os.environ.get("FLIGHTRADAR24_API_TOKEN", "")

# Forwarded booking emails (see journeys/inbound.py). The token is the secret part of the receiving
# address; the Mailgun key (if used) verifies each delivery; allowed senders limit who can forward.
INBOUND_EMAIL_TOKEN = os.environ.get("INBOUND_EMAIL_TOKEN", "")
MAILGUN_WEBHOOK_SIGNING_KEY = os.environ.get("MAILGUN_WEBHOOK_SIGNING_KEY", "")
INBOUND_ALLOWED_SENDERS = [s.lower() for s in env_list("INBOUND_ALLOWED_SENDERS")]

SITE_NAME = "Aaron Daws Trips"
SITE_TAGLINE = "Travel plans, places and notes by Aaron Daws."

# Written by deploy.sh with the deploy time (UTC); the change log shows it in AEST.
DEPLOYED_AT_FILE = os.environ.get("DJANGO_DEPLOYED_AT_FILE", BASE_DIR / "DEPLOYED_AT")
