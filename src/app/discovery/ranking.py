"""Deterministic textual ranking; provider order is a bounded tie-breaker."""

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
    if all(word in target for word in words):
        return 800 - min(50, len(target) - len(words))
    if name.startswith(query):
        return 740
    limit = 0 if len(query) < 4 else 1 if len(query) < 8 else 2
    distance = edit_distance(query, name, limit)
    if distance <= limit:
        return 700 - distance * 50
    edits = 0
    remaining = target.copy()
    for word in words:
        limit = 0 if len(word) < 4 else 1 if len(word) < 8 else 2
        matches = [
            (edit_distance(word, candidate, limit), index)
            for index, candidate in enumerate(remaining)
        ]
        if not matches:
            return 0
        best, index = min(matches)
        if best > limit:
            return 0
        edits += best
        remaining.pop(index)
    return 600 - edits * 40 - min(100, len(remaining) * 10)


def score(query, row):
    primary = name_score(query, row["name"])
    aliases = max(
        (name_score(query, alias) - 20 for alias in row.get("aliases", [])), default=0
    )
    return max(primary, aliases, 0)


def merge_ranked(query, direct, indexed):
    """One identity per result, exact names first, no separate suggestion tier."""
    merged = {}
    for position, row in enumerate(direct):
        identity = (row["source"], row["kind"], str(row["external_id"]))
        merged[identity] = dict(row, _provider_order=position)
    for row in indexed:
        identity = (row["source"], row["kind"], str(row["external_id"]))
        if identity not in merged:
            merged[identity] = dict(row, _provider_order=10000)
        else:
            merged[identity]["aliases"] = list(
                dict.fromkeys(
                    [*merged[identity].get("aliases", []), *row.get("aliases", [])]
                )
            )

    def order(row):
        relevance = score(query, row)
        # Provider relevance orders aliases and other provider matches that do
        # not literally occur in the displayed title. It cannot outrank text.
        weight = (relevance if relevance else 200) + 5 / (1 + row["_provider_order"])
        return (-weight, len(row["name"]), row["name"].casefold(), row["external_id"])

    return sorted(merged.values(), key=order)
