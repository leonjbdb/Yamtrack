"""Authenticated discovery pages. Provider data is public; badges are owner-scoped."""

from collections import Counter
from django.apps import apps
from django.core.paginator import Paginator
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET
from app import helpers
from app.models import MediaTypes
from app.discovery import providers
from app.discovery.catalogue import (
    link_with_query,
    normalize,
)


def page_number(request):
    try:
        page = int(request.GET.get("page", 1))
    except (ValueError, TypeError):
        raise ValueError("Invalid page") from None
    if not 1 <= page <= 500:
        raise ValueError("Invalid page")
    return page


def own_badges(user, rows):
    for kind in {r["kind"] for r in rows} & set(MediaTypes.values):
        if kind in ("season", "episode"):
            continue
        selected = [r for r in rows if r["kind"] == kind]
        ids = [r["external_id"] for r in selected]
        tracked = (
            apps.get_model("app", kind)
            .objects.filter(user=user, item__media_id__in=ids)
            .order_by("created_at")
            .values("item__source", "item__media_id", "status")
        )
        lookup = {
            (r["item__source"], r["item__media_id"]): r["status"] for r in tracked
        }
        for row in selected:
            row["tracked"] = lookup.get((row["source"], str(row["external_id"])))


def private_render(request, template, context):
    response = render(request, template, context)
    response["Cache-Control"] = "private, no-store"
    return response


def enrich_cards(request, rows):
    """Reuse native result controls, grouping mixed filmographies by catalogue/type."""
    grouped = {}
    for row in rows:
        if row["kind"] not in MediaTypes.values:
            continue
        grouped.setdefault((row["source"], row["kind"]), []).append(row)
    for (source, kind), group in grouped.items():
        if source == "openlibrary" and kind == "book":
            from app.discovery.free_books import prefer_tracked_editions

            prefer_tracked_editions(request.user, group)
        items = [
            {
                "source": source,
                "media_type": kind,
                "media_id": row["external_id"],
                "title": row["name"],
                "image": row["image"],
            }
            for row in group
        ]
        for row, result in zip(
            group,
            helpers.enrich_items_with_user_data(request, items, "search"),
            strict=True,
        ):
            row.update(result)


@require_GET
def search(request):
    """Keep existing discovery bookmarks on the standard search interface."""
    from app.discovery.search import search as standard_search

    category = request.GET.get("scope", "all")
    return standard_search(
        request, category="all" if category == "screen" else category
    )


@require_GET
def entity(request, source, kind, external_id):
    if (
        source not in ("tmdb", "igdb")
        or kind not in ("person", "company", "network")
        or (source == "igdb" and kind != "company")
        or not str(external_id).isdigit()
    ):
        raise Http404
    try:
        page = page_number(request)
    except ValueError:
        return HttpResponseBadRequest("Invalid page")
    media_type = request.GET.get("type", "tv" if kind == "network" else "movie")
    layout = request.GET.get("layout", "grid")
    if layout not in ("grid", "list"):
        return HttpResponseBadRequest("Invalid layout")
    role = request.GET.get("role", "all")
    department = request.GET.get("department", "all")
    query = request.GET.get("q", "").strip()
    sort = request.GET.get("sort", "newest")
    if (
        len(query) > 200
        or len(role) > 100
        or len(department) > 100
        or sort not in ("newest", "oldest", "popular", "title")
    ):
        return HttpResponseBadRequest("Invalid filter")
    context = {
        "source": source,
        "layout": layout,
        "kind": kind,
        "search_media_type": "game"
        if source == "igdb"
        else "tv"
        if media_type == "tv"
        else "movie",
        "media_type": media_type,
        "role": role,
        "department": department,
        "query": query,
        "sort": sort,
        "page": page,
    }
    if kind == "person":
        if media_type not in ("all", "movie", "tv"):
            return HttpResponseBadRequest("Invalid media type")
        data = providers.person(external_id)
        rows = data["results"]
        role_counts = Counter()
        departments = Counter()
        for row in rows:
            if media_type != "all" and row["kind"] != media_type:
                continue
            if query and normalize(query) not in normalize(row["name"]):
                continue
            for label in {r["role"] for r in row["roles"]}:
                role_counts[label] += 1
            for label in {r["department"] for r in row["roles"]}:
                departments[label] += 1
        context.update(
            roles=sorted(
                role_counts.items(), key=lambda x: (x[0] != "Acting", -x[1], x[0])
            ),
            departments=sorted(departments.items()),
            credit_total=len(rows),
        )
        filtered = []
        for row in rows:
            if media_type != "all" and row["kind"] != media_type:
                continue
            if query and normalize(query) not in normalize(row["name"]):
                continue
            matching = [
                r
                for r in row["roles"]
                if (role == "all" or r["role"] == role)
                and (department == "all" or r["department"] == department)
            ]
            if matching:
                filtered.append(dict(row, roles=matching))
        if sort == "popular":
            filtered.sort(key=lambda r: -r["popularity"])
        elif sort == "title":
            filtered.sort(key=lambda r: r["name"].casefold())
        else:
            # Unknown dates remain at the end in either direction.
            dated = sorted(
                [r for r in filtered if r["date"]],
                key=lambda r: r["date"],
                reverse=sort == "newest",
            )
            filtered = dated + [r for r in filtered if not r["date"]]
        pagination = Paginator(filtered, 36).get_page(page)
        data["results"] = list(pagination)
        data["total_results"] = len(filtered)
        context.update(
            previous_page=pagination.previous_page_number()
            if pagination.has_previous()
            else None,
            next_page=pagination.next_page_number() if pagination.has_next() else None,
            page=pagination.number,
            total_pages=pagination.paginator.num_pages,
        )
    elif source == "igdb":
        if role not in ("all", "developer", "publisher", "porting", "supporting"):
            return HttpResponseBadRequest("Invalid company role")
        data = providers.game_company(external_id, page, role)
        context["media_type"] = "game"
        context.update(
            previous_page=page - 1 if page > 1 else None,
            next_page=page + 1 if data["has_next"] else None,
        )
    else:
        if media_type not in ("movie", "tv") or (
            kind == "network" and media_type != "tv"
        ):
            return HttpResponseBadRequest("Invalid media type")
        data = providers.company(external_id, kind, media_type, page)
        context.update(
            previous_page=page - 1 if page > 1 else None,
            next_page=page + 1 if page < data["total_pages"] else None,
            total_pages=data["total_pages"],
        )
    own_badges(request.user, data["results"])
    enrich_cards(request, data["results"])
    context["entity"] = data
    params = {
        "type": context["media_type"],
        "role": role,
        "department": department,
        "q": query,
        "sort": sort,
        "layout": layout,
    }

    def chips(choices, selected, **reset):
        return [
            {
                "label": label,
                "active": value == selected,
                "url": link_with_query(
                    request.path, **(params | reset | {field: value})
                ),
            }
            for field, value, label in choices
        ]

    if kind == "person":
        context["media_filters"] = chips(
            [
                ("type", "all", "All"),
                ("type", "movie", "Movies"),
                ("type", "tv", "TV shows"),
            ],
            media_type,
        )
        context["role_filters"] = chips(
            [
                ("role", "all", "All roles"),
                *[
                    ("role", name, f"{name} ({count})")
                    for name, count in context["roles"]
                ],
            ],
            role,
            department="all",
        )
        context["department_filters"] = chips(
            [
                ("department", "all", "All departments"),
                *[
                    ("department", name, f"{name} ({count})")
                    for name, count in context["departments"]
                ],
            ],
            department,
            role="all",
        )
    elif source == "igdb":
        context["role_filters"] = chips(
            [
                ("role", "all", "All roles"),
                ("role", "developer", "Developer"),
                ("role", "publisher", "Publisher"),
                ("role", "porting", "Porting"),
                ("role", "supporting", "Support"),
            ],
            role,
        )
    elif kind == "company":
        context["media_filters"] = chips(
            [("type", "movie", "Movies"), ("type", "tv", "TV shows")], media_type
        )
    context["sort_filters"] = (
        chips(
            [
                ("sort", "popular", "Most popular"),
                ("sort", "newest", "Newest"),
                ("sort", "oldest", "Oldest"),
                ("sort", "title", "A–Z"),
            ],
            sort,
        )
        if kind == "person"
        else []
    )
    context["layout_filters"] = chips(
        [("layout", "grid", "Grid"), ("layout", "list", "List")], layout
    )
    context["back_url"] = link_with_query(
        reverse("search"),
        media_type="game" if source == "igdb" else "movie",
        filter="people"
        if kind == "person"
        else "game_studios"
        if source == "igdb"
        else "networks"
        if kind == "network"
        else "studios",
        q=data["name"],
    )

    for direction in ("previous", "next"):
        if context.get(direction + "_page"):
            context[direction] = link_with_query(
                request.path,
                type=context["media_type"],
                role=role,
                department=department,
                q=query,
                sort=sort,
                page=context[direction + "_page"],
                layout=layout,
            )
    return private_render(request, "app/discovery/entity.html", context)
