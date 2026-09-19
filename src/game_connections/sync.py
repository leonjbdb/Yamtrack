"""Bounded, owner-scoped sync; stale jobs cannot write after disconnect/reconnect."""

import uuid
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from simple_history.utils import bulk_create_with_history, bulk_update_with_history

from app.mixins import disable_fetch_releases
from app.models import Game, Item, MediaTypes, Sources, GameStatus
from app.providers import igdb, services
from .credentials import decrypt
from .models import GameConnection, LibraryGame
from .providers import ConnectionFailure, itch_identity, itch_library, steam_library


def resolve_game(provider, game):
    source = (
        igdb.ExternalGameSource.STEAM
        if provider == "steam"
        else igdb.ExternalGameSource.ITCH_IO
    )
    game_id = igdb.external_game(game.external_id, source)
    if not game_id:
        return None
    return services.get_media_metadata(
        MediaTypes.GAME.value, str(game_id), Sources.IGDB.value
    )


def apply_library(connection, games, metadata):
    """Preserve manual ratings, notes, finished/dropped statuses and existing games."""
    matched = 0
    with disable_fetch_releases():
        for entry in games:
            data = metadata[entry.external_id]
            item = None
            if data:
                item, _ = Item.objects.get_or_create(
                    media_id=str(data["media_id"]),
                    source=Sources.IGDB,
                    media_type=MediaTypes.GAME,
                    defaults={"title": data["title"], "image": data["image"]},
                )
                status = GameStatus.PLANNED
                if entry.minutes:
                    status = (
                        GameStatus.IN_PROGRESS
                        if entry.recent_minutes
                        else GameStatus.PLAYED
                    )
                game = (
                    Game.objects.filter(user_id=connection.user_id, item=item)
                    .order_by("pk")
                    .first()
                )
                if game is None:
                    game = Game(
                        user_id=connection.user_id,
                        item=item,
                        status=status,
                        progress=entry.minutes or 0,
                    )
                    bulk_create_with_history([game], Game)
                elif entry.minutes is not None:
                    game.progress = max(game.progress, entry.minutes)
                    if game.status in (
                        GameStatus.PLANNED,
                        GameStatus.IN_PROGRESS,
                        GameStatus.PLAYED,
                    ):
                        game.status = status
                    bulk_update_with_history([game], Game, ["progress", "status"])
                matched += 1
            LibraryGame.objects.update_or_create(
                connection=connection,
                external_id=entry.external_id,
                defaults={"title": entry.title, "minutes": entry.minutes, "item": item},
            )
    # A successful fetch updates membership only. Never delete tracked games.
    connection.library.exclude(external_id__in=[g.external_id for g in games]).delete()
    return matched


@sensitive_variables()
def sync_connection(connection_id):
    now = timezone.now()
    lease = uuid.uuid4()
    claimed = (
        GameConnection.objects.filter(
            pk=connection_id, enabled=True, user__is_active=True
        )
        .filter(Q(busy_until__isnull=True) | Q(busy_until__lt=now))
        .update(
            lease=lease,
            busy_until=now + timedelta(minutes=30),
            last_attempt=now,
            status="Syncing",
        )
    )
    if not claimed:
        return
    connection = GameConnection.objects.filter(pk=connection_id, lease=lease).first()
    if not connection:
        return
    try:
        api_key = decrypt(connection)
        if connection.provider == "steam":
            games = steam_library(api_key, connection.external_id)
        elif connection.provider == "itch":
            if itch_identity(api_key) != connection.external_id:
                raise ConnectionFailure(
                    "The credential belongs to a different account. Reconnect."
                )
            games = itch_library(api_key)
        else:
            raise ConnectionFailure("Unsupported game service.")
        metadata = {g.external_id: resolve_game(connection.provider, g) for g in games}
        with transaction.atomic():
            current = (
                GameConnection.objects.select_for_update()
                .filter(
                    pk=connection_id,
                    lease=lease,
                    generation=connection.generation,
                    enabled=True,
                )
                .first()
            )
            if not current:
                return
            matched = apply_library(current, games, metadata)
            current.last_success = timezone.now()
            current.status = f"{len(games)} library games; {matched} matched to IGDB; {len(games) - matched} unmatched."
            current.lease = None
            current.busy_until = None
            current.save(
                update_fields=["last_success", "status", "lease", "busy_until"]
            )
    except Exception as error:
        # Do not propagate request/response objects, credentials or provider error bodies
        # into Celery results, tracebacks, task events or notifications.
        message = (
            str(error)
            if isinstance(error, ConnectionFailure)
            else "Sync failed. Check service availability or reconnect your account."
        )
        GameConnection.objects.filter(
            pk=connection_id, lease=lease, generation=connection.generation
        ).update(status=message, lease=None, busy_until=None)
