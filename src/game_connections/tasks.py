from datetime import timedelta

from celery import shared_task
from django.db.models import Q
from django.utils import timezone

from .models import GameConnection
from .sync import sync_connection


@shared_task(ignore_result=True, soft_time_limit=1500, time_limit=1560)
def sync_game_connection(connection_id):
    """Task messages contain only a database ID, never credentials."""
    sync_connection(connection_id)


@shared_task(ignore_result=True)
def sync_due_connections():
    """Daily sync, dispatched hourly; inactive users are excluded."""
    due = GameConnection.objects.filter(enabled=True, user__is_active=True).filter(
        Q(last_attempt__isnull=True)
        | Q(last_attempt__lt=timezone.now() - timedelta(days=1))
    )
    for connection_id in due.values_list("pk", flat=True):
        sync_game_connection.delay(connection_id)
