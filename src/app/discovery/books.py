"""Public book relationships from Hardcover's explicit catalogue records."""

import copy
from collections import Counter

import requests
from django.conf import settings
from django.core.cache import cache
from django.core.paginator import Paginator
from django.http import Http404, HttpResponseBadRequest
from django.urls import reverse
from django.views.decorators.http import require_GET

from app.providers import hardcover, services
from app.discovery.catalogue import link_with_query

BOOK_FIELDS = 'id title cached_image(path:"url") release_date users_count compilation cached_contributors'


def query(document, variables):
    try:
        response = services.api_request(
            "hardcover",
            "POST",
            hardcover.base_url,
            params={"query": document, "variables": variables},
            headers={"Authorization": settings.HARDCOVER_API},
        )
    except requests.RequestException as error:
        raise services.ProviderAPIError("hardcover", error) from error
    if response.get("errors") or not isinstance(response.get("data"), dict):
        raise services.ProviderAPIError(
            "hardcover", ValueError("Catalogue query failed")
        )
    return response["data"]


def entity_url(kind, external_id):
    return reverse("book_entity", args=[kind, external_id])


def book_links(book):
    contributors = []
    for credit in book.get("cached_contributors") or []:
        author = credit.get("author") or {}
        if author.get("id") and author.get("name"):
            contributors.append(
                {
                    "name": author["name"],
                    "url": entity_url("author", author["id"]),
                    "role": credit.get("contributor_role_name")
                    or credit.get("contribution")
                    or "Author",
                }
            )
    series = []
    for membership in book.get("book_series") or []:
        group = membership.get("series")
        if group:
            series.append(
                {
                    "id": group["id"],
                    "name": group["name"],
                    "url": entity_url("series", group["id"]),
                    "position": membership.get("details") or membership.get("position"),
                }
            )
    publisher = (book.get("default_cover_edition") or {}).get("publisher")
    return {
        "contributors": contributors,
        "series": series,
        "publisher": {
            "name": publisher["name"],
            "url": entity_url("publisher", publisher["id"]),
        }
        if publisher and publisher.get("id")
        else None,
    }


def card(book, **extra):
    return {
        "kind": "book",
        "source": "hardcover",
        "external_id": str(book["id"]),
        "name": book["title"],
        "image": book.get("cached_image") or settings.IMG_NONE,
        "date": book.get("release_date") or "",
        **extra,
    }


def series(external_id):
    key = f"book-series:v1:{external_id}"
    cached = cache.get(key)
    if cached is not None:
        return copy.deepcopy(cached)
    data = query(
        "query($id:Int!){ series_by_pk(id:$id){id name description} }",
        {"id": int(external_id)},
    )
    group = data["series_by_pk"]
    if not group:
        raise Http404
    memberships = []
    for offset in range(0, 5000, 100):
        batch = query(
            """query($id:Int!,$offset:Int!){ book_series(
          where:{series_id:{_eq:$id},book:{canonical_id:{_is_null:true}}},
          order_by:[{position:asc_nulls_last},{book:{users_count:desc}},{book_id:asc}],limit:100,offset:$offset
        ){position details compilation book{"""
            + BOOK_FIELDS
            + "}}}",
            {"id": int(external_id), "offset": offset},
        )["book_series"]
        memberships.extend(batch)
        if len(batch) < 100:
            break
    else:
        raise services.ProviderAPIError(
            "hardcover", ValueError("Series exceeds catalogue limit")
        )
    rows, positions, ids = [], set(), set()
    for membership in memberships:
        book = membership.get("book")
        if not book or not book.get("title") or book["id"] in ids:
            continue
        ids.add(book["id"])
        position = membership.get("position")
        if membership.get("compilation") or book.get("compilation"):
            section = "collections"
        elif (
            position is not None
            and position > 0
            and float(position).is_integer()
            and position not in positions
        ):
            section = "books"
            positions.add(position)
        else:
            section = "other"
        label = membership.get("details") or (
            f"{position:g}" if position is not None else ""
        )
        rows.append(card(book, section=section, position=label))
    result = {
        "name": group["name"],
        "description": group.get("description") or "",
        "results": rows,
    }
    cache.set(key, result)
    return copy.deepcopy(result)


def bibliography(kind, external_id, page, role="all"):
    key = f"book-{kind}:v1:{external_id}:{page}:{role}"
    cached = cache.get(key)
    if cached is not None:
        return copy.deepcopy(cached)
    table = "authors" if kind == "author" else "publishers"
    detail = "bio" if kind == "author" else ""
    id_type = "Int" if kind == "author" else "bigint"
    info = query(
        f"query($id:{id_type}!){{ {table}_by_pk(id:$id){{id name {detail}}} }}",
        {"id": int(external_id)},
    )[f"{table}_by_pk"]
    if not info:
        raise Http404
    where = {"canonical_id": {"_is_null": True}}
    if kind == "author":
        contribution = {"author_id": {"_eq": int(external_id)}}
        if role != "all":
            if role == "Author":
                contribution["_or"] = [
                    {"contribution": {"_eq": role}},
                    {"contribution": {"_is_null": True}},
                ]
            else:
                contribution["contribution"] = {"_eq": role}
        where["contributions"] = contribution
    else:
        where["editions"] = {"publisher_id": {"_eq": int(external_id)}}
    data = query(
        "query($where:books_bool_exp!,$offset:Int!){ books(where:$where,order_by:[{users_count:desc},{id:asc}],limit:37,offset:$offset){"
        + BOOK_FIELDS
        + "} }",
        {"where": where, "offset": (page - 1) * 36},
    )["books"]
    rows = []
    for book in data[:36]:
        roles = (
            sorted(
                {
                    c.get("contribution") or "Author"
                    for c in book.get("cached_contributors", [])
                    if str((c.get("author") or {}).get("id")) == str(external_id)
                }
            )
            if kind == "author"
            else []
        )
        rows.append(card(book, roles=[{"role": r} for r in roles]))
    roles = []
    if kind == "author":
        role_key = f"book-author-roles:v1:{external_id}"
        roles = cache.get(role_key)
        if roles is None:
            credits = query(
                "query($id:Int!){contributions(where:{author_id:{_eq:$id}},distinct_on:contribution){contribution}}",
                {"id": int(external_id)},
            )["contributions"]
            roles = sorted({c["contribution"] or "Author" for c in credits})
            cache.set(role_key, roles)
    result = {
        "roles": roles,
        "name": info["name"],
        "description": info.get("bio") or "",
        "results": rows,
        "has_next": len(data) > 36,
    }
    cache.set(key, result)
    return copy.deepcopy(result)


def enrich_detail(request, metadata):
    from app.discovery.views import enrich_cards

    sections = []
    for link in metadata.get("book_links", {}).get("series", []):
        data = series(link["id"])
        rows = [r for r in data["results"] if r["section"] == "books"]
        for row in rows:
            row["current"] = str(row["external_id"]) == str(metadata["media_id"])
        enrich_cards(request, rows[:12])
        sections.append({**link, "results": rows[:12], "total": len(rows)})
    metadata["book_series"] = sections


@require_GET
def entity(request, kind, external_id):
    from app.discovery.views import enrich_cards, private_render, page_number

    if kind not in ("series", "author", "publisher"):
        raise Http404
    try:
        page = page_number(request)
    except ValueError:
        return HttpResponseBadRequest("Invalid page")
    layout = request.GET.get("layout", "grid")
    section = request.GET.get("section", "books")
    role = request.GET.get("role", "all")
    if (
        layout not in ("grid", "list")
        or section not in ("books", "other", "collections", "all")
        or len(role) > 100
    ):
        return HttpResponseBadRequest("Invalid filter")
    filters = []
    if kind == "series":
        data = series(external_id)
        counts = Counter(r["section"] for r in data["results"])
        for value, label in [
            ("books", "Books"),
            ("other", "Other books"),
            ("collections", "Collections"),
            ("all", "All"),
        ]:
            count = len(data["results"]) if value == "all" else counts[value]
            filters.append(
                {
                    "label": f"{label} ({count})",
                    "active": section == value,
                    "url": link_with_query(request.path, section=value, layout=layout),
                }
            )
        rows = [
            r for r in data["results"] if section == "all" or r["section"] == section
        ]
        pagination = Paginator(rows, 36).get_page(page)
        data["results"] = list(pagination)
        page, has_next = pagination.number, pagination.has_next()
    else:
        data = bibliography(kind, external_id, page, role)
        has_next = data["has_next"]
        if kind == "author":
            roles = data["roles"]
            filters = [
                {
                    "label": "All roles" if r == "all" else r,
                    "active": r == role,
                    "url": link_with_query(request.path, role=r, layout=layout),
                }
                for r in ["all", *sorted(roles)]
            ]
    enrich_cards(request, data["results"])
    params = {"section": section, "layout": layout, "role": role}
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
