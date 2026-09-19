import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_GET, require_POST

from . import openid
from .credentials import encrypt
from .models import GameConnection
from .providers import ConnectionFailure, itch_identity, itch_library, steam_library
from .tasks import sync_game_connection


def allow_attempt(user_id):
    if not cache.add(f"game-connection-attempt:{user_id}", True, timeout=10):
        raise ConnectionFailure("Please wait a few seconds before trying again.")


@login_required
@never_cache
@require_GET
def index(request):
    cards = []
    for provider, label in GameConnection.Provider.choices:
        connection = (
            GameConnection.objects.filter(user=request.user, provider=provider)
            .defer("credential")
            .first()
        )
        cards.append(
            {
                "provider": provider,
                "label": label,
                "connection": connection,
                "unmatched": connection.library.filter(item__isnull=True)[:100]
                if connection
                else [],
            }
        )
    verified = request.session.get("verified_steam", {})
    verified_id = (
        verified.get("id")
        if verified.get("expires", 0) > timezone.now().timestamp()
        else None
    )
    response = render(
        request,
        "game_connections/index.html",
        {"cards": cards, "verified_steam": verified_id},
    )
    response["Referrer-Policy"] = "no-referrer"
    return response


@login_required
@require_POST
def steam_start(request):
    if GameConnection.objects.filter(user=request.user, provider="steam").exists():
        messages.error(request, "Disconnect the existing Steam connection first.")
        return redirect("game_connections:index")
    return redirect(openid.begin(request))


@login_required
@never_cache
@require_GET
def steam_callback(request):
    try:
        steam_id = openid.finish(request)
        request.session["verified_steam"] = {
            "id": steam_id,
            "expires": timezone.now().timestamp() + 600,
        }
        messages.success(
            request,
            "Steam ownership verified. Add this account's personal API key to enable private-library sync.",
        )
    except ConnectionFailure as error:
        messages.error(request, str(error))
    return redirect("game_connections:index")


@sensitive_post_parameters("api_key")
@sensitive_variables()
@login_required
@never_cache
@require_POST
def connect(request, provider):
    if provider not in GameConnection.Provider.values:
        return redirect("game_connections:index")
    try:
        allow_attempt(request.user.pk)
        if GameConnection.objects.filter(user=request.user, provider=provider).exists():
            raise ConnectionFailure(
                "Disconnect the existing connection before adding another."
            )
        api_key = request.POST.get("api_key", "").strip()
        if (
            not api_key
            or len(api_key) > 512
            or any(ord(c) < 33 or ord(c) > 126 for c in api_key)
        ):
            raise ConnectionFailure("Enter a valid personal API key.")
        if provider == "steam":
            verified = request.session.get("verified_steam", {})
            if verified.get("expires", 0) < timezone.now().timestamp():
                raise ConnectionFailure(
                    "Verify your Steam account with Connect Steam first."
                )
            external_id = verified["id"]
            steam_library(api_key, external_id)
        else:
            external_id = itch_identity(api_key)
            itch_library(api_key)
        with transaction.atomic():
            connection = GameConnection(
                user=request.user, provider=provider, external_id=external_id
            )
            connection.credential = encrypt(connection, api_key)
            connection.save()
        request.session.pop("verified_steam", None)
        messages.success(
            request,
            "Connected. Library access verified. Use Sync now or let the daily sync run.",
        )
    except IntegrityError:
        messages.error(
            request,
            "This service account is already connected. Disconnect it before connecting again.",
        )
    except ConnectionFailure as error:
        messages.error(request, str(error))
    return redirect("game_connections:index")


@login_required
@require_POST
def action(request, provider):
    with transaction.atomic():
        connection = get_object_or_404(
            GameConnection.objects.select_for_update(),
            user=request.user,
            provider=provider,
        )
        action_name = request.POST.get("action")
        if action_name == "disconnect":
            connection.delete()
            request.session.pop("verified_steam", None)
            messages.success(
                request,
                "Disconnected and removed the stored credential. Tracked games are retained.",
            )
        elif action_name == "toggle":
            connection.enabled = not connection.enabled
            connection.generation = uuid.uuid4()
            connection.lease = None
            connection.busy_until = None
            connection.save(
                update_fields=["enabled", "generation", "lease", "busy_until"]
            )
        elif action_name == "sync" and connection.enabled:
            if cache.add(f"game-connection-sync:{connection.pk}", True, timeout=60):
                transaction.on_commit(lambda: sync_game_connection.delay(connection.pk))
                messages.success(
                    request, "Sync queued. Refresh this page to see its result."
                )
            else:
                messages.info(
                    request, "A sync was requested recently. Please wait a minute."
                )
    return redirect("game_connections:index")
