"""Search relevance must distinguish an established audience from title noise."""

from unittest.mock import patch
from xml.etree import ElementTree

from django.core.cache import cache
from django.test import TestCase

from app.discovery.catalogue import close_matches, remember
from app.discovery.prominence import audience
from app.discovery.ranking import merge_ranked
from app.providers import bgg, openlibrary
from app.tests.test_search_ranking import row


def popular(name, identity, count, kind="book", source="openlibrary"):
    return {
        **row(name, identity, kind, source),
        "prominence": audience((count, 10000, 1)),
    }


class PopularityTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_main_novels_outrank_short_exact_ancillary_titles(self):
        # Public Open Library readership counts: even the least-read main novel
        # must precede the unrelated short exact-title companion publication.
        novels = [
            ("Philosopher's Stone", 23821),
            ("Chamber of Secrets", 6704),
            ("Prisoner of Azkaban", 5707),
            ("Deathly Hallows", 5974),
            ("Goblet of Fire", 4933),
            ("Order of the Phoenix", 4587),
            ("Half-Blood Prince", 3754),
        ]
        main = [
            popular(f"Harry Potter and the {title}", i, count)
            for i, (title, count) in enumerate(novels)
        ]
        noise = [
            popular("Harry Potter", 20, 236),
            popular("Harry Potter", 21, 50),
            popular("Harry Potter Deluxe Coloring Book", 22, 92),
        ]
        ranked = merge_ranked("Harry Potter", noise + main, [])
        self.assertEqual(
            {r["external_id"] for r in ranked[:7]}, {str(i) for i in range(7)}
        )

    def test_popularity_applies_across_franchises_and_media(self):
        for query, title, kind, source in [
            (
                "Lord Rings",
                "The Lord of the Rings: The Fellowship of the Ring",
                "book",
                "openlibrary",
            ),
            ("Star Wars", "Star Wars: A New Hope", "movie", "tmdb"),
            ("Batman", "Batman Begins", "movie", "tmdb"),
            ("Elder Scrolls", "The Elder Scrolls V: Skyrim", "game", "igdb"),
            ("One Piece", "One Piece", "anime", "mal"),
            ("Dune", "Dune", "book", "openlibrary"),
            ("David", "David Tennant", "person", "tmdb"),
        ]:
            with self.subTest(query=query):
                main = popular(title, 1, 10000, kind, source)
                obscure = popular(query, 2, 5, kind, source)
                self.assertEqual(
                    merge_ranked(query, [obscure, main], [])[0]["external_id"], "1"
                )

    def test_exact_film_and_precise_low_audience_title_stay_first(self):
        alien = popular("Alien", 1, 9500, "movie", "tmdb")
        others = [
            popular("Aliens", 2, 10000),
            popular("Alien: Romulus", 3, 10000),
            popular("Resident Alien", 4, 10000),
        ]
        self.assertEqual(
            merge_ranked("Alien", others + [alien], [])[0]["name"], "Alien"
        )
        query = "Harry Potter Deluxe Coloring Book"
        exact = popular(query, 5, 1)
        extra = popular(query + " Companion Guide", 6, 10000)
        self.assertEqual(merge_ranked(query, [extra, exact], [])[0]["external_id"], "5")

    def test_fuzzy_franchises_use_audience_without_unrelated_hits(self):
        for query, title, extra in [
            ("Alen", "Alien", "Aliens 3"),
            ("Batmn", "Batman", "Batman Begins"),
            (
                "Hary Poter",
                "Harry Potter and the Philosopher's Stone",
                "Harry Potter and the Chamber of Secrets",
            ),
        ]:
            with self.subTest(query=query):
                rows = [
                    popular("The Godfather", 9, 9999999),
                    popular(title, 1, 10000),
                    popular(extra, 2, 9000),
                ]
                ranked = merge_ranked(query, rows, [])
                self.assertEqual({r["external_id"] for r in ranked[:2]}, {"1", "2"})

    def test_counts_are_bounded_and_missing_evidence_is_neutral(self):
        for value in [None, "bad", float("nan"), float("inf"), -10]:
            self.assertEqual(audience((value, 10000, 1)), 0)
        self.assertEqual(audience((10**12, 10000, 1)), 1)
        self.assertLess(audience((10, 10000, 1)), audience((1000, 10000, 1)))
        direct = [row("Aliens", 2), row("Alien", 1)]
        self.assertEqual(merge_ranked("Alien", direct, [])[0]["name"], "Alien")

    def test_index_retains_audience_when_metadata_has_no_counts(self):
        card = popular("Batman Begins", 1, 10000, "movie", "tmdb")
        remember([card])
        remember([row("Batman Begins", 1)])
        found = close_matches("Batmn", ["movie"], ["tmdb"])
        self.assertEqual(found[0]["prominence"], 1)
        ranked = merge_ranked(
            "Batman", [row("Batman Begins", 1), row("Batman", 2)], found
        )
        self.assertEqual(ranked[0]["external_id"], "1")

    def test_openlibrary_uses_work_audience_and_consistent_edition_identity(self):
        data = {
            "docs": [
                {
                    "key": "/works/OL1W",
                    "title": "Novel",
                    "readinglog_count": 10000,
                    "ratings_count": 500,
                    "edition_count": 30,
                    "editions": {
                        "docs": [
                            {"key": "/books/OL2M", "title": "Novel", "cover_i": 123}
                        ]
                    },
                }
            ]
        }
        result = openlibrary.search_items(data)[0]
        self.assertEqual(result["media_id"], "OL2M")
        self.assertEqual(result["prominence"], 1)
        self.assertIn("readinglog_count", openlibrary.SEARCH_FIELDS)

    @patch("app.providers.bgg.services.api_request")
    def test_boardgames_fetch_counts_in_existing_image_batch(self, request):
        request.side_effect = [
            ElementTree.fromstring(
                '<items><item id="1"><name value="Catan"/></item></items>'
            ),
            ElementTree.fromstring(
                '<items><item id="1"><thumbnail>https://example.org/catan.jpg</thumbnail><statistics><ratings><usersrated value="100000"/></ratings></statistics></item></items>'
            ),
        ]
        result = bgg.search("Catan", 1)["results"][0]
        self.assertEqual(result["prominence"], 1)
        self.assertEqual(result["image"], "https://example.org/catan.jpg")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args.kwargs["params"]["stats"], "1")
