from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from app.discovery.catalogue import remember, close_matches
from app.discovery.ranking import merge_ranked, name_score


def row(name, id, kind="movie", source="tmdb"):
    return {
        "name": name,
        "external_id": str(id),
        "kind": kind,
        "source": source,
        "image": "https://example.org/image.jpg",
    }


def provider_row(name, id, kind="movie", source="tmdb"):
    return {
        "title": name,
        "media_id": str(id),
        "media_type": kind,
        "source": source,
        "image": "https://example.org/image.jpg",
    }


class SearchRankingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(username="searcher")
        self.client.force_login(self.user)

    def test_exact_title_beats_sequel_partial_and_provider_order(self):
        rows = [
            row("Aliens", 2),
            row("Resident Alien", 3),
            row("Alien", 1),
            row("Alien: Romulus", 4),
        ]
        ranked = merge_ranked("alien", rows, [row("Aliens", 2)])
        self.assertEqual(ranked[0]["name"], "Alien")
        self.assertEqual(len(ranked), 4)
        self.assertGreater(name_score("Alien", "Alien"), name_score("Alien", "Aliens"))

    def test_index_alias_is_retained_when_provider_returns_same_identity(self):
        indexed = {**row("Original title", 1), "aliases": ["Alien"]}
        ranked = merge_ranked(
            "Alien", [row("Alien invasion", 2), row("Original title", 1)], [indexed]
        )
        self.assertEqual(ranked[0]["external_id"], "1")
        self.assertEqual(len(ranked), 2)

    def test_short_typo_and_transposition_find_unseen_direct_results(self):
        remember([row("Alien", 1), row("Aliens", 2), row("The Alienist", 3)])
        matches = close_matches("Alen", ["movie"], ["tmdb"])
        self.assertEqual(matches[0]["name"], "Alien")
        self.assertEqual(
            close_matches("Alein", ["movie"], ["tmdb"])[0]["name"], "Alien"
        )
        self.assertEqual(name_score("up", "us"), 0)

    @patch("app.providers.services.search")
    def test_original_search_has_inline_fuzzy_results_and_all_hover_actions(
        self, search
    ):
        remember([row("Alien", 1), row("Aliens", 2)])
        search.return_value = {
            "results": [],
            "page": 1,
            "total_pages": 1,
            "total_results": 0,
        }
        response = self.client.get(
            reverse("search"), {"q": "Alen", "media_type": "movie"}
        )
        self.assertEqual(response.context["data"]["results"][0]["name"], "Alien")
        self.assertContains(response, 'hx-get="/track_modal/tmdb/movie/1"')
        self.assertContains(response, 'hx-get="/lists_modal/tmdb/movie/1"')
        self.assertContains(response, 'hx-get="/history_modal/tmdb/movie/1"')
        self.assertNotContains(response, "Close matches")
        self.assertNotContains(response, "Find your next")
        self.assertContains(response, "Search Results")
        self.assertContains(response, "All Search")
        self.assertContains(response, "List View")

    @patch("app.providers.services.search")
    def test_categories_stay_separate_and_source_and_pagination_survive(self, search):
        search.return_value = {
            "results": [provider_row("Alien", 1)],
            "page": 1,
            "total_pages": 4,
            "total_results": 80,
        }
        response = self.client.get(
            reverse("search"), {"q": "Alien", "media_type": "movie", "layout": "list"}
        )
        search.assert_called_once_with("movie", "Alien", 1, "tmdb")
        self.assertEqual(
            {r["kind"] for r in response.context["data"]["results"]}, {"movie"}
        )
        self.assertContains(response, "page=4")
        self.assertContains(response, "layout=list")
        self.assertContains(response, "source=tmdb")
        self.assertContains(response, "media_type=tv")
        self.assertContains(response, "media_type=people")

    @patch(
        "app.discovery.providers.search_game_companies", return_value={"results": []}
    )
    @patch("app.discovery.providers.search_screen", return_value={"results": []})
    @patch("app.providers.services.search")
    def test_all_search_combines_categories_without_merging_identity_or_credentials(
        self, search, people, companies
    ):
        def fetch(kind, query, page, source):
            return {
                "results": [provider_row("Alien", 1, kind, source)],
                "page": 1,
                "total_pages": 1,
                "total_results": 1,
            }

        search.side_effect = fetch
        response = self.client.get(
            reverse("search"), {"q": "Alien", "media_type": "all"}
        )
        self.assertEqual(response.status_code, 200)
        kinds = {r["kind"] for r in response.context["data"]["results"]}
        self.assertTrue({"movie", "tv", "game", "book"}.issubset(kinds))
        self.user.refresh_from_db()
        self.assertNotEqual(self.user.last_search_type, "all")
        self.assertEqual(response["Cache-Control"], "private, no-store")

    @patch("app.discovery.providers.person")
    def test_filmography_has_clickable_roles_and_native_actions(self, person):
        person.return_value = {
            **row("Richard Donner", 7187, "person"),
            "results": [
                {
                    **row("Superman", 1924),
                    "date": "1978-12-13",
                    "popularity": 10,
                    "roles": [
                        {"role": "Director", "department": "Directing", "character": ""}
                    ],
                }
            ],
        }
        response = self.client.get(
            reverse("discovery_entity", args=["tmdb", "person", 7187])
        )
        self.assertContains(response, 'aria-label="Role filters"')
        self.assertContains(response, "role=Director")
        self.assertContains(response, 'hx-get="/track_modal/tmdb/movie/1924"')
        self.assertContains(response, 'hx-get="/lists_modal/tmdb/movie/1924"')
        self.assertContains(response, 'hx-get="/history_modal/tmdb/movie/1924"')
        self.assertNotContains(response, "Apply filters")
        self.assertNotContains(response, "Explore the filmography")
