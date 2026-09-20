from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from app.models import DiscoveryEntry, Item, Movie
from app.discovery import providers
from app.discovery.catalogue import remember, close_matches


def title(id=10, kind="movie", name="A Film", **extra):
    return {
        "id": id,
        "media_type": kind,
        "title": name,
        "poster_path": None,
        "release_date": "2024-01-02",
        **extra,
    }


class DiscoveryTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(username="viewer")
        self.other = get_user_model().objects.create_user(username="other")
        self.client.force_login(self.user)

    def test_name_matching_accent_transposition_alias_and_manual_exclusion(self):
        remember(
            [
                {
                    "source": "tmdb",
                    "kind": "movie",
                    "external_id": "1",
                    "name": "Interstellar",
                },
                {
                    "source": "tmdb",
                    "kind": "person",
                    "external_id": "2",
                    "name": "Christopher Nolan",
                    "aliases": ["Chris Nolan"],
                },
                {
                    "source": "tmdb",
                    "kind": "person",
                    "external_id": "3",
                    "name": "Léa Seydoux",
                },
                {
                    "source": "manual",
                    "kind": "movie",
                    "external_id": "4",
                    "name": "Private family title",
                },
            ]
        )
        self.assertEqual(
            close_matches("Interstelar", ["movie"])[0]["name"], "Interstellar"
        )
        self.assertEqual(
            close_matches("Nolan Christopher", ["person"])[0]["name"],
            "Christopher Nolan",
        )
        self.assertEqual(
            close_matches("Lea Seydoux", ["person"])[0]["name"], "Léa Seydoux"
        )
        remember(
            [
                {
                    "source": "tmdb",
                    "kind": "person",
                    "external_id": "2",
                    "name": "Christopher Nolan",
                }
            ]
        )
        self.assertEqual(
            DiscoveryEntry.objects.get(external_id="2").aliases, ["Chris Nolan"]
        )
        self.assertFalse(DiscoveryEntry.objects.filter(source="manual").exists())
        self.assertEqual(close_matches("abc", ["person"]), [])

    def test_full_credits_keep_role_and_episode_scope(self):
        data = providers.group_credits(
            {
                "cast": [
                    {
                        "id": 1,
                        "name": "Actor",
                        "roles": [{"character": "One"}, {"character": "Two"}],
                        "total_episode_count": 5,
                    }
                ],
                "crew": [
                    {
                        "id": 2,
                        "name": "Maker",
                        "department": "Directing",
                        "jobs": [{"job": "Director", "episode_count": 3}],
                    },
                    {
                        "id": 2,
                        "name": "Maker",
                        "department": "Writing",
                        "jobs": [{"job": "Screenplay", "episode_count": 2}],
                    },
                ],
            },
            True,
            [{"id": 2, "name": "Maker"}],
        )
        self.assertEqual(data["cast"][0]["role"], "One / Two")
        self.assertEqual(data["cast"][0]["episodes"], 5)
        self.assertEqual(
            [g["label"] for g in data["important"]],
            ["Director", "Created by", "Screenplay"],
        )
        self.assertEqual(len(data["crew"]), 2)
        self.assertTrue(data["cast"][0]["url"].startswith("/discover/tmdb/person/"))

    @patch("app.discovery.providers.tmdb_request")
    def test_person_filters_roles_and_media_without_duplicate_titles(self, request):
        request.return_value = {
            "id": 2,
            "name": "Director Actor",
            "combined_credits": {
                "cast": [
                    title(character="A character"),
                    title(id=11, kind="tv", name="A series", character="A guest"),
                ],
                "crew": [
                    title(job="Director", department="Directing"),
                    title(job="Screenplay", department="Writing"),
                ],
            },
        }
        item = Item.objects.create(
            media_id="10", source="tmdb", media_type="movie", title="A Film", image=""
        )
        Movie.objects.bulk_create(
            [Movie(item=item, user=self.other, status="Completed")]
        )
        path = reverse("discovery_entity", args=["tmdb", "person", 2])
        response = self.client.get(path, {"role": "Director", "type": "movie"})
        self.assertEqual(response.status_code, 200)
        rows = response.context["entity"]["results"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["roles"][0]["role"], "Director")
        self.assertNotIn("Completed", response.content.decode())
        self.assertEqual(response.context["credit_total"], 2)
        Movie.objects.bulk_create([Movie(item=item, user=self.user, status="Planning")])
        response = self.client.get(path, {"role": "Acting", "type": "movie"})
        self.assertContains(response, "A character")
        self.assertEqual(
            response.context["entity"]["results"][0]["media"].status, "Planning"
        )
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(
            self.client.get(path, {"role": "Director", "department": "Acting"}).context[
                "entity"
            ]["results"],
            [],
        )

    @patch("app.discovery.providers.tmdb_request")
    def test_company_and_network_use_distinct_provider_filters(self, request):
        request.side_effect = [
            {"id": 4, "name": "Studio"},
            {"results": [title()], "total_results": 1, "total_pages": 1},
        ]
        result = providers.company(4, "company", "movie", 1)
        self.assertEqual(result["results"][0]["kind"], "movie")
        self.assertEqual(request.call_args.kwargs["with_companies"], 4)
        request.side_effect = [
            {"id": 4, "name": "Network"},
            {"results": [], "total_results": 0, "total_pages": 1},
        ]
        providers.company(4, "network", "tv", 1)
        self.assertEqual(request.call_args.kwargs["with_networks"], 4)

    @patch("app.discovery.providers.tmdb_request")
    def test_season_uses_its_own_aggregate_credits(self, request):
        request.side_effect = [
            {
                "id": 44,
                "aggregate_credits": {
                    "cast": [
                        {
                            "id": 5,
                            "name": "Season actor",
                            "roles": [{"character": "Role"}],
                        }
                    ]
                },
            },
            {
                "id": 10,
                "name": "Series",
                "production_companies": [{"id": 8, "name": "Studio"}],
                "networks": [{"id": 9, "name": "Network"}],
            },
        ]
        data = providers.title_credits("season", 10, 2)
        self.assertEqual(request.call_args_list[0].args, ("tv/10/season/2",))
        self.assertEqual(data["cast"][0]["name"], "Season actor")
        self.assertEqual(data["networks"][0]["kind"], "network")

    @patch("app.providers.services.api_request")
    def test_provider_cache_does_not_retain_mutated_owner_data(self, request):
        request.return_value = {"results": [{"id": 1, "name": "Person"}]}
        first = providers.tmdb_request("search/person", query="Person")
        first["results"][0]["tracked"] = "private"
        second = providers.tmdb_request("search/person", query="Person")
        self.assertNotIn("tracked", second["results"][0])
        self.assertEqual(request.call_count, 1)

    @patch("app.providers.services.search")
    def test_search_ranks_fuzzy_matches_inline_and_keeps_original_query(self, search):
        remember(
            [
                {
                    "source": "tmdb",
                    "kind": "movie",
                    "external_id": "1",
                    "name": "Interstellar",
                }
            ]
        )
        search.return_value = {
            "results": [],
            "page": 1,
            "total_pages": 1,
            "total_results": 0,
        }
        response = self.client.get(
            reverse("search"), {"q": "Interstelar", "media_type": "movie"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Close matches")
        self.assertContains(response, "Interstelar")
        self.assertContains(response, "Interstellar")
        self.assertEqual(search.call_count, 2)
        search.assert_any_call("movie", "Interstelar", 1, "tmdb")
        search.assert_any_call("movie", "interstellar", 1, "tmdb")

    @patch("app.discovery.providers.search_screen")
    def test_unknown_multiword_name_uses_bounded_token_candidates(self, search):
        def lookup(query, kind, page):
            if query == "christopher":
                remember(
                    [
                        {
                            "source": "tmdb",
                            "kind": "person",
                            "external_id": "2",
                            "name": "Christopher Nolan",
                        }
                    ]
                )
            return {"results": [], "page": page, "total_pages": 1, "total_results": 0}

        search.side_effect = lookup
        response = self.client.get(
            reverse("discover"), {"q": "Christopher Nolna", "scope": "people"}
        )
        self.assertContains(response, "Christopher Nolan")
        self.assertLessEqual(search.call_count, 3)

    @patch("app.discovery.providers.igdb_request")
    def test_game_company_preserves_relationship_role_and_escapes_search(self, request):
        request.side_effect = [
            [{"id": 1, "name": "Studio"}],
            [
                {
                    "developer": True,
                    "publisher": False,
                    "game": {"id": 10, "name": "Game"},
                }
            ],
        ]
        data = providers.game_company(1, 1, "developer")
        self.assertIn("company = 1 & developer = true", request.call_args.args[1])
        self.assertEqual(data["results"][0]["roles"], [{"role": "Developer"}])
        request.side_effect = None
        request.return_value = []
        providers.search_game_companies('Studio"; limit 999;', 1)
        self.assertIn('Studio\\"; limit 999;', request.call_args.args[1])

    def test_all_search_categories_render_and_auth_is_required(self):
        response = self.client.get(reverse("discover"))
        self.assertContains(response, "People")
        self.assertContains(response, "Game Companies")
        self.assertContains(response, 'name="media_type"')
        self.assertEqual(
            self.client.get(reverse("discover"), {"page": "no"}).status_code, 400
        )
        self.assertEqual(
            self.client.get(reverse("discover"), {"scope": "private"}).status_code, 400
        )
        self.assertEqual(
            self.client.get(
                reverse("discovery_entity", args=["manual", "person", 1])
            ).status_code,
            404,
        )
        from app.discovery.search import CATEGORIES

        for scope, _ in CATEGORIES:
            with self.subTest(scope=scope):
                self.assertEqual(
                    self.client.get(reverse("discover"), {"scope": scope}).status_code,
                    200,
                )
        self.client.logout()
        self.assertEqual(self.client.get(reverse("discover")).status_code, 302)
        self.assertEqual(
            self.client.get(
                reverse("discovery_entity", args=["tmdb", "person", 1])
            ).status_code,
            302,
        )

    def test_sparse_index_refresh_preserves_known_metadata(self):
        row = {"source": "tmdb", "kind": "movie", "external_id": "7", "name": "Title"}
        remember(
            [
                {
                    **row,
                    "adult": True,
                    "description": "2024",
                    "image": "https://example.org/poster.jpg",
                }
            ]
        )
        remember([row])
        entry = DiscoveryEntry.objects.get(external_id="7")
        self.assertTrue(entry.adult)
        self.assertEqual(entry.description, "2024")
        self.assertEqual(entry.image, "https://example.org/poster.jpg")

    @patch("app.discovery.tasks.title_credits")
    @patch("app.discovery.tasks.game_companies")
    def test_background_index_is_public_and_avoids_repeat_requests(
        self, companies, credits
    ):
        from app.discovery.tasks import index_catalogue_credits

        Item.objects.create(
            source="tmdb", media_type="movie", media_id="17", title="A public film"
        )
        Item.objects.create(
            source="manual",
            media_type="movie",
            media_id="18",
            title="Private custom film",
        )
        index_catalogue_credits()
        index_catalogue_credits()
        credits.assert_called_once_with("movie", "17")
        companies.assert_not_called()
        self.assertEqual(
            list(DiscoveryEntry.objects.values_list("name", flat=True)),
            ["A public film"],
        )
        self.assertIsNone(cache.get("discovery:index-catalogue-running"))
