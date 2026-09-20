"""Populate public spelling candidates from catalogue identities already tracked."""

from celery import shared_task
from django.core.cache import cache
from app.models import Item
from app.discovery.catalogue import remember
from app.discovery.providers import title_credits, game_companies


@shared_task(ignore_result=True, soft_time_limit=3500, time_limit=3550)
def index_catalogue_credits():
    lock = "discovery:index-catalogue-running"
    if not cache.add(lock, True, timeout=3600):
        return
    try:
        items = (
            Item.objects.exclude(source="manual")
            .exclude(media_type__in=["episode", "season"])
            .order_by("pk")
        )
        for item in items.iterator(chunk_size=100):
            remember(
                [
                    {
                        "source": item.source,
                        "kind": item.media_type,
                        "external_id": item.media_id,
                        "name": item.title,
                        "image": item.image,
                    }
                ]
            )
            indexed_key = (
                f"discovery:indexed:{item.source}:{item.media_type}:{item.media_id}"
            )
            if cache.get(indexed_key):
                continue
            if item.source == "tmdb" and item.media_type in ("movie", "tv"):
                title_credits(item.media_type, item.media_id)
            elif item.source == "igdb" and item.media_type == "game":
                game_companies(item.media_id)
            cache.set(indexed_key, True, 7 * 86400)
    finally:
        cache.delete(lock)
