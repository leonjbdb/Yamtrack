"""Search categories and ranked results using the existing Yamtrack controls."""

import copy
import hashlib
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.core.cache import cache
from django.core.paginator import Paginator
from django.http import HttpResponseBadRequest
from django.urls import reverse

from app import config
from app.discovery import providers
from app.discovery.catalogue import close_matches, link_with_query, normalize, remember
from app.discovery.ranking import matched_tokens, merge_ranked, score
from app.models import MediaTypes
from app.providers import services

CATEGORIES = [
    ("all", "All Search"),
    ("movie", "Movies"),
    ("tv", "TV Shows"),
    ("people", "People"),
    ("anime", "Anime"),
    ("game", "Games"),
    ("book", "Books"),
    ("manga", "Manga"),
    ("comic", "Comics"),
    ("boardgame", "Boardgames"),
    ("studios", "Studios"),
    ("networks", "TV Networks"),
    ("game_studios", "Game Companies"),
]
ENTITY_KINDS = {
    "people": ["person"],
    "studios": ["company"],
    "networks": ["network"],
    "game_studios": ["company"],
}


def categories(user):
    enabled = user.get_enabled_media_types()
    return [
        (key, label)
        for key, label in CATEGORIES
        if key not in MediaTypes.values or key in enabled
    ]


def media_rows(rows):
    return [
        {
            "source": r["source"],
            "kind": r["media_type"],
            "external_id": str(r["media_id"]),
            "name": r["title"],
            "image": r["image"],
            "aliases": r.get("aliases", []),
            "work_id": r.get("work_id"),
            "edition_ids": r.get("edition_ids", []),
            **({"prominence": r["prominence"]} if "prominence" in r else {}),
        }
        for r in rows
    ]


def fetch_category(category, query, page, source):
    if category in ("people", "studios"):
        return providers.search_screen(query, category, page)
    if category == "game_studios":
        return providers.search_game_companies(query, page)
    if category == "networks":
        rows = close_matches(query, ["network"], ["tmdb"], limit=100)
        return {
            "results": rows,
            "page": 1,
            "total_pages": 1,
            "total_results": len(rows),
        }
    result = copy.deepcopy(services.search(category, query, page, source))
    result["results"] = media_rows(result.get("results", []))
    return result


def candidates(category, query, rows, source, kinds):
    indexed = close_matches(query, kinds, [source] if source else None, limit=80)
    if category == "all":
        book_source = config.get_default_source_name("book").value
        indexed = [
            r for r in indexed if r["kind"] != "book" or r["source"] == book_source
        ]
    # Search corrected words as well as the literal query. An indexed original
    # must lead to unindexed sequels, not stop retrieval at the first good hit.
    if category != "networks":
        probes = []
        for row in merge_ranked(query, rows, indexed):
            match = matched_tokens(query, row["name"])
            if match and match[0] and match[1] != normalize(query):
                target_category = category
                if category == "all":
                    target_category = {
                        "person": "people",
                        "company": "game_studios"
                        if row["source"] == "igdb"
                        else "studios",
                    }.get(row["kind"], row["kind"])
                if target_category == "network":
                    continue
                probe = (target_category, match[1], row["source"])
                if probe not in probes:
                    probes.append(probe)
            if len(probes) == 2:
                break
        for target_category, probe, target_source in probes:
            extra = fetch_category(target_category, probe, 1, target_source)["results"]
            remember(extra)
            indexed.extend(
                dict(row, _provider_order=position)
                for position, row in enumerate(extra)
                if score(query, row) >= 400
            )
    # Ask for a short public prefix only if there is no useful textual match.
    # This can find an unindexed typo without replacing the submitted query.
    if (
        category != "all"
        and category != "networks"
        and not any(score(query, row) >= 400 for row in [*rows, *indexed])
    ):
        words = normalize(query).split()
        probes = (
            list(dict.fromkeys([word for word in words if len(word) >= 4]))[:2]
            if len(words) > 1
            else [words[0][:2]]
            if words and len(words[0]) >= 4
            else []
        )
        for probe in probes:
            if probe != normalize(query):
                extra = fetch_category(category, probe, 1, source)["results"]
                remember(extra)
                indexed.extend(
                    dict(row, _provider_order=position)
                    for position, row in enumerate(extra)
                    if score(query, row) >= 400
                )
        indexed.extend(
            close_matches(query, kinds, [source] if source else None, limit=80)
        )
    return indexed


def all_results(user, query):
    enabled = [key for key, _ in categories(user) if key in MediaTypes.values]
    identity = [
        query,
        enabled,
        settings.TMDB_LANG,
        settings.TMDB_NSFW,
        settings.IGDB_NSFW,
    ]
    key = "search:ranked:v5:" + hashlib.sha256(repr(identity).encode()).hexdigest()
    result = cache.get(key)
    if result is not None:
        return copy.deepcopy(result)
    # Existing provider rate limiters still apply; no account data leaves here.
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            (
                kind,
                executor.submit(
                    services.search,
                    kind,
                    query,
                    1,
                    config.get_default_source_name(kind).value,
                ),
            )
            for kind in enabled
        ]
        rows = []
        for kind, future in futures:
            rows.extend(media_rows(future.result().get("results", [])))
    for category in ("people", "studios", "game_studios"):
        rows.extend(fetch_category(category, query, 1, None)["results"])
    remember(rows)
    kinds = [*enabled, "person", "company", "network"]
    indexed = candidates("all", query, rows, None, kinds)
    result = merge_ranked(query, rows, indexed)
    cache.set(key, result, 180)
    return copy.deepcopy(result)


def search(request, category=None):
    from app.discovery.views import enrich_cards, private_render

    category = category or request.GET.get("media_type", "all")
    legacy_filter = request.GET.get("filter")
    if legacy_filter and legacy_filter != "titles":
        category = legacy_filter
    if category not in dict(CATEGORIES):
        return HttpResponseBadRequest("Invalid search category")
    query = request.GET.get("q", "").strip()
    layout = request.GET.get("layout", "grid")
    try:
        page = int(request.GET.get("page", 1))
    except ValueError:
        return HttpResponseBadRequest("Invalid page")
    if len(query) > 200 or not 1 <= page <= 500 or layout not in ("grid", "list"):
        return HttpResponseBadRequest("Invalid search")
    source = None
    sources = []
    if category in MediaTypes.values:
        request.user.update_preference("last_search_type", category)
        sources = config.get_sources(category)
        source = (
            request.GET.get("source") or config.get_default_source_name(category).value
        )
        if source not in [option.value for option in sources]:
            return HttpResponseBadRequest("Invalid catalogue source")
    elif category in ("people", "studios", "networks"):
        source = "tmdb"
    elif category == "game_studios":
        source = "igdb"
    data = {"results": [], "page": page, "total_pages": 1, "total_results": 0}
    if query and category == "all":
        pagination = Paginator(all_results(request.user, query), 40).get_page(page)
        data.update(
            results=list(pagination),
            page=pagination.number,
            total_pages=pagination.paginator.num_pages,
            total_results=pagination.paginator.count,
        )
    elif query:
        data = fetch_category(category, query, page, source)
        remember(data["results"])
        kinds = ENTITY_KINDS.get(category, [category])
        indexed = (
            candidates(category, query, data["results"], source, kinds)
            if page == 1
            else []
        )
        data["results"] = merge_ranked(query, data["results"], indexed)
        data["total_results"] = max(data.get("total_results", 0), len(data["results"]))
    enrich_cards(request, data["results"])
    params = {
        "q": query,
        "media_type": category,
        "layout": layout,
        "source": source if sources else None,
    }
    context = {
        "data": data,
        "source": source,
        "source_options": sources,
        "show_sources": bool(sources),
        "media_type": category,
        "search_category": category,
        "layout": layout,
        "entity_search": True,
        "search_filters": [
            {
                "label": label,
                "active": key == category,
                "url": link_with_query(
                    reverse("search"), q=query, media_type=key, layout=layout
                ),
            }
            for key, label in categories(request.user)
        ],
    }
    if page > 1:
        context["previous"] = link_with_query(
            reverse("search"), **params, page=page - 1
        )
    if data.get("has_next") or page < data.get("total_pages", 1):
        context["next"] = link_with_query(reverse("search"), **params, page=page + 1)
    return private_render(request, "app/search.html", context)
