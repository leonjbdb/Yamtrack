"""A public name index and explicit, bounded typo suggestions."""

import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import urlencode

from django.conf import settings
from django.db.models import Q
from django.urls import reverse
from django.utils.text import slugify

from app.models import DiscoveryEntry


def normalize(value):
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(re.findall(r"[^\W_]+", value, re.UNICODE))


def url_for(source, kind, external_id, name=""):
    if kind in ("person", "company", "network"):
        return reverse("discovery_entity", args=[source, kind, external_id])
    return reverse(
        "media_details", args=[source, kind, external_id, slugify(name) or "title"]
    )


def remember(entries):
    unique = {}
    for row in entries:
        if row["source"] == "manual" or not row.get("name"):
            continue
        identity = (row["source"], row["kind"], str(row["external_id"]))
        unique[identity] = row
    if not unique:
        return
    # Preserve alternate names learned from detail pages when search returns less.
    existing = {
        (x.source, x.kind, x.external_id): x
        for x in DiscoveryEntry.objects.filter(
            source__in={k[0] for k in unique},
            kind__in={k[1] for k in unique},
            external_id__in={k[2] for k in unique},
        )
    }
    objects = []
    for identity, row in unique.items():
        previous = existing.get(identity)
        aliases = list(
            dict.fromkeys(
                [*(previous.aliases if previous else []), *row.get("aliases", [])]
            )
        )[:40]
        objects.append(
            DiscoveryEntry(
                source=identity[0],
                kind=identity[1],
                external_id=identity[2],
                name=row["name"][:500],
                aliases=aliases,
                search_text=normalize(" ".join([row["name"], *aliases])),
                image=row.get("image") or (previous.image if previous else ""),
                description=(
                    row.get("description") or (previous.description if previous else "")
                )[:500],
                adult=bool(row.get("adult", previous.adult if previous else False)),
            )
        )
    DiscoveryEntry.objects.bulk_create(
        objects,
        update_conflicts=True,
        unique_fields=["source", "kind", "external_id"],
        update_fields=[
            "name",
            "aliases",
            "search_text",
            "image",
            "description",
            "adult",
            "updated_at",
        ],
    )


def card(entry):
    return {
        "source": entry.source,
        "kind": entry.kind,
        "external_id": entry.external_id,
        "name": entry.name,
        "image": entry.image or settings.IMG_NONE,
        "description": entry.description,
        "url": url_for(entry.source, entry.kind, entry.external_id, entry.name),
    }


def similarity(query, name):
    a, b = normalize(query), normalize(name)
    if not a or not b:
        return 0
    if a == b:
        return 1
    if a in b:
        return 0.94
    direct = SequenceMatcher(None, a, b).ratio()
    sorted_words = SequenceMatcher(
        None, " ".join(sorted(a.split())), " ".join(sorted(b.split()))
    ).ratio()
    # A surname or single title word may be the intended query.
    token = (
        max((SequenceMatcher(None, a, word).ratio() for word in b.split()), default=0)
        * 0.94
        if len(a.split()) == 1
        else 0
    )
    return max(direct, sorted_words, token)


def close_matches(query, kinds, sources=None, limit=8):
    text = normalize(query)
    if len(text) < 3:
        return []
    fragments = set()
    for word in text.split()[:6]:
        if len(word) < 3:
            continue
        for pos in (0, max(0, len(word) // 2 - 1), max(0, len(word) - 3)):
            fragments.add(word[pos : pos + 3])
    if not fragments:
        return []
    terms = Q()
    for fragment in fragments:
        terms |= Q(search_text__contains=fragment)
    queryset = DiscoveryEntry.objects.filter(terms, kind__in=kinds)
    if sources:
        queryset = queryset.filter(source__in=sources)
    if not settings.TMDB_NSFW:
        queryset = queryset.exclude(source="tmdb", adult=True)
    ranked = []
    for entry in queryset.order_by("-updated_at")[:2000]:
        score = max(similarity(text, name) for name in [entry.name, *entry.aliases])
        if score >= 0.72:
            ranked.append((score, entry))
    ranked.sort(key=lambda item: (-item[0], len(item[1].name), item[1].name))
    return [
        dict(card(entry), match=round(score * 100)) for score, entry in ranked[:limit]
    ]


def link_with_query(path, **params):
    return (
        path + "?" + urlencode({k: v for k, v in params.items() if v not in (None, "")})
    )
