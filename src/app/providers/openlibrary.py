import copy
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from django.conf import settings
from django.core.cache import cache

from app import helpers
from app.discovery.prominence import audience
from app.models import Sources
from app.providers import services

logger = logging.getLogger(__name__)

base_url = "https://openlibrary.org/api"
search_url = "https://openlibrary.org/search.json"


def handle_error(error):
    """Handle Open Library API errors."""
    raise services.ProviderAPIError(
        Sources.OPENLIBRARY.value,
        error,
    )


SEARCH_FIELDS = "key,title,author_name,edition_key,editions,editions.key,editions.title,editions.cover_i,editions.language,readinglog_count,ratings_count,edition_count"


def search_data(query, page=1, limit=None):
    from app.providers.book_catalogue import request

    return request(
        "openlibrary",
        "/search.json",
        {
            "q": query,
            "lang": settings.BOOK_LANGUAGE,
            "fields": SEARCH_FIELDS,
            "limit": limit or settings.PER_PAGE,
            "page": page,
        },
        ttl=900,
    )


def search_items(data):
    results = []
    for doc in data.get("docs", []):
        editions = (doc.get("editions") or {}).get("docs") or []
        if not editions:
            continue
        edition = editions[0]
        media_id = extract_openlibrary_id(edition.get("key", ""))
        if not re.fullmatch(r"OL[0-9]+M", media_id or "") or not edition.get("title"):
            continue
        results.append(
            {
                "media_id": media_id,
                "source": "openlibrary",
                "media_type": "book",
                "title": edition["title"],
                "image": get_image_url(edition),
                "work_id": extract_openlibrary_id(doc.get("key", "")),
                "edition_ids": doc.get("edition_key") or [media_id],
                "prominence": audience(
                    (doc.get("readinglog_count"), 10000, 1),
                    (doc.get("ratings_count"), 1000, 1),
                    (doc.get("edition_count"), 300, 0.35),
                ),
                "aliases": [doc["title"]]
                if doc.get("title") != edition["title"]
                else [],
            }
        )
    return results


def search(query, page):
    """Search works while selecting one internally consistent edition per work."""
    data = search_data(query, page)
    return helpers.format_search_response(
        page,
        settings.PER_PAGE,
        data["numFound"],
        search_items(data),
    )


def extract_openlibrary_id(path):
    """
    Extract the ID from an OpenLibrary path.

    Args:
        path (str): A path like '/works/OL123W'

    Returns:
        str: The extracted ID (e.g., 'OL123W')
    """
    if not path:
        return None

    # Handle both full URLs and path fragments
    return path.rstrip("/").split("/")[-1]


def get_image_url(doc):
    """Get the cover image URL for a book."""
    try:
        cover_id = doc["cover_i"]
        if cover_id:
            return f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"

    except KeyError:
        pass
    return settings.IMG_NONE


def record(kind, external_id, *, refresh=False):
    """Follow explicit same-kind catalogue merges, retaining identifier aliases."""
    from app.providers.book_catalogue import request

    suffix = {"works": "W", "books": "M", "authors": "A"}[kind]
    seen = []
    current = external_id
    for _ in range(5):
        if not re.fullmatch(r"OL[0-9]+" + suffix, current or "") or current in seen:
            raise services.ProviderAPIError(
                "openlibrary", ValueError("Invalid catalogue redirect")
            )
        seen.append(current)
        data = request("openlibrary", f"/{kind}/{current}.json", refresh=refresh)
        if data.get("type", {}).get("key") != "/type/redirect":
            return data, seen
        location = data.get("location", "")
        if not location.startswith(f"/{kind}/"):
            raise services.ProviderAPIError(
                "openlibrary", ValueError("Invalid catalogue redirect")
            )
        current = location.removeprefix(f"/{kind}/")
    raise services.ProviderAPIError(
        "openlibrary", ValueError("Catalogue redirect limit reached")
    )


def book(media_id, *, refresh=False):
    """Edition metadata and work relationships, without rewriting tracked records."""
    from app.discovery.free_books import entity_url, related_url
    from app.providers.book_catalogue import request

    if not re.fullmatch(r"OL[0-9]+M", str(media_id)):
        services.raise_not_found_error("openlibrary", media_id, "book")
    key = f"openlibrary_book_{media_id}"
    cached = cache.get(key)
    if not refresh and cached is not None and cached.get("catalogue_version") == 2:
        return copy.deepcopy(cached)
    # Reuse successful component requests after a partial failure. Only an
    # explicit Refresh metadata action bypasses still-fresh catalogue records.
    edition, edition_aliases = record("books", media_id, refresh=refresh)
    work_ids = [
        extract_openlibrary_id(w.get("key", "")) for w in edition.get("works", [])
    ]
    work_ids = [w for w in work_ids if re.fullmatch(r"OL[0-9]+W", w or "")]
    work_id = work_ids[0] if len(work_ids) == 1 else None
    work, work_aliases = (
        record("works", work_id, refresh=refresh) if work_id else ({}, [])
    )
    if work_aliases:
        work_id = work_aliases[-1]
    contributors = []
    author_refs = edition.get("authors") or work.get("authors") or []
    for reference in author_refs:
        author_key = (reference.get("author") or reference).get("key", "")
        author_id = extract_openlibrary_id(author_key)
        if not re.fullmatch(r"OL[0-9]+A", author_id or ""):
            continue
        author = request("openlibrary", f"/authors/{author_id}.json", refresh=refresh)
        contributors.append(
            {
                "name": author.get("name", author_id),
                "role": "Author",
                "url": entity_url("author", author_id),
            }
        )
    for credit in edition.get("contributions") or []:
        if isinstance(credit, dict) and credit.get("name"):
            contributors.append(
                {"name": credit["name"], "role": credit.get("role") or "Contributor"}
            )
    publishers = [
        {"name": name, "url": entity_url("publisher", "books", name=name)}
        for name in edition.get("publishers", [])
    ]
    ratings = (
        request("openlibrary", f"/works/{work_id}/ratings.json", refresh=refresh).get(
            "summary", {}
        )
        if work_id
        else {}
    )
    data = {
        "catalogue_version": 2,
        "media_id": media_id,
        "source": "openlibrary",
        "source_url": f"https://openlibrary.org/books/{media_id}",
        "media_type": "book",
        "title": edition["title"],
        "max_progress": edition.get("number_of_pages"),
        "image": get_cover_image_url(edition),
        "synopsis": get_description(edition, work),
        "genres": get_subjects(work),
        "score": round(ratings["average"] * 2, 1)
        if ratings.get("average") is not None
        else None,
        "score_count": ratings.get("count"),
        "details": {
            "physical_format": get_physical_format(edition),
            "number_of_pages": edition.get("number_of_pages"),
            "publish_date": get_publish_date(edition),
            "author": [c["name"] for c in contributors if c["role"] == "Author"]
            or None,
            "publishers": get_publishers(edition),
            "isbn": get_isbns(edition),
            "language": [
                x["key"].rsplit("/", 1)[-1]
                for x in edition.get("languages", [])
                if x.get("key")
            ],
        },
        "work_id": work_id,
        "work_aliases": work_aliases,
        "edition_aliases": edition_aliases,
        "book_links": {
            "contributors": contributors,
            "publishers": publishers,
            "series": [],
        },
        "book_related_url": related_url(media_id),
        "book_editions_url": entity_url("editions", work_id) if work_id else None,
        "related": {},
    }
    cache.set(key, data)
    return copy.deepcopy(data)


def get_cover_image_url(response):
    """Get the cover image URL from a work response."""
    covers = response.get("covers", [])
    if covers:
        return f"https://covers.openlibrary.org/b/id/{covers[0]}-L.jpg"
    return settings.IMG_NONE


def get_description(response_book, response_work):
    """Extract and clean up the book description."""
    if "description" in response_book:
        description = response_book["description"]
    elif "description" in response_work:
        description = response_work["description"]
    else:
        description = "No synopsis available."

    # sometimes the description is a dict
    # like {'type': '/type/text', 'value': '...'}
    if isinstance(description, dict):
        description = description["value"]

    if description != "No synopsis available.":
        soup = BeautifulSoup(description, "html.parser")
        text = soup.get_text(separator=" ")
        description = " ".join(text.split())

    return description


def get_physical_format(response):
    """Get the physical format of the book."""
    format_value = response.get("physical_format")
    if format_value:
        return format_value.title()
    return None


def get_publish_date(response):
    """Get the first publication date."""
    if "publish_date" in response:
        publish_date = response["publish_date"].removeprefix("cop. ")

        date_formats = [
            "%B %d, %Y",  # January 19, 2001
            "%b %d, %Y",  # Oct 01, 2017
            "%d %B %Y",  # 18 March 2025
        ]
        for date_format in date_formats:
            try:
                parsed_date = datetime.strptime(publish_date, date_format).replace(
                    tzinfo=ZoneInfo("UTC"),
                )
                return parsed_date.strftime("%Y-%m-%d")
            except ValueError:
                continue
        # If no format matches, return the original string
        return publish_date
    return None


def get_subjects(response):
    """Get list of subjects/genres."""
    if "subjects" in response:
        return response["subjects"][:5]
    return None


def get_publishers(response):
    """Get list of publishers."""
    if "publishers" in response:
        return response.get("publishers", [])[:5]
    return None


def get_isbns(response):
    """Get list of ISBNs."""
    isbn_13 = response.get("isbn_13", [])
    isbn_10 = response.get("isbn_10", [])
    isbns = isbn_13 + isbn_10
    if isbns:
        return isbns
    return None
