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

    def test_provider_relevance_survives_corrected_queries_and_long_subtitles(self):
        film = {
            **row("Harry Potter and the Philosopher's Stone", 1),
            "_provider_order": 0,
        }
        extra = {**row("Harry Potter: Fireplace", 2), "_provider_order": 15}
        indexed = [row(film["name"], 1), row(extra["name"], 2), film, extra]
        ranked = merge_ranked("Hary Poter", [], indexed)
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

    def test_typos_complete_franchises_words_names_and_numbered_sequels(self):
        cases = {
            "Alen": ["Alien", "Aliens", "Alien³", "Aliens 3", "Alien: Romulus"],
            "Batmn": ["Batman", "Batman Returns", "Batman Begins"],
            "Termintor": ["The Terminator", "Terminator 2: Judgment Day"],
            "Star Wras": ["Star Wars", "Star Wars: The Last Jedi"],
            "Hary Poter": ["Harry Potter and the Philosopher's Stone"],
            "Lord Rings": ["The Lord of the Rings: The Fellowship of the Ring"],
            "Jurasic": ["Jurassic Park", "Jurassic World"],
            "Baldurs Gat": ["Baldur's Gate 3"],
            "Cristopher Nolan": ["Christopher Nolan"],
            "Sigourny Weaver": ["Sigourney Weaver"],
            "Amelie": ["Amélie"],
        }
        for query, titles in cases.items():
            for title in titles:
                with self.subTest(query=query, title=title):
                    self.assertGreaterEqual(name_score(query, title), 400)

    def test_unrelated_and_short_typos_are_not_fuzzy_candidates(self):
        for query, title in [
            ("up", "Us"),
            ("it", "Up"),
            ("dun", "Dawn"),
            ("Alen", "Batman"),
            ("Star Wras", "Star Trek"),
            ("Harry Potter", "Harry Brown"),
            ("Alien Alien", "Alien"),
        ]:
            with self.subTest(query=query, title=title):
                self.assertEqual(name_score(query, title), 0)

    def test_index_retrieves_series_for_multiple_misspellings(self):
        titles = [
            "Alien",
            "Aliens",
            "Alien³",
            "Alien: Romulus",
            "Batman",
            "Batman Returns",
            "Star Wars",
            "Star Wars: The Last Jedi",
        ]
        remember([row(title, index) for index, title in enumerate(titles)])
        for query, expected in [
            ("Alen", set(titles[:4])),
            ("Batmn", set(titles[4:6])),
            ("Star Wras", set(titles[6:])),
        ]:
            with self.subTest(query=query):
                found = {r["name"] for r in close_matches(query, ["movie"], ["tmdb"])}
                self.assertTrue(expected.issubset(found), found)

    @patch("app.providers.services.search")
    def test_corrected_query_fetches_sequels_missing_from_index(self, search):
        remember([row("Alien", 1)])

        def fetch(kind, query, page, source):
            titles = (
                ["Alien", "Aliens", "Alien³", "Alien: Romulus"]
                if query == "alien"
                else []
            )
            return {
                "results": [
                    provider_row(title, i + 1) for i, title in enumerate(titles)
                ],
                "page": 1,
                "total_pages": 1,
                "total_results": len(titles),
            }

        search.side_effect = fetch
        response = self.client.get(
            reverse("search"), {"q": "Alen", "media_type": "movie"}
        )
        names = {r["name"] for r in response.context["data"]["results"]}
        self.assertEqual(names, {"Alien", "Aliens", "Alien³", "Alien: Romulus"})
        self.assertEqual(response.context["data"]["total_results"], 4)
        self.assertContains(response, 'hx-get="/track_modal/tmdb/movie/3"')
        self.assertLessEqual(search.call_count, 3)

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
