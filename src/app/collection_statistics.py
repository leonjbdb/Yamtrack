"""Owner-scoped collection analytics; catalogue estimates stay separate from logs."""

import re
from collections import Counter
from datetime import timedelta

from django.apps import apps
from django.urls import reverse
from django.utils import timezone

from app import config
from app.models import CollectionFacts, Episode

TYPES = [
    "game",
    "movie",
    "tv",
    "season",
    "anime",
    "book",
    "manga",
    "comic",
    "boardgame",
]
LABELS = dict(
    zip(
        TYPES,
        [
            "Games",
            "Movies",
            "TV shows",
            "TV seasons",
            "Anime",
            "Books",
            "Manga",
            "Comics",
            "Board games",
        ],
        strict=True,
    )
)
UNITS = {
    "game": "hours played",
    "movie": "watches",
    "tv": "episodes watched",
    "season": "episodes watched",
    "anime": "episodes watched",
    "book": "pages read",
    "manga": "chapters read",
    "comic": "issues read",
    "boardgame": "plays",
}
FINISHED = {"Completed", "Played"}
PLANNED = {"Planning", "Planned"}


def duration(value):
    """Parse explicit catalogue durations, never infer a generic episode length."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if value > 0 else None
    if not isinstance(value, str):
        return None
    match = re.fullmatch(
        r"\s*(?:(\d+(?:\.\d+)?)h\s*)?(?:(\d+(?:\.\d+)?)\s*(?:min|m))?\s*", value
    )
    if match and any(match.groups()):
        return float(match[1] or 0) * 60 + float(match[2] or 0)
    return None


def public_facts(data):
    details = data.get("details", {})
    year = None
    for key in ("release_date", "first_air_date", "start_date", "publish_date", "year"):
        match = re.match(r"^(\d{4})(?:\D|$)", str(details.get(key, "")))
        if match:
            year = int(match[1])
            break

    def names(value):
        if isinstance(value, str):
            return [value] if value else []
        return [
            str(x.get("name", "")) if isinstance(x, dict) else str(x)
            for x in (value or [])
        ][:40]

    return {
        "runtime": duration(details.get("runtime")),
        "playtime": duration(details.get("playtime")),
        "maximum": data.get("max_progress"),
        "year": year,
        "genres": names(data.get("genres")),
        "platforms": names(details.get("platforms")),
        "episodes": {
            str(e["episode_number"]): e.get("runtime")
            for e in data.get("episodes", [])
            if "episode_number" in e
        },
    }


def owner_rows(user):
    result = {}
    for kind in TYPES:
        fields = [
            "id",
            "item_id",
            "status",
            "score",
            "notes",
            "created_at",
            "item__title",
            "item__image",
            "item__media_id",
            "item__source",
            "item__season_number",
        ]
        if kind not in ("tv", "season"):
            fields.extend(["progress", "start_date", "end_date"])
        if kind == "season":
            fields.append("related_tv_id")
        result[kind] = list(
            apps.get_model("app", kind).objects.filter(user=user).values(*fields)
        )
    return result


def tracked_item_ids(user):
    return {r["item_id"] for rows in owner_rows(user).values() for r in rows}


def hour_text(minutes):
    hours, mins = divmod(round(minutes), 60)
    return f"{hours:,}h {mins:02d}m"


def month_labels(today, months):
    total = today.year * 12 + today.month - 1
    return [
        f"{(total - i) // 12:04d}-{(total - i) % 12 + 1:02d}"
        for i in reversed(range(months))
    ]


def dashboard(user, kind="all", months=12):
    today = timezone.localdate()
    rows = owner_rows(user)
    item_ids = {r["item_id"] for group in rows.values() for r in group}
    fact_rows = list(CollectionFacts.objects.filter(item_id__in=item_ids))
    facts = {f.item_id: f.data for f in fact_rows if f.fetched_at}
    labels = month_labels(today, months)
    monthly = {k: Counter() for k in TYPES}
    monthly_units = {k: Counter() for k in TYPES}
    monthly_time = {k: Counter() for k in TYPES}
    active_days = Counter()
    episode_counts = Counter()
    episode_minutes = Counter()
    episode_known = Counter()
    seasons = {r["id"]: r for r in rows["season"]}
    for ep in Episode.objects.filter(related_season__user=user).values(
        "related_season_id", "item__episode_number", "end_date"
    ):
        sid = ep["related_season_id"]
        season = seasons[sid]
        minutes = (
            facts.get(season["item_id"], {})
            .get("episodes", {})
            .get(str(ep["item__episode_number"]))
        )
        episode_counts[sid] += 1
        if minutes is not None and minutes > 0:
            episode_minutes[sid] += minutes
            episode_known[sid] += 1
        if ep["end_date"]:
            day = timezone.localtime(ep["end_date"]).date()
            month = day.strftime("%Y-%m")
            for k in ("tv", "season"):
                monthly_units[k][month] += 1
                monthly_time[k][month] += minutes or 0
            if kind in ("all", "tv", "season"):
                active_days[day.isoformat()] += 1
    tv_units, tv_minutes, tv_known = Counter(), Counter(), Counter()
    for sid, season in seasons.items():
        tid = season["related_tv_id"]
        tv_units[tid] += episode_counts[sid]
        tv_minutes[tid] += episode_minutes[sid]
        tv_known[tid] += episode_known[sid]
    summaries = []
    all_records = []
    for k in TYPES:
        # Steam reports cumulative time. Repeated tracking rows must not multiply it.
        if k == "game":
            by_item = {}
            for row in sorted(rows[k], key=lambda r: r["created_at"]):
                previous = by_item.get(row["item_id"])
                row["progress"] = max(
                    row["progress"], previous["progress"] if previous else 0
                )
                by_item[row["item_id"]] = row
            collection = list(by_item.values())
        else:
            collection = rows[k]
        statuses = Counter(r["status"] for r in collection)
        units = minutes = known = possible = 0
        scored = [float(r["score"]) for r in collection if r["score"] is not None]
        note_count = sum(bool(r["notes"].strip()) for r in collection)
        for row in collection:
            f = facts.get(row["item_id"], {})
            runtime = f.get("runtime")
            progress = row.get("progress", 0)
            amount = progress
            time = 0
            if k == "game":
                time = progress
                known += 1
                possible += 1
            elif k == "movie":
                amount = int(row["status"] == "Completed")
                time = amount * (runtime or 0)
                possible += amount
                known += amount if runtime else 0
            elif k in ("tv", "season"):
                counts = tv_units if k == "tv" else episode_counts
                times = tv_minutes if k == "tv" else episode_minutes
                coverage = tv_known if k == "tv" else episode_known
                amount, time = counts[row["id"]], times[row["id"]]
                possible += amount
                known += coverage[row["id"]]
            elif k == "anime":
                time = amount * (runtime or 0)
                possible += amount
                known += amount if runtime else 0
            units += amount
            minutes += time
            month = timezone.localtime(row["created_at"]).strftime("%Y-%m")
            monthly[k][month] += 1
            if k == "movie" and amount and row["end_date"]:
                day = timezone.localtime(row["end_date"]).date()
                monthly_units[k][day.strftime("%Y-%m")] += 1
                monthly_time[k][day.strftime("%Y-%m")] += time
                if kind in ("all", k):
                    active_days[day.isoformat()] += 1
            maximum = f.get("maximum")
            remaining = (
                max(0, maximum - amount)
                if isinstance(maximum, (int, float))
                and maximum > 0
                and k in ("anime", "book", "manga", "comic")
                else None
            )
            all_records.append(
                {
                    **row,
                    "kind": k,
                    "label": LABELS[k],
                    "units": amount,
                    "minutes": time,
                    "time": hour_text(time),
                    "facts": f,
                    "remaining": remaining,
                    "url": reverse("medialist", args=[user.username, k]),
                    "age": (today - timezone.localtime(row["created_at"]).date()).days,
                }
            )
        summaries.append(
            {
                "kind": k,
                "label": LABELS[k],
                "color": config.get_stats_color(k),
                "entries": len(collection),
                "titles": len({r["item_id"] for r in collection}),
                "repeats": len(collection) - len({r["item_id"] for r in collection}),
                "units": round(units / 60, 1) if k == "game" else units,
                "unit": UNITS[k],
                "minutes": minutes,
                "time": hour_text(minutes),
                "known": known,
                "possible": possible,
                "coverage": round(known / possible * 100) if possible else None,
                "statuses": dict(statuses),
                "planned": sum(statuses[x] for x in PLANNED),
                "engaged": sum(
                    v for status, v in statuses.items() if status not in PLANNED
                ),
                "engaged_percent": round(
                    sum(v for status, v in statuses.items() if status not in PLANNED)
                    / len(collection)
                    * 100
                )
                if collection
                else 0,
                "rated": len(scored),
                "reviewed": note_count,
                "average": round(sum(scored) / len(scored), 1) if scored else None,
            }
        )
    # Progress timestamps describe when increments were recorded, not when imported
    # lifetime totals were played/read. '+' baselines deliberately stay out of trends.
    for k in ("game", "anime", "book", "manga", "comic", "boardgame"):
        row_map = {r["id"]: r for r in rows[k]}
        previous = {}
        history = (
            apps.get_model("app", k)
            .history.filter(id__in=row_map)
            .order_by("id", "history_date", "history_id")
            .values("id", "progress", "history_date", "history_type")
        )
        for h in history.iterator(chunk_size=2000):
            old = previous.get(h["id"])
            previous[h["id"]] = h["progress"]
            if old is None or h["history_type"] != "~":
                continue
            delta = max(0, h["progress"] - old)
            if not delta:
                continue
            day = timezone.localtime(h["history_date"]).date()
            month = day.strftime("%Y-%m")
            monthly_units[k][month] += delta
            minutes = (
                delta
                if k == "game"
                else delta
                * (facts.get(row_map[h["id"]]["item_id"], {}).get("runtime") or 0)
                if k == "anime"
                else 0
            )
            monthly_time[k][month] += minutes
            if kind in ("all", k):
                active_days[day.isoformat()] += 1
    selected = (
        summaries if kind == "all" else [s for s in summaries if s["kind"] == kind]
    )
    records = [r for r in all_records if kind == "all" or r["kind"] == kind]
    primary = [s for s in selected if s["kind"] != "season" or kind == "season"]
    primary_records = [r for r in records if r["kind"] != "season" or kind == "season"]
    genres, eras, sources, platforms = Counter(), Counter(), Counter(), Counter()
    categorized = set()
    for row in primary_records:
        identity = (row["kind"], row["item_id"])
        if identity in categorized:
            continue
        categorized.add(identity)
        for genre in row["facts"].get("genres", []):
            genres[genre] += 1
        for platform in row["facts"].get("platforms", []):
            platforms[platform] += 1
        if row["facts"].get("year"):
            eras[str(row["facts"]["year"] // 10 * 10) + "s"] += 1
        sources[row["item__source"]] += 1
    status_order = [
        "Planned",
        "Played",
        "Planning",
        "Completed",
        "In progress",
        "Paused",
        "Dropped",
    ]
    status_order = [
        s for s in status_order if any(c["statuses"].get(s) for c in selected)
    ]
    native = kind in ("book", "manga", "comic", "boardgame")
    biggest = sorted(
        [r for r in primary_records if r["units"]]
        if native
        else [r for r in primary_records if r["minutes"]],
        key=lambda r: r["units"] if native else r["minutes"],
        reverse=True,
    )[:10]
    for record in biggest:
        record["investment"] = (
            f"{record['units']:,} {UNITS[record['kind']]}" if native else record["time"]
        )

    backlog = sorted(
        [r for r in records if r["status"] in PLANNED], key=lambda r: r["created_at"]
    )[:8]
    selected_ids = {r["item_id"] for r in records}
    unmatched = 0
    if kind in ("all", "game"):
        from game_connections.models import LibraryGame

        unmatched = LibraryGame.objects.filter(
            connection__user=user, item__isnull=True
        ).count()
    # Include catalogue-unmatched Steam time separately so it is not lost in totals.
    unmatched_minutes = 0
    if unmatched:
        from django.db.models import Sum

        unmatched_minutes = (
            LibraryGame.objects.filter(
                connection__user=user, item__isnull=True
            ).aggregate(total=Sum("minutes"))["total"]
            or 0
        )
    playing = (
        sum(s["minutes"] for s in primary if s["kind"] == "game") + unmatched_minutes
    )
    viewing = sum(
        s["minutes"] for s in primary if s["kind"] in ("movie", "tv", "season", "anime")
    )
    chart_types = [s for s in primary if s["entries"]]
    chart = {
        "mix": {
            "labels": [s["label"] for s in chart_types],
            "values": [s["titles"] for s in chart_types],
            "colors": [s["color"] for s in chart_types],
        },
        "status": {
            "labels": [s["label"] for s in selected if s["entries"]],
            "datasets": [
                {
                    "label": status,
                    "data": [
                        s["statuses"].get(status, 0) for s in selected if s["entries"]
                    ],
                    "backgroundColor": config.get_status_stats_color(status),
                }
                for status in status_order
            ],
        },
        "time": {
            "labels": [s["label"] for s in primary if s["minutes"]],
            "values": [round(s["minutes"] / 60, 2) for s in primary if s["minutes"]],
            "colors": [s["color"] for s in primary if s["minutes"]],
        },
        "months": labels,
        "growth": [
            {
                "label": s["label"],
                "data": [monthly[s["kind"]][m] for m in labels],
                "backgroundColor": s["color"],
            }
            for s in primary
            if s["entries"]
        ],
        "recordedTime": [
            {
                "label": s["label"],
                "data": [round(monthly_time[s["kind"]][m] / 60, 2) for m in labels],
                "borderColor": s["color"],
                "backgroundColor": s["color"] + "30",
                "fill": True,
            }
            for s in primary
            if s["kind"] in ("game", "movie", "tv", "season", "anime")
        ],
        "progress": {
            "labels": labels,
            "values": [monthly_units[kind][m] for m in labels],
            "unit": UNITS[kind] if kind != "all" else "",
        }
        if kind != "all"
        else None,
        "genres": dict(genres.most_common(10)),
        "eras": dict(sorted(eras.items())),
        "sources": dict(sources),
        "platforms": dict(platforms.most_common(8)),
        "ratings": {
            str(i): sum(
                r["score"] is not None and int(r["score"]) == i for r in records
            )
            for i in range(11)
        },
    }
    heat = []
    start = today - timedelta(days=364)
    start -= timedelta(days=start.weekday())
    for offset in range((today - start).days + 1):
        day = start + timedelta(days=offset)
        count = active_days[day.isoformat()]
        heat.append({"date": day.isoformat(), "count": count, "level": min(4, count)})
    investment = Counter(
        {
            "Unplayed": 0,
            "Under 1h": 0,
            "1–10h": 0,
            "10–50h": 0,
            "50–100h": 0,
            "100h+": 0,
        }
    )
    for record in primary_records:
        if record["kind"] == "game":
            mins = record["minutes"]
            bucket = (
                "Unplayed"
                if mins == 0
                else "Under 1h"
                if mins < 60
                else "1–10h"
                if mins < 600
                else "10–50h"
                if mins < 3000
                else "50–100h"
                if mins < 6000
                else "100h+"
            )
            investment[bucket] += 1
    chart["investment"] = dict(investment)
    chart["depth"] = dict.fromkeys(
        ["Not started", "Under 25%", "25–50%", "50–75%", "75–99%", "100%"], 0
    )
    for record in primary_records:
        maximum = record["facts"].get("maximum")
        if (
            record["kind"] in ("anime", "book", "manga", "comic")
            and isinstance(maximum, (int, float))
            and maximum > 0
        ):
            ratio = record["units"] / maximum
            bucket = (
                "Not started"
                if ratio <= 0
                else "Under 25%"
                if ratio < 0.25
                else "25–50%"
                if ratio < 0.5
                else "50–75%"
                if ratio < 0.75
                else "75–99%"
                if ratio < 1
                else "100%"
            )
            chart["depth"][bucket] += 1
    scores = [float(r["score"]) for r in records if r["score"] is not None]
    return {
        "kind": kind,
        "native": native,
        "native_value": sum(s["units"] for s in selected),
        "native_unit": UNITS.get(kind, ""),
        "finished": sum(s["statuses"].get("Completed", 0) for s in selected),
        "period": months,
        "title": LABELS.get(kind, "Every collection"),
        "types": [{"key": k, "label": LABELS[k]} for k in TYPES],
        "summaries": selected,
        "chart": chart,
        "playing": hour_text(playing),
        "viewing": hour_text(viewing),
        "combined": hour_text(playing + viewing),
        "titles": sum(s["titles"] for s in primary),
        "planned": sum(s["planned"] for s in primary),
        "active": sum(s["statuses"].get("In progress", 0) for s in primary),
        "dropped": sum(s["statuses"].get("Dropped", 0) for s in primary),
        "repeats": sum(s["repeats"] for s in primary),
        "reviewed": sum(s["reviewed"] for s in selected),
        "rated": len(scores),
        "average": round(sum(scores) / len(scores), 1) if scores else None,
        "biggest": biggest,
        "backlog": backlog,
        "heat": heat,
        "active_days": sum(
            v > 0
            for d, v in active_days.items()
            if start.isoformat() <= d <= today.isoformat()
        ),
        "metadata_known": len(selected_ids & facts.keys()),
        "metadata_total": len(selected_ids),
        "metadata_errors": sum(f.error for f in fact_rows if f.item_id in selected_ids),
        "unmatched": unmatched,
        "unmatched_time": hour_text(unmatched_minutes),
        "records": records,
    }
