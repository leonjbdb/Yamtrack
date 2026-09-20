"""Bounded, cached requests to the two public, free book catalogues."""

import copy
import hashlib
import json
import logging
import math
import threading
import time
from email.utils import parsedate_to_datetime

import requests
from django.conf import settings
from django.core.cache import cache
from pyrate_limiter import RedisBucket
from requests_ratelimiter import LimiterSession

from app.providers import services

logger = logging.getLogger(__name__)
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


class CatalogueUnavailable(services.ProviderAPIError):
    """A temporary upstream failure with an explicit, shared retry deadline."""

    def __init__(self, provider, error, retry_after, status_code=None):
        super().__init__(provider, error)
        self.retry_after = max(1, math.ceil(retry_after))
        self.status_code = status_code or self.status_code
        label = {"openlibrary": "Open Library", "wikidata": "Wikidata"}[provider]
        reason = (
            "is limiting requests"
            if self.status_code == 429
            else "is temporarily unavailable"
        )
        self.user_message = (
            f"{label} {reason}. Try again in {self.retry_after} seconds."
        )


def retry_delay(response):
    """Honor both standard Retry-After forms, without blocking a web worker long."""
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(1, math.ceil(float(value)))
    except (TypeError, ValueError, OverflowError):
        try:
            return max(
                1, math.ceil(parsedate_to_datetime(value).timestamp() - time.time())
            )
        except (TypeError, ValueError, OverflowError):
            return None


def unavailable(provider, cooldown, error, delay, status_code=None):
    cache.set(cooldown, {"until": time.time() + delay, "status": status_code}, delay)
    raise CatalogueUnavailable(provider, error, delay, status_code) from error


def request(provider, path, params=None, *, ttl=86400, refresh=False):
    if provider not in HOSTS or not path.startswith("/") or path.startswith("//"):
        raise ValueError("Invalid catalogue endpoint")
    identity = json.dumps([provider, path, params], sort_keys=True)
    key = "free-books:v1:" + hashlib.sha256(identity.encode()).hexdigest()
    if not refresh:
        cached = cache.get(key)
        if cached is not None:
            return copy.deepcopy(cached)
    cooldown = f"free-books:cooldown:v2:{provider}"
    blocked = cache.get(cooldown)
    if blocked and blocked["until"] > time.time():
        raise CatalogueUnavailable(
            provider,
            ValueError("Catalogue retry deadline active"),
            blocked["until"] - time.time(),
            blocked["status"],
        )
    for attempt in range(3):
        try:
            response = session().get(
                HOSTS[provider] + path,
                params=params,
                headers={
                    "User-Agent": settings.BOOK_CATALOGUE_USER_AGENT,
                    "Accept": "application/json",
                },
                timeout=(5, 10),
                allow_redirects=False,
            )
        except (requests.ConnectionError, requests.Timeout) as error:
            logger.warning(
                "%s %s: %s (attempt %s)",
                provider,
                path,
                type(error).__name__,
                attempt + 1,
            )
            if attempt < 2:
                time.sleep(attempt + 1)
                continue
            unavailable(provider, cooldown, error, 15)
        except requests.RequestException as error:
            raise services.ProviderAPIError(provider, error) from error
        if response.status_code in (429, 502, 503, 504):
            delay = retry_delay(response)
            logger.warning(
                "%s %s: HTTP %s (attempt %s)",
                provider,
                path,
                response.status_code,
                attempt + 1,
            )
            # Rate limits and long server delays are returned to the user, never
            # ignored. Only safe GETs with a short delay are retried in-request.
            if (
                response.status_code != 429
                and attempt < 2
                and (delay is None or delay <= 2)
            ):
                response.close()
                time.sleep(delay or attempt + 1)
                continue
            error = requests.HTTPError(response=response)
            unavailable(
                provider,
                cooldown,
                error,
                delay or (60 if response.status_code == 429 else 15),
                response.status_code,
            )
        try:
            response.raise_for_status()
            if response.is_redirect:
                raise ValueError("Unexpected catalogue redirect")
            result = response.json()
            if (
                not isinstance(result, dict)
                or result.get("error")
                or result.get("errors")
            ):
                raise ValueError("Invalid catalogue response")
        except (requests.RequestException, ValueError) as error:
            raise services.ProviderAPIError(provider, error) from error
        cache.set(key, result, ttl)
        return copy.deepcopy(result)
