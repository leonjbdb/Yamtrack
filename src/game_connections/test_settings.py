import base64

from config.test_settings import *  # noqa: F403

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
# Tests never touch runtime credentials or the production cache/broker.
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
GAME_CONNECTIONS_KEY = base64.urlsafe_b64encode(b"0" * 32).decode()
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
LOGGING = {"version": 1, "disable_existing_loggers": True}
