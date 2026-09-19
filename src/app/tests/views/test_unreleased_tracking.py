from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app.forms import get_form_class
from app.models import Item, MediaTypes, Movie, Sources, Status
from app.release_status import is_unreleased


class UnreleasedTrackingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="viewer")
        self.client.force_login(self.user)

    @patch("app.views.services.get_media_metadata")
    def test_all_media_types_default_to_planning_for_future_release(self, metadata):
        cases = [
            ("movie", "tmdb", "release_date"),
            ("tv", "tmdb", "first_air_date"),
            ("season", "tmdb", "first_air_date"),
            ("game", "igdb", "release_date"),
            ("anime", "mal", "start_date"),
            ("manga", "mal", "start_date"),
            ("book", "hardcover", "publish_date"),
            ("book", "openlibrary", "publish_date"),
            ("comic", "comicvine", "start_date"),
            ("boardgame", "bgg", "year"),
        ]
        for media_type, source, field in cases:
            with self.subTest(media_type=media_type, source=source):
                metadata.return_value = {
                    "title": "Future title",
                    "details": {field: "2999" if field == "year" else "2999-01-01"},
                }
                kwargs = {"source": source, "media_type": media_type, "media_id": "123"}
                if media_type == "season":
                    kwargs["season_number"] = 1
                response = self.client.get(
                    reverse("track_modal", kwargs=kwargs),
                    {"return_url": "/", "is_create": "true"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.context["form"]["status"].value(), Status.PLANNING
                )
                self.assertIn(
                    Status.COMPLETED,
                    dict(response.context["form"].fields["status"].choices),
                )

    def test_release_boundaries_status_and_partial_dates(self):
        today = date(2026, 9, 20)
        for value, expected in [
            ("2026-09-21", True),
            ("2026-09-20", False),
            ("2026-09-19", False),
            ("2027", True),
            ("2026", False),
            ("2026-10", True),
            ("2026-09", False),
            (None, False),
            ("TBA", False),
            ("2026-99-99", False),
        ]:
            with self.subTest(value=value):
                self.assertEqual(
                    is_unreleased({"details": {"release_date": value}}, today), expected
                )
        for status in [
            "Upcoming",
            "In Production",
            "Planned",
            "not_yet_aired",
            "not_yet_published",
        ]:
            self.assertTrue(is_unreleased({"details": {"status": status}}, today))
        self.assertFalse(
            is_unreleased({"details": {"status": "Returning Series"}}, today)
        )

    @patch("app.views.services.get_media_metadata")
    def test_released_and_unknown_keep_upstream_default(self, metadata):
        for details in ({"release_date": "2000-01-01"}, {}):
            metadata.return_value = {"title": "A title", "details": details}
            response = self.client.get(
                reverse(
                    "track_modal",
                    kwargs={"source": "tmdb", "media_type": "movie", "media_id": "123"},
                ),
                {"return_url": "/"},
            )
            self.assertEqual(
                response.context["form"]["status"].value(), Status.COMPLETED
            )

    def test_existing_tracking_and_explicit_user_choice_preserved(self):
        item = Item.objects.bulk_create(
            [
                Item(
                    media_id="123",
                    source=Sources.TMDB,
                    media_type=MediaTypes.MOVIE,
                    title="Future",
                    image="",
                )
            ]
        )[0]
        Movie.objects.bulk_create(
            [Movie(item=item, user=self.user, status=Status.DROPPED)]
        )
        response = self.client.get(
            reverse(
                "track_modal",
                kwargs={"source": "tmdb", "media_type": "movie", "media_id": "123"},
            ),
            {"return_url": "/"},
        )
        self.assertEqual(response.context["form"]["status"].value(), Status.DROPPED)
        form = get_form_class("movie")(
            {
                "media_id": "123",
                "source": "tmdb",
                "media_type": "movie",
                "status": Status.COMPLETED,
            },
            initial={"status": Status.PLANNING},
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["status"], Status.COMPLETED)
