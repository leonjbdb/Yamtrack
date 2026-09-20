"""Viewing time from individual catalogue runtimes and recorded watches."""

import math
import re

from django.urls import reverse
from django.utils.text import slugify


def duration(value):
    """Accept positive finite minutes or an explicitly formatted duration."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) and value > 0 else None
    if not isinstance(value, str):
        return None
    match = re.fullmatch(
        r"\s*(?:(\d+(?:\.\d+)?)h\s*)?(?:(\d+(?:\.\d+)?)\s*(?:min|m))?\s*", value
    )
    if match and any(match.groups()):
        return duration(float(match[1] or 0) * 60 + float(match[2] or 0))
    return None


def hour_text(minutes):
    hours, mins = divmod(round(minutes), 60)
    return f"{hours:,}h {mins:02d}m"


def episode_runtime(facts, number):
    return duration(facts.get("episodes", {}).get(str(number)))


def anime_minutes(facts, start, end):
    """Only individually documented episodes count, never a series average."""
    return sum(episode_runtime(facts, n) or 0 for n in range(start + 1, end + 1))


def breakdown(rows, facts, episodes, kind):
    """One auditable row per movie or episode, grouping repeated watches."""
    grouped = {}

    def add(row, media_type, runtime, number=None, name=""):
        source, media_id = row["item__source"], row["item__media_id"]
        season = row["item__season_number"] if media_type == "episode" else None
        identity = (media_type, source, media_id, season, number)
        if identity not in grouped:
            title = row["item__title"]
            if media_type == "episode":
                url = reverse(
                    "season_details",
                    args=[source, media_id, slugify(title) or "title", season],
                )
                episode = f"S{season:02d}E{number:02d}"
            else:
                url = reverse(
                    "media_details",
                    args=[source, media_type, media_id, slugify(title) or "title"],
                )
                episode = f"Episode {number}" if number is not None else ""
            grouped[identity] = {
                "kind": media_type,
                "title": title,
                "episode": episode,
                "episode_name": name,
                "season_number": season,
                "episode_number": number,
                "source": source,
                "media_id": media_id,
                "url": url,
                "runtime_minutes": runtime,
                "runtime": hour_text(runtime) if runtime is not None else "Unknown",
                "watches": 0,
            }
        grouped[identity]["watches"] += 1

    if kind in ("all", "movie"):
        for row in rows["movie"]:
            if row["status"] == "Completed":
                add(
                    row, "movie", duration(facts.get(row["item_id"], {}).get("runtime"))
                )
    if kind in ("all", "tv", "season"):
        seasons = {row["id"]: row for row in rows["season"]}
        for watch in episodes:
            row = seasons[watch["related_season_id"]]
            number = watch["item__episode_number"]
            f = facts.get(row["item_id"], {})
            add(
                row,
                "episode",
                episode_runtime(f, number),
                number,
                f.get("episode_names", {}).get(str(number), ""),
            )
    if kind in ("all", "anime"):
        for row in rows["anime"]:
            f = facts.get(row["item_id"], {})
            for number in range(1, row["progress"] + 1):
                add(
                    row,
                    "anime",
                    episode_runtime(f, number),
                    number,
                    f.get("episode_names", {}).get(str(number), ""),
                )
    result = list(grouped.values())
    for row in result:
        runtime = row["runtime_minutes"]
        row["minutes"] = runtime * row["watches"] if runtime is not None else None
        row["time"] = (
            hour_text(row["minutes"]) if row["minutes"] is not None else "Unknown"
        )
    result.sort(
        key=lambda row: (
            -(row["minutes"] or 0),
            row["title"].casefold(),
            row["season_number"] or 0,
            row["episode_number"] or 0,
        )
    )
    known = sum(row["watches"] for row in result if row["runtime_minutes"] is not None)
    watched = sum(row["watches"] for row in result)
    return {
        "rows": result,
        "watched": watched,
        "known": known,
        "missing": watched - known,
        "missing_titles": sum(row["runtime_minutes"] is None for row in result),
        "minutes": sum(row["minutes"] or 0 for row in result),
        "coverage": round(100 * known / watched, 1) if watched else None,
    }
