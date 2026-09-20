import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from app.models import UserMessage

logger = logging.getLogger(__name__)


@shared_task(name="Cleanup user messages")
def cleanup_user_messages():
    """Delete shown user messages older than the configured retention window."""
    cutoff = timezone.now() - timedelta(days=settings.USER_MESSAGE_RETENTION_DAYS)
    deleted_count, _ = UserMessage.objects.filter(
        shown_at__isnull=False,
        shown_at__lt=cutoff,
    ).delete()

    logger.info("Deleted %s old shown user messages.", deleted_count)

    return deleted_count


@shared_task(ignore_result=True, soft_time_limit=1400, time_limit=1450)
def refresh_collection_facts(user_id):
    """Enrich public catalogue facts in the worker, never during page rendering."""
    from django.contrib.auth import get_user_model
    from django.core.cache import cache
    from django.db.models import Q
    from app.collection_statistics import tracked_item_ids, public_facts
    from app.models import Item, CollectionFacts
    from app.providers import services

    user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
    if not user:
        return
    lock = f"collection-facts-running:{user_id}"
    if not cache.add(lock, True, timeout=1500):
        return
    try:
        ids = tracked_item_ids(user)
        cutoff = timezone.now() - timedelta(days=7)
        recent = (
            CollectionFacts.objects.filter(item_id__in=ids)
            .filter(
                Q(error=False, fetched_at__gte=cutoff)
                | Q(
                    error=True, attempted_at__gte=timezone.now() - timedelta(minutes=15)
                )
            )
            .values_list("item_id", flat=True)
        )
        items = (
            Item.objects.filter(pk__in=ids)
            .exclude(pk__in=recent)
            .order_by("media_type", "pk")
        )
        for item in items.iterator(chunk_size=100):
            now = timezone.now()
            try:
                raw = services.get_media_metadata(
                    item.media_type, item.media_id, item.source, [item.season_number]
                )
                CollectionFacts.objects.update_or_create(
                    item=item,
                    defaults={
                        "data": public_facts(raw),
                        "fetched_at": now,
                        "attempted_at": now,
                        "error": False,
                    },
                )
            except Exception:
                # Keep the previous successful facts, flag the provider failure.
                fact, _ = CollectionFacts.objects.get_or_create(item=item)
                fact.attempted_at, fact.error = now, True
                fact.save(update_fields=["attempted_at", "error"])
    finally:
        cache.delete(lock)

# Register the public catalogue indexing task with the existing app worker.
from app.discovery.tasks import index_catalogue_credits  # noqa: F401, E402
