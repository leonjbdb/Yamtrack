"""Text relevance blended with bounded public audience evidence."""

import math
from collections import Counter

from app.discovery.catalogue import normalize


def edit_distance(left, right, limit=2):
    """Bounded Damerau-Levenshtein distance, including adjacent transpositions."""
    if abs(len(left) - len(right)) > limit:
        return limit + 1
    previous = list(range(len(right) + 1))
    previous_previous = None
    for i, a in enumerate(left, 1):
        row = [i]
        for j, b in enumerate(right, 1):
            value = min(row[-1] + 1, previous[j] + 1, previous[j - 1] + (a != b))
            if (
                previous_previous is not None
                and i > 1
                and j > 1
                and a == right[j - 2]
                and left[i - 2] == b
            ):
                value = min(value, previous_previous[j - 2] + 1)
            row.append(value)
        if min(row) > limit:
            return limit + 1
        previous_previous, previous = previous, row
    return previous[-1]


def token_match(word, candidate):
    """Return edit/completion cost and the corrected portion of a title word."""
    limit = 0 if len(word) < 4 or word.isdigit() else 1 if len(word) < 8 else 2
    distance = edit_distance(word, candidate, limit)
    if distance <= limit:
        return distance * 40, candidate
    if candidate.startswith(word) and not word.isdigit():
        return 15 + min(25, len(candidate) - len(word)), word
    if not limit:
        return None
    # Complete a misspelled prefix without charging the rest of the word as
    # typos. This includes plural titles and attached sequel numbers.
    matches = []
    for end in range(len(word), min(len(candidate), len(word) + limit) + 1):
        distance = edit_distance(word, candidate[:end], limit)
        if distance <= limit:
            matches.append(
                (distance * 40 + 15 + min(25, len(candidate) - end), candidate[:end])
            )
    return min(matches) if matches else None


def matched_tokens(query, name):
    """Match every query word to a distinct title word, most specific first."""
    words, target = normalize(query).split(), normalize(name).split()
    options = []
    for position, word in enumerate(words):
        matches = [
            (match[0], index, match[1])
            for index, candidate in enumerate(target)
            if (match := token_match(word, candidate)) is not None
        ]
        if not matches:
            return None
        options.append((len(matches), position, sorted(matches)))
    used, corrected, cost = set(), {}, 0
    for _, position, matches in sorted(options):
        available = [match for match in matches if match[1] not in used]
        if not available:
            return None
        penalty, index, word = available[0]
        used.add(index)
        corrected[position] = word
        cost += penalty
    return cost, " ".join(corrected[index] for index in range(len(words)))


def name_score(query, name):
    query, name = normalize(query), normalize(name)
    if not query or not name:
        return 0
    if query == name:
        return 1000
    words, target = query.split(), name.split()
    if len(words) == len(target) and sorted(words) == sorted(target):
        return 940
    if f" {query} " in f" {name} ":
        return 850 - min(50, len(target) - len(words))
    if not Counter(words) - Counter(target):
        return 800 - min(50, len(target) - len(words))
    if name.startswith(query):
        return 740
    limit = 0 if len(query) < 4 else 1 if len(query) < 8 else 2
    distance = edit_distance(query, name, limit)
    if distance <= limit:
        return 700 - distance * 50
    matched = matched_tokens(query, name)
    if matched is None:
        return 0
    # Extra subtitle words are weak evidence, not ten points per word: a short
    # documentary title must not bury a relevant feature film or game sequel.
    return max(400, 600 - matched[0] - min(10, (len(target) - len(words)) * 0.25))


def score(query, row):
    primary = name_score(query, row["name"])
    aliases = max(
        (name_score(query, alias) - 20 for alias in row.get("aliases", [])), default=0
    )
    return max(primary, aliases, 0)


def merge_ranked(query, direct, indexed):
    """One ranked list; popularity helps relevant works, not unrelated matches."""
    merged = {}
    for position, row in enumerate(direct):
        identity = (row["source"], row["kind"], str(row["external_id"]))
        merged[identity] = dict(row, _provider_order=position, _direct=True)
    for row in indexed:
        identity = (row["source"], row["kind"], str(row["external_id"]))
        if identity not in merged:
            merged[identity] = dict(
                row, _provider_order=row.get("_provider_order", 10000)
            )
        else:
            merged[identity]["_provider_order"] = min(
                merged[identity]["_provider_order"], row.get("_provider_order", 10000)
            )
            if "prominence" not in merged[identity] and "prominence" in row:
                merged[identity]["prominence"] = row["prominence"]
            merged[identity]["aliases"] = list(
                dict.fromkeys(
                    [*merged[identity].get("aliases", []), *row.get("aliases", [])]
                )
            )

    # Function words do not make a broad franchise query more specific. They
    # remain part of title matching; this only controls the exact-title bonus.
    specific_words = [
        word
        for word in normalize(query).split()
        if word not in {"a", "an", "the", "of", "and", "or", "in", "on", "for", "to"}
    ]

    def order(row):
        relevance = score(query, row)
        try:
            prominence = float(row.get("prominence", 0))
        except (TypeError, ValueError):
            prominence = 0
        prominence = min(1, max(0, prominence)) if math.isfinite(prominence) else 0
        # Every query token must match before audience size can help. Log-scaled
        # evidence may lift a famous franchise novel over an obscure exact-name
        # tie, while precise full titles retain a stronger exact-match benefit.
        popularity_bonus = 360 * prominence if relevance >= 400 else 0
        exact_bonus = (
            min(240, max(0, len(specific_words) - 2) * 80) if relevance >= 980 else 0
        )
        weight = (
            (relevance if relevance else 200)
            + popularity_bonus
            + exact_bonus
            + (5 if row.get("_direct") else 0)
            + 5 / (1 + row["_provider_order"])
        )
        return (-weight, len(row["name"]), row["name"].casefold(), row["external_id"])

    return sorted(merged.values(), key=order)
