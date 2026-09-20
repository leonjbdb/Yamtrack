from copy import deepcopy
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app.models import TV, Episode, Item, Season, Status
from app.providers.services import ProviderAPIError
from app.templatetags.app_tags import get_sidebar_media_types
from app.tv_tracking import summary


class TVTrackingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="viewer")
        self.other = get_user_model().objects.create_user(username="other")
        self.client.force_login(self.user)
        self.show = {
            "source": "tmdb",
            "media_id": "42",
            "media_type": "tv",
            "title": "Test Show",
            "image": "https://example.com/poster.jpg",
            "details": {"status": "Ended"},
            "related": {
                "seasons": [
                    {"season_number": 1, "max_progress": 3},
                    {"season_number": 2, "max_progress": 1},
                    {"season_number": 0, "max_progress": 1},
                ]
            },
        }
        self.seasons = {}
        for number, episodes in [(1, [1, 2, 3]), (2, [1]), (0, [1])]:
            self.seasons[number] = {
                "source": "tmdb",
                "media_id": "42",
                "season_number": number,
                "title": "Test Show",
                "image": self.show["image"],
                "episodes": [
                    {
                        "episode_number": n,
                        "name": f"Episode {n}",
                        "air_date": "2020-01-01",
                        "runtime": 40,
                        "still_path": None,
                    }
                    for n in episodes
                ],
            }
        provider = patch(
            "app.providers.services.get_media_metadata", side_effect=self.metadata
        )
        self.provider = provider.start()
        self.addCleanup(provider.stop)
        self.path = reverse("tv_watch", args=["tmdb", "42", 1])

    def metadata(self, kind, media_id, source, numbers=None):
        if kind == "tv":
            return deepcopy(self.show)
        if kind == "season":
            return deepcopy(self.seasons[numbers[0]])
        if kind == "tv_with_seasons":
            return {
                **deepcopy(self.show),
                **{f"season/{n}": deepcopy(self.seasons[n]) for n in numbers},
            }
        self.fail(f"Unexpected provider request: {kind}")

    def watch(self, number=1, action="watch", season=1):
        return self.client.post(
            reverse("tv_watch", args=["tmdb", "42", season]),
            {"action": action, "episode_number": number},
        )

    def test_summary_is_lazy_and_get_never_creates_tracking(self):
        response = self.client.get(reverse("tv_tracker", args=["tmdb", "42"]))
        self.assertContains(response, "0 / 4 episodes watched")
        self.assertContains(response, "Specials")
        self.assertEqual(TV.objects.count(), 0)
        self.provider.assert_called_once_with("tv", "42", "tmdb")

    def test_late_episode_is_one_watch_not_three(self):
        self.assertEqual(self.watch(3).status_code, 200)
        tv, season = TV.objects.get(), Season.objects.get()
        self.assertEqual(tv.progress, 1)
        self.assertEqual(tv.status, Status.IN_PROGRESS)
        self.assertEqual(season.status, Status.IN_PROGRESS)
        response = self.client.get(reverse("tv_tracker_season", args=["tmdb", "42", 1]))
        self.assertEqual(
            [ep["watched"] for ep in response.context["episodes"]], [False, False, True]
        )
        self.assertContains(response, "1 / 4 episodes watched")

    def test_watch_is_idempotent_and_history_is_recorded(self):
        self.watch(1)
        first = Episode.objects.get()
        self.watch(1)
        self.assertEqual(Episode.objects.count(), 1)
        self.assertEqual(Episode.objects.get().end_date, first.end_date)
        self.assertEqual(first.history.count(), 1)

    def test_whole_season_fills_gaps_without_replacing_existing_dates(self):
        self.watch(3)
        last = Episode.objects.get()
        self.watch(action="season")
        self.assertEqual(
            set(Episode.objects.values_list("item__episode_number", flat=True)),
            {1, 2, 3},
        )
        self.assertEqual(Episode.objects.get(pk=last.pk).end_date, last.end_date)
        self.assertEqual(Season.objects.get().status, Status.COMPLETED)
        self.assertEqual(TV.objects.get().status, Status.IN_PROGRESS)
        self.watch(action="season")
        self.assertEqual(Episode.objects.count(), 3)

    def test_unaired_and_unknown_dates_not_marked_by_bulk_action(self):
        self.seasons[1]["episodes"][1]["air_date"] = "2999-01-01"
        self.seasons[1]["episodes"][2]["air_date"] = None
        self.assertEqual(self.watch(2).status_code, 400)
        self.assertEqual(self.watch(3).status_code, 400)
        self.assertEqual(TV.objects.count(), 0)
        self.watch(action="season")
        self.assertEqual(
            list(Episode.objects.values_list("item__episode_number", flat=True)), [1]
        )
        self.assertEqual(Season.objects.get().status, Status.IN_PROGRESS)

    def test_specials_do_not_inflate_show_progress(self):
        self.watch(season=0)
        self.assertEqual(TV.objects.get().progress, 0)
        data = summary(self.user, self.show)
        self.assertEqual(data["watched"], 0)
        self.assertEqual(data["seasons"][-1]["watched"], 1)

    def test_all_known_seasons_complete_ended_show_only(self):
        self.watch(action="season")
        self.watch(season=2)
        self.assertEqual(TV.objects.get().status, Status.COMPLETED)
        self.watch(1, action="undo")
        self.assertEqual(TV.objects.get().status, Status.IN_PROGRESS)
        self.assertEqual(
            Season.objects.get(item__season_number=1).status, Status.IN_PROGRESS
        )

    def test_returning_show_stays_in_progress_when_caught_up(self):
        self.show["details"]["status"] = "Returning Series"
        self.watch(action="season")
        self.watch(season=2)
        self.assertEqual(TV.objects.get().status, Status.IN_PROGRESS)

    def test_undo_last_watch_preserves_earlier_rewatch_history(self):
        self.watch(1)
        first = Episode.objects.get()
        second = Episode.objects.bulk_create(
            [
                Episode(
                    item=first.item,
                    related_season=first.related_season,
                    end_date=timezone.now(),
                )
            ]
        )[0]
        self.assertEqual(TV.objects.get().progress, 1)
        response = self.watch(1, action="undo")
        self.assertEqual(response.context["episodes"][0]["watched"], True)
        self.assertTrue(Episode.objects.filter(pk=first.pk).exists())
        self.assertFalse(Episode.objects.filter(pk=second.pk).exists())

    def test_nonowner_ids_cannot_change_other_library(self):
        self.watch(1)
        original = Episode.objects.get()
        self.client.force_login(self.other)
        self.watch(1, action="undo")
        self.assertTrue(Episode.objects.filter(pk=original.pk).exists())
        self.watch(2)
        self.assertEqual(
            Episode.objects.filter(related_season__user=self.user).count(), 1
        )
        self.assertEqual(
            Episode.objects.filter(related_season__user=self.other).count(), 1
        )

    def test_invalid_requests_do_not_write(self):
        for data in (
            {"action": "delete-all"},
            {"action": "watch"},
            {"action": "watch", "episode_number": -1},
        ):
            self.assertEqual(self.client.post(self.path, data).status_code, 400)
        self.assertEqual(self.watch(99).status_code, 404)
        self.assertEqual(self.watch(season=99).status_code, 404)
        self.assertEqual(TV.objects.count(), 0)

    def test_paused_and_dropped_choices_survive_progress_updates(self):
        self.watch(1)
        for status in (Status.PAUSED, Status.DROPPED):
            TV.objects.update(status=status)
            Season.objects.update(status=status)
            self.watch(action="season")
            self.assertEqual(TV.objects.get().status, status)
            self.assertEqual(Season.objects.get().status, status)

    def test_batch_failure_rolls_back_entire_change(self):
        with patch(
            "app.tv_tracking.bulk_create_with_history",
            side_effect=RuntimeError("write failed"),
        ):
            with self.assertRaises(RuntimeError):
                self.watch(action="season")
        self.assertEqual(TV.objects.count(), 0)
        self.assertEqual(Item.objects.count(), 0)

    def test_catalogue_error_is_visible_and_retryable(self):
        self.provider.side_effect = ProviderAPIError("tmdb", "unavailable")
        response = self.client.get(reverse("tv_tracker", args=["tmdb", "42"]))
        self.assertContains(response, "Episode data could not be loaded")
        self.assertContains(response, "Try Again")
        self.assertEqual(Episode.objects.count(), 0)

    def test_one_sidebar_entry_and_old_season_collection_redirect(self):
        labels = [row["media_type"] for row in get_sidebar_media_types(self.user)]
        self.assertIn("tv", labels)
        self.assertNotIn("season", labels)
        self.assertRedirects(
            self.client.get("/viewer/season"),
            "/viewer/tv",
            fetch_redirect_response=False,
        )
        self.assertContains(self.client.get("/viewer/tv"), "TV Shows")

    def test_hidden_episode_titles_are_not_revealed_by_default(self):
        self.user.obfuscate_unseen_episodes = True
        self.user.save(update_fields=["obfuscate_unseen_episodes"])
        response = self.client.get(reverse("tv_tracker_season", args=["tmdb", "42", 1]))
        self.assertContains(response, "<summary>Episode 1</summary>")

    def test_legacy_completion_fills_nonconsecutive_gaps(self):
        self.watch(3)
        season = Season.objects.get()
        missing = season.get_remaining_eps(self.seasons[1], timezone.localdate())
        self.assertEqual({ep.item.episode_number for ep in missing}, {1, 2})

    def test_native_episode_save_does_not_complete_after_only_finale(self):
        self.watch(1)
        season = Season.objects.get()
        item = season.get_episode_item(3, self.seasons[1])
        Episode.objects.create(
            item=item, related_season=season, end_date=timezone.now()
        )
        season.refresh_from_db()
        self.assertEqual(season.status, Status.IN_PROGRESS)

    def test_expired_tracker_uses_full_sso_navigation(self):
        self.client.logout()
        response = self.client.post(
            self.path,
            {"action": "watch", "episode_number": 1},
            headers={"HX-Request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("/accounts/login/?next=", response["HX-Redirect"])
        self.assertIn("/details/tmdb/tv/42/episodes", response["HX-Redirect"])
        self.assertEqual(Episode.objects.count(), 0)

    def test_failed_watch_fetch_is_visible_and_never_saves(self):
        self.provider.side_effect = ProviderAPIError("tmdb", "unavailable")
        response = self.watch(1)
        self.assertContains(response, "Episode data could not be loaded")
        self.assertEqual(Episode.objects.count(), 0)

    def test_home_groups_shows_and_provides_episode_controls(self):
        self.watch(1)
        response = self.client.get(reverse("home"))
        self.assertContains(response, "TV Shows")
        self.assertNotContains(response, "TV Seasons")
        self.assertContains(response, reverse("tv_tracker", args=["tmdb", "42"]))
        self.assertNotContains(response, "/progress_edit/tv/")

    def test_explicit_dated_rewatch_retains_both_watches(self):
        self.watch(1)
        response = self.client.post(
            self.path,
            {"action": "log", "episode_number": 1, "end_date": "2024-05-20T12:00"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Episode.objects.count(), 2)
        self.assertEqual(TV.objects.get().progress, 1)
        self.assertEqual(response.context["episodes"][0]["watches"], 2)

    def test_undo_missing_watch_does_not_add_show(self):
        self.assertEqual(self.watch(1, action="undo").status_code, 200)
        self.assertEqual(TV.objects.count(), 0)
