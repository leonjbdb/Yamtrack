"""Authenticated discovery pages. Provider data is public; badges are owner-scoped."""

from collections import Counter
from django.apps import apps
from django.core.paginator import Paginator
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET
from app import config
from app.models import MediaTypes
from app.providers import services
from app.discovery import providers
from app.discovery.catalogue import (
    close_matches,
    remember,
    url_for,
    link_with_query,
    normalize,
)

SCOPES = [
    ("screen", "Movies, TV & people"),
    ("people", "People"),
    ("studios", "Film & TV studios"),
    ("networks", "TV networks"),
    ("game_studios", "Game studios"),
    ("game", "Games"),
    ("anime", "Anime"),
    ("book", "Books"),
    ("manga", "Manga"),
    ("comic", "Comics"),
    ("boardgame", "Board games"),
]


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


@require_GET
def search(request):
    scope = request.GET.get("scope", "screen")
    query = request.GET.get("q", "").strip()
    if scope not in dict(SCOPES) or len(query) > 200:
        return HttpResponseBadRequest("Invalid search")
    try:
        page = page_number(request)
    except ValueError:
        return HttpResponseBadRequest("Invalid page")
    data = {"results": [], "page": page, "total_pages": 1, "total_results": 0}
    suggestions = []
    source = None
    source_options = []
    if scope in MediaTypes.values:
        allowed = config.get_sources(scope)
        source = (
            request.GET.get("source") or config.get_default_source_name(scope).value
        )
        if source not in [x.value for x in allowed]:
            return HttpResponseBadRequest("Invalid catalogue source")
        source_options = [
            {
                "key": x.value,
                "label": x.label,
                "url": link_with_query(
                    reverse("discover"), q=query, scope=scope, source=x.value
                ),
            }
            for x in allowed
        ]
    note = ""
    if query:
        if scope in ("screen", "people", "studios"):
            data = providers.search_screen(query, scope, page)
            kinds = {
                "screen": ["movie", "tv", "person"],
                "people": ["person"],
                "studios": ["company"],
            }[scope]
            sources = ["tmdb"]
        elif scope == "game_studios":
            data = providers.search_game_companies(query, page)
            kinds = ["company"]
            sources = ["igdb"]
        elif scope == "networks":
            # TMDB has network detail/discover APIs but no network-name search API.
            data["results"] = close_matches(query, ["network"], ["tmdb"], limit=40)
            kinds = ["network"]
            sources = ["tmdb"]
            note = "Networks are indexed from the TV catalogues explored on this server. Open any show to discover its network."
        else:
            result = services.search(scope, query, page, source)
            cards = []
            for r in result["results"]:
                cards.append(
                    {
                        "source": r["source"],
                        "kind": r["media_type"],
                        "external_id": str(r["media_id"]),
                        "name": r["title"],
                        "image": r["image"],
                        "description": "",
                        "url": url_for(
                            r["source"], r["media_type"], r["media_id"], r["title"]
                        ),
                    }
                )
            remember(cards)
            data = dict(result, results=cards)
            kinds = [scope]
            sources = [source]
        if page == 1 and scope != "networks":
            exact = {
                (r["source"], r["kind"], r["external_id"]) for r in data["results"]
            }
            suggestions = [
                r
                for r in close_matches(query, kinds, sources)
                if (r["source"], r["kind"], r["external_id"]) not in exact
            ]
            # With no good local spelling candidate, a bounded name-token lookup
            # can discover candidates the instance has not encountered before.
            if (
                not data["results"]
                and not suggestions
                and scope in ("screen", "people", "studios")
            ):
                tokens = [t for t in normalize(query).split() if len(t) >= 4]
                probes = (
                    list(dict.fromkeys(tokens))[:2]
                    if len(tokens) > 1
                    else [tokens[0][:4]]
                    if tokens and len(tokens[0]) >= 6
                    else []
                )
                for probe in probes:
                    if normalize(probe) != normalize(query):
                        providers.search_screen(probe, scope, 1)
                suggestions = close_matches(query, kinds, sources)
    own_badges(request.user, [*data["results"], *suggestions])
    base = reverse("discover")
    context = {
        "query": query,
        "scope": scope,
        "scopes": [
            {"key": k, "label": v, "url": link_with_query(base, q=query, scope=k)}
            for k, v in SCOPES
        ],
        "data": data,
        "suggestions": suggestions,
        "note": note,
        "source": source,
        "source_options": source_options,
    }
    if page > 1:
        context["previous"] = link_with_query(
            base, q=query, scope=scope, source=source, page=page - 1
        )
    if data.get("has_next") or page < data.get("total_pages", 1):
        context["next"] = link_with_query(
            base, q=query, scope=scope, source=source, page=page + 1
        )
    return private_render(request, "app/discovery/search.html", context)


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
    media_type = request.GET.get(
        "type", "all" if kind == "person" else "tv" if kind == "network" else "movie"
    )
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
        "kind": kind,
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
    context["entity"] = data
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
            )
    return private_render(request, "app/discovery/entity.html", context)
