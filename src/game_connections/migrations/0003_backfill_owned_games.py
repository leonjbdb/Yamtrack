from django.db import migrations
from django.utils import timezone


def backfill_owned(apps, schema_editor):
    alias = schema_editor.connection.alias
    Game = apps.get_model("app", "Game")
    History = apps.get_model("app", "HistoricalGame")
    LibraryGame = apps.get_model("game_connections", "LibraryGame")
    memberships = LibraryGame.objects.using(alias).filter(owned=True, item__isnull=False).values_list("connection__user_id", "item_id").distinct()
    for user_id, item_id in memberships.iterator():
        games = Game.objects.using(alias).filter(user_id=user_id, item_id=item_id, status="Planned")
        for game in games.iterator():
            Game.objects.using(alias).filter(pk=game.pk).update(status="Owned")
            History.objects.using(alias).create(
                id=game.pk, status="Owned", progress=game.progress, score=game.score,
                start_date=game.start_date, end_date=game.end_date, notes=game.notes,
                history_type="~", history_date=timezone.now(),
                history_change_reason="Owned library status",
            )


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0067_owned_status_and_wishlist"),
        ("game_connections", "0002_owned_status_and_wishlist"),
    ]
    operations = [migrations.RunPython(backfill_owned)]
