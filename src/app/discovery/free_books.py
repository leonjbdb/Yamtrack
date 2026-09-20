"""Free book discovery using Open Library editions and Wikidata relationships."""

import copy
import re
from collections import Counter
from urllib.parse import urlencode

from django.conf import settings
from django.core.cache import cache
from django.core.paginator import Paginator
from django.http import Http404, HttpResponseBadRequest
from django.urls import reverse
from django.views.decorators.http import require_GET

from app.discovery.catalogue import link_with_query
from app.providers import openlibrary, wikidata_books
from app.providers.book_catalogue import request as catalogue_request
from app.providers.services import ProviderAPIError


def entity_url(kind, external_id, **params):
    return link_with_query(
        reverse("free_book_entity", args=[kind, external_id]), **params
    )


def related_url(media_id):
    return reverse("free_book_related", args=[media_id])


def card(item, **extra):
    return {
        "kind": "book",
        "source": "openlibrary",
        "external_id": item["media_id"],
        "name": item["title"],
        "image": item["image"],
        "work_id": item.get("work_id"),
        "edition_ids": item.get("edition_ids", []),
        **extra,
    }


def catalogue_matches(ids):
    matched = {}
    # Group identifier values under one Solr field. Open Library's query
    # parser does not preserve OR across repeated, differently named fields.
    for suffix, field in [("W", "key"), ("M", "edition_key")]:
        selected = [x for x in ids if x.endswith(suffix)]
        for offset in range(0, len(selected), 50):
            batch = selected[offset : offset + 50]
            terms = [f"/works/{x}" if suffix == "W" else x for x in batch]
            query = f"{field}:(" + " OR ".join(terms) + ")"
            data = openlibrary.search_data(query, limit=100)
            if data.get("numFound", 0) > 100:
                raise ProviderAPIError(
                    "openlibrary", ValueError("Ambiguous catalogue mappings")
                )
            for item in openlibrary.search_items(data):
                for identity in [item.get("work_id"), *item.get("edition_ids", [])]:
                    if identity in batch:
                        matched.setdefault(identity, {})[item["work_id"]] = item
    return matched


def series_cards(rows, current=None):
    ids = sorted({x for r in rows for x in r["ol_ids"]})
    matched = catalogue_matches(ids)
    redirects = {}
    for identity in ids:
        if identity not in matched:
            kind = "works" if identity.endswith("W") else "books"
            try:
                _, aliases = openlibrary.record(kind, identity)
            except ProviderAPIError as error:
                if error.status_code == 404:
                    continue
                raise
            if aliases[-1] != identity:
                redirects[identity] = aliases[-1]
    if redirects:
        resolved = catalogue_matches(sorted(set(redirects.values())))
        for identity, canonical in redirects.items():
            if canonical in resolved:
                matched[identity] = resolved[canonical]
    results = []
    for row in rows:
        candidates = {
            key: value
            for identity in row["ol_ids"]
            for key, value in matched.get(identity, {}).items()
        }
        if len(candidates) == 1:
            item = next(iter(candidates.values()))
            # This reverse mapping is learned only from verified identifiers
            # and explicit Open Library merges, never from similar titles.
            identity_key = f"free-books:identity:{item['work_id']}"
            existing = cache.get(identity_key)
            if existing and existing != row["qid"]:
                raise ProviderAPIError(
                    "wikidata", ValueError("Conflicting book identifiers")
                )
            cache.set(identity_key, row["qid"], 86400)
            active = bool(current and current.get("work_id") == item.get("work_id"))
            if active:
                item = {
                    **item,
                    "media_id": current["media_id"],
                    "title": current["title"],
                    "image": current["image"],
                }
            results.append(card(item, position=row["position"], current=active))
        else:
            # Keep the explicitly named series member visible. A manual search
            # is not a claimed identity match and never creates a tracking row.
            results.append(
                {
                    "kind": "book_link",
                    "source": "wikidata",
                    "external_id": row["qid"],
                    "name": row["name"],
                    "image": settings.IMG_NONE,
                    "position": row["position"],
                    "url": reverse("search")
                    + "?"
                    + urlencode(
                        {
                            "media_type": "book",
                            "source": "openlibrary",
                            "q": row["name"],
                        }
                    ),
                    "description": "No linked edition. Search books.",
                }
            )
    return results


def prefer_tracked_editions(user, rows):
    """Reuse the owner's tracked edition when the catalogue proves the same work."""
    from app.models import Book

    ids = {x for row in rows for x in row.get("edition_ids", [])}
    if not ids:
        return
    tracked = list(
        Book.objects.filter(
            user=user, item__source="openlibrary", item__media_id__in=ids
        )
        .select_related("item")
        .order_by("-created_at")
    )
    for row in rows:
        if row.get("current"):
            continue
        matches = [b for b in tracked if b.item.media_id in row.get("edition_ids", [])]
        if matches:
            item = matches[0].item
            row.update(
                external_id=item.media_id,
                name=item.title,
                image=item.image or settings.IMG_NONE,
            )


@require_GET
def related(request, media_id):
    from app.discovery.views import enrich_cards, private_render

    if not re.fullmatch(r"OL[0-9]+M", media_id):
        raise Http404
    try:
        metadata = openlibrary.book(media_id)
        book = wikidata_books.book_identity(
            media_id, metadata.get("work_id"), metadata.get("work_aliases", [])
        )
        links = wikidata_books.memberships(book) if book else []
        sections = []
        for link in links:
            data = wikidata_books.series(link["id"])
            main = [r for r in data["results"] if r["section"] == "books"]
            rows = series_cards(main[:12], metadata)
            enrich_cards(request, rows)
            sections.append(
                {
                    **link,
                    "name": data["name"],
                    "url": entity_url("series", link["id"]),
                    "total": len(main),
                    "results": rows,
                }
            )
        context = {
            "sections": sections,
            "source_url": f"https://www.wikidata.org/wiki/{book['id']}"
            if book
            else None,
            "related_url": related_url(media_id),
        }
    except ProviderAPIError as error:
        context = {"error": str(error), "related_url": related_url(media_id)}
    return private_render(request, "app/discovery/free_book_related.html", context)


def quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


@require_GET
def entity(request, kind, external_id):
    from app.discovery.views import enrich_cards, page_number, private_render

    try:
        page = page_number(request)
    except ValueError:
        return HttpResponseBadRequest("Invalid page")
    layout = request.GET.get("layout", "grid")
    section = request.GET.get("section", "books")
    if layout not in ("grid", "list") or section not in ("books", "other", "all"):
        return HttpResponseBadRequest("Invalid filter")
    filters = []
    params = {"layout": layout, "section": section}
    if kind == "series" and wikidata_books.QID.fullmatch(external_id):
        data = copy.deepcopy(wikidata_books.series(external_id))
        counts = Counter(r["section"] for r in data["results"])
        filters = [
            {
                "label": f"{label} ({len(data['results']) if value == 'all' else counts[value]})",
                "active": section == value,
                "url": link_with_query(request.path, **(params | {"section": value})),
            }
            for value, label in [
                ("books", "Books"),
                ("other", "Other books"),
                ("all", "All"),
            ]
        ]
        rows = [
            r for r in data["results"] if section == "all" or r["section"] == section
        ]
        pagination = Paginator(rows, 36).get_page(page)
        page, has_next = pagination.number, pagination.has_next()
        data["results"] = series_cards(list(pagination))
    elif kind == "author" and re.fullmatch(r"OL[0-9]+A", external_id):
        info = catalogue_request("openlibrary", f"/authors/{external_id}.json")
        response = openlibrary.search_data(f"author_key:{external_id}", page, 36)
        description = info.get("bio") or ""
        if isinstance(description, dict):
            description = description.get("value", "")
        data = {
            "name": info.get("name", external_id),
            "description": description,
            "results": [card(i) for i in openlibrary.search_items(response)],
            "source_url": f"https://openlibrary.org/authors/{external_id}",
        }
        has_next = page * 36 < response["numFound"]
    elif kind == "publisher" and external_id == "books":
        name = request.GET.get("name", "").strip()
        if not name or len(name) > 200:
            return HttpResponseBadRequest("Invalid publisher")
        params["name"] = name
        response = openlibrary.search_data(f"publisher:{quote(name)}", page, 36)
        data = {
            "name": name,
            "results": [card(i) for i in openlibrary.search_items(response)],
        }
        has_next = page * 36 < response["numFound"]
    elif kind == "editions" and re.fullmatch(r"OL[0-9]+W", external_id):
        info = catalogue_request("openlibrary", f"/works/{external_id}.json")
        response = catalogue_request(
            "openlibrary",
            f"/works/{external_id}/editions.json",
            {"limit": 36, "offset": (page - 1) * 36},
        )
        rows = []
        for edition in response.get("entries", []):
            media_id = openlibrary.extract_openlibrary_id(edition.get("key", ""))
            if not re.fullmatch(r"OL[0-9]+M", media_id or "") or not edition.get(
                "title"
            ):
                continue
            description = [
                openlibrary.get_publish_date(edition),
                openlibrary.get_physical_format(edition),
                ", ".join(
                    x["key"].rsplit("/", 1)[-1]
                    for x in edition.get("languages", [])
                    if x.get("key")
                ),
            ]
            rows.append(
                card(
                    {
                        "media_id": media_id,
                        "title": edition["title"],
                        "image": openlibrary.get_cover_image_url(edition),
                    },
                    roles=[{"role": " · ".join(x for x in description if x)}],
                )
            )
        data = {
            "name": info["title"] + " — Editions",
            "results": rows,
            "source_url": f"https://openlibrary.org/works/{external_id}",
        }
        has_next = page * 36 < response.get("size", 0)
    else:
        raise Http404
    enrich_cards(request, data["results"])
    return private_render(
        request,
        "app/discovery/book_entity.html",
        {
            "entity": data,
            "kind": kind,
            "layout": layout,
            "filters": filters,
            "search_media_type": "book",
            "page": page,
            "previous": link_with_query(request.path, **params, page=page - 1)
            if page > 1
            else None,
            "next": link_with_query(request.path, **params, page=page + 1)
            if has_next
            else None,
            "layout_filters": [
                {
                    "label": label,
                    "active": layout == value,
                    "url": link_with_query(
                        request.path, **(params | {"layout": value}), page=page
                    ),
                }
                for value, label in [("grid", "Grid"), ("list", "List")]
            ],
        },
    )
