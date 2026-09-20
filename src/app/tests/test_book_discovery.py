from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from app.discovery import books
from app.models import Book, Item
from app.providers import hardcover, services


class BookDiscoveryTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(username="reader")
        self.other = get_user_model().objects.create_user(username="other")
        self.client.force_login(self.user)

    def membership(self, id, position, compilation=False):
        return {
            "position": position,
            "details": str(position) if position is not None else None,
            "compilation": compilation,
            "book": {
                "id": id,
                "title": f"Book {id}",
                "cached_image": "",
                "compilation": compilation,
            },
        }

    @patch("app.discovery.books.query")
    def test_series_order_extra_volumes_and_cache_owner_isolation(self, query):
        rows = [
            self.membership(10, 0.5),
            self.membership(1, 1),
            self.membership(11, 1),
            self.membership(20, 1, True),
            *[self.membership(i, i) for i in range(2, 8)],
            self.membership(30, None),
        ]
        query.side_effect = [
            {"series_by_pk": {"id": 1, "name": "Series"}},
            {"book_series": rows},
        ]
        data = books.series(1)
        self.assertEqual(
            [r["external_id"] for r in data["results"] if r["section"] == "books"],
            list(map(str, range(1, 8))),
        )
        item = Item.objects.create(
            source="hardcover",
            media_type="book",
            media_id="2",
            title="Book 2",
            image="",
        )
        Book.objects.bulk_create(
            [Book(item=item, user=self.other, status="Completed", progress=100)]
        )
        response = self.client.get(reverse("book_entity", args=["series", 1]))
        self.assertContains(response, "Books (7)")
        self.assertContains(response, "Add to tracker", count=7)
        self.assertContains(response, "Add to custom lists", count=7)
        self.assertNotContains(response, "Completed")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertFalse(any("item" in r for r in books.series(1)["results"]))
        extras = self.client.get(
            reverse("book_entity", args=["series", 1]),
            {"section": "other", "layout": "list"},
        )
        self.assertContains(extras, "Book 10")
        self.assertNotContains(extras, "Book 20")

    @patch("app.discovery.books.query")
    def test_series_fetches_all_provider_pages(self, query):
        query.side_effect = [
            {"series_by_pk": {"name": "Long series"}},
            {"book_series": [self.membership(i, i) for i in range(1, 101)]},
            {"book_series": [self.membership(101, 101)]},
        ]
        result = books.series(3)
        self.assertEqual(len(result["results"]), 101)
        self.assertEqual(query.call_args.args[1]["offset"], 100)

    def test_all_contributor_roles_series_and_publisher_links(self):
        result = books.book_links(
            {
                "cached_contributors": [
                    {"author": {"id": 1, "name": "Writer"}, "contribution": "Author"},
                    {
                        "author": {"id": 2, "name": "Artist"},
                        "contribution": "Illustrator",
                    },
                ],
                "book_series": [
                    {
                        "position": 1.5,
                        "details": "1.5",
                        "series": {"id": 8, "name": "Series"},
                    }
                ],
                "default_cover_edition": {"publisher": {"id": 9, "name": "Publisher"}},
            }
        )
        self.assertEqual(
            [x["role"] for x in result["contributors"]], ["Author", "Illustrator"]
        )
        self.assertEqual(result["series"][0]["position"], "1.5")
        self.assertEqual(result["publisher"]["url"], "/books/publisher/9")

    @patch("app.discovery.books.query")
    def test_publisher_uses_bigint_and_books_relation(self, query):
        query.side_effect = [{"publishers_by_pk": {"name": "Publisher"}}, {"books": []}]
        self.assertEqual(books.bibliography("publisher", 99, 2)["results"], [])
        self.assertIn("bigint!", query.call_args_list[0].args[0])
        self.assertEqual(
            query.call_args.args[1]["where"]["editions"], {"publisher_id": {"_eq": 99}}
        )
        self.assertEqual(query.call_args.args[1]["offset"], 36)

    @patch("app.providers.services.api_request")
    def test_provider_failure_does_not_become_empty_series(self, api):
        api.return_value = {"errors": [{"message": "unavailable"}]}
        with self.assertRaises(services.ProviderAPIError):
            books.series(1)
        self.assertIsNone(cache.get("book-series:v1:1"))

    @patch("app.providers.services.api_request")
    def test_old_metadata_cache_is_refreshed_with_book_relationships(self, api):
        cache.set("hardcover_book_1", {"title": "Old metadata"})
        api.return_value = {
            "data": {
                "books_by_pk": {
                    "id": 1,
                    "title": "Book",
                    "slug": "book",
                    "book_series": [],
                    "cached_contributors": [],
                }
            }
        }
        result = hardcover.book(1)
        self.assertIn("book_links", result)
        self.assertEqual(api.call_count, 1)
        hardcover.book(1)
        self.assertEqual(api.call_count, 1)

    @patch("app.providers.services.get_media_metadata")
    @patch("app.discovery.books.series")
    def test_detail_has_seven_native_cards_and_contributor_link(self, series, metadata):
        series.return_value = {
            "results": [
                books.card(
                    {"id": i, "title": f"Volume {i}"}, section="books", position=str(i)
                )
                for i in range(1, 8)
            ]
        }
        metadata.return_value = {
            "source": "hardcover",
            "media_type": "book",
            "media_id": "1",
            "title": "Volume 1",
            "image": "",
            "details": {},
            "book_links": {
                "series": [
                    {
                        "id": 1,
                        "name": "Series",
                        "url": "/books/series/1",
                        "position": "1",
                    }
                ],
                "contributors": [
                    {"name": "Writer", "role": "Author", "url": "/books/author/1"}
                ],
            },
        }
        response = self.client.get(
            reverse("media_details", args=["hardcover", "book", "1", "volume-1"])
        )
        from bs4 import BeautifulSoup

        ids = [
            n["id"]
            for n in BeautifulSoup(response.content, "html.parser").find_all(id=True)
        ]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertContains(response, "7 books")
        self.assertContains(response, "Volume 7")
        self.assertContains(response, 'href="/books/author/1"')
        self.assertContains(response, "Add to tracker", count=8)

    def test_auth_and_filter_validation(self):
        url = reverse("book_entity", args=["series", 1])
        self.assertEqual(self.client.get(url, {"section": "invalid"}).status_code, 400)
        self.assertEqual(self.client.get(url, {"page": -1}).status_code, 400)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)

    @patch("app.discovery.books.query")
    def test_author_role_filter_keeps_global_roles_and_null_author_credits(self, query):
        query.side_effect = [
            {"authors_by_pk": {"name": "Writer"}},
            {"books": []},
            {
                "contributions": [
                    {"contribution": "Author"},
                    {"contribution": "Illustrator"},
                    {"contribution": None},
                ]
            },
        ]
        data = books.bibliography("author", 1, 1, "Author")
        self.assertEqual(data["roles"], ["Author", "Illustrator"])
        condition = query.call_args_list[1].args[1]["where"]["contributions"]
        self.assertEqual(condition["author_id"], {"_eq": 1})
        self.assertIn({"contribution": {"_is_null": True}}, condition["_or"])
