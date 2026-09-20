"""Provider-specific public credits and identities, with no account data in cache."""

import copy
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone

import requests
from django.conf import settings
from django.core.cache import cache

from app.discovery.catalogue import remember, url_for
from app.discovery.prominence import audience
from app.providers import igdb, services, tmdb


def tmdb_request(path, **params):
    key = (
        "discovery:v1:tmdb:"
        + hashlib.sha256(
            json.dumps([path, settings.TMDB_LANG, params], sort_keys=True).encode()
        ).hexdigest()
    )
    data = cache.get(key)
    if data is None:
        try:
            data = services.api_request(
                "tmdb",
                "GET",
                f"{tmdb.base_url}/{path}",
                params={**tmdb.base_params, **params},
            )
        except requests.exceptions.HTTPError as error:
            tmdb.handle_error(error)
        cache.set(key, data, 86400)
    return copy.deepcopy(data)


def entity_card(raw, kind, source="tmdb"):
    image = tmdb.get_image_url(
        raw.get("profile_path") if kind == "person" else raw.get("logo_path")
    )
    entry = {
        "source": source,
        "kind": kind,
        "external_id": str(raw["id"]),
        "name": raw.get("name", ""),
        "image": image,
        "aliases": raw.get("also_known_as", []),
        "adult": raw.get("adult", False),
        **(
            {"prominence": audience((raw["popularity"], 100, 1))}
            if "popularity" in raw
            else {}
        ),
        "description": raw.get("known_for_department") or raw.get("origin_country", ""),
    }
    return dict(entry, url=url_for(source, kind, raw["id"]))


def media_card(raw, kind=None):
    kind = kind or raw.get("media_type")
    if kind not in ("movie", "tv") or not raw.get("id"):
        return None
    name = raw.get("title") or raw.get("name") or "Untitled"
    date = raw.get("release_date") or raw.get("first_air_date") or ""
    entry = {
        "source": "tmdb",
        "kind": kind,
        "external_id": str(raw["id"]),
        "name": name,
        "image": tmdb.get_image_url(raw.get("poster_path")),
        "description": date[:4],
        "adult": raw.get("adult", False),
        "aliases": [
            x
            for x in [raw.get("original_title"), raw.get("original_name")]
            if x and x != name
        ],
    }
    return dict(
        entry,
        url=url_for("tmdb", kind, raw["id"], name),
        date=date,
        popularity=raw.get("popularity", 0) or 0,
        prominence=audience(
            (raw.get("vote_count"), 25000, 1), (raw.get("popularity"), 200, 0.85)
        ),
        rating=raw.get("vote_average", 0) or 0,
    )


def search_screen(query, kind, page=1):
    endpoint = {"screen": "multi", "people": "person", "studios": "company"}[kind]
    response = tmdb_request(
        f"search/{endpoint}", query=query, page=page, include_adult=settings.TMDB_NSFW
    )
    rows = []
    for raw in response["results"]:
        item_kind = (
            raw.get("media_type")
            if kind == "screen"
            else "person"
            if kind == "people"
            else "company"
        )
        entry = (
            entity_card(raw, item_kind)
            if item_kind in ("person", "company")
            else media_card(raw, item_kind)
        )
        if entry and (settings.TMDB_NSFW or not entry.get("adult")):
            rows.append(entry)
    remember(rows)
    return {
        "results": rows,
        "page": page,
        "total_pages": min(response.get("total_pages", 1), 500),
        "total_results": response.get("total_results", 0),
    }


def group_credits(raw, aggregate=False, creators=()):
    cast = []
    crew = defaultdict(dict)
    important = defaultdict(dict)
    entries = []
    for member in raw.get("cast", []):
        if not member.get("id"):
            continue
        entry = entity_card(member, "person")
        entries.append(entry)
        roles = (
            member.get("roles", [])
            if aggregate
            else [{"character": member.get("character", "")}]
        )
        role_names = list(
            dict.fromkeys(r.get("character", "") for r in roles if r.get("character"))
        )
        cast.append(
            dict(
                entry,
                role=" / ".join(role_names) or "Cast",
                episodes=member.get("total_episode_count") if aggregate else None,
            )
        )
    priority = {
        "Director": "Director",
        "Screenplay": "Screenplay",
        "Writer": "Writing",
        "Story": "Story",
        "Novel": "Based on a novel by",
        "Characters": "Characters",
        "Executive Producer": "Executive producers",
        "Producer": "Producers",
        "Original Music Composer": "Music",
        "Director of Photography": "Cinematography",
        "Editor": "Editing",
    }
    for member in raw.get("crew", []):
        if not member.get("id"):
            continue
        entry = entity_card(member, "person")
        entries.append(entry)
        department = member.get("department") or "Other"
        jobs = (
            member.get("jobs", [])
            if aggregate
            else [{"job": member.get("job", "Crew")}]
        )
        for job in jobs:
            title = job.get("job") or "Crew"
            key = (entry["external_id"], title)
            credit = dict(entry, role=title, episodes=job.get("episode_count"))
            crew[department][key] = credit
            if title in priority:
                important[priority[title]][entry["external_id"]] = credit
    for person in creators:
        if person.get("id"):
            entry = entity_card(person, "person")
            entries.append(entry)
            important["Created by"][entry["external_id"]] = dict(entry, role="Creator")
    remember(entries)
    order = [
        "Director",
        "Created by",
        "Screenplay",
        "Writing",
        "Story",
        "Based on a novel by",
        "Characters",
        "Producers",
        "Executive producers",
        "Music",
        "Cinematography",
        "Editing",
    ]
    return {
        "cast": cast,
        "crew": [
            {"department": dep, "people": list(people.values())}
            for dep, people in sorted(crew.items())
        ],
        "important": [
            {"label": label, "people": list(important[label].values())}
            for label in order
            if important.get(label)
        ],
    }


def title_credits(kind, external_id, season=None):
    if kind == "season":
        raw = tmdb_request(
            f"tv/{int(external_id)}/season/{int(season)}",
            append_to_response="aggregate_credits",
        )
        parent = tmdb_request(f"tv/{int(external_id)}")
    else:
        raw = tmdb_request(
            f"{kind}/{int(external_id)}",
            append_to_response="aggregate_credits" if kind == "tv" else "credits",
        )
        parent = raw
    title = media_card(parent, "tv" if kind == "season" else kind)
    if title:
        remember([title])
    aggregate = kind != "movie"
    data = group_credits(
        raw.get("aggregate_credits" if aggregate else "credits", {}),
        aggregate,
        parent.get("created_by", []),
    )
    companies = [
        entity_card(c, "company")
        for c in parent.get("production_companies", [])
        if c.get("id")
    ]
    networks = [
        entity_card(c, "network") for c in parent.get("networks", []) if c.get("id")
    ]
    remember([*companies, *networks])
    data.update(companies=companies, networks=networks)
    return data


def person(external_id):
    raw = tmdb_request(
        f"person/{int(external_id)}", append_to_response="combined_credits"
    )
    person_entry = entity_card(raw, "person")
    remember([person_entry])
    credits = {}
    for section in ("cast", "crew"):
        for credit in raw.get("combined_credits", {}).get(section, []):
            entry = media_card(credit)
            if not entry or (entry.get("adult") and not settings.TMDB_NSFW):
                continue
            key = (entry["kind"], entry["external_id"])
            if key not in credits:
                credits[key] = dict(entry, roles=[])
            role = (
                "Acting"
                if section == "cast"
                else credit.get("job") or credit.get("department") or "Crew"
            )
            detail = {
                "role": role,
                "character": credit.get("character", "") if section == "cast" else "",
                "department": "Acting"
                if section == "cast"
                else credit.get("department") or "Other",
                "episodes": credit.get("episode_count"),
            }
            if detail not in credits[key]["roles"]:
                credits[key]["roles"].append(detail)
    rows = list(credits.values())
    remember(rows)
    return dict(
        person_entry,
        biography=raw.get("biography", ""),
        birthday=raw.get("birthday"),
        birthplace=raw.get("place_of_birth"),
        results=rows,
    )


def company(external_id, kind, media_type, page):
    raw = tmdb_request(f"{kind}/{int(external_id)}")
    entry = entity_card(raw, kind)
    remember([entry])
    response = tmdb_request(
        f"discover/{media_type}",
        page=page,
        sort_by="popularity.desc",
        include_adult=settings.TMDB_NSFW,
        **{
            "with_networks" if kind == "network" else "with_companies": int(external_id)
        },
    )
    rows = [
        media_card(r, media_type)
        for r in response["results"]
        if settings.TMDB_NSFW or not r.get("adult")
    ]
    remember(rows)
    return dict(
        entry,
        biography=raw.get("description", ""),
        results=rows,
        total_pages=min(response.get("total_pages", 1), 500),
        total_results=response.get("total_results", 0),
        page=page,
    )


def igdb_request(endpoint, query):
    key = "discovery:v1:igdb:" + hashlib.sha256((endpoint + query).encode()).hexdigest()
    data = cache.get(key)
    if data is None:
        headers = {
            "Client-ID": settings.IGDB_ID,
            "Authorization": f"Bearer {igdb.get_access_token()}",
        }
        try:
            data = services.api_request(
                "igdb",
                "POST",
                f"{igdb.base_url}/{endpoint}",
                data=query,
                headers=headers,
            )
        except requests.exceptions.HTTPError as error:
            result = igdb.handle_error(error)
            if result and result.get("retry"):
                headers["Authorization"] = f"Bearer {igdb.get_access_token()}"
                data = services.api_request(
                    "igdb",
                    "POST",
                    f"{igdb.base_url}/{endpoint}",
                    data=query,
                    headers=headers,
                )
        cache.set(key, data, 86400)
    return copy.deepcopy(data)


def game_company_card(raw):
    logo = raw.get("logo", {}).get("image_id")
    return {
        "source": "igdb",
        "kind": "company",
        "external_id": str(raw["id"]),
        "name": raw["name"],
        "description": "Game studio / publisher",
        "image": f"https://images.igdb.com/igdb/image/upload/t_logo_med/{logo}.png"
        if logo
        else settings.IMG_NONE,
        "url": url_for("igdb", "company", raw["id"]),
    }


def search_game_companies(query, page):
    escaped = json.dumps(query, ensure_ascii=False)
    rows = igdb_request(
        "companies",
        f"fields name,logo.image_id; where name ~ *{escaped}*; limit 40; offset {(page - 1) * 40};",
    )
    cards = [game_company_card(r) for r in rows]
    remember(cards)
    return {"results": cards, "page": page, "has_next": len(cards) == 40}


def game_companies(game_id):
    rows = igdb_request(
        "games",
        f"fields involved_companies.company.name,involved_companies.company.logo.image_id,involved_companies.developer,involved_companies.publisher,involved_companies.porting,involved_companies.supporting; where id = {int(game_id)};",
    )
    if not rows:
        services.raise_not_found_error("igdb", game_id, "game")
    cards = []
    for row in rows[0].get("involved_companies", []):
        if isinstance(row.get("company"), dict):
            c = game_company_card(row["company"])
            c["role"] = " · ".join(
                label
                for field, label in [
                    ("developer", "Developer"),
                    ("publisher", "Publisher"),
                    ("porting", "Porting"),
                    ("supporting", "Support"),
                ]
                if row.get(field)
            )
            cards.append(c)
    remember(cards)
    return {"companies": cards}


def game_company(external_id, page, role="all"):
    rows = igdb_request(
        "companies",
        f"fields name,description,logo.image_id; where id = {int(external_id)};",
    )
    if not rows:
        services.raise_not_found_error("igdb", external_id, "company")
    entry = game_company_card(rows[0])
    remember([entry])
    # Filter on the same involved-company relation to preserve its actual role.
    condition = f"company = {int(external_id)}" + (
        f" & {role} = true" if role != "all" else ""
    )
    excluded = "" if settings.IGDB_NSFW else " & game.themes != (42)"
    relations = igdb_request(
        "involved_companies",
        f"fields developer,publisher,porting,supporting,game.name,game.cover.image_id,game.first_release_date; where {condition}{excluded}; sort game.first_release_date desc; limit 40; offset {(page - 1) * 40};",
    )
    games = []
    for row in relations:
        game = row.get("game")
        if not isinstance(game, dict):
            continue
        name = game["name"]
        date = (
            datetime.fromtimestamp(game["first_release_date"], timezone.utc)
            .date()
            .isoformat()
            if game.get("first_release_date")
            else ""
        )
        games.append(
            {
                "source": "igdb",
                "kind": "game",
                "external_id": str(game["id"]),
                "name": name,
                "image": igdb.get_image_url(game),
                "description": date[:4],
                "url": url_for("igdb", "game", game["id"], name),
                "roles": [
                    {"role": label}
                    for field, label in [
                        ("developer", "Developer"),
                        ("publisher", "Publisher"),
                        ("porting", "Porting"),
                        ("supporting", "Support"),
                    ]
                    if row.get(field)
                ],
            }
        )
    remember(games)
    return dict(
        entry,
        biography=rows[0].get("description", ""),
        results=games,
        page=page,
        has_next=len(relations) == 40,
    )
