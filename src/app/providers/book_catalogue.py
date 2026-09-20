"""Bounded, cached requests to the two public, free book catalogues."""

import copy
import hashlib
import json
import threading
import time

import requests
from django.conf import settings
from django.core.cache import cache
from pyrate_limiter import RedisBucket
from requests_ratelimiter import LimiterSession

from app.providers import services

_local = threading.local()
HOSTS = {
    "openlibrary": "https://openlibrary.org",
    "wikidata": "https://www.wikidata.org",
}


def session():
    # Separate connection pools per thread; the Redis rate bucket is shared by
    # web and worker processes, and by threads, for each provider host.
    if not hasattr(_local, "session"):
        _local.session = LimiterSession(
            per_second=2,
            max_delay=5,
            bucket_class=RedisBucket,
            bucket_kwargs={"redis": services.redis_db},
            bucket_name=f"{settings.REDIS_PREFIX}:free-books",
        )
    return _local.session


def request(provider, path, params=None, *, ttl=86400, refresh=False):
    if provider not in HOSTS or not path.startswith("/") or path.startswith("//"):
        raise ValueError("Invalid catalogue endpoint")
    identity = json.dumps([provider, path, params], sort_keys=True)
    key = "free-books:v1:" + hashlib.sha256(identity.encode()).hexdigest()
    if not refresh:
        cached = cache.get(key)
        if cached is not None:
            return copy.deepcopy(cached)
    cooldown = f"free-books:cooldown:{provider}"
    if cache.get(cooldown):
        raise services.ProviderAPIError(
            provider, ValueError("Catalogue rate limit; try again later")
        )
    try:
        response = session().get(
            HOSTS[provider] + path,
            params=params,
            headers={
                "User-Agent": settings.BOOK_CATALOGUE_USER_AGENT,
                "Accept": "application/json",
            },
            timeout=(5, 20),
            allow_redirects=False,
        )
        if response.status_code in (429, 503):
            try:
                delay = min(3600, max(1, int(response.headers.get("Retry-After", 60))))
            except ValueError:
                delay = 60
            cache.set(cooldown, time.time(), delay)
        response.raise_for_status()
        if response.is_redirect:
            raise ValueError("Unexpected catalogue redirect")
        result = response.json()
        if not isinstance(result, dict) or result.get("error") or result.get("errors"):
            raise ValueError(
                "Invalid catalogue response: "
                + str(result.get("error", "unexpected JSON"))
                if isinstance(result, dict)
                else "Invalid catalogue response"
            )
    except (requests.RequestException, ValueError) as error:
        raise services.ProviderAPIError(provider, error) from error
    cache.set(key, result, ttl)
    return copy.deepcopy(result)
