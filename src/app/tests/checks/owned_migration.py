"""Exercise the production upgrade on a disposable database, including history."""

import os
import sys
import tempfile

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
from django.utils import timezone

old_targets = [
    ("app", "0066_discovery_catalogue"),
    ("users", "0057_remove_user_game_status_valid_and_more"),
    ("game_connections", "0001_initial"),
]
try:
    executor = MigrationExecutor(connection)
    executor.migrate(old_targets)
    old = executor.loader.project_state(old_targets).apps
    User = old.get_model("users", "User")
    owner = User.objects.create(username="owner")
    other = User.objects.create(username="other")
    Connection = old.get_model("game_connections", "GameConnection")
    library = Connection.objects.create(
        user_id=owner.pk, provider="steam", external_id="test", credential="test-only"
    )
    Game, History, Item = (
        old.get_model("app", name) for name in ("Game", "HistoricalGame", "Item")
    )
    Membership = old.get_model("game_connections", "LibraryGame")
    for i, status in enumerate(
        ["Planned", "Played", "In progress", "Dropped", "Planned"]
    ):
        item = Item.objects.create(
            media_id=str(i), source="igdb", media_type="game", title=status, image=""
        )
        game = Game.objects.create(
            user_id=owner.pk,
            item_id=item.pk,
            status=status,
            progress=i * 60,
            notes="Keep",
            score=8,
            start_date=timezone.now(),
            end_date=timezone.now(),
        )
        Game.objects.create(user_id=other.pk, item_id=item.pk, status="Planned")
        History.objects.create(
            id=game.pk,
            status=status,
            progress=i * 60,
            notes="Keep",
            score=8,
            history_type="+",
            history_date=timezone.now(),
        )
        if i < 4:
            Membership.objects.create(
                connection_id=library.pk,
                item_id=item.pk,
                external_id=str(i),
                title=status,
                minutes=i * 60,
            )
    before = list(
        Game.objects.order_by("pk").values_list(
            "pk",
            "user_id",
            "item_id",
            "progress",
            "notes",
            "score",
            "start_date",
            "end_date",
        )
    )
    history_before = list(History.objects.order_by("history_id").values())
    executor = MigrationExecutor(connection)
    targets = executor.loader.graph.leaf_nodes()
    executor.migrate(targets)
    new = executor.loader.project_state(targets).apps
    Game = new.get_model("app", "Game")
    assert list(
        Game.objects.filter(user_id=owner.pk)
        .order_by("pk")
        .values_list("status", flat=True)
    ) == ["Owned", "Played", "In progress", "Dropped", "Planned"]
    assert set(
        Game.objects.filter(user_id=other.pk).values_list("status", flat=True)
    ) == {"Planned"}
    assert before == list(
        Game.objects.order_by("pk").values_list(
            "pk",
            "user_id",
            "item_id",
            "progress",
            "notes",
            "score",
            "start_date",
            "end_date",
        )
    )
    History = new.get_model("app", "HistoricalGame")
    assert list(History.objects.order_by("history_id").values())[:5] == history_before
    assert History.objects.count() == 6
    assert History.objects.latest("history_date").status == "Owned"
    User = new.get_model("users", "User")
    User.objects.filter(pk=owner.pk).update(
        game_status="Owned", movie_status="Owned", list_detail_status="Owned"
    )
    assert (
        new.get_model("game_connections", "LibraryGame")
        .objects.filter(owned=True)
        .count()
        == 4
    )
    print(
        "PASS: Owned upgrade preserves activity, other accounts, manual plans, all fields and original history; adds an audit event and accepts Owned filters."
    )
finally:
    connection.close()
    os.unlink(dbpath)
