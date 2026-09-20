from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app.models import (
    Book,
    Item,
    MediaTypes,
    Movie,
    Sources,
    Status,
)
from app.templatetags import app_tags
from users.forms import UserUpdateForm


class MediaListViewTests(TestCase):
    """Test the media list view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.external_credentials = {
            "username": "test2",
            "password": "12345",
            "profile_private": True,
        }
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.external_user = get_user_model().objects.create_user(
            **self.external_credentials
        )
        self.client.login(**self.credentials)

        movies_id = ["278", "238", "129", "424", "680"]
        num_completed = 3
        for i in range(1, 6):
            item = Item.objects.create(
                media_id=movies_id[i - 1],
                source=Sources.TMDB.value,
                media_type=MediaTypes.MOVIE.value,
                title=f"Test Movie {i}",
                image="http://example.com/image.jpg",
            )
            status = (
                Status.COMPLETED.value
                if i < num_completed
                else Status.IN_PROGRESS.value
            )
            Movie.objects.create(
                item=item,
                user=self.user,
                status=status,
                progress=1 if i < num_completed else 0,
                score=i,
            )

    def test_media_list_view(self):
        """Test the media list view displays media items."""
        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/media_list.html")

        self.assertIn("media_list", response.context)
        self.assertEqual(response.context["media_list"].paginator.count, 5)

        self.assertIn("sort_choices", response.context)
        self.assertIn("status_choices", response.context)
        self.assertEqual(response.context["media_type"], MediaTypes.MOVIE.value)
        self.assertEqual(
            response.context["media_type_plural"],
            app_tags.media_type_readable_plural(MediaTypes.MOVIE.value).lower(),
        )

    def test_media_list_with_filters(self):
        """Test the media list view with filters."""
        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?status=Completed&sort=score&layout=table",
        )

        self.assertEqual(response.status_code, 200)

        self.assertEqual(
            response.context["current_status"],
            Status.COMPLETED.value,
        )
        self.assertEqual(response.context["current_sort"], "score")
        self.assertEqual(response.context["current_layout"], "table")

        self.assertEqual(response.context["media_list"].paginator.count, 2)

        self.user.refresh_from_db()
        self.assertEqual(self.user.movie_status, Status.COMPLETED.value)
        self.assertEqual(self.user.movie_sort, "score")
        self.assertEqual(self.user.movie_layout, "table")

    def test_media_list_htmx_request(self):
        """Test the media list view with HTMX request."""
        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?layout=grid",
            headers={"hx-request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/media_grid_items.html")

        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?layout=table",
            headers={"hx-request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/media_table_items.html")

    def test_media_list_soft_navigation_returns_full_page(self):
        """Soft-navigation body swaps (after an edit modal) get the full page."""
        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value]),
            headers={"hx-request": "true", "x-soft-navigation": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/media_list.html")

    def test_public_media_list_ignores_invalid_filters(self):
        """Test invalid public filters fall back to the target user's preferences."""
        self.external_user.profile_private = False
        self.external_user.save(update_fields=["profile_private"])

        response = self.client.get(
            reverse(
                "medialist", args=[self.external_user.username, MediaTypes.MOVIE.value]
            )
            + "?status=invalid&sort=bad_field&layout=invalid",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["current_status"], self.external_user.movie_status
        )
        self.assertEqual(
            response.context["current_sort"], self.external_user.movie_sort
        )
        self.assertEqual(
            response.context["current_layout"], self.external_user.movie_layout
        )

    def test_anonymous_user_can_view_public_media_list(self):
        """Test anonymous users can view public media lists."""
        self.external_user.profile_private = False
        self.external_user.save(update_fields=["profile_private"])
        self.client.logout()

        response = self.client.get(
            reverse(
                "medialist", args=[self.external_user.username, MediaTypes.MOVIE.value]
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("media_list", response.context)

    def test_profile_private_defaults_to_true(self):
        """Test new users have private profiles by default."""
        user = get_user_model().objects.create_user(
            username="private-default",
        )

        self.assertTrue(user.profile_private)

    def test_private_media_list(self):
        """Test the private media list view."""
        response = self.client.get(
            reverse(
                "medialist", args=[self.external_user.username, MediaTypes.MOVIE.value]
            )
        )
        self.assertEqual(response.status_code, 404)

        form = UserUpdateForm(
            data={"username": "test2", "profile_private": False},
            instance=self.external_user,
        )
        self.assertTrue(form.is_valid(), form.errors)
        external_user = form.save()
        external_user.refresh_from_db()

        response = self.client.get(
            reverse(
                "medialist", args=[self.external_user.username, MediaTypes.MOVIE.value]
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("media_list", response.context)

    def test_anonymous_private_collections_resume_through_login(self):
        """Every private media route retains its destination after expiry."""
        self.client.logout()
        for media_type in MediaTypes.values:
            with self.subTest(media_type=media_type):
                path = reverse("medialist", args=[self.user.username, media_type])
                path += "?status=Planning&sort=title&layout=table&search=ring&page=2"
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                location = urlsplit(response.url)
                self.assertEqual(location.path, reverse("account_login"))
                self.assertEqual(parse_qs(location.query)["next"], [path])
                self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_expired_session_redirects_instead_of_hiding_own_books(self):
        """Exercise a genuinely expired session cookie, not just logout."""
        session = self.client.session
        session.set_expiry(-1)
        session.save()
        path = reverse("medialist", args=[self.user.username, "book"])
        response = self.client.get(path)
        self.assertRedirects(
            response,
            reverse("account_login") + "?next=" + path,
            fetch_redirect_response=False,
        )
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(path).status_code, 200)

    def test_expired_htmx_requests_use_full_page_login(self):
        """Filters, pagination and soft navigation must not embed the login page."""
        self.client.logout()
        path = reverse("medialist", args=[self.user.username, "book"])
        for headers in (
            {"HX-Request": "true"},
            {"HX-Request": "true", "X-Soft-Navigation": "true"},
            {"HX-Request": "true", "HX-Target": "empty_list"},
        ):
            with self.subTest(headers=headers):
                response = self.client.get(path, headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response["HX-Redirect"],
                    reverse("account_login") + "?next=" + path,
                )
                self.assertEqual(response.content, b"")
                self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_anonymous_missing_and_private_profiles_have_same_login_behavior(self):
        """The redirect must not disclose whether a private username exists."""
        self.client.logout()
        for username in (self.user.username, "missing-user"):
            path = reverse("medialist", args=[username, "book"])
            self.assertRedirects(
                self.client.get(path),
                reverse("account_login") + "?next=" + path,
                fetch_redirect_response=False,
            )

    def test_signed_in_nonowner_and_missing_profiles_still_return_404(self):
        """Reauthentication does not grant access to another private library."""
        for username in (self.external_user.username, "missing-user"):
            path = reverse("medialist", args=[username, "book"])
            self.assertEqual(self.client.get(path).status_code, 404)

    @patch("requests.sessions.Session.request", side_effect=AssertionError("Network"))
    def test_book_collection_and_actions_do_not_require_catalogue(self, network):
        """Stored books and their action controls survive catalogue outages."""
        item = Item.objects.create(
            media_id="OL26449223M",
            source=Sources.OPENLIBRARY,
            media_type=MediaTypes.BOOK,
            title="The Fellowship of the Ring",
            image="https://example.com/cover.jpg",
        )
        Book.objects.bulk_create(
            [
                Book(item=item, user=self.user, status=Status.PLANNING),
            ]
        )
        path = reverse("medialist", args=[self.user.username, "book"])
        for layout in ("grid", "table"):
            with self.subTest(layout=layout):
                response = self.client.get(path, {"layout": layout})
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, item.title)
                self.assertContains(
                    response,
                    reverse(
                        "track_modal",
                        args=[item.source, item.media_type, item.media_id],
                    ),
                )
        network.assert_not_called()
