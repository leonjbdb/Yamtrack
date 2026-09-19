"""Steam OpenID verifies identity; it does not authorize private library access."""

import re
import secrets
from datetime import timedelta
from urllib.parse import urlencode

import requests
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .providers import ConnectionFailure

ENDPOINT = "https://steamcommunity.com/openid/login"
NS = "http://specs.openid.net/auth/2.0"


def begin(request):
    state = secrets.token_urlsafe(32)
    callback = (
        request.build_absolute_uri(reverse("game_connections:steam_callback"))
        + "?state="
        + state
    )
    request.session["steam_link"] = {
        "callback": callback,
        "expires": (timezone.now() + timedelta(minutes=10)).timestamp(),
    }
    request.session.pop("verified_steam", None)
    return (
        ENDPOINT
        + "?"
        + urlencode(
            {
                "openid.ns": NS,
                "openid.mode": "checkid_setup",
                "openid.return_to": callback,
                "openid.realm": request.build_absolute_uri("/"),
                "openid.identity": NS + "/identifier_select",
                "openid.claimed_id": NS + "/identifier_select",
            }
        )
    )


def finish(request):
    pending = request.session.pop("steam_link", None)
    params = request.GET
    error = "Steam verification failed or expired. Start Connect Steam again."
    if not pending or pending["expires"] < timezone.now().timestamp():
        raise ConnectionFailure(error)
    if any(len(params.getlist(key)) != 1 for key in params):
        raise ConnectionFailure(error)
    expected_return = pending["callback"]
    if (
        request.build_absolute_uri(reverse("game_connections:steam_callback"))
        + "?state="
        + params.get("state", "")
        != expected_return
        or params.get("openid.return_to") != expected_return
        or params.get("openid.ns") != NS
        or params.get("openid.mode") != "id_res"
        or params.get("openid.op_endpoint") != ENDPOINT
    ):
        raise ConnectionFailure(error)
    claimed = params.get("openid.claimed_id", "")
    match = re.fullmatch(
        r"https://steamcommunity\.com/openid/id/(7656119\d{10})", claimed
    )
    if not match or params.get("openid.identity") != claimed:
        raise ConnectionFailure(error)
    required = {
        "op_endpoint",
        "claimed_id",
        "identity",
        "return_to",
        "response_nonce",
        "assoc_handle",
    }
    if not required.issubset(set(params.get("openid.signed", "").split(","))):
        raise ConnectionFailure(error)
    nonce = params.get("openid.response_nonce", "")
    try:
        issued = parse_datetime(nonce[:20])
        if not issued or abs((timezone.now() - issued).total_seconds()) > 600:
            raise ValueError
        verification = {
            key: value for key, value in params.items() if key.startswith("openid.")
        }
        verification["openid.mode"] = "check_authentication"
        response = requests.post(
            ENDPOINT, data=verification, timeout=(5, 20), allow_redirects=False
        )
        if (
            response.status_code != 200
            or "is_valid:true" not in response.text.splitlines()
        ):
            raise ValueError
        if not cache.add("steam-openid-nonce:" + nonce, True, timeout=1200):
            raise ValueError
    except (requests.RequestException, ValueError, TypeError):
        raise ConnectionFailure(error) from None
    return match.group(1)
