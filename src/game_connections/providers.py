"""Authenticated, uncached library reads with no secret-bearing errors or URLs."""

import re
from dataclasses import dataclass

import requests
from django.views.decorators.debug import sensitive_variables


class ConnectionFailure(Exception):
    """A fixed, safe message suitable for displaying to the connection owner."""


@dataclass(frozen=True)
class OwnedGame:
    external_id: str
    title: str
    minutes: int | None = None
    recent_minutes: int = 0
    owned: bool = True


@sensitive_variables()
def api_get(url, headers, params=None):
    # Never use the shared metadata cache/request logger for private account data.
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(
                url,
                headers=headers,
                params=params,
                timeout=(5, 30),
                allow_redirects=False,
            )
        if response.status_code in (401, 403):
            raise ConnectionFailure(
                "Access denied. Reconnect with your own account's API key."
            )
        if response.status_code == 429:
            raise ConnectionFailure(
                "The service is rate limiting requests. Try again later."
            )
        if response.status_code != 200:
            raise ConnectionFailure(
                "The gaming service is unavailable. Try again later."
            )
        data = response.json()
        if not isinstance(data, dict) or data.get("errors"):
            raise ConnectionFailure(
                "The credential does not grant the required library access."
            )
        return data
    except (requests.RequestException, ValueError):
        raise ConnectionFailure(
            "The gaming service could not be reached or returned an invalid response."
        ) from None


@sensitive_variables()
def steam_library(api_key, steam_id):
    if not re.fullmatch(r"[0-9A-Fa-f]{32}", api_key) or not re.fullmatch(
        r"7656119\d{10}", steam_id
    ):
        raise ConnectionFailure(
            "Enter the personal Steam Web API key for your verified account."
        )
    data = api_get(
        "https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/",
        {"x-webapi-key": api_key},
        {
            "steamid": steam_id,
            "include_appinfo": 1,
            "include_played_free_games": 1,
            "format": "json",
        },
    )
    payload = data.get("response", {})
    # Steam returns {} for inaccessible profiles; never misreport that as an empty library.
    if "game_count" not in payload:
        raise ConnectionFailure(
            "Steam did not grant library access. Use this Steam account's own API key; your library can stay private."
        )
    games = payload.get("games", [])
    if not isinstance(games, list) or len(games) != payload["game_count"]:
        raise ConnectionFailure(
            "Steam returned an incomplete library. No games were changed."
        )
    try:
        return [
            OwnedGame(
                str(int(g["appid"])),
                str(g["name"])[:500],
                max(0, int(g["playtime_forever"])),
                max(0, int(g.get("playtime_2weeks", 0))),
            )
            for g in games
        ]
    except (KeyError, TypeError, ValueError):
        raise ConnectionFailure(
            "Steam returned an invalid library. No games were changed."
        ) from None


@sensitive_variables()
def itch_identity(api_key):
    data = api_get(
        "https://api.itch.io/profile", {"Authorization": f"Bearer {api_key}"}
    )
    try:
        return str(int(data["user"]["id"]))
    except (KeyError, TypeError, ValueError):
        raise ConnectionFailure("itch.io could not verify this account.") from None


@sensitive_variables()
def itch_library(api_key):
    games = {}
    for page in range(1, 1001):
        data = api_get(
            "https://api.itch.io/profile/owned-keys",
            {"Authorization": f"Bearer {api_key}"},
            {"page": page},
        )
        keys = data.get("owned_keys")
        if not isinstance(keys, list):
            raise ConnectionFailure(
                "itch.io did not grant access to purchased and claimed games."
            )
        try:
            for entry in keys:
                game = entry["game"]
                if game.get("classification") != "game":
                    continue
                game_id = str(int(game["id"]))
                games[game_id] = OwnedGame(game_id, str(game["title"])[:500])
            per_page = int(data["per_page"])
            if int(data["page"]) != page or per_page < 1:
                raise ValueError
        except (KeyError, ValueError, TypeError):
            raise ConnectionFailure(
                "itch.io returned an invalid library. No games were changed."
            ) from None
        if len(keys) < per_page:
            return list(games.values())
    raise ConnectionFailure(
        "The library exceeds the supported pagination limit. No games were changed."
    )


@sensitive_variables()
def steam_wishlist(api_key, steam_id):
    """Read the verified owner's wishlist without public caches or cookie scraping."""
    if not re.fullmatch(r"[0-9A-Fa-f]{32}", api_key) or not re.fullmatch(
        r"7656119\d{10}", steam_id
    ):
        raise ConnectionFailure("Reconnect your verified Steam account.")
    data = api_get(
        "https://api.steampowered.com/IWishlistService/GetWishlist/v1/",
        {"x-webapi-key": api_key},
        {"steamid": steam_id},
    )
    payload = data.get("response")
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ConnectionFailure(
            "Steam did not provide wishlist access. Existing wishlist entries were kept."
        )
    if len(items) > 5000:
        raise ConnectionFailure(
            "The wishlist exceeds the supported limit. Existing entries were kept."
        )
    games = []
    seen = set()
    for item in items:
        appid = item.get("appid") if isinstance(item, dict) else None
        if type(appid) is not int or appid <= 0 or appid in seen:
            raise ConnectionFailure(
                "Steam returned an invalid wishlist. Existing entries were kept."
            )
        seen.add(appid)
        games.append(OwnedGame(str(appid), f"Steam App {appid}", owned=False))
    return games
