"""Book relationships from explicit Wikidata statements and identifiers."""

import re
from decimal import Decimal, InvalidOperation

from django.core.cache import cache

from app.providers.book_catalogue import request
from app.providers.services import ProviderAPIError

QID = re.compile(r"Q[1-9][0-9]*")
OLID = re.compile(r"OL[0-9]+[WM]")


def api(params):
    # These are interactive, user-requested lookups. MediaWiki recommends
    # maxlag for noninteractive bots; it can reject interactive reads solely
    # because its separate query-service index is lagging.
    return request("wikidata", "/w/api.php", {"format": "json", **params})


def entities(ids):
    ids = list(dict.fromkeys(ids))
    if any(not QID.fullmatch(x) for x in ids):
        raise ValueError("Invalid Wikidata identifier")
    result = {}
    for offset in range(0, len(ids), 50):
        data = api(
            {
                "action": "wbgetentities",
                "ids": "|".join(ids[offset : offset + 50]),
                "props": "claims|labels|descriptions",
                "languages": "en|nb|nn",
            }
        )
        if not isinstance(data.get("entities"), dict):
            raise ProviderAPIError("wikidata", ValueError("Missing entities"))
        result.update(data["entities"])
    return result


def search_ids(expression):
    """Use the statement index, avoiding expensive public SPARQL joins."""
    result = []
    offset = 0
    while True:
        data = api(
            {
                "action": "query",
                "list": "search",
                "srsearch": expression,
                "srnamespace": 0,
                "srlimit": 50,
                "sroffset": offset,
            }
        )
        rows = data.get("query", {}).get("search")
        if not isinstance(rows, list):
            raise ProviderAPIError("wikidata", ValueError("Missing search results"))
        result.extend(r["title"] for r in rows if QID.fullmatch(r.get("title", "")))
        following = data.get("continue", {}).get("sroffset")
        if following is None:
            return list(dict.fromkeys(result))
        if following <= offset or following >= 5000:
            raise ProviderAPIError(
                "wikidata", ValueError("Series exceeds catalogue limit")
            )
        offset = following


def statements(entity, prop):
    claims = [
        x
        for x in entity.get("claims", {}).get(prop, [])
        if x.get("rank") != "deprecated"
        and x.get("mainsnak", {}).get("snaktype") == "value"
    ]
    preferred = [x for x in claims if x.get("rank") == "preferred"]
    return preferred or claims


def values(entity, prop):
    return [
        x["mainsnak"]["datavalue"]["value"]
        for x in statements(entity, prop)
        if "datavalue" in x["mainsnak"]
    ]


def label(entity, field="labels"):
    labels = entity.get(field, {})
    return next(
        (labels[lang]["value"] for lang in ("en", "nb", "nn") if lang in labels),
        entity.get("id", "") if field == "labels" else "",
    )


def book_identity(media_id, work_id, work_aliases=()):
    ids = [x for x in (work_id, media_id, *work_aliases) if x and OLID.fullmatch(x)]
    learned = cache.get(f"free-books:identity:{work_id}")
    if learned and QID.fullmatch(learned):
        return entities([learned]).get(learned)
    if not ids:
        return None
    candidates = entities(
        search_ids("haswbstatement:" + "|".join(f"P648={x}" for x in ids))
    )
    # The search index can lag; verify each identifier against the actual claims.
    work_matches = [
        e
        for e in candidates.values()
        if set([work_id, *work_aliases]) & set(values(e, "P648"))
    ]
    if len(work_matches) == 1:
        return work_matches[0]
    if len(work_matches) > 1:
        raise ProviderAPIError("wikidata", ValueError("Conflicting book identifiers"))
    edition_matches = [e for e in candidates.values() if media_id in values(e, "P648")]
    if len(edition_matches) > 1:
        raise ProviderAPIError(
            "wikidata", ValueError("Conflicting edition identifiers")
        )
    if not edition_matches:
        return None
    edition = edition_matches[0]
    works = [
        v["id"]
        for v in values(edition, "P629")
        if isinstance(v, dict) and QID.fullmatch(v.get("id", ""))
    ]
    if len(works) > 1:
        raise ProviderAPIError("wikidata", ValueError("Edition has multiple works"))
    return entities(works).get(works[0]) if works else edition


def memberships(entity):
    result = []
    for statement in statements(entity, "P179"):
        value = statement["mainsnak"].get("datavalue", {}).get("value", {})
        if not isinstance(value, dict) or not QID.fullmatch(value.get("id", "")):
            continue
        ordinals = [
            q["datavalue"]["value"]
            for q in statement.get("qualifiers", {}).get("P1545", [])
            if q.get("snaktype") == "value" and "datavalue" in q
        ]
        result.append(
            {
                "id": value["id"],
                "position": str(ordinals[0]) if len(ordinals) == 1 else "",
            }
        )
    return result


def number(position):
    try:
        value = Decimal(position)
        return value if value.is_finite() and value >= 0 else None
    except (InvalidOperation, TypeError):
        return None


def series(qid):
    if not QID.fullmatch(qid):
        raise ValueError("Invalid series identifier")
    info = entities([qid]).get(qid, {})
    if "missing" in info or not info:
        raise ProviderAPIError("wikidata", ValueError("Series not found"))
    members = entities(search_ids(f"haswbstatement:P179={qid}"))
    rows = []
    for external_id, book in members.items():
        # Verify membership rather than trusting stale search-index data.
        positions = {m["position"] for m in memberships(book) if m["id"] == qid}
        if not positions:
            continue
        position = next(iter(positions)) if len(positions) == 1 else ""
        ordinal = number(position)
        rows.append(
            {
                "qid": external_id,
                "name": label(book),
                "position": position,
                "section": "books"
                if ordinal is not None
                and ordinal > 0
                and ordinal == ordinal.to_integral_value()
                else "other",
                "ol_ids": sorted(
                    {
                        x
                        for x in values(book, "P648")
                        if isinstance(x, str) and OLID.fullmatch(x)
                    }
                ),
            }
        )
    rows.sort(
        key=lambda r: (
            number(r["position"]) is None,
            number(r["position"]) or Decimal(0),
            r["name"].casefold(),
            r["qid"],
        )
    )
    return {
        "name": label(info),
        "description": label(info, "descriptions"),
        "results": rows,
        "source_url": f"https://www.wikidata.org/wiki/{qid}",
    }
