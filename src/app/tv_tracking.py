"""Unified show, season and episode tracking without replacing watch history."""

import copy
import json
from functools import wraps

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.views import redirect_to_login
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from simple_history.utils import bulk_create_with_history, bulk_update_with_history

from app.helpers import is_released_date
from app.models import TV, Episode, Item, MediaTypes, Season, Sources, Status
from app.providers import services


def authenticated_tracker(view):
    """Resume expired HTMX sessions through a full SSO navigation."""

    @login_not_required
    @wraps(view)
    def wrapped(request, source, media_id, season_number=None):
        if not request.user.is_authenticated:
            path = reverse("media_details", args=[source, "tv", media_id, "episodes"])
            if season_number is not None:
                path += f"?season={season_number}"
            response = redirect_to_login(path + "#episodes")
            if request.headers.get("HX-Request"):
                response = HttpResponse(headers={"HX-Redirect": response.url})
            response["Cache-Control"] = "private, no-store"
            return response
        try:
            return view(request, source, media_id, season_number)
        except services.ProviderAPIError:
            retry = reverse("tv_tracker", args=[source, media_id])
            response = render(request, "app/tv/error.html", {"retry_url": retry})
            response["Cache-Control"] = "private, no-store"
            return response

    return wrapped


class WatchAction(forms.Form):
    """Validate explicit episode and season actions."""

    action = forms.ChoiceField(
        choices=[(v, v) for v in ("watch", "undo", "season", "log")]
    )
    episode_number = forms.IntegerField(min_value=0, required=False)
    end_date = forms.DateTimeField(required=False)


def _metadata(source, media_id, season_number=None):
    if source not in (Sources.TMDB, Sources.MANUAL):
        raise Http404
    if season_number is None:
        return copy.deepcopy(services.get_media_metadata("tv", media_id, source))
    return copy.deepcopy(
        services.get_media_metadata("season", media_id, source, [season_number])
    )


def summary(user, metadata):
    """Count distinct watched episodes; rewatches never fill an unwatched gap."""
    seasons = {
        season.item.season_number: season
        for season in Season.objects.filter(
            user=user,
            related_tv__user=user,
            related_tv__item__source=metadata["source"],
            related_tv__item__media_id=metadata["media_id"],
        )
        .select_related("item")
        .prefetch_related("episodes__item")
    }
    rows = []
    for raw in metadata.get("related", {}).get("seasons", []):
        row = dict(raw)
        season = seasons.get(row["season_number"])
        row["watched"] = (
            len({ep.item.episode_number for ep in season.episodes.all()})
            if season
            else 0
        )
        row["total"] = row.get("max_progress")
        row["complete"] = bool(row["total"] and row["watched"] >= row["total"])
        row["label"] = (
            "Specials"
            if row["season_number"] == 0
            else f"Season {row['season_number']}"
        )
        row["tracking"] = season
        rows.append(row)
    rows.sort(key=lambda row: (row["season_number"] == 0, row["season_number"]))
    regular = [row for row in rows if row["season_number"] != 0]
    return {
        "show": metadata,
        "seasons": rows,
        "watched": sum(row["watched"] for row in regular),
        "total": sum(row["total"] or 0 for row in regular),
        "completed_seasons": sum(row["complete"] for row in regular),
        "season_count": len(regular),
    }


def _episodes(user, source, media_id, number, metadata):
    watches = {}
    for watch in (
        Episode.objects.filter(
            related_season__user=user,
            related_season__related_tv__user=user,
            item__source=source,
            item__media_id=media_id,
            item__season_number=number,
        )
        .select_related("item")
        .order_by("-created_at", "-pk")
    ):
        watches.setdefault(watch.item.episode_number, []).append(watch)
    result = []
    for raw in metadata["episodes"]:
        history = watches.get(raw["episode_number"], [])
        result.append(
            {
                "number": raw["episode_number"],
                "title": raw.get("name") or raw.get("title"),
                "air_date": raw.get("air_date"),
                "runtime": raw.get("runtime"),
                "released": source == Sources.MANUAL
                or is_released_date(raw.get("air_date")),
                "watched": bool(history),
                "watches": len(history),
                "history": history,
                "last_watched": history[0].end_date if history else None,
            }
        )
    return sorted(result, key=lambda episode: episode["number"])


def _response(
    request, source, media_id, number=None, metadata=None, season_metadata=None
):
    data = summary(request.user, metadata or _metadata(source, media_id))
    data["selected_season"] = number
    if number is not None:
        if not any(row["season_number"] == number for row in data["seasons"]):
            raise Http404
        data["episodes"] = _episodes(
            request.user,
            source,
            media_id,
            number,
            season_metadata or _metadata(source, media_id, number),
        )
    response = render(request, "app/tv/tracker.html", data)
    response["Cache-Control"] = "private, no-store"
    return response


@authenticated_tracker
@require_GET
def tracker(request, source, media_id, season_number=None):
    """Show season summaries and lazily load one season's episodes."""
    return _response(request, source, media_id, season_number)


def _create_tracking(user, metadata, season_metadata, number):
    source, media_id = metadata["source"], metadata["media_id"]
    item, _ = Item.objects.get_or_create(
        source=source,
        media_id=media_id,
        media_type=MediaTypes.TV,
        defaults={"title": metadata["title"], "image": metadata["image"]},
    )
    tv = TV.objects.filter(user=user, item=item).first()
    if tv is None:
        tv = TV(user=user, item=item, status=Status.PLANNING)
        bulk_create_with_history([tv], TV, default_user=user)
        transaction.on_commit(lambda: item.fetch_releases(delay=True))
    season_item, _ = Item.objects.get_or_create(
        source=source,
        media_id=media_id,
        media_type=MediaTypes.SEASON,
        season_number=number,
        defaults={"title": metadata["title"], "image": season_metadata["image"]},
    )
    season = Season.objects.filter(user=user, related_tv=tv, item=season_item).first()
    if season is None:
        season = Season(
            user=user, related_tv=tv, item=season_item, status=Status.PLANNING
        )
        bulk_create_with_history([season], Season, default_user=user)
    return tv, season


def _status(instance, status, user):
    if (
        instance.status not in (Status.PAUSED, Status.DROPPED)
        and instance.status != status
    ):
        instance.status = status
        bulk_update_with_history(
            [instance], type(instance), ["status"], default_user=user
        )


def _reconcile(tv, season, metadata, season_metadata, user):
    watched = set(season.episodes.values_list("item__episode_number", flat=True))
    expected = {episode["episode_number"] for episode in season_metadata["episodes"]}
    complete = bool(expected) and expected <= watched
    _status(
        season,
        Status.COMPLETED
        if complete
        else Status.IN_PROGRESS
        if watched
        else Status.PLANNING,
        user,
    )
    data = summary(user, metadata)
    ended = (
        metadata.get("details", {}).get("status") in ("Ended", "Canceled")
        or metadata["source"] == Sources.MANUAL
    )
    completed = (
        data["season_count"] > 0 and data["completed_seasons"] == data["season_count"]
    )
    _status(
        tv,
        Status.COMPLETED
        if completed and ended
        else Status.IN_PROGRESS
        if data["watched"]
        else Status.PLANNING,
        user,
    )
    return data


def _apply_action(user, season, metadata, released, action_data):
    action, number = action_data["action"], action_data["episode_number"]
    if action == "undo":
        latest = (
            season.episodes.filter(item__episode_number=number)
            .order_by("-created_at", "-pk")
            .first()
        )
        if latest:
            Episode.objects.filter(pk=latest.pk).delete()
        return
    watched = set(season.episodes.values_list("item__episode_number", flat=True))
    wanted = released if action == "season" else {number: released[number]}
    rows = []
    for n, episode in wanted.items():
        if n not in watched or action == "log":
            item = season.get_episode_item(n, metadata)
            end_date = (
                action_data["end_date"]
                if action == "log"
                else user.resolve_watch_date(timezone.now(), episode.get("air_date"))
            )
            rows.append(Episode(item=item, related_season=season, end_date=end_date))
    bulk_create_with_history(rows, Episode, default_user=user)


@authenticated_tracker
@require_POST
def watch(request, source, media_id, season_number):
    """Apply explicit, user-scoped watched actions atomically and idempotently."""
    form = WatchAction(request.POST)
    if not form.is_valid():
        return HttpResponseBadRequest("Invalid watch action")
    action, number = form.cleaned_data["action"], form.cleaned_data["episode_number"]
    if action != "season" and number is None:
        return HttpResponseBadRequest("Episode required")
    metadata = _metadata(source, media_id)
    if not any(
        row["season_number"] == season_number
        for row in metadata.get("related", {}).get("seasons", [])
    ):
        raise Http404
    season_metadata = _metadata(source, media_id, season_number)
    episodes = {ep["episode_number"]: ep for ep in season_metadata["episodes"]}
    if action != "season" and number not in episodes:
        raise Http404
    released = {
        n: ep
        for n, ep in episodes.items()
        if source == Sources.MANUAL or is_released_date(ep.get("air_date"))
    }
    if action in ("watch", "log") and number not in released:
        return HttpResponseBadRequest("Episode has not aired")
    with transaction.atomic():
        # Serialize even the first watch of a show; there may be no TV row yet.
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        if (
            action == "undo"
            and not Season.objects.filter(
                user=request.user,
                related_tv__user=request.user,
                item__source=source,
                item__media_id=media_id,
                item__season_number=season_number,
            ).exists()
        ):
            return _response(
                request, source, media_id, season_number, metadata, season_metadata
            )
        if action == "season" and not released:
            return _response(
                request, source, media_id, season_number, metadata, season_metadata
            )
        tv, season = _create_tracking(
            request.user, metadata, season_metadata, season_number
        )
        _apply_action(
            request.user, season, season_metadata, released, form.cleaned_data
        )
        data = _reconcile(tv, season, metadata, season_metadata, request.user)
    response = _response(
        request, source, media_id, season_number, metadata, season_metadata
    )
    response["HX-Trigger"] = json.dumps(
        {
            "tv-progress-changed": {
                "key": f"{source}-{media_id}",
                "watched": data["watched"],
                "status": tv.status,
            }
        }
    )
    return response
