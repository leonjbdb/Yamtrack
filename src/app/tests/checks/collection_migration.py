import os, sys, tempfile

sys.path.insert(0, os.path.abspath("src"))
os.environ["DJANGO_SETTINGS_MODULE"] = "game_connections.test_settings"
from django.conf import settings

fd, dbpath = tempfile.mkstemp(suffix=".sqlite3")
os.close(fd)
settings.DATABASES["default"]["NAME"] = dbpath
import django

django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

old_targets = [
    ("app", "0064_restore_tz_shifted_media_dates"),
    ("users", "0056_user_home_hide_unreleased"),
]
new_targets = [
    ("app", "0065_collectionfacts_alter_game_status_and_more"),
    ("users", "0057_remove_user_game_status_valid_and_more"),
]
try:
    executor = MigrationExecutor(connection)
    executor.migrate(old_targets)
    old = executor.loader.project_state(old_targets).apps
    user = old.get_model("users", "User").objects.create(
        username="migration-fixture", game_status="Paused"
    )
    Game = old.get_model("app", "Game")
    History = old.get_model("app", "HistoricalGame")
    Item = old.get_model("app", "Item")
    from django.utils import timezone

    for i, status in enumerate(
        ["Completed", "Paused", "Planning", "In progress", "Dropped"]
    ):
        item = Item.objects.create(
            media_id=str(i), source="manual", media_type="game", title=status, image=""
        )
        game = Game.objects.create(
            user_id=user.pk,
            item_id=item.pk,
            status=status,
            progress=120 + i,
            notes="Keep this",
            score=7,
            end_date=timezone.now(),
        )
        History.objects.create(
            id=game.pk,
            status=status,
            progress=120 + i,
            notes="Keep this",
            score=7,
            history_type="+",
            history_date=timezone.now(),
        )
    movie_item = Item.objects.create(
        media_id="movie", source="manual", media_type="movie", title="Movie", image=""
    )
    old.get_model("app", "Movie").objects.create(
        user_id=user.pk, item_id=movie_item.pk, status="Completed"
    )
    before = list(
        Game.objects.order_by("pk").values_list(
            "progress", "notes", "score", "end_date"
        )
    )
    executor = MigrationExecutor(connection)
    executor.migrate(new_targets)
    new = executor.loader.project_state(new_targets).apps
    assert list(
        new.get_model("app", "Game")
        .objects.order_by("pk")
        .values_list("status", flat=True)
    ) == ["Played", "Played", "Planned", "In progress", "Dropped"]
    assert list(
        new.get_model("app", "HistoricalGame")
        .objects.order_by("id")
        .values_list("status", flat=True)
    ) == ["Played", "Played", "Planned", "In progress", "Dropped"]
    assert (
        list(
            new.get_model("app", "Game")
            .objects.order_by("pk")
            .values_list("progress", "notes", "score", "end_date")
        )
        == before
    )
    assert new.get_model("app", "Movie").objects.get().status == "Completed"
    assert new.get_model("users", "User").objects.get().game_status == "Played"
    print(
        "PASS: existing game/history/filter statuses migrated; time, notes, ratings, dates and movie status preserved."
    )
finally:
    connection.close()
    os.unlink(dbpath)
