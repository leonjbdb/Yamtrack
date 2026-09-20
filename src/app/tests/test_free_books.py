from unittest.mock import Mock, patch

import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from app import config
from app.discovery import free_books
from app.models import Book, Item
from app.providers import book_catalogue, openlibrary
from app.providers import wikidata_books as wd
from app.providers.services import ProviderAPIError


def claim(value, *, rank="normal", position=None):
    result = {
        "rank": rank,
        "mainsnak": {"snaktype": "value", "datavalue": {"value": value}},
    }
    if position is not None:
        result["qualifiers"] = {
            "P1545": [{"snaktype": "value", "datavalue": {"value": position}}]
        }
    return result


def entity(qid, name, ol_ids=(), memberships=()):
    return {
        "id": qid,
        "labels": {"en": {"value": name}},
        "claims": {
            "P648": [claim(x) for x in ol_ids],
            "P179": [
                claim({"id": series}, position=position)
                for series, position in memberships
            ],
        },
    }


def doc(work="OL1W", edition="OL1M", title="Novel", editions=None):
    return {
        "key": "/works/" + work,
        "title": "Original title",
        "edition_key": editions or [edition],
        "editions": {
            "docs": [
                {
                    "key": "/books/" + edition,
                    "title": title,
                    "cover_i": 123,
                    "language": ["eng"],
                }
            ]
        },
    }


class FreeBookTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(username="reader")
        self.other = get_user_model().objects.create_user(username="other")
        self.client.force_login(self.user)

    @patch("app.providers.openlibrary.search_data")
    def test_search_uses_selected_edition_without_mixing_work_fields(self, search):
        search.return_value = {"numFound": 1, "docs": [doc(title="Translated title")]}
        result = openlibrary.search("novel", 1)["results"][0]
        self.assertEqual(result["title"], "Translated title")
        self.assertEqual(result["media_id"], "OL1M")
        self.assertEqual(result["aliases"], ["Original title"])
        self.assertEqual(config.get_default_source_name("book").value, "openlibrary")

    @patch("app.providers.book_catalogue.request")
    def test_edition_metadata_consistency_and_no_wikidata_on_initial_load(
        self, request
    ):
        edition = {
            "title": "English edition",
            "number_of_pages": 412,
            "publish_date": "2011",
            "isbn_13": ["9780316129084"],
            "publishers": ["Orbit"],
            "covers": [7],
            "works": [{"key": "/works/OL1W"}],
            "languages": [{"key": "/languages/eng"}],
        }
        work = {
            "title": "Another language",
            "number_of_pages": 999,
            "first_publish_date": "2009",
            "covers": [99],
            "authors": [{"author": {"key": "/authors/OL1A"}}],
        }
        request.side_effect = [
            edition,
            work,
            {"name": "Writer"},
            {"summary": {"average": 4, "count": 8}},
        ]
        data = openlibrary.book("OL1M")
        self.assertEqual(data["title"], "English edition")
        self.assertEqual(data["max_progress"], 412)
        self.assertEqual(data["details"]["publish_date"], "2011")
        self.assertEqual(data["details"]["isbn"], ["9780316129084"])
        self.assertIn("/7-", data["image"])
        self.assertEqual(data["score"], 8)
        self.assertTrue(
            all(call.args[0] == "openlibrary" for call in request.call_args_list)
        )
        self.assertIn("related", data["book_related_url"])
        data["book_links"]["contributors"][0]["name"] = "owner data"
        self.assertEqual(
            openlibrary.book("OL1M")["book_links"]["contributors"][0]["name"], "Writer"
        )
        self.assertEqual(request.call_count, 4)

    @patch("app.providers.wikidata_books.entities")
    @patch("app.providers.wikidata_books.search_ids")
    def test_identifier_match_ignores_stale_and_ambiguous_search_results(
        self, search, entities
    ):
        search.return_value = ["Q1", "Q2"]
        entities.return_value = {
            "Q1": entity("Q1", "Novel", ["OL1W"]),
            "Q2": entity("Q2", "Novel", ["OL2W"]),
        }
        self.assertEqual(wd.book_identity("OL1M", "OL1W")["id"], "Q1")
        entities.return_value["Q2"]["claims"]["P648"] = [claim("OL1W")]
        with self.assertRaises(ProviderAPIError):
            wd.book_identity("OL1M", "OL1W")
        self.assertNotIn("Novel", search.call_args.args[0])

    @patch("app.providers.wikidata_books.api")
    def test_identifier_search_fetches_all_pages(self, api):
        api.side_effect = [
            {"query": {"search": [{"title": "Q1"}]}, "continue": {"sroffset": 50}},
            {"query": {"search": [{"title": "Q2"}]}},
        ]
        self.assertEqual(wd.search_ids("haswbstatement:P179=Q9"), ["Q1", "Q2"])
        self.assertEqual(api.call_args.args[0]["sroffset"], 50)

    @patch("app.providers.wikidata_books.search_ids")
    @patch("app.providers.wikidata_books.entities")
    def test_series_numeric_order_companions_and_deprecated_membership(
        self, entities, search
    ):
        rows = {
            f"Q{i}": entity(f"Q{i}", f"Title {i}", [f"OL{i}W"], [("Q99", p)])
            for i, p in [(1, "10"), (2, "2"), (3, "1.5"), (4, None), (5, "1")]
        }
        rows["Q6"] = entity("Q6", "Old membership", ["OL6W"])
        rows["Q6"]["claims"]["P179"] = [
            claim({"id": "Q99"}, rank="deprecated", position="3")
        ]
        entities.side_effect = [{"Q99": entity("Q99", "Series")}, rows]
        search.return_value = list(rows)
        result = wd.series("Q99")["results"]
        self.assertEqual([r["position"] for r in result], ["1", "1.5", "2", "10", ""])
        self.assertEqual(
            [r["section"] for r in result],
            ["books", "other", "books", "books", "other"],
        )
        self.assertEqual(len(result), 5)

    @patch("app.providers.openlibrary.search_data")
    def test_series_batch_mapping_by_work_and_edition_never_title(self, search):
        search.return_value = {"numFound": 1, "docs": [doc(editions=["OL1M", "OL2M"])]}
        rows = [
            {"qid": "Q1", "name": "Novel", "position": "1", "ol_ids": ["OL2M"]},
            {"qid": "Q2", "name": "Novel", "position": "2", "ol_ids": []},
        ]
        results = free_books.series_cards(rows)
        self.assertEqual(results[0]["external_id"], "OL1M")
        self.assertEqual(results[1]["kind"], "book_link")
        self.assertIn("No linked edition", results[1]["description"])
        search.assert_called_once_with("edition_key:(OL2M)", limit=100)

    def test_reuses_own_tracked_edition_without_changing_any_records(self):
        item = Item.objects.create(
            source="openlibrary",
            media_type="book",
            media_id="OL2M",
            title="My edition",
            image="",
        )
        Book.objects.bulk_create(
            [Book(user=self.user, item=item, status="Dropped", progress=123)]
        )
        rows = [
            free_books.card(
                {
                    "media_id": "OL1M",
                    "title": "Novel",
                    "image": "",
                    "edition_ids": ["OL1M", "OL2M"],
                }
            )
        ]
        free_books.prefer_tracked_editions(self.other, rows)
        self.assertEqual(rows[0]["external_id"], "OL1M")
        free_books.prefer_tracked_editions(self.user, rows)
        self.assertEqual(rows[0]["external_id"], "OL2M")
        book = Book.objects.get(user=self.user)
        self.assertEqual((book.status, book.progress), ("Dropped", 123))
        self.assertEqual(Book.objects.count(), 1)

    @patch("app.providers.openlibrary.search_data")
    @patch("app.providers.wikidata_books.series")
    def test_series_page_keeps_native_controls_and_private_cache(self, series, search):
        series.return_value = {
            "name": "Series",
            "results": [
                {
                    "qid": "Q1",
                    "name": "Novel",
                    "position": "1",
                    "section": "books",
                    "ol_ids": ["OL1W"],
                }
            ],
        }
        search.return_value = {"numFound": 1, "docs": [doc()]}
        url = reverse("free_book_entity", args=["series", "Q9"])
        response = self.client.get(url)
        self.assertContains(response, "Add to tracker")
        self.assertContains(response, "Add to custom lists")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertContains(self.client.get(url, {"layout": "list"}), "Add to tracker")

    @patch("app.providers.openlibrary.book")
    def test_series_failure_is_visible_and_retryable(self, book):
        book.side_effect = ProviderAPIError("wikidata", requests.Timeout("timeout"))
        response = self.client.get(reverse("free_book_related", args=["OL1M"]))
        self.assertContains(response, "Series could not be loaded")
        self.assertContains(response, "Retry")
        self.assertNotContains(response, "No series information available")

    @patch("app.providers.book_catalogue.session")
    def test_transport_bounds_errors_and_never_caches_them(self, session):
        response = Mock(status_code=429, headers={"Retry-After": "45"})
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
        session.return_value.get.return_value = response
        with self.assertRaises(ProviderAPIError):
            book_catalogue.request("openlibrary", "/search.json", {"q": "Novel"})
        with self.assertRaises(ProviderAPIError):
            book_catalogue.request("openlibrary", "/search.json", {"q": "Another"})
        self.assertEqual(session.return_value.get.call_count, 1)
        self.assertEqual(session.return_value.get.call_args.kwargs["timeout"], (5, 10))
        self.assertFalse(session.return_value.get.call_args.kwargs["allow_redirects"])

    @patch("app.providers.book_catalogue.session")
    def test_public_cache_is_copied_and_paid_hosts_are_impossible(self, session):
        response = Mock(status_code=200, is_redirect=False)
        response.json.return_value = {"title": "Novel"}
        session.return_value.get.return_value = response
        first = book_catalogue.request("openlibrary", "/books/OL1M.json")
        first["title"] = "Private note"
        self.assertEqual(
            book_catalogue.request("openlibrary", "/books/OL1M.json")["title"], "Novel"
        )
        self.assertEqual(session.return_value.get.call_count, 1)
        with self.assertRaises(ValueError):
            book_catalogue.request("isbndb", "/book/123")

    def test_invalid_identifiers_rejected_without_remote_request(self):
        with patch("app.providers.book_catalogue.request") as request:
            self.assertEqual(
                self.client.get(
                    reverse("free_book_entity", args=["series", "invalid"])
                ).status_code,
                404,
            )
            self.assertEqual(
                self.client.get(
                    reverse("free_book_related", args=["invalid"])
                ).status_code,
                404,
            )
            request.assert_not_called()

    @patch("app.providers.book_catalogue.request")
    def test_explicit_catalogue_redirects_are_bounded_and_same_kind(self, request):
        request.side_effect = [
            {"type": {"key": "/type/redirect"}, "location": "/works/OL2W"},
            {"key": "/works/OL2W", "title": "Canonical novel"},
        ]
        result, aliases = openlibrary.record("works", "OL1W")
        self.assertEqual(aliases, ["OL1W", "OL2W"])
        self.assertEqual(result["title"], "Canonical novel")
        request.side_effect = None
        request.return_value = {
            "type": {"key": "/type/redirect"},
            "location": "https://example.org/private",
        }
        with self.assertRaises(ProviderAPIError):
            openlibrary.record("works", "OL1W")

    @patch("app.providers.openlibrary.record")
    @patch("app.providers.openlibrary.search_data")
    def test_stale_series_identifier_follows_verified_merge(self, search, record):
        search.side_effect = [
            {"numFound": 0, "docs": []},
            {"numFound": 1, "docs": [doc(work="OL2W")]},
        ]
        record.return_value = ({"key": "/works/OL2W"}, ["OL1W", "OL2W"])
        result = free_books.series_cards(
            [{"qid": "Q1", "name": "Novel", "position": "1", "ol_ids": ["OL1W"]}]
        )
        self.assertEqual(result[0]["external_id"], "OL1M")
        self.assertEqual(cache.get("free-books:identity:OL2W"), "Q1")
        self.assertEqual(search.call_args.args[0], "key:(/works/OL2W)")

    @patch("app.providers.openlibrary.search_data")
    def test_multiple_work_identifiers_use_one_grouped_query(self, search):
        search.return_value = {
            "numFound": 2,
            "docs": [doc(), doc(work="OL2W", edition="OL2M")],
        }
        rows = [
            {
                "qid": f"Q{i}",
                "name": f"Novel {i}",
                "position": str(i),
                "ol_ids": [f"OL{i}W"],
            }
            for i in (1, 2)
        ]
        result = free_books.series_cards(rows)
        self.assertEqual([r["external_id"] for r in result], ["OL1M", "OL2M"])
        search.assert_called_once_with("key:(/works/OL1W OR /works/OL2W)", limit=100)

    @patch("app.providers.services.get_media_metadata")
    def test_tracking_provider_failure_is_visible_and_retryable(self, metadata):
        metadata.side_effect = ProviderAPIError(
            "openlibrary", requests.Timeout("timeout")
        )
        url = reverse("track_modal", args=["openlibrary", "book", "OL1M"])
        response = self.client.get(
            url, {"return_url": "/search?q=novel"}, HTTP_HX_REQUEST="true"
        )
        self.assertContains(response, "Unable to Load Tracking Form")
        self.assertContains(response, "Retry")
        self.assertContains(response, "data-tracking-error")
        self.assertNotContains(response, "<form")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(Book.objects.count(), 0)
        metadata.side_effect = None
        metadata.return_value = {"title": "Novel"}
        retry = self.client.get(
            url, {"return_url": "/search?q=novel"}, HTTP_HX_REQUEST="true"
        )
        self.assertContains(retry, "<form")
        self.assertNotContains(retry, "Unable to Load Tracking Form")
        self.assertEqual(Book.objects.count(), 0)


class CatalogueReliabilityTests(TestCase):
    def setUp(self):
        cache.clear()

    def response(self, status=200, data=None, headers=None):
        response = Mock(
            status_code=status,
            is_redirect=False,
            headers=headers or {},
            text=f"HTTP {status}",
        )
        response.json.return_value = data or {"title": "Novel"}
        if status >= 400:
            response.raise_for_status.side_effect = requests.HTTPError(
                response=response
            )
        return response

    @patch("app.providers.book_catalogue.time.sleep")
    @patch("app.providers.book_catalogue.session")
    def test_transient_503_retries_same_endpoint_and_caches_success(
        self, session, sleep
    ):
        session.return_value.get.side_effect = [self.response(503), self.response()]
        self.assertEqual(
            book_catalogue.request("openlibrary", "/books/OL1M.json")["title"], "Novel"
        )
        self.assertEqual(
            book_catalogue.request("openlibrary", "/books/OL1M.json")["title"], "Novel"
        )
        self.assertEqual(session.return_value.get.call_count, 2)
        self.assertEqual(
            session.return_value.get.call_args_list[0],
            session.return_value.get.call_args_list[1],
        )
        sleep.assert_called_once_with(1)

    @patch("app.providers.book_catalogue.time.sleep")
    @patch("app.providers.book_catalogue.session")
    def test_outage_is_bounded_and_cooldown_retains_actual_reason(self, session, sleep):
        session.return_value.get.return_value = self.response(503)
        with self.assertRaises(book_catalogue.CatalogueUnavailable) as first:
            book_catalogue.request("openlibrary", "/books/OL1M.json")
        self.assertEqual(session.return_value.get.call_count, 3)
        with self.assertRaises(book_catalogue.CatalogueUnavailable) as blocked:
            book_catalogue.request("openlibrary", "/books/OL1M.json")
        self.assertEqual(session.return_value.get.call_count, 3)
        self.assertEqual(first.exception.status_code, 503)
        self.assertEqual(blocked.exception.status_code, 503)
        self.assertIn("temporarily unavailable", blocked.exception.user_message)
        self.assertNotIn("rate limit", blocked.exception.user_message)

    @patch("app.providers.book_catalogue.time.sleep")
    @patch("app.providers.book_catalogue.session")
    def test_retry_after_is_honored_without_immediate_requests(self, session, sleep):
        session.return_value.get.return_value = self.response(
            503, headers={"Retry-After": "90"}
        )
        with self.assertRaises(book_catalogue.CatalogueUnavailable) as error:
            book_catalogue.request("openlibrary", "/books/OL1M.json")
        self.assertEqual(error.exception.retry_after, 90)
        self.assertEqual(session.return_value.get.call_count, 1)
        sleep.assert_not_called()
        from email.utils import formatdate

        with patch("app.providers.book_catalogue.time.time", return_value=1000):
            self.assertEqual(
                book_catalogue.retry_delay(
                    self.response(
                        headers={"Retry-After": formatdate(1045, usegmt=True)}
                    )
                ),
                45,
            )

    @patch("app.providers.book_catalogue.time.sleep")
    @patch("app.providers.book_catalogue.session")
    def test_connection_reset_retries_but_missing_records_do_not(self, session, sleep):
        session.return_value.get.side_effect = [
            requests.ConnectionError("reset"),
            self.response(),
        ]
        self.assertEqual(
            book_catalogue.request("openlibrary", "/books/OL1M.json")["title"], "Novel"
        )
        session.return_value.get.side_effect = None
        session.return_value.get.return_value = self.response(404)
        with self.assertRaises(ProviderAPIError):
            book_catalogue.request("openlibrary", "/books/OL2M.json")
        self.assertEqual(session.return_value.get.call_count, 3)

    @patch("app.providers.book_catalogue.time.sleep")
    @patch("app.providers.book_catalogue.session")
    def test_partial_book_failure_reuses_successful_components_on_retry(
        self, session, sleep
    ):
        edition = {
            "title": "The Fellowship of the Ring",
            "works": [{"key": "/works/OL27513W"}],
        }
        work = {"title": "The Fellowship of the Ring"}
        session.return_value.get.side_effect = [
            self.response(data=edition),
            self.response(data=work),
            *[self.response(503) for _ in range(3)],
        ]
        with self.assertRaises(book_catalogue.CatalogueUnavailable):
            openlibrary.book("OL26449223M")
        self.assertIsNone(cache.get("openlibrary_book_OL26449223M"))
        # Advance beyond the circuit deadline; successful edition/work responses
        # stay fresh. Only the failed ratings request should run again.
        cache.delete("free-books:cooldown:v2:openlibrary")
        session.return_value.get.side_effect = [
            self.response(data={"summary": {"average": 4, "count": 10}})
        ]
        result = openlibrary.book("OL26449223M")
        self.assertEqual(result["title"], edition["title"])
        self.assertEqual(session.return_value.get.call_count, 6)
        self.assertTrue(
            session.return_value.get.call_args.args[0].endswith("/ratings.json")
        )
        self.assertEqual(result["score"], 8)

    @patch("app.providers.book_catalogue.request")
    def test_explicit_refresh_bypasses_all_component_caches(self, request):
        request.side_effect = [
            {"title": "Corrected edition", "works": [{"key": "/works/OL1W"}]},
            {"authors": [{"author": {"key": "/authors/OL1A"}}]},
            {"name": "Author"},
            {"summary": {}},
        ]
        cache.set(
            "openlibrary_book_OL1M", {"catalogue_version": 2, "title": "Old title"}
        )
        self.assertEqual(
            openlibrary.book("OL1M", refresh=True)["title"], "Corrected edition"
        )
        self.assertTrue(all(call.kwargs["refresh"] for call in request.call_args_list))

    @patch("app.providers.services.get_media_metadata")
    def test_detail_outage_uses_503_with_retry_after_and_clear_reason(self, metadata):
        user = get_user_model().objects.create_user(username="outage-reader")
        self.client.force_login(user)
        metadata.side_effect = book_catalogue.CatalogueUnavailable(
            "openlibrary", requests.Timeout("timeout"), 15, 503
        )
        response = self.client.get(
            reverse(
                "media_details",
                args=[
                    "openlibrary",
                    "book",
                    "OL26449223M",
                    "the-fellowship-of-the-ring",
                ],
            )
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Retry-After"], "15")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertContains(response, "Catalogue Unavailable", status_code=503)
        self.assertContains(response, "Try again in 15 seconds", status_code=503)
        self.assertNotContains(response, "(network error)", status_code=503)

    @patch("app.views.Item.fetch_releases")
    @patch("app.views.openlibrary.book")
    @patch("app.views.cache")
    def test_native_metadata_refresh_explicitly_refreshes_openlibrary(
        self, view_cache, book, releases
    ):
        view_cache.ttl.return_value = None
        user = get_user_model().objects.create_user(username="refresh-reader")
        self.client.force_login(user)
        book.return_value = {
            "title": "Corrected title",
            "image": "https://example.org/book.jpg",
        }
        response = self.client.post(
            reverse("sync_metadata", args=["openlibrary", "book", "OL26449223M"]),
            {"next": "/"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 204)
        book.assert_called_once_with("OL26449223M", refresh=True)
        self.assertEqual(
            Item.objects.get(media_id="OL26449223M").title, "Corrected title"
        )
