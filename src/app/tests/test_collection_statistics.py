from datetime import timedelta
from unittest.mock import patch
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from app.collection_statistics import dashboard, public_facts, duration, TYPES
from app.forms import GameForm, MovieForm
from app.models import (
    Item,
    CollectionFacts,
    Game,
    Episode,
)


class CollectionFixtures:
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(username="owner")
        self.other = get_user_model().objects.create_user(username="other")
        self.client.force_login(self.user)
        self.sequence = 0

    def record(
        self, kind, progress=0, status="Completed", facts=None, user=None, **extra
    ):
        self.sequence += 1
        item = Item.objects.create(
            media_id=str(self.sequence),
            source="manual",
            media_type=kind,
            title=f"Title {self.sequence}",
            image="",
            **({"season_number": 1} if kind == "season" else {}),
        )
        model = apps.get_model("app", kind)
        values = {"item": item, "user": user or self.user, "status": status, **extra}
        if kind not in ("tv", "season"):
            values["progress"] = progress
        obj = model.objects.bulk_create([model(**values)])[0]
        if facts is not None:
            CollectionFacts.objects.create(
                item=item, data=facts, fetched_at=timezone.now()
            )
        return obj


class CollectionStatisticsTests(CollectionFixtures, TestCase):
    def test_every_type_empty_and_owner_isolation(self):
        self.record("game", 600, "Played", user=self.other)
        data = dashboard(self.user)
        self.assertEqual([r["kind"] for r in data["summaries"]], TYPES)
        self.assertEqual(data["titles"], 0)
        self.assertEqual(data["playing"], "0h 00m")
        self.assertEqual(data["records"], [])

    def test_time_units_rewatches_unknowns_and_no_double_tv(self):
        self.record("game", 150, "Played")
        movie = self.record("movie", facts={"runtime": 100}, end_date=timezone.now())
        model = type(movie)
        model.objects.bulk_create(
            [
                model(
                    item=movie.item,
                    user=self.user,
                    status="Completed",
                    end_date=timezone.now(),
                )
            ]
        )
        self.record("movie", status="Planning", facts={"runtime": 900})
        self.record("movie", facts={})
        self.record("anime", 3, "In progress", {"runtime": 20})
        tv = self.record("tv")
        season = self.record(
            "season", related_tv=tv, facts={"episodes": {"1": 40, "2": None}}
        )
        for n in (1, 1, 2):
            item, _ = Item.objects.get_or_create(
                media_id=season.item.media_id,
                source="manual",
                media_type="episode",
                season_number=1,
                episode_number=n,
                defaults={"title": "Episode", "image": ""},
            )
            Episode.objects.bulk_create(
                [Episode(item=item, related_season=season, end_date=timezone.now())]
            )
        for kind, amount in [
            ("book", 250),
            ("manga", 40),
            ("comic", 6),
            ("boardgame", 9),
        ]:
            self.record(kind, amount)
        data = dashboard(self.user)
        rows = {r["kind"]: r for r in data["summaries"]}
        self.assertEqual(data["playing"], "2h 30m")
        self.assertEqual(
            data["viewing"], "5h 40m"
        )  # 200 + 60 + 80, no season duplication
        self.assertEqual(rows["movie"]["units"], 3)
        self.assertEqual(rows["movie"]["known"], 2)
        self.assertEqual(rows["tv"]["units"], 3)
        self.assertEqual(rows["season"]["minutes"], 80)
        self.assertEqual(rows["game"]["units"], 2.5)
        for kind, amount in [
            ("book", 250),
            ("manga", 40),
            ("comic", 6),
            ("boardgame", 9),
        ]:
            self.assertEqual(rows[kind]["units"], amount)
            self.assertEqual(rows[kind]["minutes"], 0)
        self.assertEqual(data["repeats"], 1)
        self.assertEqual(sum(data["chart"]["sources"].values()), data["titles"])

    def test_steam_baseline_not_counted_as_daily_playtime(self):
        obj = self.record("game", 600, "Played")
        Game.history.bulk_create(
            [
                Game.history.model(
                    id=obj.pk,
                    progress=600,
                    status="Played",
                    history_type="+",
                    history_date=timezone.now() - timedelta(days=2),
                )
            ]
        )
        Game.history.bulk_create(
            [
                Game.history.model(
                    id=obj.pk,
                    progress=630,
                    status="Played",
                    history_type="~",
                    history_date=timezone.now() - timedelta(days=1),
                )
            ]
        )
        Game.history.bulk_create(
            [
                Game.history.model(
                    id=obj.pk,
                    progress=630,
                    status="Played",
                    history_type="~",
                    history_date=timezone.now(),
                )
            ]
        )
        data = dashboard(self.user, "game")
        self.assertEqual(sum(data["chart"]["recordedTime"][0]["data"]), 0.5)
        self.assertEqual(sum(data["chart"]["progress"]["values"]), 30)
        self.assertEqual(data["playing"], "10h 00m")

    def test_repeat_game_does_not_duplicate_cumulative_time(self):
        obj = self.record("game", 600, "Played")
        Game.objects.bulk_create(
            [Game(item=obj.item, user=self.user, progress=500, status="Planned")]
        )
        self.assertEqual(dashboard(self.user)["playing"], "10h 00m")

    def test_notes_and_ratings_distinct_and_zero_rating_counts(self):
        self.record("book", 10, score=0, notes="Thoughts")
        self.record("book", 20, score=8)
        data = dashboard(self.user, "book")
        self.assertEqual((data["rated"], data["reviewed"], data["average"]), (2, 1, 4))

    @patch("app.tasks.refresh_collection_facts.delay")
    def test_page_csv_filters_and_refresh_auth(self, enqueue):
        obj = self.record("game", 60, "Played")
        Item.objects.filter(pk=obj.item_id).update(title="=FORMULA()")
        response = self.client.get(reverse("statistics"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Time well spent.")
        self.assertContains(response, "1h 00m")
        self.assertContains(response, "collection-chart-data")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(
            self.client.get(reverse("statistics"), {"type": "nope"}).status_code, 400
        )
        csv = self.client.get(reverse("statistics"), {"export": "csv", "type": "game"})
        self.assertIn("'=FORMULA()", csv.content.decode())
        self.assertEqual(
            self.client.get(reverse("refresh_statistics")).status_code, 405
        )
        self.client.logout()
        self.assertEqual(self.client.get(reverse("statistics")).status_code, 302)

    def test_facts_do_not_guess_runtimes(self):
        self.assertEqual(duration("1h 20min"), 80)
        self.assertEqual(duration("40 min"), 40)
        self.assertIsNone(duration("Unknown"))
        facts = public_facts(
            {
                "details": {"release_date": "2024-01-02", "runtime": None},
                "genres": ["Drama"],
            }
        )
        self.assertIsNone(facts["runtime"])
        self.assertEqual(facts["year"], 2024)


class GameStatusTests(CollectionFixtures, TestCase):
    def test_game_forms_only_offer_new_states(self):
        self.assertEqual(
            list(dict(GameForm().fields["status"].choices)),
            ["Planned", "Played", "In progress", "Dropped"],
        )
        self.assertIn("Completed", dict(MovieForm().fields["status"].choices))
        self.assertNotIn("Played", dict(MovieForm().fields["status"].choices))

    def test_legacy_imports_normalize_and_database_rejects_invalid_state(self):
        obj = self.record("game", 30, "Completed")
        obj.refresh_from_db()
        self.assertEqual(obj.status, "Played")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Game.objects.filter(pk=obj.pk).update(status="Paused")

    @patch("app.models.Item.fetch_releases")
    def test_progress_never_marks_game_completed_or_clamps_time(self, fetch):
        obj = self.record("game", 60, "In progress")
        obj.progress = 100000
        obj.save()
        obj.refresh_from_db()
        self.assertEqual((obj.status, obj.progress), ("In progress", 100000))


class CollectionFactsTaskTests(CollectionFixtures, TestCase):
    @patch("app.providers.services.get_media_metadata")
    def test_owner_scope_cache_and_failed_refresh_preserve_previous_facts(self, fetch):
        from app.tasks import refresh_collection_facts

        owned = self.record("movie")
        self.record("movie", user=self.other)
        fetch.return_value = {"details": {"runtime": "100 min"}}
        refresh_collection_facts(self.user.pk)
        self.assertEqual(fetch.call_count, 1)
        fact = CollectionFacts.objects.get(item=owned.item)
        self.assertEqual(fact.data["runtime"], 100)
        refresh_collection_facts(self.user.pk)
        self.assertEqual(fetch.call_count, 1)
        fact.fetched_at = timezone.now() - timedelta(days=8)
        fact.save()
        fetch.side_effect = RuntimeError("Provider unavailable")
        refresh_collection_facts(self.user.pk)
        fact.refresh_from_db()
        self.assertTrue(fact.error)
        self.assertEqual(fact.data["runtime"], 100)
        refresh_collection_facts(self.user.pk)
        self.assertEqual(fetch.call_count, 2)
        fact.attempted_at = timezone.now() - timedelta(minutes=16)
        fact.save()
        refresh_collection_facts(self.user.pk)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(CollectionFacts.objects.count(), 1)
